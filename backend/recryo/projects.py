"""The set of RELION projects a user has added to their agent.

The agent runs long-lived, so the list has to survive restarts. It is stored in
the user's own config directory rather than in any project, because a project
is shared and its contents should not change just because somebody opened it in
a browser.

Only projects on the list are reachable: a client cannot point the API at an
arbitrary path, which matters because the API can submit jobs there.
"""

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from . import pipeline as pipeline_module

PROJECT_LIST_FILENAME = 'projects.json'


def default_config_directory() -> Path:
    """Per-user config location: ``$HOME/.relion_web``.

    Deliberately beside RELION's own ``$HOME/.relion`` (``jaz/util/log.cpp``
    writes its theme there) rather than under ``~/.config``: someone looking for
    "where did the web UI keep my projects" will look where RELION keeps its
    settings, not in the XDG tree.

    ``RECRYO_HOME`` overrides it, which is what the tests use and what a user
    needs if their home is read-only or on a shared filesystem.
    """
    override = os.environ.get('RECRYO_HOME')
    if override:
        return Path(override).expanduser()
    return Path.home() / '.relion_web'


def legacy_config_directory() -> Optional[Path]:
    """Where earlier versions kept the project list, if it exists.

    The path moved from ``$XDG_CONFIG_HOME/relion-web`` (``~/.config`` by
    default) to ``$HOME/.relion_web``. An existing list is carried over rather
    than silently abandoned, because a project list is user data: losing it
    looks like the agent forgot every project.
    """
    xdg = os.environ.get('XDG_CONFIG_HOME')
    base = Path(xdg).expanduser() if xdg else Path.home() / '.config'
    legacy = base / 'relion-web'
    try:
        if (legacy / PROJECT_LIST_FILENAME).is_file():
            return legacy
    except OSError:
        return None
    return None


@dataclass
class ProjectEntry:
    path: str
    added_at: float = field(default_factory=time.time)
    name: Optional[str] = None

    def as_dict(self, *, readable: bool = True, job_count: Optional[int] = None) -> dict:
        payload = {
            'path': self.path,
            'name': self.name or Path(self.path).name,
            'added_at': self.added_at,
            'readable': readable,
        }
        if job_count is not None:
            payload['job_count'] = job_count
        return payload


class ProjectRegistry:
    """Ordered list of project directories, persisted as JSON."""

    def __init__(self, config_directory: Optional[Path] = None):
        self.config_directory = Path(config_directory or default_config_directory())
        self._file = self.config_directory / PROJECT_LIST_FILENAME
        self._entries: List[ProjectEntry] = []
        self._load()
        # Only when the caller did not name a directory: an explicit path means
        # "use this list", not "go looking for another one".
        if config_directory is None:
            self._adopt_legacy_list()

    def _adopt_legacy_list(self) -> None:
        """Move a list from the old XDG location into ``$HOME/.relion_web``.

        Copies rather than moves: if the new location is unwritable, the old file
        is still there and the user has lost nothing. The old file is left in
        place too, because deleting user data on startup is not this code's call.
        """
        if self._entries or self._file.exists():
            return
        legacy = legacy_config_directory()
        if legacy is None or legacy == self.config_directory:
            return
        try:
            raw = (legacy / PROJECT_LIST_FILENAME).read_text(encoding='utf-8')
            self.config_directory.mkdir(parents=True, exist_ok=True)
            self._file.write_text(raw, encoding='utf-8')
        except OSError:
            return
        self._load()

    # ------------------------------------------------------------- persistence
    def _load(self) -> None:
        try:
            raw = json.loads(self._file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            self._entries = []
            return
        entries: List[ProjectEntry] = []
        for item in raw.get('projects', []) if isinstance(raw, dict) else []:
            path = item.get('path')
            if not path:
                continue
            entries.append(
                ProjectEntry(
                    path=str(path),
                    added_at=float(item.get('added_at') or time.time()),
                    name=item.get('name'),
                )
            )
        self._entries = entries

    def _save(self) -> None:
        self.config_directory.mkdir(parents=True, exist_ok=True)
        payload = {
            'projects': [
                {'path': e.path, 'added_at': e.added_at, 'name': e.name}
                for e in self._entries
            ]
        }
        # Write-then-rename so a crash cannot leave a half-written list.
        temporary = self._file.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
        temporary.replace(self._file)

    # ----------------------------------------------------------------- access
    def list(self) -> List[ProjectEntry]:
        return list(self._entries)

    def get(self, path) -> Optional[ProjectEntry]:
        target = str(Path(path).expanduser().resolve(strict=False))
        for entry in self._entries:
            if entry.path == target:
                return entry
        return None

    def contains(self, path) -> bool:
        return self.get(path) is not None

    def add(self, path, name: Optional[str] = None) -> ProjectEntry:
        """Register a project directory, initialising it if it is not one yet.

        A directory with no ``default_pipeline.star`` but nothing else in it is
        a fresh project in waiting: RELION's own GUI does exactly this, writing
        an empty pipeline when it starts in a directory that has none
        (``gui_mainwindow.cpp``: ``if (exists(...)) read else write``). Doing the
        same here means "add this folder" works on a folder you just made.

        A directory that already has files but no pipeline is refused. Choosing
        between "adopt it anyway" and "the user picked the wrong folder" is not
        something the agent can answer, and writing a pipeline into somebody's
        data directory is not a recoverable mistake.
        """
        resolved = Path(path).expanduser().resolve(strict=False)
        if not resolved.is_dir():
            raise ValueError(f'not a directory: {resolved}')

        if not pipeline_module.pipeline_path(resolved).is_file():
            try:
                contents = list(resolved.iterdir())
            except OSError as exc:
                raise ValueError(f'cannot read {resolved}: {exc}') from exc
            if contents:
                raise ValueError(
                    f'{resolved} contains files but no '
                    f'{pipeline_module.PIPELINE_FILENAME}. Open it in RELION first, '
                    'or choose an empty directory to start a new project.'
                )
            try:
                pipeline_module.write_pipeline(
                    resolved, pipeline_module.Pipeline()
                )
            except OSError as exc:
                raise ValueError(f'could not create a pipeline: {exc}') from exc

        existing = self.get(resolved)
        if existing is not None:
            return existing

        entry = ProjectEntry(path=str(resolved), name=name)
        self._entries.append(entry)
        self._save()
        return entry

    def create(self, path, name: Optional[str] = None) -> dict:
        """Make a new project directory that RELION will accept.

        The only file a project needs is ``default_pipeline.star`` with an empty
        ``pipeline_general`` table; that is what ``PipeLine::write`` produces
        when the desktop GUI starts in an empty directory. The directory is
        created if it does not exist, but an existing non-empty directory is
        refused rather than overwritten.
        """
        resolved = Path(path).expanduser().resolve(strict=False)
        if resolved.exists():
            if not resolved.is_dir():
                raise ValueError(f'not a directory: {resolved}')
            if any(resolved.iterdir()):
                raise ValueError(
                    f'{resolved} is not empty; create the project elsewhere'
                )
        else:
            try:
                resolved.mkdir(parents=True)
            except OSError as exc:
                raise ValueError(f'could not create {resolved}: {exc}') from exc

        try:
            pipeline_module.write_pipeline(resolved, pipeline_module.Pipeline())
        except OSError as exc:
            raise ValueError(f'could not write the pipeline: {exc}') from exc

        return {
            'path': str(resolved),
            'name': name or resolved.name,
            'pipeline_file': str(pipeline_module.pipeline_path(resolved)),
        }

    def remove(self, path) -> bool:
        entry = self.get(path)
        if entry is None:
            return False
        self._entries.remove(entry)
        self._save()
        return True

    # ---------------------------------------------------------------- summary
    def summarize(self) -> List[dict]:
        """Entries with a readable flag, job count and status breakdown.

        The breakdown and the recent jobs are what a project card needs: a card
        that only said "17 jobs" would not tell you whether anything is running
        or broken. Reading the pipeline is the expensive part, so it is done
        once per entry and both facts come from the same read.
        """
        summary: List[dict] = []
        for entry in self._entries:
            readable = Path(entry.path).is_dir()
            job_count = None
            payload = entry.as_dict(readable=readable)
            if readable:
                try:
                    pipeline = pipeline_module.read_pipeline(entry.path)
                except (OSError, ValueError):
                    payload = entry.as_dict(readable=False)
                else:
                    job_count = len(pipeline.processes)
                    payload = entry.as_dict(readable=True, job_count=job_count)
                    payload['statuses'] = _status_counts(pipeline)
                    payload['recent'] = _recent_processes(pipeline, limit=3)
                    payload['types'] = len(
                        {process.type_label for process in pipeline.processes}
                    )
            summary.append(payload)
        return summary


def _status_counts(pipeline) -> dict:
    """How many jobs are in each pipeline status."""
    counts: dict = {}
    for process in pipeline.processes:
        counts[process.status] = counts.get(process.status, 0) + 1
    return counts


def _recent_processes(pipeline, limit: int = 3) -> List[dict]:
    """The highest-numbered jobs, which are the ones last worked on."""
    numbered = [
        (pipeline.job_number_of(process.name), process)
        for process in pipeline.processes
    ]
    numbered.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {
            'name': process.name,
            'alias': process.alias,
            'type_label': process.type_label,
            'status': process.status,
            'job_number': number,
        }
        for number, process in numbered[:limit]
    ]
