import base64
import io
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
import json
import os
import logging

from celery import shared_task
from django.db import transaction
from django.utils.timezone import now
from github.GithubObject import NotSet
from github import Github, GithubException, InputGitTreeElement

from ide.git import git_auth_check, get_github
from ide.models.build import BuildResult
from ide.models.files import SourceFile, ResourceFile, ResourceVariant, ResourceIdentifier
from ide.models.project import Project
from ide.tasks import do_import_archive, run_compile
from ide.tasks.archive import get_filename_variant
from ide.utils.git import git_sha, git_blob
from ide.utils.project import find_project_root_and_manifest, BaseProjectItem, InvalidProjectArchiveException
from ide.utils.sdk import generate_manifest_dict, generate_manifest, generate_wscript_file, manifest_name_for_project, load_manifest_dict
from utils.td_helper import send_td_event
from utils.events import publish_event

__author__ = 'katharine'

logger = logging.getLogger(__name__)


def exception_reason(error):
    reason = str(error)
    if reason:
        return reason
    return error.__class__.__name__


@shared_task(acks_late=True)
def do_import_github(project_id, github_user, github_project, github_branch, delete_project=False):
    project = None
    user = None
    try:
        try:
            project = Project.objects.get(pk=project_id)
            user = project.owner
        except:
            pass

        url = "https://github.com/%s/%s/archive/%s.zip" % (github_user, github_project, github_branch)
        auth_url = get_authenticated_archive_url(user, github_user, github_project, github_branch)
        archive = None

        if file_exists(url):
            archive = urlopen(url)
        elif auth_url:
            try:
                archive = urlopen(auth_url)
            except (HTTPError, URLError):
                pass

        if archive is None:
            raise Exception(
                "Unable to import github.com/%s/%s on branch '%s'. Verify repository, branch, and access."
                % (github_user, github_project, github_branch)
            )

        return do_import_archive(project_id, archive.read())
    except Exception as e:
        if delete_project and project is not None:
            try:
                project.delete()
            except:
                pass
        send_td_event('cloudpebble_github_import_failed', data={
            'data': {
                'reason': exception_reason(e),
                'github_user': github_user,
                'github_project': github_project,
                'github_branch': github_branch
            }
        }, user=user)
        raise


def file_exists(url):
    request = Request(url)
    request.get_method = lambda: 'HEAD'
    try:
        urlopen(request)
    except:
        return False
    else:
        return True


def get_authenticated_archive_url(user, github_user, github_project, github_branch):
    if user is None:
        return None
    try:
        g = get_github(user)
        repo = g.get_repo("%s/%s" % (github_user, github_project))
        return repo.get_archive_link('zipball', ref=github_branch)
    except:
        return None


@git_auth_check
def github_push(user, commit_message, repo_name, project):
    g = Github(user.github_repo_sync.token)
    repo = g.get_repo(repo_name)
    try:
        branch = repo.get_branch(project.github_branch or repo.default_branch)
    except GithubException:
        raise Exception("Unable to get branch.")
    commit = repo.get_git_commit(branch.commit.sha)
    tree = repo.get_git_tree(commit.tree.sha, recursive=True)

    next_tree = {x.path: InputGitTreeElement(path=x.path, mode=x.mode, type=x.type, sha=x.sha) for x in tree.tree}

    try:
        root, manifest_item = find_project_root_and_manifest([GitProjectItem(repo, x) for x in tree.tree])
    except InvalidProjectArchiveException:
        root = ''
        manifest_item = None

    expected_paths = set()

    def update_expected_paths(new_path):
        # This adds the path *and* its parent directories to the list of expected paths.
        # The parent directories are already keys in next_tree, so if they aren't present in expected_paths
        # then, when iterating over next_tree to see which files have been deleted, we would have to treat
        # directories as special cases.
        split_path = new_path.split('/')
        expected_paths.update('/'.join(split_path[:p]) for p in range(2, len(split_path) + 1))

    project_sources = project.source_files.all()
    has_changed = False
    for source in project_sources:
        repo_path = os.path.join(root, source.project_path)

        update_expected_paths(repo_path)
        our_content = source.get_contents()
        if repo_path not in next_tree:
            has_changed = True
            if isinstance(our_content, bytes):
                blob = repo.create_git_blob(base64.b64encode(our_content).decode('ascii'), 'base64')
                logger.debug("Created blob %s for binary source %s", blob.sha, repo_path)
                next_tree[repo_path] = InputGitTreeElement(path=repo_path, mode='100644', type='blob', sha=blob.sha)
            else:
                next_tree[repo_path] = InputGitTreeElement(path=repo_path, mode='100644', type='blob',
                                                           content=our_content)
            logger.debug("New file: %s", repo_path)
        else:
            sha = next_tree[repo_path]._InputGitTreeElement__sha
            expected_sha = git_sha(our_content)
            if expected_sha != sha:
                logger.debug("Updated file: %s", repo_path)
                if isinstance(our_content, bytes):
                    blob = repo.create_git_blob(base64.b64encode(our_content).decode('ascii'), 'base64')
                    logger.debug("Created blob %s for binary source %s", blob.sha, repo_path)
                    next_tree[repo_path]._InputGitTreeElement__content = NotSet
                    next_tree[repo_path]._InputGitTreeElement__sha = blob.sha
                else:
                    next_tree[repo_path]._InputGitTreeElement__sha = NotSet
                    next_tree[repo_path]._InputGitTreeElement__content = our_content
                has_changed = True

    # Now try handling resource files.
    resources = project.resources.all()
    resource_root = project.resources_path
    for res in resources:
        for variant in res.variants.all():
            repo_path = os.path.join(resource_root, variant.path)
            update_expected_paths(repo_path)
            if repo_path in next_tree:
                content = variant.get_contents()
                if git_sha(content) != next_tree[repo_path]._InputGitTreeElement__sha:
                    logger.debug("Changed resource: %s", repo_path)
                    has_changed = True
                    blob = repo.create_git_blob(base64.b64encode(content).decode('ascii'), 'base64')
                    logger.debug("Created blob %s", blob.sha)
                    next_tree[repo_path]._InputGitTreeElement__sha = blob.sha
            else:
                logger.debug("New resource: %s", repo_path)
                has_changed = True
                blob = repo.create_git_blob(base64.b64encode(variant.get_contents()).decode('ascii'), 'base64')
                logger.debug("Created blob %s", blob.sha)
                next_tree[repo_path] = InputGitTreeElement(path=repo_path, mode='100644', type='blob', sha=blob.sha)

    # Manage deleted files
    src_root = os.path.join(root, 'src')
    worker_src_root = os.path.join(root, 'worker_src')
    paths_to_remove = []
    for path in next_tree.keys():
        if not (any(path.startswith(root+'/') for root in (src_root, resource_root, worker_src_root))):
            continue
        if path not in expected_paths:
            paths_to_remove.append(path)
    for path in paths_to_remove:
        del next_tree[path]
        logger.debug("Deleted file: %s", path)
        has_changed = True

    # Compare the resource dicts
    remote_manifest_path = root + manifest_name_for_project(project)
    remote_wscript_path = root + 'wscript'

    if manifest_item:
        their_manifest_dict = json.loads(manifest_item.read())
        their_res_dict = their_manifest_dict.get('resources', their_manifest_dict.get('pebble', their_manifest_dict).get('resources', {'media': []}))
        # If the manifest needs a new path (e.g. it is now package.json), delete the old one
        if manifest_item.path != remote_manifest_path:
            del next_tree[manifest_item.path]
    else:
        their_manifest_dict = {}
        their_res_dict = {'media': []}

    our_manifest_dict = generate_manifest_dict(project, resources)
    our_res_dict = our_manifest_dict.get('resources', our_manifest_dict.get('pebble', our_manifest_dict).get('resources', {'media': []}))

    if our_res_dict != their_res_dict:
        logger.debug("Resources mismatch.")
        has_changed = True
        # Try removing things that we've deleted, if any
        to_remove = set(x['file'] for x in their_res_dict['media']) - set(x['file'] for x in our_res_dict['media'])
        for path in to_remove:
            repo_path = resource_root + path
            if repo_path in next_tree:
                logger.debug("Deleted resource: %s", repo_path)
                del next_tree[repo_path]

    # This one is separate because there's more than just the resource map changing.
    if their_manifest_dict != our_manifest_dict:
        has_changed = True
        if remote_manifest_path in next_tree:
            next_tree[remote_manifest_path]._InputGitTreeElement__sha = NotSet
            next_tree[remote_manifest_path]._InputGitTreeElement__content = generate_manifest(project, resources)
        else:
            next_tree[remote_manifest_path] = InputGitTreeElement(path=remote_manifest_path, mode='100644', type='blob',
                                                                  content=generate_manifest(project, resources))

    if project.project_type == 'native' and remote_wscript_path not in next_tree:
        next_tree[remote_wscript_path] = InputGitTreeElement(path=remote_wscript_path, mode='100644', type='blob',
                                                             content=generate_wscript_file(project, True))
        has_changed = True

    # Add .gitignore if the repo doesn't have one
    gitignore_path = os.path.join(root, '.gitignore') if root else '.gitignore'
    if gitignore_path not in next_tree:
        next_tree[gitignore_path] = InputGitTreeElement(
            path=gitignore_path, mode='100644', type='blob',
            content="build/\nnode_modules/\n")
        has_changed = True

    # Commit the new tree.
    if has_changed:
        logger.debug("Has changed; committing")
        # GitHub seems to choke if we pass the raw directory nodes off to it,
        # so we delete those.
        paths_to_remove = []
        for x in next_tree.keys():
            if next_tree[x]._InputGitTreeElement__mode == '040000':
                paths_to_remove.append(x)
        for path in paths_to_remove:
            del next_tree[path]
            logger.debug("removing subtree node %s", path)

        logger.debug([x._InputGitTreeElement__mode for x in next_tree.values()])
        branch_name = project.github_branch or repo.default_branch
        try:
            git_tree = repo.create_git_tree(next_tree.values())
            logger.debug("Created tree %s", git_tree.sha)
            git_commit = repo.create_git_commit(commit_message, git_tree, [commit])
            logger.debug("Created commit %s", git_commit.sha)
            git_ref = repo.get_git_ref('heads/%s' % branch_name)
            git_ref.edit(git_commit.sha)
            logger.debug("Updated ref %s", git_ref.ref)
        except GithubException as e:
            if e.status == 404:
                raise Exception(
                    "Could not push to GitHub: you may not have write access to %s, "
                    "or the branch '%s' may not exist." % (repo_name, branch_name))
            elif e.status == 409:
                raise Exception(
                    "Could not push to GitHub: the remote branch has changed. "
                    "Try pulling first.")
            raise Exception("GitHub push failed: %s" % str(e))
        project.github_last_commit = git_commit.sha
        project.github_last_sync = now()
        project.save()
        return True

    send_td_event('cloudpebble_github_push', data={
        'data': {
            'repo': project.github_repo
        }
    }, user=user)

    return False


def get_root_path(path):
    path, extension = os.path.splitext(path)
    return path.split('~', 1)[0] + extension


class GitProjectItem(BaseProjectItem):
    def __init__(self, repo, tree_item):
        self.repo = repo
        self.git_item = tree_item

    def read(self):
        return git_blob(self.repo, self.git_item.sha)

    @property
    def path(self):
        return self.git_item.path


PEBBLEJS_BUILTIN_RESOURCES = frozenset({
    'MONO_FONT_14', 'IMAGE_MENU_ICON', 'IMAGE_LOGO_SPLASH', 'IMAGE_TILE_SPLASH',
})


def validate_resources_against_tree(paths_notags, manifest, project, root=''):
    """Validate that all resources referenced in the manifest exist in the tree.

    Given a set of tag-stripped paths from a git tree and a parsed manifest dict,
    checks that every resource file listed in the manifest is present in the tree.
    Skips built-in Pebble.js resources that don't need to be in the repo.

    Returns the manifest's media list for further processing.

    Raises Exception if a required resource is missing.
    """
    resource_root = ((root + '/' if root else '') + project.resources_path).rstrip('/') + '/'
    pebble = manifest.get('pebble', manifest)
    manifest_resources = pebble.get('resources', {}).get('media', [])
    project_type = pebble.get('projectType', 'native')

    for resource in manifest_resources:
        path = resource_root + resource['file']
        if project_type == 'pebblejs' and resource['name'] in PEBBLEJS_BUILTIN_RESOURCES:
            continue
        if path not in paths_notags:
            raise Exception("Resource %s not found in repo." % path)

    return manifest_resources


def parse_manifest_from_tree(tree_items, project):
    """Find and parse the manifest from a git tree, returning (root, manifest_dict).

    Takes a list of BaseProjectItem instances and a Project. Returns a tuple of
    (project_root_path, manifest_dict). Raises ValueError or
    InvalidProjectArchiveException if the tree has no valid manifest.
    """
    root, manifest_item = find_project_root_and_manifest(tree_items)
    manifest = json.loads(manifest_item.read())
    return root, manifest


@git_auth_check
def github_pull(user, project, force=False):
    g = get_github(user)
    repo_name = project.github_repo
    if repo_name is None:
        raise Exception("No GitHub repo defined.")
    repo = g.get_repo(repo_name)
    branch_name = project.github_branch or repo.default_branch
    try:
        branch = repo.get_branch(branch_name)
    except GithubException:
        raise Exception("Unable to get the branch.")

    new_commit_sha = branch.commit.sha

    if project.github_last_commit == new_commit_sha:
        # Nothing to do.
        return False

    # Use full wipe-and-replace for force pulls or when we have no previous commit
    if force or project.github_last_commit is None:
        return _github_pull_full(user, project, repo, branch)

    # Try incremental delta sync
    try:
        return _github_pull_delta(user, project, repo, new_commit_sha)
    except Exception as e:
        logger.warning("Delta sync failed (%s), falling back to full pull", e)
        return _github_pull_full(user, project, repo, branch)


def _github_pull_full(user, project, repo, branch):
    """Full wipe-and-replace pull: downloads entire zip and re-imports everything."""
    branch_name = project.github_branch or repo.default_branch
    commit = repo.get_git_commit(branch.commit.sha)
    tree = repo.get_git_tree(commit.tree.sha, recursive=True)

    paths_notags = {get_root_path(x.path) for x in tree.tree}

    try:
        root, manifest = parse_manifest_from_tree(
            [GitProjectItem(repo, x) for x in tree.tree], project)
    except ValueError as e:
        raise ValueError("In manifest file: %s" % str(e))

    validate_resources_against_tree(paths_notags, manifest, project, root)

    # Start the zip download in parallel with validation.
    zip_url = repo.get_archive_link('zipball', branch_name)
    u = urlopen(zip_url)

    import_result = do_import_archive(project.id, u.read(), wipe_existing=True)

    project.github_last_commit = branch.commit.sha
    project.github_last_sync = now()
    project.save()

    send_td_event('cloudpebble_github_pull', data={
        'data': {'repo': project.github_repo}
    }, user=user)

    return import_result


def _github_pull_delta(user, project, repo, new_commit_sha):
    """Incremental pull: only processes files that changed between commits."""
    comparison = repo.compare(project.github_last_commit, new_commit_sha)

    if comparison.ahead_by == 0:
        project.github_last_commit = new_commit_sha
        project.github_last_sync = now()
        project.save()
        return False

    commit = repo.get_git_commit(new_commit_sha)
    tree = repo.get_git_tree(commit.tree.sha, recursive=True)

    paths_notags = {get_root_path(x.path) for x in tree.tree}

    try:
        root, manifest = parse_manifest_from_tree(
            [GitProjectItem(repo, x) for x in tree.tree], project)
    except ValueError as e:
        raise ValueError("In manifest file: %s" % str(e))

    validate_resources_against_tree(paths_notags, manifest, project, root)

    changed_files = comparison.files
    _apply_delta_changes(project, repo, root, manifest, changed_files)

    project.github_last_commit = new_commit_sha
    project.github_last_sync = now()
    project.save()

    send_td_event('cloudpebble_github_pull', data={
        'data': {'repo': project.github_repo}
    }, user=user)

    return True


def _apply_delta_changes(project, repo, root, manifest, changed_files):
    """Apply incremental file changes to the project database without a full wipe.

    Given a list of changed files from GitHub's Compare API, creates, updates,
    or deletes only the affected SourceFile and ResourceFile/ResourceVariant
    records. All changes are wrapped in a single atomic transaction.
    """
    manifest_kind = 'package.json' if 'pebble' in manifest else 'appinfo.json'
    resource_root = ((root + '/' if root else '') + project.resources_path).rstrip('/') + '/'

    with transaction.atomic():
        project_options, media_map, dependencies = load_manifest_dict(manifest, manifest_kind)

        for k, v in project_options.items():
            setattr(project, k, v)
        project.full_clean()
        project.set_dependencies(dependencies)

        tag_map = {v: k for k, v in ResourceVariant.VARIANT_STRINGS.items() if v}

        existing_sources = {s.project_path: s for s in project.source_files.all()}
        existing_resources = {}
        for r in project.resources.all():
            dir_prefix = ResourceFile.DIR_MAP.get(r.kind, '') + '/'
            root_file_name = dir_prefix + r.file_name if r.kind in ResourceFile.DIR_MAP else r.file_name
            existing_resources[root_file_name] = r

        for change in changed_files:
            filename = change.filename
            status = change.status
            project_path = filename[len(root) + 1:] if root and filename.startswith(root + '/') else filename

            if status in ('added', 'modified', 'renamed'):
                if status == 'renamed' and change.previous_filename:
                    prev_project_path = change.previous_filename[len(root) + 1:] if root and change.previous_filename.startswith(root + '/') else change.previous_filename
                    _remove_file_by_path(project, prev_project_path, existing_sources, existing_resources)

                if project_path.startswith(project.resources_path + '/'):
                    _upsert_resource_variant(project, repo, change, project_path, existing_resources, tag_map, media_map)
                else:
                    try:
                        base_filename, target = SourceFile.get_details_for_path(project.project_type, project_path)
                        _upsert_source_file(project, repo, change, base_filename, target, existing_sources, project_path)
                    except ValueError:
                        logger.debug("Skipping unrecognized file in delta: %s", filename)
                        continue

            elif status == 'removed':
                _remove_file_by_path(project, project_path, existing_sources, existing_resources)

        _sync_resource_files_from_manifest(project, media_map, existing_resources)

        project.save()


def _upsert_source_file(project, repo, change, base_filename, target, existing_sources, project_path=None):
    """Create or update a SourceFile from a changed file in a GitHub comparison."""
    content = _fetch_file_content(repo, change)
    if content is None:
        logger.warning("Could not fetch content for %s, skipping", change.filename)
        return

    if project_path is None:
        project_path = change.filename
    if project_path in existing_sources:
        source = existing_sources[project_path]
    else:
        source = SourceFile.objects.create(project=project, file_name=base_filename, target=target)
        existing_sources[project_path] = source

    if source.is_editable_text:
        source.save_text(content.decode('utf-8') if isinstance(content, bytes) else content)
    else:
        source.save_string(content)


def _upsert_resource_variant(project, repo, change, project_path, existing_resources, tag_map, media_map=None):
    """Create or update a ResourceVariant from a changed resource file in a GitHub comparison."""
    resource_root = project.resources_path + '/'
    base_filename = project_path[len(resource_root):]
    try:
        tags, root_file_name = get_filename_variant(base_filename, tag_map)
    except ValueError:
        root_file_name = os.path.splitext(base_filename.split('~', 1)[0])[0] + os.path.splitext(base_filename)[1]
        tags = []
    tags_string = ",".join(str(int(t)) for t in tags)

    if root_file_name in existing_resources:
        resource_file = existing_resources[root_file_name]
    else:
        kind = _infer_resource_kind_from_path(root_file_name)
        if media_map:
            for resource in media_map:
                try:
                    _, manifest_root = get_filename_variant(resource['file'], tag_map)
                except ValueError:
                    manifest_root = os.path.splitext(resource['file'].split('~', 1)[0])[0] + os.path.splitext(resource['file'])[1]
                if manifest_root == root_file_name:
                    kind = resource['type']
                    break
        file_name = _strip_resource_dir_prefix(root_file_name)
        resource_file = ResourceFile.objects.create(
            project=project, file_name=file_name, kind=kind)
        existing_resources[root_file_name] = resource_file

    content = _fetch_file_content(repo, change)
    if content is None:
        logger.warning("Could not fetch content for resource %s, skipping", change.filename)
        return

    variant = ResourceVariant.objects.filter(
        resource_file=resource_file, tags=tags_string).first()
    if variant is None:
        variant = ResourceVariant.objects.create(resource_file=resource_file, tags=tags_string)

    variant.save_file(io.BytesIO(content))


def _remove_file_by_path(project, filename, existing_sources, existing_resources):
    """Remove a SourceFile or ResourceFile/Variant by its repo path."""
    resource_root = project.resources_path + '/'
    tag_map = {v: k for k, v in ResourceVariant.VARIANT_STRINGS.items() if v}

    if filename.startswith(resource_root):
        base_filename = filename[len(resource_root):]
        try:
            tags, root_file_name = get_filename_variant(base_filename, tag_map)
        except ValueError:
            return

        if root_file_name in existing_resources:
            resource_file = existing_resources[root_file_name]
            tags_string = ",".join(str(int(t)) for t in tags)
            ResourceVariant.objects.filter(
                resource_file=resource_file, tags=tags_string).delete()
            if resource_file.variants.count() == 0:
                del existing_resources[root_file_name]
                resource_file.delete()
    else:
        try:
            base_filename, target = SourceFile.get_details_for_path(project.project_type, filename)
        except ValueError:
            return
        if filename in existing_sources:
            existing_sources[filename].delete()
            del existing_sources[filename]
        else:
            SourceFile.objects.filter(
                project=project, file_name=base_filename, target=target).delete()


def _sync_resource_files_from_manifest(project, media_map, existing_resources):
    """Reconcile ResourceFile and ResourceIdentifier records with the manifest.

    Creates new ResourceFile entries for resources in the manifest that don't
    yet exist, creates ResourceIdentifier entries for each resource, and removes
    resources no longer in the manifest.
    """
    all_dir_prefixes = set(v + '/' for v in ResourceFile.DIR_MAP.values())
    desired_file_names = set()

    for resource in media_map:
        if project.project_type in {'pebblejs', 'simplyjs'}:
            if resource['name'] in PEBBLEJS_BUILTIN_RESOURCES:
                continue

        file_name = resource['file']
        try:
            tags, root_file_name = get_filename_variant(file_name, {v: k for k, v in ResourceVariant.VARIANT_STRINGS.items() if v})
        except ValueError:
            root_file_name = os.path.splitext(file_name.split('~', 1)[0])[0] + os.path.splitext(file_name)[1]

        bare_name = _strip_resource_dir_prefix(root_file_name, all_dir_prefixes)
        desired_file_names.add(root_file_name)

        if root_file_name not in existing_resources:
            resource_file = ResourceFile.objects.create(
                project=project,
                file_name=bare_name,
                kind=resource['type'],
                is_menu_icon=resource.get('menuIcon', False),
            )
            existing_resources[root_file_name] = resource_file
        else:
            resource_file = existing_resources[root_file_name]
            if resource_file.kind != resource['type'] or resource_file.is_menu_icon != resource.get('menuIcon', False):
                resource_file.kind = resource['type']
                resource_file.is_menu_icon = resource.get('menuIcon', False)
                resource_file.save()

        resource_file = existing_resources[root_file_name]
        target_platforms = json.dumps(resource['targetPlatforms']) if 'targetPlatforms' in resource else None

        ResourceIdentifier.objects.update_or_create(
            resource_file=resource_file,
            resource_id=resource['name'],
            defaults={
                'character_regex': resource.get('characterRegex', None),
                'tracking': resource.get('trackingAdjust', None),
                'compatibility': resource.get('compatibility', None),
                'memory_format': resource.get('memoryFormat', None),
                'storage_format': resource.get('storageFormat', None),
                'space_optimisation': resource.get('spaceOptimization', None),
                'target_platforms': target_platforms,
            }
        )

    for file_name in list(existing_resources.keys()):
        if file_name not in desired_file_names:
            existing_resources[file_name].delete()
            del existing_resources[file_name]


def _fetch_file_content(repo, change):
    """Fetch the content of a file from GitHub, handling both text and binary files."""
    filename = change.filename
    try:
        contents = repo.get_contents(filename, ref=change.sha if hasattr(change, 'sha') and change.sha else None)
        if isinstance(contents, list):
            logger.warning("Expected file but got directory at %s, skipping", filename)
            return None
        if contents.encoding == 'base64':
            return base64.b64decode(contents.content)
        return contents.decoded_content
    except GithubException as e:
        logger.warning("Failed to fetch %s from GitHub: %s", filename, e)
        return None


def _infer_resource_kind_from_path(filename):
    """Infer the resource kind from the file extension."""
    ext = os.path.splitext(filename)[1].lower()
    kind_map = {
        '.png': 'png',
        '.pbi': 'pbi',
        '.ttf': 'font',
        '.otf': 'font',
        '.woff': 'font',
    }
    return kind_map.get(ext, 'raw')


def _strip_resource_dir_prefix(file_name, all_dir_prefixes=None):
    """Strip resource directory prefix (e.g. 'images/', 'fonts/') from a file name.

    If all_dir_prefixes is not given, uses the default ResourceFile.DIR_MAP prefixes.
    """
    if all_dir_prefixes is None:
        all_dir_prefixes = set(v + '/' for v in ResourceFile.DIR_MAP.values())
    for prefix in all_dir_prefixes:
        if file_name.startswith(prefix):
            return file_name[len(prefix):]
    return file_name


@shared_task
def do_github_push(project_id, commit_message):
    project = Project.objects.select_related('owner__github').get(pk=project_id)
    return github_push(project.owner, commit_message, project.github_repo, project)


@shared_task
def do_github_pull(project_id, force=False):
    project = Project.objects.select_related('owner__github').get(pk=project_id)
    publish_event(project_id, 'pull_start')
    try:
        changed = github_pull(project.owner, project, force=force)
        publish_event(project_id, 'pull_complete', github_last_commit=project.github_last_commit or '')
    except Exception:
        publish_event(project_id, 'pull_failed')
        raise

    if changed and project.github_hook_build:
        build = BuildResult.objects.create(project=project)
        publish_event(project_id, 'build_start', build_id=build.id)
        run_compile(build.id)


@shared_task
def hooked_commit(project_id, target_commit):
    project = Project.objects.select_related('owner__github').get(pk=project_id)
    did_something = False
    logger.debug("Comparing %s versus %s", project.github_last_commit, target_commit)
    if project.github_last_commit != target_commit:
        publish_event(project_id, 'pull_start')
        try:
            github_pull(project.owner, project, force=project.github_hook_force)
            publish_event(project_id, 'pull_complete', github_last_commit=project.github_last_commit or '', github_last_sync=str(project.github_last_sync) if project.github_last_sync else '')
        except Exception:
            publish_event(project_id, 'pull_failed')
            raise
        did_something = True

    if project.github_hook_build:
        build = BuildResult.objects.create(project=project)
        publish_event(project_id, 'build_start', build_id=build.id)
        run_compile(build.id)
        did_something = True

    return did_something
