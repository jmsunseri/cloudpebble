import urllib.parse
from urllib.request import urlopen, Request
import uuid
import json
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseRedirect, HttpResponseBadRequest
from django.shortcuts import render
from django.views.decorators.http import require_safe, require_POST
from ide.forms import SettingsForm
from ide.models.user import UserGithub, UserGithubRepoSync
from utils.td_helper import send_td_event

__author__ = 'katharine'

GITHUB_REPO_SYNC_PENDING_STATES_SESSION_KEY = 'github_repo_sync_pending_states'
GITHUB_REPO_SYNC_MAX_PENDING_STATES = 3


@login_required
def settings_page(request):
    user_settings = request.user.settings
    try:
        github_dev = request.user.github
    except UserGithub.DoesNotExist:
        github_dev = None
    try:
        github_repo_sync = request.user.github_repo_sync
    except UserGithubRepoSync.DoesNotExist:
        github_repo_sync = None

    if request.method == 'POST':
        form = SettingsForm(request.POST, instance=user_settings)
        if form.is_valid():
            form.save()
            send_td_event('cloudpebble_change_user_settings', request=request)
            return render(request, 'ide/settings.html', {
                'form': form,
                'saved': True,
                'github_dev': github_dev,
                'github_repo_sync': github_repo_sync,
            })

    else:
        form = SettingsForm(instance=user_settings)

    send_td_event('cloudpebble_view_user_settings', request=request)

    return render(request, 'ide/settings.html', {
        'form': form,
        'saved': False,
        'github_dev': github_dev,
        'github_repo_sync': github_repo_sync,
    })


def _github_auth_redirect(client_id, nonce, callback_path):
    redirect_uri = settings.PUBLIC_URL.rstrip('/') + callback_path
    return HttpResponseRedirect(
        'https://github.com/login/oauth/authorize?client_id=%s&scope=repo&state=%s&redirect_uri=%s'
        % (client_id, nonce, urllib.parse.quote(redirect_uri, safe=''))
    )


def _complete_github_auth(request, model, client_id, client_secret, callback_path):
    nonce = request.GET['state']
    code = request.GET['code']
    try:
        user_github = model.objects.get(user=request.user)
    except model.DoesNotExist:
        return None
    if user_github.nonce is None or nonce != user_github.nonce:
        return HttpResponseBadRequest('nonce mismatch.')

    _store_github_token_and_profile(user_github, client_id, client_secret, callback_path, code)

    return user_github


def _store_github_token_and_profile(user_github, client_id, client_secret, callback_path, code):
    redirect_uri = settings.PUBLIC_URL.rstrip('/') + callback_path
    params = urllib.parse.urlencode({
        'client_id': client_id,
        'client_secret': client_secret,
        'code': code,
        'redirect_uri': redirect_uri
    }).encode()
    token_request = Request('https://github.com/login/oauth/access_token', params, headers={'Accept': 'application/json'})
    token_result = json.loads(urlopen(token_request).read())
    user_github.token = token_result['access_token']
    user_github.nonce = None

    profile_request = Request('https://api.github.com/user')
    profile_request.add_header("Authorization", "token %s" % user_github.token)
    profile_result = json.loads(urlopen(profile_request).read())
    user_github.username = profile_result['login']
    user_github.avatar = profile_result['avatar_url']
    user_github.save()


def _start_repo_sync_session(user):
    try:
        user_github = user.github_repo_sync
    except UserGithubRepoSync.DoesNotExist:
        user_github = UserGithubRepoSync.objects.create(user=user)
    user_github.nonce = uuid.uuid4().hex
    user_github.save()
    return user_github


def _get_pending_repo_sync_states(request):
    return request.session.get(GITHUB_REPO_SYNC_PENDING_STATES_SESSION_KEY, [])


def _add_pending_repo_sync_state(request, state):
    pending_states = list(_get_pending_repo_sync_states(request))
    if state in pending_states:
        pending_states.remove(state)
    pending_states.append(state)
    request.session[GITHUB_REPO_SYNC_PENDING_STATES_SESSION_KEY] = pending_states[-GITHUB_REPO_SYNC_MAX_PENDING_STATES:]


def _consume_pending_repo_sync_state(request, state):
    pending_states = list(_get_pending_repo_sync_states(request))
    if state not in pending_states:
        return False
    pending_states.remove(state)
    request.session[GITHUB_REPO_SYNC_PENDING_STATES_SESSION_KEY] = pending_states
    return True


@login_required
@require_POST
def start_github_dev_auth(request):
    if not settings.GITHUB_DEV_CLIENT_ID or not settings.GITHUB_DEV_CLIENT_SECRET:
        return HttpResponseBadRequest('Cloud Dev Connection GitHub app is not configured.')
    nonce = uuid.uuid4().hex
    try:
        user_github = request.user.github
    except UserGithub.DoesNotExist:
        user_github = UserGithub.objects.create(user=request.user)
    user_github.nonce = nonce
    user_github.save()
    send_td_event('cloudpebble_github_dev_started', request=request)
    return _github_auth_redirect(settings.GITHUB_DEV_CLIENT_ID, nonce, '/ide/settings/github/callback')


@login_required
@require_POST
def remove_github_dev_auth(request):
    try:
        user_github = request.user.github
        user_github.delete()
    except UserGithub.DoesNotExist:
        pass
    send_td_event('cloudpebble_github_dev_revoked', request=request)
    return HttpResponseRedirect('/ide/settings')


@login_required
@require_safe
def complete_github_dev_auth(request):
    if 'error' in request.GET:
        return HttpResponseRedirect('/ide/settings')
    if not settings.GITHUB_DEV_CLIENT_ID or not settings.GITHUB_DEV_CLIENT_SECRET:
        return HttpResponseBadRequest('Cloud Dev Connection GitHub app is not configured.')
    user_github = _complete_github_auth(
        request,
        UserGithub,
        settings.GITHUB_DEV_CLIENT_ID,
        settings.GITHUB_DEV_CLIENT_SECRET,
        '/ide/settings/github/callback'
    )
    if user_github is None:
        return HttpResponseBadRequest('Missing GitHub auth session.')
    send_td_event('cloudpebble_github_dev_linked', data={
        'data': {'username': user_github.username}
    }, request=request)
    return HttpResponseRedirect('/ide/settings')


@login_required
@require_POST
def start_github_repo_sync_install(request):
    user_github = _start_repo_sync_session(request.user)
    _add_pending_repo_sync_state(request, user_github.nonce)
    send_td_event('cloudpebble_github_repo_sync_started', request=request)
    return HttpResponseRedirect(
        'https://github.com/apps/cloudpebble-repo-sync/installations/new?state=%s'
        % urllib.parse.quote(user_github.nonce, safe='')
    )


@login_required
@require_POST
def start_github_repo_sync_auth(request):
    if not settings.GITHUB_SYNC_CLIENT_ID or not settings.GITHUB_SYNC_CLIENT_SECRET:
        return HttpResponseBadRequest('GitHub Repo Sync app is not configured.')
    user_github = _start_repo_sync_session(request.user)
    _add_pending_repo_sync_state(request, user_github.nonce)
    send_td_event('cloudpebble_github_repo_sync_started', request=request)
    return _github_auth_redirect(settings.GITHUB_SYNC_CLIENT_ID, user_github.nonce, '/ide/settings/github-sync/callback')


@login_required
@require_POST
def remove_github_repo_sync_auth(request):
    try:
        user_github = request.user.github_repo_sync
        user_github.delete()
    except UserGithubRepoSync.DoesNotExist:
        pass
    send_td_event('cloudpebble_github_repo_sync_revoked', request=request)
    return HttpResponseRedirect('/ide/settings')


@login_required
@require_safe
def complete_github_repo_sync_auth(request):
    if 'error' in request.GET:
        messages.error(request, 'GitHub connection could not be completed. Start again from Settings.')
        return HttpResponseRedirect('/ide/settings')
    if not settings.GITHUB_SYNC_CLIENT_ID or not settings.GITHUB_SYNC_CLIENT_SECRET:
        return HttpResponseBadRequest('GitHub Repo Sync app is not configured.')

    nonce = request.GET.get('state')
    code = request.GET.get('code')
    if not nonce or not code:
        messages.error(request, 'GitHub connection could not be completed. Start again from Settings.')
        return HttpResponseRedirect('/ide/settings')

    try:
        user_github = request.user.github_repo_sync
    except UserGithubRepoSync.DoesNotExist:
        messages.error(request, 'GitHub connection could not be completed. Start again from Settings.')
        return HttpResponseRedirect('/ide/settings')
    matched_pending_state = _consume_pending_repo_sync_state(request, nonce)
    if not matched_pending_state and (user_github.nonce is None or nonce != user_github.nonce):
        messages.error(request, 'GitHub connection could not be completed. Start again from Settings.')
        return HttpResponseRedirect('/ide/settings')

    _store_github_token_and_profile(
        user_github,
        settings.GITHUB_SYNC_CLIENT_ID,
        settings.GITHUB_SYNC_CLIENT_SECRET,
        '/ide/settings/github-sync/callback',
        code
    )
    send_td_event('cloudpebble_github_repo_sync_linked', data={
        'data': {'username': user_github.username}
    }, request=request)

    return HttpResponseRedirect('/ide/settings')
