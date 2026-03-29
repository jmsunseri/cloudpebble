import json
import logging

import requests
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, Http404
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_safe

logger = logging.getLogger(__name__)

PLATFORM_DISPLAY_NAMES = {
    'aplite': 'OG Pebble/Steel',
    'basalt': 'Time/Time Steel',
    'chalk': 'Time Round',
    'diorite': '2/2HR',
    'emery': 'Time 2',
    'flint': '2 Duo',
    'gabbro': 'Round 2',
}

# Default platform preference (first match wins)
PLATFORM_PREFERENCE = ['emery', 'basalt', 'chalk', 'diorite', 'aplite', 'gabbro', 'flint']


def _fetch_app_info(app_id):
    """Fetch app metadata from the App Store API."""
    url = '%s/api/v1/apps/id/%s?hardware=basalt' % (settings.APPSTORE_API_BASE, app_id)
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json().get('data', [])
    if not data:
        return None
    return data[0]


def _get_supported_platforms(app_info):
    """Extract list of supported platform names from compatibility object."""
    compatibility = app_info.get('compatibility', {})
    supported = []
    for platform in PLATFORM_PREFERENCE:
        if compatibility.get(platform, {}).get('supported'):
            supported.append(platform)
    return supported


def _get_pbw_url(app_info):
    """Get the PBW file URL from the latest release."""
    latest = app_info.get('latest_release', {})
    pbw_file = latest.get('pbw_file', '')
    if not pbw_file:
        return None
    if pbw_file.startswith('http'):
        return pbw_file
    return '%s%s' % (settings.APPSTORE_API_BASE, pbw_file)


@require_safe
@login_required
@ensure_csrf_cookie
def run_app(request, app_id):
    app_info = _fetch_app_info(app_id)
    if not app_info:
        raise Http404("App not found")

    supported_platforms = _get_supported_platforms(app_info)
    if not supported_platforms:
        raise Http404("App has no supported platforms")

    # Default to emery if supported, else first in preference order
    default_platform = supported_platforms[0]

    # Build platform list with display names, sorted alphabetically by platform codename
    platform_choices = sorted(
        [{'id': p, 'name': PLATFORM_DISPLAY_NAMES.get(p, p)} for p in supported_platforms],
        key=lambda x: x['id']
    )

    try:
        token = request.user.social_auth.get(provider='pebble').extra_data['access_token']
    except Exception:
        token = ''
    firebase_token = request.session.get('firebase_id_token', '')
    firebase_token_exp = request.session.get('firebase_id_token_exp', '')

    return render(request, 'ide/run.html', {
        'app_id': app_id,
        'app_title': app_info.get('title', 'Unknown App'),
        'app_author': app_info.get('author', 'Unknown'),
        'platform_choices': platform_choices,
        'platform_choices_json': json.dumps(platform_choices),
        'supported_platforms_json': json.dumps(supported_platforms),
        'default_platform': default_platform,
        'token': token,
        'firebase_token': firebase_token,
        'firebase_token_exp': firebase_token_exp,
        'libpebble_proxy': json.dumps(settings.LIBPEBBLE_PROXY),
        'cloudpebble_proxy': json.dumps(settings.CLOUDPEBBLE_PROXY),
        'phone_shorturl': settings.PHONE_SHORTURL,
    })


@require_safe
@login_required
def run_app_pbw(request, app_id):
    """Proxy the PBW download from the App Store API to avoid CORS issues."""
    app_info = _fetch_app_info(app_id)
    if not app_info:
        raise Http404("App not found")

    pbw_url = _get_pbw_url(app_info)
    if not pbw_url:
        raise Http404("No PBW available for this app")

    resp = requests.get(pbw_url, timeout=30, stream=True)
    resp.raise_for_status()

    response = HttpResponse(
        resp.iter_content(chunk_size=8192),
        content_type='application/octet-stream',
    )
    response['Content-Disposition'] = 'attachment; filename="%s.pbw"' % app_id
    if resp.headers.get('Content-Length'):
        response['Content-Length'] = resp.headers['Content-Length']
    return response
