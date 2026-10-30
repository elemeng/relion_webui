"""Directory browsing for the path pickers.

Why this is not restricted to the allowed roots: the point of the picker is to
*choose* a project directory, and that directory is by definition not yet in
the list. Restricting browsing to roots that are derived from the project list
would make adding the first project impossible.

The honest model is that a logged-in session is the account the agent runs as,
so browsing shows what that account can read anyway — the same way a file
manager would. Listing is read-only. Creating a directory is the one mutation
this module offers, and it is a deliberate, single-purpose action
(`make_directory`) rather than a general file API: it creates one empty
directory, refuses to touch anything that already exists, and is only reachable
through the picker. `POST /api/projects` and the job APIs remain the way to act
on a path; the job APIs stay root-restricted.

Entries are capped so a directory with a hundred thousand files cannot stall the
browser, and unreadable directories are reported rather than crashing.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

MAX_ENTRIES = 4000

# What a picker is looking for. Filtering server-side keeps large directories
# usable: a project picker should not have to page through thousands of files.
FILTERS = ('all', 'directory', 'project', 'star')


@dataclass
class Entry:
    name: str
    path: str
    is_dir: bool
    is_project: bool = False
    is_star: bool = False
    size: Optional[int] = None
    error: Optional[str] = None

    def as_dict(self) -> dict:
        payload = {
            'name': self.name,
            'path': self.path,
            'is_dir': self.is_dir,
            'is_project': self.is_project,
            'is_star': self.is_star,
        }
        if self.size is not None:
            payload['size'] = self.size
        if self.error:
            payload['error'] = self.error
        return payload


@dataclass
class Listing:
    path: str
    parent: Optional[str]
    entries: List[Entry] = field(default_factory=list)
    truncated: bool = False
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            'path': self.path,
            'parent': self.parent,
            'entries': [entry.as_dict() for entry in self.entries],
            'truncated': self.truncated,
            'error': self.error,
        }


def default_start_directory() -> str:
    """Where the picker opens when no path is given."""
    return str(Path.home())


def _is_project(directory: Path) -> bool:
    try:
        return (directory / 'default_pipeline.star').is_file()
    except OSError:
        return False


def _matches(entry: Entry, wanted: str) -> bool:
    """Whether an entry is worth showing under a filter.

    Directories are always listed, whatever the filter. A picker is a browser
    first: hiding directories makes them impossible to reach, which is how
    "nothing here (filter: project)" happened. The filter decides which *files*
    appear; directories carry `is_project`/`is_star` for the client to mark.
    """
    if wanted == 'all' or entry.is_dir:
        return True
    return wanted == 'star' and entry.is_star


def list_directory(
    path: Optional[str] = None,
    *,
    filter_kind: str = 'all',
    show_hidden: bool = False,
) -> Listing:
    """List one directory for the picker.

    Raises ``ValueError`` for a non-existent or non-directory path; permission
    problems on individual children are reported per entry.
    """
    if filter_kind not in FILTERS:
        raise ValueError(f'unknown filter {filter_kind!r}; expected one of {FILTERS}')

    target = Path(path).expanduser() if path else Path(default_start_directory())
    try:
        resolved = target.resolve(strict=False)
    except OSError as exc:
        raise ValueError(f'cannot resolve {target}: {exc}') from exc

    if not resolved.exists():
        raise ValueError(f'no such directory: {resolved}')
    if not resolved.is_dir():
        raise ValueError(f'not a directory: {resolved}')

    parent = str(resolved.parent) if resolved.parent != resolved else None

    try:
        with os.scandir(resolved) as iterator:
            raw = list(iterator)
    except PermissionError as exc:
        return Listing(
            path=str(resolved),
            parent=parent,
            error=f'permission denied: {exc}',
        )
    except OSError as exc:
        return Listing(path=str(resolved), parent=parent, error=str(exc))

    entries: List[Entry] = []
    truncated = False
    for item in sorted(raw, key=lambda entry: entry.name.lower()):
        if truncated:
            break
        if not show_hidden and item.name.startswith('.'):
            continue

        try:
            is_dir = item.is_dir(follow_symlinks=True)
        except OSError:
            is_dir = False

        entry = Entry(
            name=item.name,
            path=str(resolved / item.name),
            is_dir=is_dir,
            is_project=is_dir and _is_project(Path(item.path)),
            is_star=(not is_dir) and item.name.endswith('.star'),
        )
        if not is_dir:
            try:
                entry.size = item.stat(follow_symlinks=False).st_size
            except OSError:
                entry.size = None

        if not _matches(entry, filter_kind):
            continue

        entries.append(entry)
        if len(entries) >= MAX_ENTRIES:
            truncated = True

    return Listing(
        path=str(resolved),
        parent=parent,
        entries=entries,
        truncated=truncated,
    )


def make_directory(parent: str, name: str) -> dict:
    """Create one empty directory under ``parent`` and return it.

    Deliberately narrower than a general mkdir: the name must be a single path
    component, the parent must already exist and be a directory, and an existing
    entry of that name is refused rather than reused. That keeps the operation
    to what the picker's "New folder" needs - you are standing in a directory
    looking at it, and you want one more entry beside the ones you can see.

    Raises ``ValueError`` with a message the picker shows verbatim.
    """
    clean = name.strip()
    if not clean:
        raise ValueError('a folder name is required')
    if clean in ('.', '..') or '/' in clean or '\\' in clean or '\0' in clean:
        raise ValueError(f'{name!r} is not a folder name')
    if clean.startswith('.'):
        raise ValueError('folder names may not start with a dot')

    try:
        base = Path(parent).expanduser().resolve(strict=False)
    except OSError as exc:
        raise ValueError(f'cannot resolve {parent}: {exc}') from exc

    if not base.is_dir():
        raise ValueError(f'not a directory: {base}')

    target = base / clean
    if target.exists() or target.is_symlink():
        raise ValueError(f'{clean} already exists')

    try:
        target.mkdir()
    except OSError as exc:
        raise ValueError(f'could not create {clean}: {exc}') from exc

    return {
        'path': str(target),
        'name': clean,
        'parent': str(base),
    }


def stat_path(path: str) -> dict:
    """Describe one path, so the picker can validate a typed value."""
    target = Path(path).expanduser()
    try:
        resolved = target.resolve(strict=False)
    except OSError as exc:
        raise ValueError(f'cannot resolve {target}: {exc}') from exc

    exists = resolved.exists()
    payload = {
        'path': str(resolved),
        'exists': exists,
        'is_dir': False,
        'is_project': False,
        'is_star': resolved.name.endswith('.star'),
    }
    if exists:
        try:
            payload['is_dir'] = resolved.is_dir()
        except OSError:
            payload['is_dir'] = False
        payload['is_project'] = payload['is_dir'] and _is_project(resolved)
    return payload


# Convenience list for the picker: where to start browsing.
def quick_locations() -> List[dict]:
    """Common starting points, so the picker does not always start at home."""
    home = Path.home()
    candidates = [
        ('home', home),
        ('current project', Path.cwd()),
    ]
    locations = []
    for label, path in candidates:
        try:
            if path.is_dir():
                locations.append({'label': label, 'path': str(path)})
        except OSError:
            continue
    return locations
