#!/usr/bin/env python3
"""
Update CloudPebble SDK version across active code paths.

Usage:
  scripts/update_sdk_version.py 4.9.148
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "cloudpebble" / "ide" / "migrations"
PROJECT_MODEL = ROOT / "cloudpebble" / "ide" / "models" / "project.py"

# Files that should always follow the active SDK version.
FILES_TO_UPDATE = [
    ROOT / "cloudpebble" / "Dockerfile",
    ROOT / "cloudpebble-qemu-controller" / "Dockerfile",
    ROOT / "cloudpebble" / "ide" / "api" / "project.py",
    ROOT / "cloudpebble" / "ide" / "models" / "project.py",
    ROOT / "cloudpebble" / "ide" / "tasks" / "gist.py",
    ROOT / "cloudpebble" / "ide" / "templates" / "ide" / "index.html",
    ROOT / "cloudpebble" / "ide" / "templates" / "ide" / "project" / "settings.html",
    ROOT / "cloudpebble" / "ide" / "tests" / "test_compile.py",
    ROOT / "cloudpebble" / "ide" / "tests" / "test_gist.py",
    ROOT / "cloudpebble" / "ide" / "tests" / "test_manifest_generation.py",
    ROOT / "cloudpebble" / "ide" / "tests" / "test_project_import_api.py",
    ROOT / "cloudpebble" / "ide" / "utils" / "cloudpebble_test.py",
    ROOT / "cloudpebble" / "ide" / "utils" / "sdk" / "manifest.py",
]


def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def validate_version(version: str) -> None:
    # Accepts values like 4.9.148 or 4.9.121-1-moddable.
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", version):
        fail(f"invalid SDK version '{version}'")


def get_current_sdk_version() -> str:
    data = read_text(PROJECT_MODEL)
    m = re.search(r"SDK_VERSIONS\s*=\s*\(\s*\(\s*'([^']+)'", data, flags=re.S)
    if not m:
        fail(f"could not find current SDK version in {PROJECT_MODEL}")
    return m.group(1)


def update_version_strings(current_version: str, new_version: str) -> list[Path]:
    changed: list[Path] = []
    for path in FILES_TO_UPDATE:
        if not path.exists():
            fail(f"missing file: {path}")
        old = read_text(path)
        new = old

        # Force-update SDK pins in Dockerfiles even if other files are already on target.
        if path == ROOT / "cloudpebble" / "Dockerfile":
            new = re.sub(r"RUN pebble sdk install [^\s]+", f"RUN pebble sdk install {new_version}", new)
            new = re.sub(r"RUN pebble sdk activate [^\s]+", f"RUN pebble sdk activate {new_version}", new)
        elif path == ROOT / "cloudpebble-qemu-controller" / "Dockerfile":
            new = re.sub(
                r"https://sdk\.repebble\.com/v1/files/sdk-core/[^\s)]+",
                f"https://sdk.repebble.com/v1/files/sdk-core/{new_version}",
                new,
            )

        # General replacement for active SDK references.
        new = new.replace(current_version, new_version)
        if new != old:
            write_text(path, new)
            changed.append(path)
    return changed


def latest_migration() -> tuple[int, str]:
    migrations = []
    for p in MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.py"):
        m = re.match(r"^(\d{4})_(.+)\.py$", p.name)
        if m:
            migrations.append((int(m.group(1)), p.stem))
    if not migrations:
        fail(f"no migrations found in {MIGRATIONS_DIR}")
    return sorted(migrations)[-1]


def migration_slug_for_version(version: str) -> str:
    compact = re.sub(r"[^0-9A-Za-z]+", "", version)
    return f"single_sdk_{compact.lower()}"


def create_migration(new_version: str) -> Path | None:
    for existing in MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.py"):
        text = read_text(existing)
        if f"default='{new_version}'" in text and f"choices=[('{new_version}', 'SDK {new_version}')]" in text:
            return None

    last_num, last_stem = latest_migration()
    next_num = last_num + 1
    slug = migration_slug_for_version(new_version)
    migration_name = f"{next_num:04d}_{slug}.py"
    migration_path = MIGRATIONS_DIR / migration_name

    if migration_path.exists():
        return None

    func_name = f"migrate_all_projects_to_{re.sub(r'[^0-9A-Za-z]+', '', new_version).lower()}"
    content = f"""from django.db import migrations, models


def {func_name}(apps, schema_editor):
    Project = apps.get_model('ide', 'Project')
    Project.objects.exclude(sdk_version='{new_version}').update(sdk_version='{new_version}')


class Migration(migrations.Migration):
    dependencies = [
        ('ide', '{last_stem}'),
    ]

    operations = [
        migrations.RunPython({func_name}, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='project',
            name='sdk_version',
            field=models.CharField(
                choices=[('{new_version}', 'SDK {new_version}')],
                default='{new_version}',
                max_length=32,
            ),
        ),
    ]
"""
    write_text(migration_path, content)
    return migration_path


def main(argv: list[str]) -> None:
    if len(argv) != 2:
        fail("usage: scripts/update_sdk_version.py <sdk-version>")

    new_version = argv[1].strip()
    validate_version(new_version)

    current_version = get_current_sdk_version()
    changed = update_version_strings(current_version, new_version)
    migration = None
    if current_version != new_version:
        migration = create_migration(new_version)

    if current_version == new_version:
        if changed:
            print(f"SDK version already {new_version}; normalized out-of-sync files:")
            for path in changed:
                print(f"updated: {path.relative_to(ROOT)}")
        else:
            print(f"SDK version is already {new_version}. Nothing to update.")
    else:
        print(f"updated SDK version: {current_version} -> {new_version}")
        for path in changed:
            print(f"updated: {path.relative_to(ROOT)}")
        if migration:
            print(f"created migration: {migration.relative_to(ROOT)}")
        else:
            print("no migration created (already exists).")


if __name__ == "__main__":
    main(sys.argv)
