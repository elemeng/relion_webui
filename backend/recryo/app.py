"""FastAPI application exposing RELION cryo-ET data and jobs over HTTP."""

import asyncio
import json
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import api_assistant
from . import assistant as assistant_module
from . import browse, job_actions, job_artifacts
from . import hosts as hosts_module
from . import node_metrics
from . import cli_options
from . import mcp_server
from . import queue_system
from . import dataset, mrc, png
from . import job_commands, job_specs
from . import pipeline as pipeline_module
from .auth import AuthError, AuthManager
from .jobs import (
    DEFAULT_POLL_INTERVAL,
    JobManager,
    SELECTABLE_JOBS,
    SUPPORTED_JOBS,
)
from .paths import PathGuard
from .projects import ProjectRegistry
from .selection import write_selected_tilt_series

STATIC_DIRECTORY = Path(__file__).parent / 'static'
MAX_RENDER_PIXELS = 4096
SESSION_COOKIE = 'relion_web_session'


def _action_response(result: job_actions.ActionResult) -> dict:
    """Serialise a job action result, mapping its error to a 400."""
    return {
        'action': result.action,
        'process_name': result.process_name,
        'message': result.message,
        'detail': result.detail or {},
    }


def _schedule_result(payload: dict) -> job_actions.ActionResult:
    """Wrap a plain dict from a delegated action so it serialises the same way."""
    return job_actions.ActionResult(
        action=str(payload.get('action', 'ok')),
        process_name=None,
        message=payload.get('message', 'ok'),
        detail=payload,
    )


def _write_pick_star(tomogram, points: List[dict], output_dir) -> Path:
    """Write picked points as ``rlnCoordinateX/Y/Z`` in tomogram pixels.

    The viewer reports fractional coordinates, which are converted here so the
    file holds real pixel positions rather than a UI-space number.
    """
    header = mrc.read_header(tomogram)
    nx, ny, nz = header.nx, header.ny, header.nz
    rows = []
    for point in points:
        try:
            fx = float(point['x'])
            fy = float(point['y'])
            fz = float(point['z'])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f'point is not a coordinate: {point!r}') from exc
        rows.append(
            (
                max(0.0, min(1.0, fx)) * (nx - 1),
                max(0.0, min(1.0, fy)) * (ny - 1),
                max(0.0, min(1.0, fz)) * (nz - 1),
            )
        )
    path = output_dir / f'{Path(tomogram).stem}_picks.star'
    lines = [
        '',
        '# version 50001',
        '',
        'data_points',
        '',
        'loop_',
        '_rlnCoordinateX #1',
        '_rlnCoordinateY #2',
        '_rlnCoordinateZ #3',
    ]
    lines.extend(f'{x:.3f} {y:.3f} {z:.3f}' for x, y, z in rows)
    lines.append('')
    path.write_text('\n'.join(lines), encoding='utf-8')
    return path


def _job_title(job_type: str) -> str:
    """The title to show for a job type.

    A variant gets its own title ("Select (split into subsets)") so the picker
    can list it as a distinct job; the desktop GUI has only one such window, and
    ``form_schema`` reports its base job so the grouping stays visible.
    """
    try:
        return job_commands.form_schema(job_type)['title']
    except KeyError:
        return job_type


# --------------------------------------------------------------------- models
class SliceRequest(BaseModel):
    star_file: Optional[str] = None


class SelectionBody(BaseModel):
    star_file: str
    selections: Dict[str, List[int]] = Field(
        ..., description='tilt-series name -> selected image indices'
    )
    output_directory: str


class ValidateRequest(BaseModel):
    job_type: str
    arguments: Dict[str, object] = {}
    project_directory: Optional[str] = None


class JobRequest(BaseModel):
    job_type: str
    output_directory: str
    arguments: Dict[str, object] = Field(default_factory=dict)
    dry_run: bool = False
    # When set, the job is registered in this RELION project so the official
    # GUI sees it in the pipeline.
    project_directory: Optional[str] = None
    # Which machine runs it: omitted or 'local' means this one. A named node
    # needs key-based ssh from the agent account.
    host: Optional[str] = None


class HostRequest(BaseModel):
    """A node definition: either a spec string or explicit fields."""

    spec: Optional[str] = None
    name: Optional[str] = None
    hostname: Optional[str] = None
    port: int = 22
    user: Optional[str] = None
    note: Optional[str] = None


class HostUpdate(BaseModel):
    enabled: Optional[bool] = None
    note: Optional[str] = None
    hostname: Optional[str] = None


class QueueRequest(BaseModel):
    """A job for the queue, rather than a submission.

    The difference from `JobRequest` is when and where: an entry may wait for
    dependencies or for a free GPU, and it names the devices it may use.
    """

    project_directory: str
    job_type: str
    arguments: Dict[str, object] = Field(default_factory=dict)
    host: Optional[str] = None
    # Device indices to lease exclusively. Empty means "let the queue choose".
    gpus: List[int] = Field(default_factory=list)
    # Cores to reserve; omitted means nr_mpi * nr_threads.
    cores: Optional[int] = None
    # GPU memory each card must have, in MB. Optional: RELION's options carry no
    # memory figure, so declaring one is how a job says "I need a big card".
    gpu_memory_mb: Optional[int] = None
    priority: int = 0
    # While this runs, nothing else starts on the same node.
    exclusive: bool = False
    # Other queue entries this one waits for.
    depends_on: List[str] = Field(default_factory=list)
    # Process names that must reach Succeeded first.
    after_jobs: List[str] = Field(default_factory=list)
    # Paths that must exist first.
    after_files: List[str] = Field(default_factory=list)
    # Input slots: option variable -> "<entry id>:<output filename>". Resolved to
    # the producer's predicted path, which is what lets a chain be queued before
    # any of it has run.
    inputs: Dict[str, str] = Field(default_factory=dict)


class ProjectRequest(BaseModel):
    path: str
    name: Optional[str] = None


class CreateProjectRequest(BaseModel):
    path: str
    name: Optional[str] = None


class CreateFolderRequest(BaseModel):
    parent: str
    name: str


class AliasRequest(BaseModel):
    project_directory: str
    process_name: str
    alias: str


class NoteRequest(BaseModel):
    project_directory: str
    process_name: str
    note: str


class JobActionRequest(BaseModel):
    project_directory: str
    process_name: str
    recursive: bool = False


class UndeleteRequest(BaseModel):
    project_directory: str
    trash_entry: str


class PickPointsRequest(BaseModel):
    tomogram_path: str
    points: List[dict]
    output_directory: str


class ScheduleRequest(BaseModel):
    project_directory: str
    process_names: List[str]
    name: str = 'web'
    repeat: int = 1
    wait_minutes: int = 0
    wait_before_minutes: int = 0
    wait_after_seconds: int = 10


class ScheduleStopRequest(BaseModel):
    project_directory: str
    name: str = 'web'


class ExportRequest(BaseModel):
    project_directory: str
    process_names: List[str]
    name: str


class ImportRequest(BaseModel):
    project_directory: str
    listing: str


class JobMarkRequest(BaseModel):
    project_directory: str
    process_name: str
    failed: bool = False


class JobCleanRequest(BaseModel):
    project_directory: str
    process_name: str
    harsh: bool = False


class ProcessUpdate(BaseModel):
    """A partial edit to one process record.

    Every field is optional: only what a caller sends is changed, which is what
    makes `PATCH` the right verb for this. `None` means "leave it", so an alias
    can be cleared by sending `""` rather than by omission.
    """

    alias: Optional[str] = None
    note: Optional[str] = None
    failed: Optional[bool] = None


# ------------------------------------------------------------------ helpers
def _guard_from_request(request: Request) -> PathGuard:
    return request.app.state.guard


def _jobs_from_request(request: Request) -> JobManager:
    return request.app.state.jobs


def _queue_from_request(request: Request) -> queue_system.JobQueue:
    return request.app.state.queue


def _png_response(data: bytes, max_age: int = 60) -> Response:
    return Response(
        content=data,
        media_type='image/png',
        headers={'Cache-Control': f'private, max-age={max_age}'},
    )


def _clamp_size(width: int, height: int) -> tuple:
    width = max(16, min(int(width), MAX_RENDER_PIXELS))
    height = max(16, min(int(height), MAX_RENDER_PIXELS))
    return width, height


def _resolve_value_range(low, high) -> Optional[tuple]:
    if low is None or high is None:
        return None
    if not (np.isfinite(low) and np.isfinite(high)) or high <= low:
        return None
    return float(low), float(high)


def _plane_from_volume(volume: np.ndarray, axis: str, index: int) -> np.ndarray:
    """Extract a 2D plane from an (nz, ny, nx) volume."""
    if axis == 'z':
        index = int(np.clip(index, 0, volume.shape[0] - 1))
        return volume[index]
    if axis == 'y':
        index = int(np.clip(index, 0, volume.shape[1] - 1))
        return volume[:, index, :]
    if axis == 'x':
        index = int(np.clip(index, 0, volume.shape[2] - 1))
        return volume[:, :, index]
    raise HTTPException(400, detail=f'axis must be one of z, y, x; got {axis!r}')


def _find_tilt_series(star_file: Path, name: str) -> dataset.TiltSeriesInfo:
    for info in dataset.list_tilt_series(star_file):
        if info.name == name:
            return info
    raise HTTPException(404, detail=f'tilt-series {name!r} not found in {star_file}')


def _write_plot_png(values: List[Optional[float]], width: int, height: int) -> bytes:
    """Render a simple line/points plot as a PNG, without pulling in a plot lib."""
    width, height = _clamp_size(width, height)
    canvas = np.zeros((height, width), dtype=np.uint8)
    canvas[:] = 12

    valid = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(valid) < 2:
        return png.encode_gray8(canvas)

    xs = np.array([x for x, _ in valid], dtype=np.float64)
    ys = np.array([y for _, y in valid], dtype=np.float64)
    x0, x1 = xs.min(), xs.max()
    y0, y1 = ys.min(), ys.max()
    if x1 <= x0:
        x1 = x0 + 1.0
    if y1 <= y0:
        y1 = y0 + 1.0

    px = ((xs - x0) / (x1 - x0) * (width - 1)).astype(int)
    py = ((1.0 - (ys - y0) / (y1 - y0)) * (height - 1)).astype(int)
    canvas[np.clip(py, 0, height - 1), np.clip(px, 0, width - 1)] = 255

    # Connect consecutive samples so sparse series still read as a curve.
    for i in range(len(px) - 1):
        steps = int(max(abs(px[i + 1] - px[i]), abs(py[i + 1] - py[i])))
        steps = max(1, min(steps, 4 * max(width, height)))
        for t in np.linspace(0.0, 1.0, steps, endpoint=False):
            x = int(round(px[i] + (px[i + 1] - px[i]) * t))
            y = int(round(py[i] + (py[i + 1] - py[i]) * t))
            canvas[min(max(y, 0), height - 1), min(max(x, 0), width - 1)] = 220
    return png.encode_gray8(canvas)


# -------------------------------------------------------------------- factory
def create_app(
    roots: Optional[List[Path]] = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    *,
    auth: Optional[AuthManager] = None,
    projects: Optional[ProjectRegistry] = None,
    enable_assistant: bool = False,
    enable_mcp: bool = False,
    assistant_config: Optional['assistant_module.AssistantConfig'] = None,
    mcp_hosts: Optional[List[str]] = None,
    mcp_allow_writes: bool = True,
    host_nodes: Optional[List[str]] = None,
) -> FastAPI:
    """Build the FastAPI application.

    Parameters
    ----------
    roots
        Directories the server is allowed to read and write. Requests for paths
        outside them are rejected. Defaults to the current working directory.
    poll_interval
        Seconds between job-status polls pushed over the WebSocket.
    auth
        Session manager. When given, every page and API call requires a login;
        when omitted (tests, embedding) the app is open, which is only safe on
        loopback.
    projects
        Registry of project directories. Created from the user's config
        directory when omitted.
    enable_assistant
        Serve the LLM assistant's endpoints. Off by default: it sends project
        names, job options and log lines to a third-party API, which is a
        decision an operator makes rather than one that happens on upgrade.
    enable_mcp
        Mount the MCP server at /mcp. Off by default for the same reason, plus
        the narrower one that an MCP client acts without a human watching.
    assistant_config
        Provider and model to start from. When omitted, read from the
        environment.
    """
    roots = [Path(r) for r in (roots or [Path.cwd()])]
    # Fall back to an empty, throwaway registry rather than the user's real
    # config directory. Defaulting to the real one would make an embedded app -
    # and every test - read the developer's own project list, which then also
    # widens the allowed roots.
    if projects is None:
        projects = ProjectRegistry(config_directory=Path(tempfile.mkdtemp(prefix='relion-web-projects-')))

    def allowed_roots():
        """Configured roots plus every project currently in the list.

        Read on each check rather than snapshotted: adding a project has to make
        it usable at once. A snapshot meant a project could be added and then
        fail to open with "outside the allowed roots", which is exactly the
        confusion the project list is meant to remove.

        A project path counts only while it is in the list, so removing one
        withdraws access with it.
        """
        seen: List[Path] = []
        for candidate in [*roots, *(Path(entry.path) for entry in projects.list())]:
            try:
                resolved = candidate.expanduser().resolve(strict=False)
            except OSError:
                continue
            if resolved not in seen:
                seen.append(resolved)
        return seen

    guard = PathGuard(allowed_roots)
    # Execution hosts: `local` plus any --host-node declarations.
    # The registry persists next to the project list, so a node defined in the
    # UI survives a restart. CLI declarations are added on top and are also
    # written, so the two sources converge instead of fighting.
    host_registry = hosts_module.HostRegistry(
        config_directory=(projects.config_directory if projects else None)
    )
    for spec in host_nodes or []:
        try:
            host_registry.add(hosts_module.parse_host_spec(spec), persist=False)
        except hosts_module.HostError:
            # Already defined in the file; the file wins, which is what an
            # operator editing it by hand expects.
            continue
    manager = JobManager(guard, host_registry=host_registry)
    # The queue submits through the same manager, so a queued job is registered,
    # logged and abortable exactly like one submitted directly. State lives next
    # to hosts.json, so pending entries survive a restart.
    queue = queue_system.JobQueue(
        manager,
        guard,
        config_directory=(projects.config_directory if projects else None),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # The MCP transport keeps a session manager with its own task group.
        # Mounting a Starlette sub-app does not run that sub-app's lifespan, so
        # it has to be entered here or every MCP request waits on a task group
        # that was never created.
        session_manager = getattr(_app.state, 'mcp_session_manager', None)
        if session_manager is not None:
            async with session_manager.run():
                await queue.start()
                yield
                await queue.stop()
                await manager.shutdown()
            return
        await queue.start()
        try:
            yield
        finally:
            await queue.stop()
            await manager.shutdown()

    app = FastAPI(
        title='RELION cryo-ET web interface',
        version='0.1.0',
        lifespan=lifespan,
        description=(
            'Browse tilt-series and tomograms, render slices, and submit RELION '
            'tomo jobs. Job lifecycle follows the same directory and sentinel-file '
            'protocol as the RELION desktop GUI.'
        ),
    )
    app.state.guard = guard
    app.state.jobs = manager
    app.state.queue = queue
    app.state.poll_interval = float(poll_interval)
    app.state.auth = auth
    app.state.projects = projects

    def current_session(request: Request):
        """The logged-in session, or None when auth is disabled."""
        auth_manager: Optional[AuthManager] = request.app.state.auth
        if auth_manager is None:
            return None
        session = auth_manager.get_session(request.cookies.get(SESSION_COOKIE))
        if session is None:
            raise HTTPException(401, detail='login required')
        return session

    def session_from_websocket(websocket: WebSocket):
        auth_manager: Optional[AuthManager] = websocket.app.state.auth
        if auth_manager is None:
            return None
        session = auth_manager.get_session(websocket.cookies.get(SESSION_COOKIE))
        if session is None:
            return None
        return session

    # Paths reachable without a session, because they *are* the login flow plus
    # the assets the login page needs. Everything else is gated by the middleware
    # below, which is one place rather than a decorator per route.
    PUBLIC_PATHS = {'/api/login', '/api/logout', '/api/health', '/api/session', '/'}

    @app.middleware('http')
    async def require_login(request: Request, call_next):
        auth_manager: Optional[AuthManager] = request.app.state.auth
        if auth_manager is None:
            return await call_next(request)

        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith('/static/'):
            return await call_next(request)
        # The MCP endpoint is not under /api/, so it needs its own gate. An MCP
        # client acts on the pipeline without a person watching, so leaving it
        # open because of a path prefix would be the one hole that matters.
        if path == mcp_server.MCP_PATH or path.startswith(mcp_server.MCP_PATH + '/'):
            if auth_manager.get_session(request.cookies.get(SESSION_COOKIE)) is None:
                return JSONResponse(
                    status_code=401,
                    content={'error': 'login required', 'status': 401},
                )
            return await call_next(request)
        if path.startswith('/api/') and path not in PUBLIC_PATHS:
            if auth_manager.get_session(request.cookies.get(SESSION_COOKIE)) is None:
                return JSONResponse(
                    status_code=401,
                    content={'error': 'login required', 'status': 401},
                )
        return await call_next(request)

    if STATIC_DIRECTORY.is_dir():
        app.mount('/static', StaticFiles(directory=str(STATIC_DIRECTORY)), name='static')
        # The Vite build writes its bundle under static/assets but refers to it
        # with absolute /assets/... URLs, so that prefix needs its own mount.
        assets_directory = STATIC_DIRECTORY / 'assets'
        if assets_directory.is_dir():
            app.mount(
                '/assets',
                StaticFiles(directory=str(assets_directory)),
                name='assets',
            )

    # ------------------------------------------------------------------- auth
    @app.post('/api/login')
    async def login(request: Request) -> JSONResponse:
        """Validate the submitted credentials against the host's sshd.

        The password is forwarded once and never stored; see auth.py for why the
        check goes through SSH rather than a user database here.
        """
        auth_manager: Optional[AuthManager] = app.state.auth
        if auth_manager is None:
            raise HTTPException(400, detail='this server has no login configured')

        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, detail='expected a JSON body')
        username = str(body.get('username') or '')
        password = str(body.get('password') or '')
        remote = request.client.host if request.client else ''

        try:
            session = auth_manager.login(username, password, remote=remote)
        except AuthError as exc:
            raise HTTPException(401, detail=str(exc)) from exc

        response = JSONResponse({'username': session.username, **session.as_dict()})
        response.set_cookie(
            SESSION_COOKIE,
            session.token,
            httponly=True,
            samesite='strict',
            max_age=int(auth_manager.session_lifetime),
            # Secure is not set: the agent is often reached over plain HTTP on a
            # trusted LAN. Put it behind TLS if that is not acceptable.
        )
        return response

    @app.post('/api/logout')
    async def logout(request: Request) -> JSONResponse:
        auth_manager: Optional[AuthManager] = app.state.auth
        token = request.cookies.get(SESSION_COOKIE)
        if auth_manager is not None and token:
            auth_manager.logout(token)
        response = JSONResponse({'logged_out': True})
        response.delete_cookie(SESSION_COOKIE)
        return response

    @app.get('/api/session')
    async def session_info(request: Request) -> dict:
        auth_manager: Optional[AuthManager] = app.state.auth
        if auth_manager is None:
            return {'auth_required': False, 'username': None}
        session = auth_manager.get_session(request.cookies.get(SESSION_COOKIE))
        if session is None:
            raise HTTPException(401, detail='login required')
        return {'auth_required': True, **session.as_dict()}

    # ------------------------------------------------------------------ meta
    @app.get('/api/health')
    async def health() -> dict:
        # Unauthenticated on purpose: monitors should not need credentials.
        return {'status': 'ok', 'time': time.time()}

    @app.get('/api/config')
    async def config(request: Request) -> dict:
        current_session(request)
        return {
            'roots': [str(root) for root in guard.roots],
            'job_types': sorted(SELECTABLE_JOBS),
            'poll_interval': app.state.poll_interval,
            'auth_required': app.state.auth is not None,
            # Set by the CLI so the page can open something on first load.
            'initial_project': getattr(app.state, 'initial_project', None),
            'initial_star_file': getattr(app.state, 'initial_star_file', None),
            # Whether the panel should exist at all. Off by default, so the
            # browser must be told rather than assuming.
            'assistant_enabled': bool(getattr(app.state, 'assistant_enabled', False)),
            'mcp_enabled': bool(getattr(app.state, 'mcp_enabled', False)),
        }

    @app.get('/api/project/input-candidates')
    async def input_candidates(
        request: Request,
        job_type: str,
        variable: str,
        project_directory: str,
    ) -> dict:
        """The files a node-typed option may be filled with.

        RELION's own answer, not an approximation: the desktop GUI's picker for a
        node option lists ``.Nodes/<first type component>/``
        (``GuiEntry::cb_browse_node_i``, gui_entries.cpp:347), so this lists the
        same directory. That is what makes a queued job's output appear here - a
        reserved job touches its ``.Nodes`` entry before it runs, exactly as
        RELION does for a scheduled job.

        Each candidate carries the job that produces it and whether the file
        exists yet, so the UI can distinguish "ready to use" from "will appear
        when job007 finishes". The second kind is what an input slot is for.
        """
        current_session(request)
        project = guard.resolve_existing(project_directory, kind='directory')
        try:
            schema = job_commands.form_schema(job_type)
        except KeyError as exc:
            raise HTTPException(400, detail=f'unknown job type {job_type!r}') from exc

        field = next(
            (item for item in schema.get('fields', []) if item['variable'] == variable),
            None,
        )
        if field is None:
            raise HTTPException(
                400, detail=f'{job_type} has no option {variable!r}'
            )
        if field.get('kind') != 'input_node':
            raise HTTPException(
                400,
                detail=(
                    f'{variable!r} is a {field.get("kind")!r} option, not a pipeline '
                    'node; browse a path instead'
                ),
            )

        node_type = str(field.get('node_type') or '')
        depth = int(field.get('node_type_depth') or 1)
        names = job_artifacts.node_candidates(project, node_type, depth)

        # Producer and readiness come from the pipeline: it is the record of
        # which process makes which node, and a node whose job is Scheduled is
        # precisely a file that does not exist yet.
        try:
            graph = pipeline_module.read_pipeline(project)
        except (FileNotFoundError, ValueError):
            graph = pipeline_module.Pipeline()
        producers = {node: process for process, node in graph.output_edges}

        # A node whose producer is still queued is offered as a *slot*, which
        # the queue spells `entry:filename` - so the entry id has to come along.
        # Without it the picker could offer the file but not the wait.
        queued = {
            item['process_name']: item['id']
            for item in queue.snapshot()['entries']
            if item['state'] in ('pending', 'running') and item['process_name']
        }

        candidates = []
        for name in names:
            producer = producers.get(name)
            process = graph.find_process(producer) if producer else None
            status = process.status if process is not None else None
            exists = (project / name).exists()
            filename = name.rsplit('/', 1)[-1]
            entry_id = queued.get(producer) if producer else None
            candidates.append(
                {
                    'node': name,
                    'producer': producer,
                    'producer_status': status,
                    'exists': exists,
                    # The distinction the picker needs: usable now, or waiting
                    # for a job that is already queued.
                    'pending': not exists,
                    # Present only when the producer is a live queue entry, which
                    # is what lets the value be expressed as a slot reference.
                    'queue_entry': entry_id,
                    'slot': f'{entry_id}:{filename}' if entry_id else None,
                }
            )
        return {
            'job_type': job_type,
            'variable': variable,
            'node_type': node_type,
            'candidates': candidates,
        }

    @app.get('/api/job-types')
    async def job_types() -> dict:
        """The job types the UI can offer, in the desktop GUI's order.

        ``base`` groups a variant under the job it is a branch of, so the picker
        can show "Subset selection" once with its modes beneath it instead of
        pretending they are unrelated jobs.
        """
        return {
            'job_types': [
                {
                    'id': job_id,
                    'executable': executable,
                    'title': _job_title(job_id),
                    'base': job_commands.base_job_type(job_id),
                    'branch': (
                        job_commands.COMMAND_SPECS[job_id].branch
                        if job_id in job_commands.COMMAND_SPECS
                        else None
                    ),
                }
                for job_id, executable in sorted(SELECTABLE_JOBS.items())
            ],
            'order': list(job_commands.selectable_job_types()),
        }

    @app.get('/api/job-schema')
    async def job_schema(job_type: Optional[str] = None) -> dict:
        """Form description for one job, or all of them.

        This is what lets the UI render the real option set (labels, defaults,
        ranges, help text, radio choices) instead of a free-form JSON box. The
        layout comes from the desktop job window, so the tabs match what a RELION
        user already knows.
        """
        if job_type is None:
            return {
                'jobs': job_commands.all_form_schemas(),
                # The picker groups by workflow stage; RELION's own list is flat.
                'categories': job_commands.job_categories(),
            }
        try:
            return job_commands.form_schema(job_type)
        except KeyError as exc:
            raise HTTPException(404, detail=str(exc)) from exc

    @app.post('/api/job-validate')
    async def job_validate(body: ValidateRequest) -> dict:
        """Check a job's options without submitting.

        The form calls this so a contradictory combination is reported while the
        user is still editing, instead of failing in the job's log after the
        directory and pipeline entry exist.

        Advanced arguments are checked here too: a flag the program does not
        declare is only a warning at run time (``checkForUnknownArguments``), so
        a typo would otherwise look like it took effect.
        """
        try:
            problems = job_commands.validate(body.job_type, body.arguments)
            # The filename checks apply regardless of a project; only the
            # existence check needs a base directory to resolve relative paths.
            base = None
            if body.project_directory:
                base = guard.resolve_existing(
                    body.project_directory, kind='directory'
                )
            problems += job_commands.validate_inputs(
                body.job_type, body.arguments, base=base
            )
            try:
                _merged, unknown = cli_options.apply(body.job_type, body.arguments)
            except ValueError as exc:
                problems.append(str(exc))
                unknown = []
            warnings = [
                f'{flag} is not an option {body.job_type} reads; it will be '
                f'passed on and ignored'
                for flag in unknown
            ]
        except KeyError as exc:
            raise HTTPException(404, detail=str(exc)) from exc
        return {
            'job_type': body.job_type,
            'problems': problems,
            'warnings': warnings,
            'ok': not problems,
        }

    # ----------------------------------------------------------------- data
    @app.get('/api/datasets')
    async def list_datasets(
        star_file: str = Query(..., description='global tilt-series STAR file'),
        guard: PathGuard = Depends(_guard_from_request),
    ) -> dict:
        star_path = guard.resolve_existing(star_file)
        try:
            infos = dataset.list_tilt_series(star_path)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        return {
            'star_file': str(star_path),
            'n_tilt_series': len(infos),
            'tilt_series': [info.as_dict() for info in infos],
        }

    @app.get('/api/tilt-series')
    async def tilt_series_detail(
        star_file: str,
        name: str,
        guard: PathGuard = Depends(_guard_from_request),
    ) -> dict:
        star_path = guard.resolve_existing(star_file)
        info = _find_tilt_series(star_path, name)
        if info.star_file is None or not info.star_file.is_file():
            raise HTTPException(
                404, detail=f'tilt-series STAR file missing for {name!r}'
            )
        try:
            records = dataset.tilt_image_records(info.star_file)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        payload = info.as_dict()
        payload['images'] = [record.as_dict() for record in records]
        return payload

    @app.get('/api/tilt-series/defocus-plot')
    async def defocus_plot(
        star_file: str,
        name: str,
        width: int = Query(480, ge=64, le=2048),
        height: int = Query(160, ge=64, le=2048),
        guard: PathGuard = Depends(_guard_from_request),
    ) -> Response:
        star_path = guard.resolve_existing(star_file)
        info = _find_tilt_series(star_path, name)
        if info.star_file is None:
            raise HTTPException(404, detail=f'tilt-series STAR file missing for {name!r}')
        records = dataset.tilt_image_records(info.star_file)
        return _png_response(
            _write_plot_png([record.defocus for record in records], width, height)
        )

    # --------------------------------------------------------------- renders
    @app.get('/api/render/tilt-image')
    async def render_tilt_image(
        star_file: str,
        name: str,
        index: int,
        width: int = Query(512, ge=16, le=MAX_RENDER_PIXELS),
        height: int = Query(512, ge=16, le=MAX_RENDER_PIXELS),
        low: Optional[float] = None,
        high: Optional[float] = None,
        guard: PathGuard = Depends(_guard_from_request),
    ) -> Response:
        star_path = guard.resolve_existing(star_file)
        info = _find_tilt_series(star_path, name)
        if info.star_file is None:
            raise HTTPException(404, detail=f'tilt-series STAR file missing for {name!r}')
        records = dataset.tilt_image_records(info.star_file)
        if not 0 <= index < len(records):
            raise HTTPException(404, detail=f'image index {index} out of range')
        filename = records[index].filename
        if filename is None or not filename.is_file():
            raise HTTPException(404, detail='tilt image file not found on disk')

        width, height = _clamp_size(width, height)
        try:
            image = mrc.read_tilt_image(filename)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        preview = mrc.downsample_to(image, width, height)
        return _png_response(
            png.encode_gray8(
                mrc.to_uint8(preview, value_range=_resolve_value_range(low, high))
            )
        )

    @app.get('/api/volume')
    async def volume_info(
        path: str,
        guard: PathGuard = Depends(_guard_from_request),
    ) -> dict:
        resolved = guard.resolve_existing(path)
        try:
            header = mrc.read_header(resolved)
            low, high = mrc.sample_stats(resolved)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        return {
            'path': str(resolved),
            'shape': list(header.shape),
            'nx': header.nx,
            'ny': header.ny,
            'nz': header.nz,
            'mode': header.mode,
            'dtype': str(header.dtype),
            'voxel_size': list(header.voxel_size),
            'suggested_range': [low, high],
        }

    @app.get('/api/render/slice')
    async def render_slice(
        path: str,
        axis: str = Query('z', pattern='^[zyx]$'),
        index: int = 0,
        width: int = Query(512, ge=16, le=MAX_RENDER_PIXELS),
        height: int = Query(512, ge=16, le=MAX_RENDER_PIXELS),
        low: Optional[float] = None,
        high: Optional[float] = None,
        guard: PathGuard = Depends(_guard_from_request),
    ) -> Response:
        resolved = guard.resolve_existing(path)
        width, height = _clamp_size(width, height)
        try:
            _, volume = mrc.open_volume(resolved)
            plane = _plane_from_volume(volume, axis, index)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        preview = mrc.downsample_to(np.asarray(plane), width, height)
        return _png_response(
            png.encode_gray8(
                mrc.to_uint8(preview, value_range=_resolve_value_range(low, high))
            )
        )

    @app.get('/api/render/volume')
    async def render_volume(
        path: str,
        yaw: float = Query(0.0, ge=-180.0, le=180.0),
        pitch: float = Query(0.0, ge=-90.0, le=90.0),
        mode: str = Query('mip', pattern='^(mip|mean)$'),
        width: int = Query(512, ge=16, le=MAX_RENDER_PIXELS),
        height: int = Query(512, ge=16, le=MAX_RENDER_PIXELS),
        low: Optional[float] = None,
        high: Optional[float] = None,
        guard: PathGuard = Depends(_guard_from_request),
    ) -> Response:
        """A projection from an arbitrary viewing direction.

        This is the server side of the 3D view: the browser asks for the volume
        from a yaw/pitch angle and receives a PNG, so a multi-gigabyte tomogram
        is never downloaded. ``mode=mip`` (maximum intensity) is the default
        because it keeps membranes and particles visible; ``mode=mean`` is the
        smoother alternative.
        """
        resolved = guard.resolve_existing(path)
        width, height = _clamp_size(width, height)
        value_range = None
        if low is not None and high is not None:
            value_range = (float(low), float(high))
        try:
            projected = mrc.render_volume_projection(
                resolved,
                yaw_degrees=yaw,
                pitch_degrees=pitch,
                width=width,
                height=height,
                mode=mode,
                value_range=value_range,
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        return _png_response(png.encode_gray8(mrc.to_uint8(projected)))

    @app.get('/api/render/projection')
    async def render_projection(
        path: str,
        axis: str = Query('z', pattern='^[zyx]$'),
        width: int = Query(512, ge=16, le=MAX_RENDER_PIXELS),
        height: int = Query(512, ge=16, le=MAX_RENDER_PIXELS),
        low: Optional[float] = None,
        high: Optional[float] = None,
        guard: PathGuard = Depends(_guard_from_request),
    ) -> Response:
        """Mean projection along ``axis``, computed in z-slabs to bound memory."""
        resolved = guard.resolve_existing(path)
        width, height = _clamp_size(width, height)
        try:
            header = mrc.read_header(resolved)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, detail=str(exc)) from exc

        if axis != 'z':
            raise HTTPException(
                400, detail='projections are currently limited to axis=z'
            )
        accumulator = np.zeros((header.ny, header.nx), dtype=np.float64)
        count = 0
        try:
            for _, slab in mrc.iter_z_slabs(resolved, slab=16):
                accumulator += np.asarray(slab, dtype=np.float64).sum(axis=0)
                count += slab.shape[0]
        except (ValueError, OSError) as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        projection = accumulator / max(count, 1)
        preview = mrc.downsample_to(projection, width, height)
        return _png_response(
            png.encode_gray8(
                mrc.to_uint8(preview, value_range=_resolve_value_range(low, high))
            )
        )

    # ------------------------------------------------------------- selection
    @app.post('/api/tilt-series/selection')
    async def save_selection(
        body: SelectionBody,
        guard: PathGuard = Depends(_guard_from_request),
    ) -> dict:
        star_path = guard.resolve_existing(body.star_file)
        output_directory = guard.make_directory(body.output_directory)
        output_star = output_directory / 'selected_tilt_series.star'
        try:
            write_selected_tilt_series(
                star_path, body.selections, output_star
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        return {'star_file': str(output_star)}


    # --------------------------------------------------------------- project
    # ----------------------------------------------------------------- browse
    @app.get('/api/browse')
    async def browse_directory(
        request: Request,
        path: Optional[str] = None,
        filter: str = Query('all', description='all, directory, project or star'),
        hidden: bool = False,
    ) -> dict:
        """List a directory for the path pickers.

        Deliberately not limited to the allowed roots: the picker exists to
        choose a project directory, which is not a root until it is chosen.
        See browse.py for the reasoning; mutations stay root-restricted.
        """
        current_session(request)
        try:
            listing = browse.list_directory(path, filter_kind=filter, show_hidden=hidden)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
        return listing.as_dict()

    @app.post('/api/browse/folder', status_code=201)
    async def browse_make_folder(request: Request, body: CreateFolderRequest) -> dict:
        """Create one empty folder in the directory the picker is showing.

        The picker is where a user decides where a project goes, so it is where
        "there is no suitable folder yet" has to be answerable. Narrow by
        design: one level, no path separators, nothing overwritten.
        """
        current_session(request)
        try:
            return browse.make_directory(body.parent, body.name)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc

    @app.get('/api/browse/stat')
    async def browse_stat(request: Request, path: str) -> dict:
        """Describe one path so the picker can validate a typed value."""
        current_session(request)
        try:
            return browse.stat_path(path)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc

    @app.get('/api/browse/locations')
    async def browse_locations(request: Request) -> dict:
        """Starting points offered by the picker."""
        current_session(request)
        return {'locations': browse.quick_locations()}

    # ----------------------------------------------------------------- project
    @app.get('/api/project')
    async def project_summary(
        project_directory: str,
        guard: PathGuard = Depends(_guard_from_request),
    ) -> dict:
        """Summarise a RELION project: job counter, jobs and their statuses.

        This is what makes an existing project usable from the browser: the
        official GUI's view of the project is reproduced from
        ``default_pipeline.star`` rather than guessed from the filesystem.
        """
        project = guard.resolve_existing(project_directory, kind='directory')
        try:
            pipeline = pipeline_module.read_pipeline(project)
        except FileNotFoundError as exc:
            raise HTTPException(
                404, detail=f'{project} is not a RELION project (no pipeline file)'
            ) from exc
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from exc

        jobs = []
        for process in pipeline.processes:
            jobs.append(
                {
                    'name': process.name,
                    'alias': process.alias,
                    'type_label': process.type_label,
                    'status': process.status,
                    'directory': process.name,
                    'job_number': pipeline.job_number_of(process.name),
                    'outputs': [
                        target
                        for source, target in pipeline.output_edges
                        if source == process.name
                    ],
                }
            )
        return {
            'project_directory': str(project),
            'pipeline_file': str(pipeline_module.pipeline_path(project)),
            'job_counter': pipeline.job_counter,
            'n_jobs': len(jobs),
            'jobs': jobs,
            'nodes': [
                {'name': node.name, 'type_label': node.type_label}
                for node in pipeline.nodes
            ],
        }

    @app.get('/api/project/job-types')
    async def project_job_types() -> dict:
        """Job types the web UI can submit into a project."""
        return {
            'job_types': [
                {
                    'id': spec.job_type,
                    'title': spec.tab_title,
                    'executable': spec.executable,
                    'process_type_label': spec.process_type_label,
                    'directory': spec.directory_name,
                    'inputs': spec.input_variable,
                    'outputs': [node.filename for node in spec.output_nodes],
                    # A variant's job.star records the *base* job's option
                    # table, so reading a finished job back needs the base
                    # type even when the picker no longer offers it.
                    'base_job_type': spec.base_job_type,
                }
                for spec in job_specs.JOB_SPECS.values()
            ]
        }

    @app.get('/api/project/graph')
    async def project_graph(
        request: Request,
        project_directory: str,
        guard: PathGuard = Depends(_guard_from_request),
    ) -> dict:
        """The pipeline as nodes and edges, for drawing the job graph.

        The GUI's whole navigation model is this graph: a job consumes nodes and
        produces nodes, and a downstream job is started by pointing it at an
        upstream node. Returning it explicitly lets the UI show the flow and
        offer upstream outputs as inputs, instead of asking users to type a path
        they have to find themselves.
        """
        project = guard.resolve_existing(project_directory, kind='directory')
        try:
            pipeline = pipeline_module.read_pipeline(project)
        except FileNotFoundError as exc:
            raise HTTPException(404, detail=f'{project} has no pipeline') from exc

        produced_by = {}
        for process_name, node_name in pipeline.output_edges:
            produced_by[node_name] = process_name

        consumed_by: dict = {}
        for node_name, process_name in pipeline.input_edges:
            consumed_by.setdefault(node_name, []).append(process_name)

        # Which Scheduled rows belong to this app's queue rather than being
        # scheduled in RELION. The canvas shows them differently because only
        # the first will start on its own. Matched by process name, since that is
        # exactly what the queue reserved at enqueue.
        queued_here = {
            item['process_name']: item['id']
            for item in request.app.state.queue.snapshot()['entries']
            if item['process_name'] and item['state'] in ('pending', 'running')
        }

        return {
            'project_directory': str(project),
            'job_counter': pipeline.job_counter,
            'processes': [
                {
                    'name': process.name,
                    'alias': process.alias,
                    'type_label': process.type_label,
                    'status': process.status,
                    'job_number': pipeline.job_number_of(process.name),
                    'directory': pipeline.directory_of(process.name).rstrip('/'),
                    # Present only for a job this queue owns, so the UI can say
                    # "queued here" and point at the entry.
                    'queue_entry': queued_here.get(process.name),
                }
                for process in pipeline.processes
            ],
            'nodes': [
                {
                    'name': node.name,
                    'type_label': node.type_label,
                    'type_depth': node.type_depth,
                    'produced_by': produced_by.get(node.name),
                    'consumed_by': consumed_by.get(node.name, []),
                }
                for node in pipeline.nodes
            ],
            'edges': {
                'inputs': [
                    {'node': node, 'process': process}
                    for node, process in pipeline.input_edges
                ],
                'outputs': [
                    {'process': process, 'node': node}
                    for process, node in pipeline.output_edges
                ],
            },
        }

    # ---------------------------------------------------------- job actions
    @app.post('/api/project/job/alias', deprecated=True)
    async def job_alias(
        body: AliasRequest, guard: PathGuard = Depends(_guard_from_request)
    ) -> dict:
        """Deprecated: use ``PATCH /api/projects/processes/{name}``.

        Set a job's alias.

        An alias renames the node paths the GUI shows and is what downstream
        jobs refer to, so it is a pipeline change rather than a label.
        """
        project = guard.resolve_existing(body.project_directory, kind='directory')
        return _action_response(
            job_actions.set_alias(project, body.process_name, body.alias)
        )

    @app.get('/api/project/job/note')
    async def job_note_read(
        project_directory: str,
        process_name: str,
        guard: PathGuard = Depends(_guard_from_request),
    ) -> dict:
        project = guard.resolve_existing(project_directory, kind='directory')
        return _action_response(job_actions.read_note(project, process_name))

    @app.get('/api/project/job/options')
    async def job_options(
        project_directory: str,
        process_name: str,
        guard: PathGuard = Depends(_guard_from_request),
    ) -> dict:
        """The options a job recorded in its ``job.star``, and its ``run.out``.

        A finished job's parameters exist nowhere else - the in-memory job
        object is gone once the browser reloads - so the form that shows "what
        this job ran with" has to read them back from disk. Returning null is
        meaningful: a RELION 3.x job has only ``run.job``, and conflating that
        with "no options" would replace a real record with the schema defaults.

        ``log`` rides along because the dialog needs both when it opens on a
        finished job, and two requests into one job directory is one more than
        necessary. It is null when the job wrote no ``run.out``.
        """
        project = guard.resolve_existing(project_directory, kind='directory')
        # `process_name` comes from the browser, so it is validated against the
        # pipeline rather than joined onto the path: the root guard alone would
        # accept `../sibling` when both happen to live under the same root, and
        # a job directory must be named by the project that owns it.
        try:
            pipeline = pipeline_module.read_pipeline(project)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(404, detail=str(exc)) from exc
        _process = pipeline.find_process(process_name)
        if _process is None:
            raise HTTPException(
                404, detail=f'{process_name} is not in this project'
            )
        job_directory = guard.resolve_existing(
            str(project / process_name), kind='directory'
        )
        options = job_artifacts.read_job_star(job_directory)
        return {
            'process_name': process_name,
            'job_directory': str(job_directory),
            'type_label': job_artifacts.read_job_type_label(job_directory),
            'options': options,
            'log': job_artifacts.read_run_out(job_directory),
        }

    @app.put('/api/project/job/note')
    async def job_note_write(
        body: NoteRequest, guard: PathGuard = Depends(_guard_from_request)
    ) -> dict:
        """Write the same ``<job>note.txt`` the desktop note editor shows."""
        project = guard.resolve_existing(body.project_directory, kind='directory')
        return _action_response(
            job_actions.write_note(project, body.process_name, body.note)
        )

    # ------------------------------------------------- processes (REST)
    #
    # The job routes below were RPC-shaped: `POST /api/project/job/delete` for a
    # deletion, `POST .../job/alias` for a rename. That shape made every new
    # action a new path, and it disagreed with `/api/jobs/{id}`, which is a
    # proper resource.
    #
    # These are the canonical routes. The old paths remain as thin deprecated
    # aliases further down, so an open browser tab keeps working across the
    # change.
    #
    # `process_name` contains a slash (`Tomograms/job004`), so it is taken from
    # the path with `:path` and the project directory is the query parameter -
    # the mirror image of how `/api/jobs/{id}` handles its `forget` flag.

    @app.delete('/api/projects/processes/{process_name:path}')
    async def delete_process(
        process_name: str,
        project_directory: str,
        recursive: bool = False,
        guard: PathGuard = Depends(_guard_from_request),
    ) -> dict:
        """Remove a process from the pipeline, moving its directory to Trash.

        Mirrors the GUI's Delete: the job disappears from the pipeline and its
        directory moves to Trash, so it can be undeleted.
        """
        project = guard.resolve_existing(project_directory, kind='directory')
        return _action_response(
            job_actions.delete(project, process_name, recursive=recursive)
        )

    @app.patch('/api/projects/processes/{process_name:path}')
    async def update_process(
        process_name: str,
        body: 'ProcessUpdate',
        project_directory: str,
        guard: PathGuard = Depends(_guard_from_request),
    ) -> dict:
        """Change a process's alias, note, or pipeline status.

        One endpoint for the three things the GUI's "Job actions" menu changes,
        because they are all edits to the same record and a caller often wants
        two of them at once.
        """
        project = guard.resolve_existing(project_directory, kind='directory')
        results = []
        if body.alias is not None:
            results.append(job_actions.set_alias(project, process_name, body.alias))
        if body.note is not None:
            results.append(job_actions.write_note(project, process_name, body.note))
        if body.failed is not None:
            results.append(
                job_actions.mark_status(project, process_name, failed=body.failed)
            )
        if not results:
            raise HTTPException(
                400, detail='nothing to change: give alias, note or failed'
            )
        return {
            'process_name': process_name,
            'actions': [_action_response(result) for result in results],
        }

    @app.post('/api/projects/processes/{process_name:path}/clean')
    async def clean_process(
        process_name: str,
        project_directory: str,
        harsh: bool = False,
        guard: PathGuard = Depends(_guard_from_request),
    ) -> dict:
        """Gentle or harsh clean, using RELION's own per-job cleanup rules.

        Clean is its own endpoint because it is not an edit to the record: it
        deletes intermediate files from a finished job directory.
        """
        project = guard.resolve_existing(project_directory, kind='directory')
        return _action_response(
            job_actions.clean(project, process_name, harsh=harsh)
        )

    @app.get('/api/projects/trash')
    async def list_trash(
        project_directory: str, guard: PathGuard = Depends(_guard_from_request)
    ) -> dict:
        """Jobs that were deleted and can be restored."""
        project = guard.resolve_existing(project_directory, kind='directory')
        return _action_response(job_actions.list_trash(project))

    @app.post('/api/projects/trash/{trash_entry:path}/restore')
    async def restore_from_trash(
        trash_entry: str,
        project_directory: str,
        guard: PathGuard = Depends(_guard_from_request),
    ) -> dict:
        """Put a deleted job back where it was."""
        project = guard.resolve_existing(project_directory, kind='directory')
        return _action_response(job_actions.undelete(project, trash_entry))

    @app.post('/api/project/job/delete', deprecated=True)
    async def job_delete(
        body: JobActionRequest, guard: PathGuard = Depends(_guard_from_request)
    ) -> dict:
        """Deprecated: use ``DELETE /api/projects/processes/{name}``."""
        project = guard.resolve_existing(body.project_directory, kind='directory')
        return _action_response(
            job_actions.delete(project, body.process_name, recursive=body.recursive)
        )

    @app.get('/api/project/trash', deprecated=True)
    async def job_trash_list(
        project_directory: str,
        guard: PathGuard = Depends(_guard_from_request),
    ) -> dict:
        project = guard.resolve_existing(project_directory, kind='directory')
        return _action_response(job_actions.list_trash(project))

    @app.post('/api/project/undelete', deprecated=True)
    async def job_undelete(
        body: UndeleteRequest, guard: PathGuard = Depends(_guard_from_request)
    ) -> dict:
        project = guard.resolve_existing(body.project_directory, kind='directory')
        return _action_response(job_actions.undelete(project, body.trash_entry))

    @app.post('/api/project/pick-points')
    async def pick_points(
        body: PickPointsRequest, guard: PathGuard = Depends(_guard_from_request)
    ) -> dict:
        """Save points placed in the 3D view as a coordinates STAR file.

        The desktop GUI adds picks through napari and writes them straight into
        annotations; here the points come from clicks on the rendered
        projection, so they are written as a plain rlnCoordinateX/Y/Z table that
        ``relion_tomo_import_coordinates`` (or the pick job's input STAR) reads.
        """
        tomogram = guard.resolve_existing(body.tomogram_path)
        output_dir = guard.make_directory(body.output_directory)
        if not body.points:
            raise HTTPException(400, detail='no points to save')
        try:
            path = _write_pick_star(tomogram, body.points, output_dir)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        return {'star_file': str(path), 'n_points': len(body.points)}

    # ------------------------------------------------------------ scheduler
    @app.post('/api/project/scheduler/run')
    async def scheduler_run(
        body: ScheduleRequest, guard: PathGuard = Depends(_guard_from_request)
    ) -> dict:
        project = guard.resolve_existing(body.project_directory, kind='directory')
        return _action_response(
            _schedule_result(
                job_actions.run_schedule(
                    project,
                    body.process_names,
                    name=body.name,
                    repeat=body.repeat,
                    wait_minutes=body.wait_minutes,
                    wait_before_minutes=body.wait_before_minutes,
                    wait_after_seconds=body.wait_after_seconds,
                )
            )
        )

    @app.post('/api/project/scheduler/stop')
    async def scheduler_stop(
        body: ScheduleStopRequest, guard: PathGuard = Depends(_guard_from_request)
    ) -> dict:
        project = guard.resolve_existing(body.project_directory, kind='directory')
        return _action_response(_schedule_result(job_actions.stop_schedule(project, body.name)))

    @app.get('/api/project/scheduler')
    async def scheduler_status(
        project_directory: str,
        name: str = Query('web'),
        guard: PathGuard = Depends(_guard_from_request),
    ) -> dict:
        project = guard.resolve_existing(project_directory, kind='directory')
        return job_actions.schedule_status(project, name)

    # -------------------------------------------------------- export/import
    @app.post('/api/project/export')
    async def project_export(
        body: ExportRequest, guard: PathGuard = Depends(_guard_from_request)
    ) -> dict:
        project = guard.resolve_existing(body.project_directory, kind='directory')
        return _action_response(
            _schedule_result(
                job_actions.export_jobs(project, body.process_names, body.name)
            )
        )

    @app.post('/api/project/import')
    async def project_import(
        body: ImportRequest, guard: PathGuard = Depends(_guard_from_request)
    ) -> dict:
        project = guard.resolve_existing(body.project_directory, kind='directory')
        listing = guard.resolve_existing(body.listing)
        return _action_response(
            _schedule_result(job_actions.import_jobs(project, listing))
        )

    @app.post('/api/project/job/mark', deprecated=True)
    async def job_mark(
        body: JobMarkRequest, guard: PathGuard = Depends(_guard_from_request)
    ) -> dict:
        project = guard.resolve_existing(body.project_directory, kind='directory')
        return _action_response(
            job_actions.mark_status(project, body.process_name, failed=body.failed)
        )

    @app.post('/api/project/job/clean', deprecated=True)
    async def job_clean(
        body: JobCleanRequest, guard: PathGuard = Depends(_guard_from_request)
    ) -> dict:
        """Gentle or harsh clean, using RELION's own per-job cleanup rules."""
        project = guard.resolve_existing(body.project_directory, kind='directory')
        return _action_response(
            job_actions.clean(project, body.process_name, harsh=body.harsh)
        )

    # -------------------------------------------------------------- projects
    @app.get('/api/projects')
    async def list_projects(request: Request) -> dict:
        """The user's project list, which is also the access boundary."""
        current_session(request)
        return {
            'projects': projects.summarize(),
            'config_directory': str(projects.config_directory),
        }

    @app.post('/api/projects', status_code=201)
    async def add_project(body: ProjectRequest, request: Request) -> dict:
        current_session(request)
        try:
            entry = projects.add(body.path, name=body.name)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
        return entry.as_dict()

    @app.post('/api/projects/create', status_code=201)
    async def create_project(body: CreateProjectRequest, request: Request) -> dict:
        """Create a new RELION project directory.

        Only ``default_pipeline.star`` is written, which is all RELION needs to
        open a directory as a project (``PipeLine::write`` does the same in the
        GUI when it starts in an empty directory). Adding it to the user's list
        is a separate step, so creating and listing stay independent.
        """
        current_session(request)
        registry: ProjectRegistry = request.app.state.projects
        try:
            project = registry.create(body.path, name=body.name)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
        return project

    @app.delete('/api/projects')
    async def remove_project(path: str, request: Request) -> dict:
        current_session(request)
        if not projects.remove(path):
            raise HTTPException(404, detail=f'project not in the list: {path}')
        return {'removed': path}

    # ------------------------------------------------------------------ jobs
    @app.post('/api/jobs', status_code=201)
    async def submit_job(
        body: JobRequest,
        manager: JobManager = Depends(_jobs_from_request),
    ) -> dict:
        project_directory = (
            Path(body.project_directory) if body.project_directory else None
        )
        try:
            job = await manager.submit(
                body.job_type,
                body.arguments,
                Path(body.output_directory),
                project_directory=project_directory,
                dry_run=body.dry_run,
                host=body.host,
            )
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
        return job.as_dict()

    @app.get('/api/hosts')
    async def list_hosts(
        request: Request,
        check: bool = False,
        manager: JobManager = Depends(_jobs_from_request),
    ) -> dict:
        """The machines a job can run on.

        `check=true` contacts each remote node once, so a wrong hostname or a
        missing ssh key is reported here rather than surfacing as a failed job.
        """
        current_session(request)
        entries = []
        for host in manager.hosts.list():
            entry = host.as_dict()
            if check and not host.is_local and host.enabled:
                ok, message = hosts_module.check_reachable(host)
                entry['reachable'] = ok
                if not ok:
                    entry['problem'] = message
            entries.append(entry)
        return {
            'hosts': entries,
            'default': hosts_module.LOCAL_NAME,
        }

    @app.post('/api/hosts', status_code=201)
    async def add_host(body: HostRequest, request: Request) -> dict:
        """Define a node. `name=hostname[:port]` or explicit fields."""
        current_session(request)
        if body.spec:
            host = hosts_module.parse_host_spec(body.spec)
        else:
            if not body.name or not body.hostname:
                raise HTTPException(
                    400, detail='give a spec, or both a name and a hostname'
                )
            host = hosts_module.ExecutionHost(
                name=body.name,
                hostname=body.hostname,
                port=body.port,
                user=body.user,
                note=body.note or '',
            )
        if body.note and not host.note:
            host = hosts_module.ExecutionHost(
                name=host.name,
                hostname=host.hostname,
                port=host.port,
                user=host.user,
                enabled=host.enabled,
                note=body.note,
            )
        registry = request.app.state.jobs.hosts
        added = registry.add(host)
        return added.as_dict()

    @app.patch('/api/hosts/{name}')
    async def update_host(
        name: str, body: HostUpdate, request: Request, manager: JobManager = Depends(_jobs_from_request)
    ) -> dict:
        """Enable, disable, or re-note a node.

        Disabling keeps the definition but stops offering it for new jobs and
        stops polling it, which is what taking a node out for maintenance means.
        """
        current_session(request)
        updated = manager.hosts.update(
            name, enabled=body.enabled, note=body.note, hostname=body.hostname
        )
        return updated.as_dict()

    @app.delete('/api/hosts/{name}')
    async def remove_host(
        name: str, request: Request, manager: JobManager = Depends(_jobs_from_request)
    ) -> dict:
        current_session(request)
        if not manager.hosts.remove(name):
            raise HTTPException(404, detail=f'no host named {name!r}')
        return {'removed': name}

    @app.get('/api/nodes')
    async def node_snapshots(
        request: Request,
        manager: JobManager = Depends(_jobs_from_request),
        local: bool = True,
    ) -> dict:
        """A live snapshot of every enabled node: load, memory, GPUs, our jobs.

        One ssh round trip per node, run concurrently, so a slow or down node does
        not delay the others. Disabled nodes are skipped: they are out of service,
        so polling them would be noise.
        """
        current_session(request)
        wanted = [
            host
            for host in manager.hosts.list()
            if host.enabled and (local or not host.is_local)
        ]
        # Collection blocks for up to the per-node timeout, so it runs in a
        # worker thread. On the event loop a single dead node would freeze every
        # other request in the app for those seconds.
        import anyio

        snapshots = await anyio.to_thread.run_sync(
            node_metrics.collect_many, wanted
        )
        return {
            'nodes': [snapshot.as_dict() for snapshot in snapshots],
            'count': len(snapshots),
        }

    @app.get('/api/jobs')
    async def list_jobs(
        manager: JobManager = Depends(_jobs_from_request),
    ) -> dict:
        return {'jobs': [job.as_dict() for job in manager.list()]}

    # ------------------------------------------------------------------ queue
    #
    # A queue entry is not a job: it is a request to start one, possibly later.
    # The routes below only manage the request. Starting it happens in the
    # scheduler loop, through `JobManager.submit` - the same call POST /api/jobs
    # makes - so a queued job is indistinguishable from a directly submitted one
    # once it runs.
    @app.get('/api/queue')
    async def list_queue(
        project_directory: Optional[str] = None,
        queue: queue_system.JobQueue = Depends(_queue_from_request),
    ) -> dict:
        """Every entry, plus each node's spare capacity.

        One call rather than two: the queue view needs both, and a separate
        capacity refresh would let the two disagree about whether a GPU is free.
        Capacity is cached per node, so refreshing it here costs one round trip
        per node per cache window, not one per poll.
        """
        await queue.warm_capacity()
        return queue.snapshot(project_directory)

    @app.post('/api/queue', status_code=201)
    async def add_to_queue(
        body: QueueRequest,
        queue: queue_system.JobQueue = Depends(_queue_from_request),
    ) -> dict:
        entry = queue.add(
            body.project_directory,
            body.job_type,
            body.arguments,
            host=body.host,
            gpus=body.gpus,
            cores=body.cores,
            gpu_memory_mb=body.gpu_memory_mb,
            priority=body.priority,
            exclusive=body.exclusive,
            depends_on=body.depends_on,
            after_jobs=body.after_jobs,
            after_files=body.after_files,
            inputs=body.inputs,
        )
        return entry.as_dict()

    @app.delete('/api/queue/{entry_id}')
    async def cancel_queue_entry(
        entry_id: str,
        forget: bool = Query(
            False, description='drop a finished entry instead of cancelling a live one'
        ),
        queue: queue_system.JobQueue = Depends(_queue_from_request),
    ) -> dict:
        """Cancel a live entry, stopping its job if it already started.

        With `forget=true` a *finished* entry is dropped instead. The flag
        mirrors `DELETE /api/jobs?forget=true`, so the same request shape means
        the same thing on both lists, and a finished entry never disappears
        because someone meant to stop a running one.
        """
        entry = queue.get(entry_id)
        if entry is None:
            raise queue_system.QueueError(f'no queue entry with id {entry_id!r}')
        if forget:
            queue.remove(entry_id)
            return {'id': entry_id, 'removed': True}
        return (await queue.cancel(entry_id)).as_dict()

    @app.post('/api/queue/{entry_id}/retry')
    async def retry_queue_entry(
        entry_id: str,
        queue: queue_system.JobQueue = Depends(_queue_from_request),
    ) -> dict:
        """Put a finished entry back in the queue as a fresh job."""
        return queue.retry(entry_id).as_dict()

    @app.get('/api/jobs/{job_id}')
    async def get_job(
        job_id: str,
        manager: JobManager = Depends(_jobs_from_request),
    ) -> dict:
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(404, detail=f'no job {job_id!r}')
        payload = job.as_dict()
        payload['log'] = manager.tail_log(job_id)
        payload['log_offset'] = manager.log_size(job_id)
        return payload

    @app.get('/api/jobs/{job_id}/log')
    async def get_job_log(
        job_id: str,
        offset: int = Query(0, ge=0, description='byte offset already received'),
        manager: JobManager = Depends(_jobs_from_request),
    ) -> dict:
        """Return log bytes written after ``offset``.

        This is the live-tail primitive: a client keeps the returned
        ``next_offset`` and asks again, so a running job's log can be followed
        without refetching it in full.
        """
        if manager.get(job_id) is None:
            raise HTTPException(404, detail=f'no job {job_id!r}')
        text, next_offset, truncated = manager.read_log_chunk(job_id, offset)
        return {
            'id': job_id,
            'chunk': text,
            'next_offset': next_offset,
            'truncated': truncated,
        }

    @app.delete('/api/jobs/{job_id}')
    async def cancel_job(
        job_id: str,
        forget: bool = Query(
            False, description='drop a finished job from the list instead of stopping it'
        ),
        manager: JobManager = Depends(_jobs_from_request),
    ) -> dict:
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(404, detail=f'no job {job_id!r}')
        if forget:
            # Only finished jobs are forgettable: forgetting a running one would
            # lose track of a live process.
            if job.status == 'running':
                raise HTTPException(
                    409, detail=f'job {job_id!r} is still running; stop it first'
                )
            manager.forget(job_id)
            return {'id': job_id, 'forgotten': True}
        cancelled = await manager.cancel(job_id)
        current = manager.get(job_id)
        return {
            'id': job_id,
            'cancelled': cancelled,
            'status': current.status if current is not None else None,
        }

    @app.websocket('/ws/jobs')
    async def jobs_socket(websocket: WebSocket) -> None:
        if websocket.app.state.auth is not None:
            if session_from_websocket(websocket) is None:
                # 1008 = policy violation; the client treats it as "log in again".
                await websocket.close(code=1008)
                return
        await websocket.accept()
        manager: JobManager = websocket.app.state.jobs
        interval: float = websocket.app.state.poll_interval

        # A client may subscribe to one job's log by sending
        # {"watch": "<job_id>", "offset": <bytes already seen>}. Subsequent
        # pushes then carry the bytes written since that offset.
        watched_id: Optional[str] = None
        watched_offset = 0

        async def pump() -> None:
            """Push job state, plus a log chunk when a job is being watched."""
            nonlocal watched_id, watched_offset
            while True:
                payload = {'jobs': [job.as_dict() for job in manager.list()]}
                if watched_id is not None:
                    if manager.get(watched_id) is None:
                        watched_id = None
                    else:
                        chunk, next_offset, truncated = manager.read_log_chunk(
                            watched_id, watched_offset
                        )
                        watched_offset = next_offset
                        payload['log'] = {
                            'id': watched_id,
                            'chunk': chunk,
                            'next_offset': next_offset,
                            'truncated': truncated,
                        }
                await websocket.send_text(json.dumps(payload))
                await asyncio.sleep(interval)

        sender = asyncio.create_task(pump())
        try:
            while True:
                message = await websocket.receive_text()
                try:
                    request = json.loads(message)
                except (TypeError, ValueError):
                    continue
                if not isinstance(request, dict):
                    continue
                if request.get('unwatch'):
                    watched_id = None
                    continue
                candidate = request.get('watch')
                if isinstance(candidate, str) and candidate:
                    watched_id = candidate
                    offset = request.get('offset', 0)
                    watched_offset = int(offset) if isinstance(offset, (int, float)) else 0
        except WebSocketDisconnect:
            pass
        except (RuntimeError, asyncio.CancelledError):
            pass
        finally:
            sender.cancel()

    # ------------------------------------------------------------------- UI
    @app.get('/', response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        """Serve the single-page app.

        The page itself decides whether to show the login form: the API answers
        401 until a session exists, and the client renders the form in that
        case. Serving one HTML document for both states keeps the build to a
        single bundle.
        """
        index_file = STATIC_DIRECTORY / 'index.html'
        if not index_file.is_file():
            return HTMLResponse(
                '<h1>RELION cryo-ET web interface</h1>'
                '<p>Front-end assets are missing. Build them with '
                '<code>cd frontend &amp;&amp; bun install &amp;&amp; bun run build</code>; '
                'the API is served under <code>/api</code>.</p>',
                status_code=503,
            )
        return HTMLResponse(index_file.read_text(encoding='utf-8'))

    @app.exception_handler(hosts_module.HostError)
    async def _host_error(
        request: Request, exc: hosts_module.HostError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400, content={'error': str(exc), 'status': 400}
        )

    @app.exception_handler(queue_system.QueueError)
    async def _queue_error(
        request: Request, exc: queue_system.QueueError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400, content={'error': str(exc), 'status': 400}
        )

    @app.exception_handler(job_actions.ActionError)
    async def _action_error(request: Request, exc: job_actions.ActionError) -> JSONResponse:
        return JSONResponse(
            status_code=400, content={'error': str(exc), 'status': 400}
        )

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={'error': exc.detail, 'status': exc.status_code},
        )

    # ------------------------------------------------- assistant and MCP
    # Registered after the rest of the routes so the MCP mount is appended last:
    # a Mount at an unknown prefix would otherwise be matched before the API
    # routes that share its parent.
    if enable_assistant or enable_mcp:
        api_assistant.register(
            app,
            guard=guard,
            projects=projects,
            jobs=manager,
            enable_mcp=enable_mcp,
            enable_assistant=enable_assistant,
            default_config=assistant_config,
            mcp_hosts=mcp_hosts,
            mcp_allow_writes=mcp_allow_writes,
        )
        # /mcp is a public path only when MCP is off the request was never
        # mounted; when it is on, the middleware above already gated it.

    return app
