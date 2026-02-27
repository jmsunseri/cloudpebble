import logging

from registration.backends.simple.views import RegistrationView
from django.contrib.auth import logout, login, authenticate
from django.contrib.auth.models import User
from django.views.generic import View
from django.views.decorators.http import require_POST
from django.shortcuts import render, redirect
from django.http.response import Http404
from django.conf import settings
from django.utils.translation import gettext as _
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from ide.api import json_failure, json_response

logger = logging.getLogger(__name__)


class IdeRegistrationView(RegistrationView):
    def get_success_url(self, *args, **kwargs):
        return "/ide/"


class IdeRegistrationMissingView(View):
    def get(self, request, *args, **kwargs):
        raise Http404()


def logout_view(request):
    logout(request)
    return redirect("/")


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
    return json_response()
