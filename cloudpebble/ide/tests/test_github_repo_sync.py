import os
import sys
import types
import urllib.parse
from io import BytesIO
from unittest import mock

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'cloudpebble.settings')

import django

django.setup()

fake_td_helper = types.ModuleType('utils.td_helper')
fake_td_helper.send_td_event = lambda *args, **kwargs: None
sys.modules.setdefault('utils.td_helper', fake_td_helper)

from django.test import RequestFactory, SimpleTestCase
try:
    from django.test import override_settings
except ImportError:
    from django.test.utils import override_settings

from ide.models.user import UserGithub, UserGithubRepoSync, UserSettings
from ide.views.settings import (
    complete_github_repo_sync_auth,
    settings_page,
    start_github_repo_sync_auth,
    start_github_repo_sync_install,
)


class DummySession(dict):
    modified = False
    accessed = False

    def __setitem__(self, key, value):
        self.modified = True
        super(DummySession, self).__setitem__(key, value)

    def save(self):
        pass


class DummyMessages(object):
    def __init__(self):
        self.messages = []

    def add(self, level, message, extra_tags=''):
        self.messages.append(message)

    def __iter__(self):
        return iter(self.messages)


class FakeRepoSync(object):
    def __init__(self, nonce=None, token=None, username=None, avatar=None):
        self.nonce = nonce
        self.token = token
        self.username = username
        self.avatar = avatar
        self.save_calls = 0

    def save(self):
        self.save_calls += 1


class FakeUser(object):
    is_authenticated = True

    def __init__(self, github_repo_sync=None):
        self.settings = UserSettings()
        self._github_repo_sync = github_repo_sync

    @property
    def github(self):
        raise UserGithub.DoesNotExist

    @property
    def github_repo_sync(self):
        if self._github_repo_sync is None:
            raise UserGithubRepoSync.DoesNotExist
        return self._github_repo_sync


@override_settings(
    GITHUB_SYNC_CLIENT_ID='client-id',
    GITHUB_SYNC_CLIENT_SECRET='client-secret',
    PUBLIC_URL='http://localhost:8080/',
)
class TestGithubRepoSync(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.send_td_event_patcher = mock.patch('ide.views.settings.send_td_event')
        self.send_td_event_patcher.start()
        self.addCleanup(self.send_td_event_patcher.stop)

    def _request(self, method, path, user, data=None, with_messages=False):
        if method == 'post':
            request = self.factory.post(path, data or {})
        else:
            request = self.factory.get(path, data or {})
        request.user = user
        request.session = DummySession()
        if with_messages:
            request._messages = DummyMessages()
        return request

    @override_settings(
        GITHUB_SYNC_CLIENT_ID='',
        GITHUB_SYNC_CLIENT_SECRET='',
    )
    def test_install_route_redirects_to_github_install_without_oauth_config(self):
        user = FakeUser()
        repo_sync = FakeRepoSync()

        def create_repo_sync(**kwargs):
            kwargs['user']._github_repo_sync = repo_sync
            return repo_sync

        request = self._request('post', '/ide/settings/github-sync/install', user)

        with mock.patch('ide.views.settings.uuid.uuid4', return_value=mock.Mock(hex='install-state')):
            with mock.patch('ide.views.settings.UserGithubRepoSync.objects.create', side_effect=create_repo_sync):
                response = start_github_repo_sync_install(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response['Location'],
            'https://github.com/apps/cloudpebble-repo-sync/installations/new?state=install-state',
        )
        self.assertEqual(
            request.session['github_repo_sync_pending_states'],
            ['install-state'],
        )

    def test_install_route_stores_nonce_and_redirects_to_github_install(self):
        user = FakeUser()
        repo_sync = FakeRepoSync()

        def create_repo_sync(**kwargs):
            kwargs['user']._github_repo_sync = repo_sync
            return repo_sync

        request = self._request('post', '/ide/settings/github-sync/install', user)

        with mock.patch('ide.views.settings.uuid.uuid4', return_value=mock.Mock(hex='install-state')):
            with mock.patch('ide.views.settings.UserGithubRepoSync.objects.create', side_effect=create_repo_sync):
                response = start_github_repo_sync_install(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response['Location'],
            'https://github.com/apps/cloudpebble-repo-sync/installations/new?state=install-state',
        )
        self.assertEqual(repo_sync.nonce, 'install-state')
        self.assertEqual(repo_sync.save_calls, 1)
        self.assertEqual(
            request.session['github_repo_sync_pending_states'],
            ['install-state'],
        )

    def test_auth_route_still_redirects_to_github_auth(self):
        user = FakeUser()
        repo_sync = FakeRepoSync()

        def create_repo_sync(**kwargs):
            kwargs['user']._github_repo_sync = repo_sync
            return repo_sync

        request = self._request('post', '/ide/settings/github-sync/start', user)

        with mock.patch('ide.views.settings.uuid.uuid4', return_value=mock.Mock(hex='auth-state')):
            with mock.patch('ide.views.settings.UserGithubRepoSync.objects.create', side_effect=create_repo_sync):
                response = start_github_repo_sync_auth(request)

        self.assertEqual(response.status_code, 302)
        self.assertIn('https://github.com/login/oauth/authorize?', response['Location'])
        self.assertIn('state=auth-state', response['Location'])
        self.assertIn(
            urllib.parse.quote('http://localhost:8080/ide/settings/github-sync/callback', safe=''),
            response['Location'],
        )
        self.assertEqual(repo_sync.nonce, 'auth-state')
        self.assertEqual(repo_sync.save_calls, 1)
        self.assertEqual(
            request.session['github_repo_sync_pending_states'],
            ['auth-state'],
        )

    @override_settings(
        GITHUB_SYNC_CLIENT_ID='',
        GITHUB_SYNC_CLIENT_SECRET='',
    )
    def test_auth_route_still_requires_oauth_config(self):
        request = self._request('post', '/ide/settings/github-sync/start', FakeUser())

        response = start_github_repo_sync_auth(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn(b'GitHub Repo Sync app is not configured.', response.content)

    def test_install_and_auth_paths_keep_multiple_pending_states(self):
        repo_sync = FakeRepoSync()
        user = FakeUser(github_repo_sync=repo_sync)
        install_request = self._request('post', '/ide/settings/github-sync/install', user)

        with mock.patch('ide.views.settings.uuid.uuid4', return_value=mock.Mock(hex='install-state')):
            install_response = start_github_repo_sync_install(install_request)

        auth_request = self._request('post', '/ide/settings/github-sync/start', user)
        auth_request.session = install_request.session

        with mock.patch('ide.views.settings.uuid.uuid4', return_value=mock.Mock(hex='auth-state')):
            auth_response = start_github_repo_sync_auth(auth_request)

        self.assertEqual(install_response.status_code, 302)
        self.assertEqual(auth_response.status_code, 302)
        self.assertEqual(
            auth_request.session['github_repo_sync_pending_states'],
            ['install-state', 'auth-state'],
        )

    def test_pending_states_are_capped_to_the_newest_three(self):
        repo_sync = FakeRepoSync()
        user = FakeUser(github_repo_sync=repo_sync)
        request = self._request('post', '/ide/settings/github-sync/install', user)

        for state in ['state-1', 'state-2', 'state-3', 'state-4']:
            with mock.patch('ide.views.settings.uuid.uuid4', return_value=mock.Mock(hex=state)):
                start_github_repo_sync_install(request)

        self.assertEqual(
            request.session['github_repo_sync_pending_states'],
            ['state-2', 'state-3', 'state-4'],
        )

    @mock.patch('ide.views.settings.urlopen')
    def test_callback_links_repo_sync_with_valid_session_state_and_code(self, urlopen):
        repo_sync = FakeRepoSync(nonce='nonce')
        user = FakeUser(github_repo_sync=repo_sync)
        request = self._request(
            'get',
            '/ide/settings/github-sync/callback',
            user,
            {'state': 'install-state', 'code': 'auth-code'},
            with_messages=True,
        )
        request.session['github_repo_sync_pending_states'] = ['install-state', 'auth-state']

        urlopen.side_effect = [
            BytesIO(b'{"access_token": "token"}'),
            BytesIO(b'{"login": "repo-sync-user", "avatar_url": "https://example.com/avatar.png"}'),
        ]

        response = complete_github_repo_sync_auth(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/ide/settings')
        self.assertEqual(repo_sync.token, 'token')
        self.assertEqual(repo_sync.username, 'repo-sync-user')
        self.assertEqual(repo_sync.avatar, 'https://example.com/avatar.png')
        self.assertIsNone(repo_sync.nonce)
        self.assertEqual(repo_sync.save_calls, 1)
        self.assertEqual(
            request.session['github_repo_sync_pending_states'],
            ['auth-state'],
        )

    @mock.patch('ide.views.settings.urlopen')
    def test_callback_still_accepts_legacy_model_nonce(self, urlopen):
        repo_sync = FakeRepoSync(nonce='nonce')
        user = FakeUser(github_repo_sync=repo_sync)
        request = self._request(
            'get',
            '/ide/settings/github-sync/callback',
            user,
            {'state': 'nonce', 'code': 'auth-code'},
            with_messages=True,
        )

        urlopen.side_effect = [
            BytesIO(b'{"access_token": "token"}'),
            BytesIO(b'{"login": "repo-sync-user", "avatar_url": "https://example.com/avatar.png"}'),
        ]

        response = complete_github_repo_sync_auth(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/ide/settings')
        self.assertEqual(repo_sync.token, 'token')
        self.assertEqual(repo_sync.username, 'repo-sync-user')
        self.assertEqual(repo_sync.avatar, 'https://example.com/avatar.png')
        self.assertIsNone(repo_sync.nonce)

    def test_callback_with_missing_or_mismatched_state_redirects_to_settings(self):
        user = FakeUser(github_repo_sync=FakeRepoSync(nonce='expected-nonce'))
        request = self._request(
            'get',
            '/ide/settings/github-sync/callback',
            user,
            {'state': 'wrong-nonce', 'code': 'auth-code'},
            with_messages=True,
        )

        response = complete_github_repo_sync_auth(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/ide/settings')
        self.assertEqual(
            list(request._messages),
            ['GitHub connection could not be completed. Start again from Settings.'],
        )

    def test_settings_page_keeps_existing_repo_sync_buttons(self):
        request = self._request('get', '/ide/settings', FakeUser(), with_messages=True)

        response = settings_page(request)

        self.assertContains(response, 'Install GitHub app')
        self.assertContains(response, 'Link your GitHub account')
        self.assertContains(response, 'target="_blank"')
