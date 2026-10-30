"""Path resolution restricted to a set of allowed roots.

Every path that arrives from a client is resolved and checked against the
configured roots. This keeps a browser client from reading arbitrary files on
the machine hosting the server.

The roots can be given as a callable instead of a fixed sequence. That matters
because the project list *is* the boundary, and it changes while the agent
runs: adding a project must make it readable immediately, without a restart.
"""

from pathlib import Path
from typing import Callable, Iterable, List, Sequence, Union

from fastapi import HTTPException

RootSource = Union[Iterable[Path], Callable[[], Iterable[Path]]]


def _normalise(roots: Iterable[Path]) -> tuple:
    resolved: List[Path] = []
    for root in roots:
        try:
            resolved.append(Path(root).expanduser().resolve(strict=False))
        except OSError:
            continue
    return tuple(resolved)


class PathGuard:
    """Resolve client-supplied paths, refusing anything outside allowed roots."""

    def __init__(self, roots: RootSource):
        # A callable is read on every check, so a project added at runtime is
        # usable at once. A fixed sequence is a snapshot and is kept as-is.
        self._source: Union[tuple, Callable[[], Iterable[Path]]] = (
            roots if callable(roots) else _normalise(roots)
        )

    @property
    def roots(self) -> Sequence[Path]:
        """The currently allowed roots."""
        if callable(self._source):
            return _normalise(self._source())
        return self._source

    def resolve(self, path) -> Path:
        """Resolve ``path`` and return it if it falls under an allowed root."""
        roots = self.roots
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            if not roots:
                raise HTTPException(400, detail='no root configured for relative paths')
            candidate = roots[0] / candidate
        try:
            resolved = candidate.resolve(strict=False)
        except OSError as exc:
            raise HTTPException(400, detail=f'could not resolve path: {exc}') from exc

        for root in roots:
            if resolved == root or root in resolved.parents:
                return resolved
        raise HTTPException(
            403,
            detail=(
                f'path {str(path)!r} is outside the allowed roots: '
                + ', '.join(str(r) for r in roots)
            ),
        )

    def resolve_existing(self, path, *, kind: str = 'file') -> Path:
        """Resolve ``path`` and require that it exists (and is of ``kind``)."""
        resolved = self.resolve(path)
        if kind == 'file' and not resolved.is_file():
            raise HTTPException(404, detail=f'file not found: {resolved}')
        if kind == 'directory' and not resolved.is_dir():
            raise HTTPException(404, detail=f'directory not found: {resolved}')
        return resolved

    def make_directory(self, path) -> Path:
        """Resolve ``path`` and create it if needed. Parent must be allowed."""
        resolved = self.resolve(path)
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved
