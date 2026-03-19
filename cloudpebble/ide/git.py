import json
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
import re
import logging

from github import Github, BadCredentialsException, UnknownObjectException
from github.NamedUser import NamedUser
from django.utils.translation import gettext as _

from ide.models.user import UserGithubRepoSync

logger = logging.getLogger(__name__)


def git_auth_check(f):
    def g(user, *args, **kwargs):
        if not git_verify_tokens(user):
            raise Exception(_("Invalid user GitHub tokens."))
        try:
            return f(user, *args, **kwargs)
        except BadCredentialsException:
            # Bad credentials; remove the user's auth data.
            try:
                logger.warning("Bad credentials; revoking user's GitHub Repo Sync tokens.")
                github = user.github_repo_sync
                github.delete()
            except:
                pass
            raise

    return g


def git_verify_tokens(user):
    try:
        token = user.github_repo_sync.token
    except UserGithubRepoSync.DoesNotExist:
        return False
    if token is None:
        return False

    r = Request('https://api.github.com/user')
    r.add_header("Authorization", "token %s" % token)
    try:
        urlopen(r).read()
    except HTTPError as e:
        if e.getcode() == 401:
            user.github_repo_sync.delete()
        return False
    return True


def get_github(user):
    return Github(user.github_repo_sync.token)


def check_repo_access(user, repo):
    g = get_github(user)
    try:
        repo_obj = g.get_repo(repo)
    except UnknownObjectException:
        raise

    return repo_obj.permissions.push


def url_to_repo(url):
    match = re.match(r'^(?:https?://|git@|git://)?(?:www\.)?github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git|/|$)', url)
    if match is None:
        return None
    else:
        return match.group(1), match.group(2)


@git_auth_check
def create_repo(user, repo_name, description):
    g = get_github(user)
    user = g.get_user()
    return user.create_repo(repo_name, description=description, auto_init=True)
