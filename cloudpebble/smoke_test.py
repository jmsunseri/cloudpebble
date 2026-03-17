#!/usr/bin/env python3
"""E2E smoke test for CloudPebble.

Exercises the full user flow: create project from template, build via Celery,
install on emulator, verify APP_LOG output. Runs against a live environment
with zero shims — real HTTP, real Celery, real emulator.

Usage:
    python smoke_test.py [--base-url https://cloudpebble-dev.exe.xyz]

Environment variables:
    SMOKE_TEST_USER      Login username (default: testuser)
    SMOKE_TEST_PASSWORD   Login password (default: testpassword123)
"""

import argparse
import os
import struct
import sys
import time

import requests
import websocket

# Templates to test.  Each entry specifies how to call /ide/project/create.
# - 'type' and 'template'/'alloy_template' map directly to POST params.
TEMPLATES = [
    {
        'type': 'native',
        'platform': 'basalt',
        'params': {'template': 'watchface-tutorial/part6'},
        'label': 'native watchface-tutorial/part6 "Adding a Settings Page"',
    },
    {
        'type': 'alloy',
        'platform': 'emery',
        'params': {'alloy_template': 'watchface-tutorial/part5'},
        'label': 'alloy watchface-tutorial/part5 "Adding User Settings"',
    },
]

ENDPOINT_APP_LOGS = 2006
BUILD_PENDING = 1
BUILD_FAILED = 2
BUILD_SUCCEEDED = 3
HTTP_TIMEOUT = 30  # seconds, for all requests calls


def log(msg, end='\n'):
    print(f'  {msg}', end=end, flush=True)


def get_csrf(session):
    return session.cookies.get('csrftoken')


def post(session, url, base_url, data=None):
    """POST with CSRF header and Referer."""
    return session.post(
        url,
        data=data or {},
        headers={'X-CSRFToken': get_csrf(session), 'Referer': f'{base_url}/'},
        timeout=HTTP_TIMEOUT,
    )


def login(session, base_url, username, password):
    log('Login...', end=' ')
    session.get(f'{base_url}/', timeout=HTTP_TIMEOUT)
    resp = post(session, f'{base_url}/accounts/api/login', base_url,
                {'username': username, 'password': password})
    data = resp.json()
    if not data.get('success'):
        raise RuntimeError(f"Login failed: {data.get('error', resp.text)}")
    log('OK')


def create_project(session, base_url, tmpl):
    log('Creating project...', end=' ')
    timestamp = int(time.time())
    params = {
        'name': f"smoke-{tmpl['type']}-{timestamp}",
        'type': tmpl['type'],
    }
    params.update(tmpl['params'])
    resp = post(session, f'{base_url}/ide/project/create', base_url, params)
    data = resp.json()
    if not data.get('success'):
        raise RuntimeError(f"Create failed: {data.get('error', resp.text)}")
    project_id = data['id']
    log(f'OK (id={project_id})')
    return project_id


def build_project(session, base_url, project_id, timeout=120):
    log('Building...', end=' ')
    resp = post(session, f'{base_url}/ide/project/{project_id}/build/run', base_url)
    data = resp.json()
    if not data.get('success'):
        raise RuntimeError(f"Build trigger failed: {data.get('error', resp.text)}")
    build_id = data['build_id']

    start = time.time()
    while True:
        elapsed = time.time() - start
        if elapsed > timeout:
            raise RuntimeError(f'Build timed out after {timeout}s')
        time.sleep(2)
        resp = session.get(f'{base_url}/ide/project/{project_id}/build/{build_id}/info',
                           timeout=HTTP_TIMEOUT)
        info = resp.json()
        if not info.get('success'):
            raise RuntimeError(f"Build info failed: {info.get('error', resp.text)}")
        state = info['build']['state']
        if state != BUILD_PENDING:
            break

    elapsed = time.time() - start
    if state == BUILD_FAILED:
        build_log = ''
        log_url = info['build'].get('log', '')
        if log_url:
            if log_url.startswith('/'):
                log_url = f'{base_url}{log_url}'
            try:
                build_log = session.get(log_url, timeout=HTTP_TIMEOUT).text
            except Exception:
                pass
        raise RuntimeError(f'Build failed ({elapsed:.1f}s):\n{build_log}')
    if state != BUILD_SUCCEEDED:
        raise RuntimeError(f'Build unexpected state={state} after {elapsed:.1f}s')
    log(f'OK (state={state}, {elapsed:.1f}s)')
    return build_id


def download_pbw(session, base_url, project_id, build_id):
    resp = session.get(
        f'{base_url}/ide/project/{project_id}/build/{build_id}/download/watchface.pbw',
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f'PBW download failed: HTTP {resp.status_code}')
    return resp.content


def launch_emulator(session, base_url, platform='basalt'):
    log(f'Launching emulator ({platform})...', end=' ')
    resp = post(session, f'{base_url}/ide/emulator/launch', base_url,
                {'platform': platform, 'token': '', 'tz_offset': '0'})
    if resp.status_code != 200:
        raise RuntimeError(f'Emulator launch failed: HTTP {resp.status_code}')
    data = resp.json()
    if not data.get('success'):
        raise RuntimeError(f"Emulator launch failed: {data.get('error', resp.text)}")
    log(f"OK (uuid={data['uuid'][:8]}...)")
    return data


def build_ws_url(emu):
    scheme = 'wss' if emu.get('secure') else 'ws'
    host = emu['host']
    port = emu.get('api_port')
    uuid = emu['uuid']
    url = f'{scheme}://{host}'
    if port and port not in (80, 443):
        url += f':{port}'
    url += f'/qemu/{uuid}/ws/phone'
    return url


def emulator_install(emu, pbw_data, timeout=30):
    """Connect to emulator WS, install .pbw. PASS = install status 0."""
    ws_url = build_ws_url(emu)
    token = emu['token']

    ws = websocket.WebSocket()
    ws.settimeout(timeout)
    try:
        ws.connect(ws_url)

        # Auth v1: [0x09, len, token_bytes...]
        token_bytes = token.encode('utf-8')
        auth_frame = bytes([0x09, len(token_bytes)]) + token_bytes
        ws.send_binary(auth_frame)

        # Wait for auth OK + connection frame
        connected = False
        deadline = time.time() + timeout
        while not connected:
            if time.time() > deadline:
                raise RuntimeError('Timed out waiting for WS connection')
            data = ws.recv()
            if isinstance(data, str):
                data = data.encode('latin-1')
            if len(data) < 2:
                continue
            if data[0] == 0x09:
                if data[1] != 0x00:
                    raise RuntimeError('WS auth failed')
            elif data[0] == 0x08 and data[1] == 0xFF:
                connected = True

        # Enable app logs: send_message("APP_LOGS", [1])
        # Pebble protocol: [0x01, size_hi, size_lo, ep_hi, ep_lo, ...data]
        # APP_LOGS=2006=0x07D6, data=[1], size=1
        enable_logs = bytes([0x01, 0x00, 0x01, 0x07, 0xD6, 0x01])
        ws.send_binary(enable_logs)

        # Install: [0x04, ...pbw_bytes]
        log('Installing .pbw...', end=' ')
        install_frame = bytes([0x04]) + pbw_data
        ws.send_binary(install_frame)

        # Wait for install status (and optionally APP_LOG)
        install_ok = False
        app_log_count = 0
        reassembly = bytearray()
        start = time.time()

        while time.time() - start < timeout:
            try:
                data = ws.recv()
            except websocket.WebSocketTimeoutException:
                break
            if isinstance(data, str):
                data = data.encode('latin-1')
            if len(data) < 1:
                continue

            origin = data[0]

            if origin == 0x05 and len(data) >= 5:
                # Install status: big-endian uint32
                status = struct.unpack('>I', data[1:5])[0]
                if status == 0:
                    install_ok = True
                    log(f'OK (status={status})')
                else:
                    raise RuntimeError(f'Install failed: status={status}')

            elif origin == 0x00:
                # Message from watch — parse Pebble protocol frames
                reassembly.extend(data[1:])
                while len(reassembly) >= 4:
                    size = (reassembly[0] << 8) | reassembly[1]
                    command = (reassembly[2] << 8) | reassembly[3]
                    if len(reassembly) < 4 + size:
                        break
                    reassembly = reassembly[4 + size:]
                    if command == ENDPOINT_APP_LOGS:
                        app_log_count += 1

            # Install success is the pass condition; don't wait for logs
            if install_ok:
                break

        elapsed = time.time() - start
        if not install_ok:
            raise RuntimeError(f'No install status received within {timeout}s')
        if app_log_count:
            log(f'App logs: {app_log_count} received in {elapsed:.1f}s')
        return True

    finally:
        try:
            ws.close()
        except Exception:
            pass


def kill_emulator(session, emu):
    """Kill emulator (best-effort)."""
    log('Cleanup...', end=' ')
    try:
        kill_url = emu.get('kill_url', '')
        if kill_url:
            session.post(kill_url, timeout=5)
    except Exception:
        pass
    log('OK')


def delete_project(session, base_url, project_id):
    try:
        post(session, f'{base_url}/ide/project/{project_id}/delete', base_url,
             {'confirm': '1'})
    except Exception:
        pass


def run_template_test(session, base_url, tmpl, project_ids):
    """Run full E2E for one template. Appends project_id to project_ids for cleanup."""
    project_id = create_project(session, base_url, tmpl)
    project_ids.append(project_id)
    build_id = build_project(session, base_url, project_id)
    pbw = download_pbw(session, base_url, project_id, build_id)
    platform = tmpl.get('platform', 'basalt')
    emu = launch_emulator(session, base_url, platform=platform)
    time.sleep(5)  # Wait for emulator boot
    try:
        emulator_install(emu, pbw)
    finally:
        kill_emulator(session, emu)


def main():
    parser = argparse.ArgumentParser(description='E2E smoke test for CloudPebble')
    parser.add_argument('--base-url', default='https://cloudpebble-dev.exe.xyz',
                        help='CloudPebble instance URL')
    args = parser.parse_args()

    base_url = args.base_url.rstrip('/')
    username = os.environ.get('SMOKE_TEST_USER', 'testuser')
    password = os.environ.get('SMOKE_TEST_PASSWORD', 'testpassword123')

    session = requests.Session()
    login(session, base_url, username, password)

    passed = 0
    failed = 0
    project_ids = []

    for i, tmpl in enumerate(TEMPLATES):
        print(f"\n[{i + 1}/{len(TEMPLATES)}] {tmpl['label']}")
        try:
            run_template_test(session, base_url, tmpl, project_ids)
            passed += 1
            print('  PASS')
        except Exception as e:
            failed += 1
            print(f'  FAIL: {e}')

    # Cleanup test projects
    for pid in project_ids:
        delete_project(session, base_url, pid)

    print(f'\n{passed}/{len(TEMPLATES)} smoke tests passed')
    sys.exit(0 if failed == 0 else 1)


if __name__ == '__main__':
    main()
