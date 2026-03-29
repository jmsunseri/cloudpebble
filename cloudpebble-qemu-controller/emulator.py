__author__ = 'katharine'

import gevent
import gevent.pool
import logging
import os
import tempfile
import settings
import shutil
import socket
import subprocess
import itertools

_used_displays = set()
def _find_display():
    for i in itertools.count():
        if i not in _used_displays:
            _used_displays.add(i)
            return i

def _free_display(display):
    _used_displays.remove(display)


class Emulator(object):
    def __init__(self, token, platform, version, tz_offset=None, oauth=None):
        self.token = token
        self.qemu = None
        self.pkjs = None
        self.console_port = None
        self.bt_port = None
        self.ws_port = None
        self.spi_image = None
        self.vnc_display = None
        self.vnc_ws_port = None
        self.group = None
        self.platform = platform
        self.version = version
        self.tz_offset = tz_offset
        self.oauth = oauth
        self.persist_dir = None

    def run(self):
        self.group = gevent.pool.Group()
        self._choose_ports()
        self._make_spi_image()
        self._spawn_qemu()
        gevent.sleep(4)  # wait for the pebble to boot.
        self._spawn_pkjs()

    def kill(self):
        if self.qemu is not None:
            try:
                self.qemu.kill()
                for i in range(10):
                    gevent.sleep(0.1)
                    if self.qemu.poll() is not None:
                        break
                else:
                    raise Exception("Failed to kill qemu in one second.")
            except OSError as e:
                if e.errno == 3:  # No such process
                    pass
                else:
                    raise
            try:
                os.unlink(self.spi_image.name)
            except OSError:
                pass
        if self.pkjs is not None:
            try:
                self.pkjs.kill()
                for i in range(10):
                    gevent.sleep(0.1)
                    if self.pkjs.poll() is not None:
                        break
                else:
                    raise Exception("Failed to kill pkjs in one second.")
            except OSError as e:
                if e.errno == 3:  # No such process
                    pass
                else:
                    raise
            try:
                shutil.rmtree(self.persist_dir)
            except OSError:
                pass
        self.group.kill(block=True)

    def is_alive(self):
        if self.qemu is None or self.pkjs is None:
            return False
        return self.qemu.poll() is None and self.pkjs.poll() is None

    def _choose_ports(self):
        self.console_port = self._find_port()
        self.bt_port = self._find_port()
        self.ws_port = self._find_port()
        self.vnc_display = self._find_port() - 5900
        self.vnc_ws_port = self._find_port()

    def _make_spi_image(self):
        with tempfile.NamedTemporaryFile(delete=False) as spi:
            self.spi_image = spi
            image_dir = self._find_qemu_images()
            bz2_path = image_dir + "qemu_spi_flash.bin.bz2"
            raw_path = image_dir + "qemu_spi_flash.bin"
            if os.path.exists(bz2_path):
                import bz2
                with open(bz2_path, 'rb') as f:
                    spi.write(bz2.decompress(f.read()))
            else:
                with open(raw_path, 'rb') as f:
                    spi.write(f.read())


    @staticmethod
    def _find_port():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(('localhost', 0))
        addr, port = s.getsockname()
        s.close()
        return port

    def _spawn_qemu(self):
        image_dir = self._find_qemu_images()
        qemu_args = [
            settings.QEMU_BIN,
            "-rtc", "base=localtime",
            "-pflash", image_dir + "qemu_micro_flash.bin",
            "-serial", "null",  # this isn't useful, but...
            "-serial", "tcp:127.0.0.1:%d,server,nowait" % self.bt_port,   # Used for bluetooth data
            "-serial", "tcp:127.0.0.1:%d,server" % self.console_port,   # Used for console
            "-monitor", "stdio",
            "-vnc", ":%d,password,websocket=%d" % (self.vnc_display, self.vnc_ws_port)
        ]
        if self.platform == 'aplite':
            qemu_args.extend([
                "-machine", "pebble-bb2",
                "-mtdblock", self.spi_image.name,
                "-cpu", "cortex-m3",
            ])
        elif self.platform == 'basalt':
            qemu_args.extend([
                "-machine", "pebble-snowy-bb",
                "-pflash", self.spi_image.name,
                "-cpu", "cortex-m4",
            ])
        elif self.platform == 'chalk':
            qemu_args.extend([
                "-machine", "pebble-s4-bb",
                "-pflash", self.spi_image.name,
                "-cpu", "cortex-m4",
            ])
        elif self.platform == 'diorite':
            qemu_args.extend([
                "-machine", "pebble-silk-bb",
                "-mtdblock", self.spi_image.name,
                "-cpu", "cortex-m4",
            ])
        elif self.platform == 'emery':
            qemu_args.extend([
                "-machine", "pebble-snowy-emery-bb",
                "-pflash", self.spi_image.name,
                "-cpu", "cortex-m4",
            ])
        elif self.platform == 'gabbro':
            qemu_args.extend([
                "-machine", "pebble-spalding-gabbro-bb",
                "-pflash", self.spi_image.name,
                "-cpu", "cortex-m4",
            ])
        elif self.platform == 'flint':
            qemu_args.extend([
                "-machine", "pebble-silk-bb",
                "-cpu", "cortex-m4",
                "-mtdblock", self.spi_image.name,
            ])
        self.qemu = subprocess.Popen(qemu_args, cwd=settings.QEMU_DIR, stdout=None, stdin=subprocess.PIPE, stderr=None,
                                      preexec_fn=lambda: os.nice(19))
        self.qemu.stdin.write(b"change vnc password\n")
        self.qemu.stdin.write(("%s\n" % self.token[:8]).encode())
        self.group.spawn(self.qemu.communicate)
        self._wait_for_qemu()

    def _wait_for_qemu(self):
        for i in range(20):
            gevent.sleep(0.2)
            try:
                s = socket.create_connection(('localhost', self.console_port))
            except socket.error:
                pass
            else:
                break
        else:
            raise Exception("Emulator launch timed out.")

        received = b''
        for i in range(150):
            gevent.sleep(0.2)
            received += s.recv(256)
            # PBL-21275: we'll add less hacky solutions for this to the firmware.
            if b"<SDK Home>" in received or b"<Launcher>" in received or b"Ready for communication" in received:
                break
        else:
            raise Exception("Didn't get ready message from firmware.")
        s.close()

    def _spawn_pkjs(self):
        env = os.environ.copy()
        hours = self.tz_offset // 60
        minutes = abs(self.tz_offset % 60)
        tz = "PBL%+03d:%02d" % (-hours, minutes)  # Why minus? Because POSIX is backwards.
        env['TZ'] = tz
        if self.oauth is not None:
            oauth_arg = ['--oauth', self.oauth]
        else:
            oauth_arg = []
        self.persist_dir = tempfile.mkdtemp()
        cmd = [
            'pypkjs',
            '--qemu', '127.0.0.1:%d' % self.bt_port,
            '--port', str(self.ws_port),
            # Use --arg=value form so tokens beginning with '-' are not parsed as flags.
            '--token=%s' % self.token,
            '--persist', self.persist_dir,
        ] + oauth_arg
        if settings.BLOCK_PRIVATE_ADDRESSES:
            cmd.append('--block-private-addresses')
        logging.info("spawning pkjs: token=%r (len=%d), cmd=%s", self.token, len(self.token), cmd)
        self.pkjs = subprocess.Popen(cmd, env=env)
        self.group.spawn(self.pkjs.communicate)

    def _find_qemu_images(self):
        return settings.QEMU_IMAGE_ROOT + "/" + self.platform + "/qemu/"
