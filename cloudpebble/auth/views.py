import logging
import time

import jwt
import requests as http_requests
from registration.backends.simple.views import RegistrationView
from django.contrib.auth import logout, login, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.views.generic import View
from django.views.decorators.http import require_POST, require_GET
from django.shortcuts import render, redirect
from django.db import connections
from django.http import JsonResponse
from django.http.response import Http404
from django.conf import settings
from django.utils.translation import gettext as _
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from ide.api import json_failure, json_response

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = '__session_dashboard'
SESSION_MAX_AGE = 14 * 24 * 60 * 60  # 14 days


def _set_developer_cookie(response, firebase_uid, email):
    """Set cross-domain developer session cookie if the user is a registered developer."""
    secret = settings.REPEBBLE_SESSION_SECRET
    if not secret:
        return

    try:
        with connections['default'].cursor() as cursor:
            cursor.execute(
                'SELECT id, name, email FROM developers WHERE firebase_uid = %s LIMIT 1',
                [firebase_uid],
            )
            row = cursor.fetchone()
            if not row:
                cursor.execute(
                    'SELECT id, name, email FROM developers WHERE email = %s AND firebase_uid IS NULL LIMIT 1',
                    [email],
                )
                row = cursor.fetchone()
                if row:
                    cursor.execute(
                        'UPDATE developers SET firebase_uid = %s WHERE id = %s',
                        [firebase_uid, row[0]],
                    )

        if not row:
            return

        now = int(time.time())
        payload = {
            'uid': firebase_uid,
            'email': email,
            'developerId': row[0],
            'developerName': row[1] or '',
            'iat': now,
            'exp': now + SESSION_MAX_AGE,
        }
        token = jwt.encode(payload, secret, algorithm='HS256')
        response.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            max_age=SESSION_MAX_AGE,
            path='/',
            domain='.repebble.com',
            secure=True,
            httponly=True,
            samesite='Lax',
        )
    except Exception as e:
        logger.warning('Failed to set developer cookie: %s', e)


class IdeRegistrationView(RegistrationView):
    def get_success_url(self, *args, **kwargs):
        return "/ide/"


class IdeRegistrationMissingView(View):
    def get(self, request, *args, **kwargs):
        raise Http404()


def logout_view(request):
    logout(request)
    response = redirect("/")
    # Clear cross-domain session cookie so SSO doesn't re-sign-in
    response.delete_cookie(SESSION_COOKIE_NAME, path='/', domain='.repebble.com')
    return response


def login_action(request):
    username = request.POST['username']
    password = request.POST['password']
    user = authenticate(username=username, password=password)
    if user is None:
        return json_failure(_("Invalid username or password"))

    if not user.is_active:
        return json_failure(_("Account disabled."))

    login(request, user)
    return json_response()


@require_POST
def firebase_login(request):
    token = request.POST.get('id_token', '')
    if not token:
        return json_failure("Missing id_token")

    try:
        decoded = id_token.verify_firebase_token(
            token,
            google_requests.Request(),
            audience=settings.FIREBASE_PROJECT_ID,
        )
    except Exception as e:
        logger.warning("Firebase token verification failed: %s", e)
        return json_failure("Invalid token")

    email = decoded.get('email')
    if not email:
        return json_failure("No email in token")

    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        user = User.objects.create_user(
            username=email,
            email=email,
        )

    if not user.is_active:
        return json_failure("Account disabled.")

    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    # Store the verified Firebase token in session for Cloud Dev Connection v2.
    request.session['firebase_id_token'] = token
    request.session['firebase_id_token_exp'] = decoded.get('exp')

    response = json_response()
    _set_developer_cookie(response, decoded['sub'], email)
    return response


@require_POST
@login_required
def firebase_refresh_token(request):
    """Update the Firebase ID token in the existing session without re-logging in.

    Unlike firebase_login, this does NOT call login() so the session and CSRF
    token are preserved. Use this to refresh an expired Firebase token from JS
    after the user is already authenticated.
    """
    token = request.POST.get('id_token', '')
    if not token:
        return json_failure("Missing id_token")

    try:
        decoded = id_token.verify_firebase_token(
            token,
            google_requests.Request(),
            audience=settings.FIREBASE_PROJECT_ID,
        )
    except Exception as e:
        logger.warning("Firebase token refresh verification failed: %s", e)
        return json_failure("Invalid token")

    email = decoded.get('email')
    if not email:
        return json_failure("No email in token")

    # Verify the token belongs to the logged-in user
    if email != request.user.email:
        return json_failure("Token email does not match logged-in user")

    request.session['firebase_id_token'] = token
    request.session['firebase_id_token_exp'] = decoded.get('exp')
    return json_response()


@require_GET
def sso_custom_token(request):
    """Proxy to appstore-api for a Firebase custom token (cross-domain SSO).

    Reads the __session_dashboard cookie from the user's request and forwards
    it to the backend, which verifies it and returns a Firebase custom token.
    """
    cookie_value = request.COOKIES.get(SESSION_COOKIE_NAME)
    if not cookie_value:
        return JsonResponse({'error': 'No session'}, status=401)

    try:
        resp = http_requests.get(
            f'{settings.APPSTORE_API_BASE}/api/auth/firebase/custom-token',
            cookies={SESSION_COOKIE_NAME: cookie_value},
            timeout=5,
        )
        if resp.ok:
            return JsonResponse(resp.json())
        return JsonResponse({'error': 'Failed to get token'}, status=resp.status_code)
    except Exception as e:
        logger.warning('SSO custom token proxy failed: %s', e)
        return JsonResponse({'error': 'SSO unavailable'}, status=502)
