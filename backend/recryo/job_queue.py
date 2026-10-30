"""MPI wrapping and queue submission, following ``prepareFinalCommand``.

RELION does not run a job's command directly. ``RelionJob::prepareFinalCommand``
(pipeline_jobs.cpp:693) turns the per-job commands into one of two things:

*local* — the commands joined with ``&&``, with ``mpirun -n N`` in front of each
program whose name ends in ``_mpi`` and whose ``nr_mpi`` is greater than one;
*queued* — a submission script written from the user's template
(``saveJobSubmissionScript``, pipeline_jobs.cpp:548) and started with ``qsub``.

Both paths append ``--pipeline_control <job_dir>`` to every RELION program first,
and both redirect to ``run.out``/``run.err``. The web agent has to reproduce the
same two shapes, otherwise a job submitted here behaves differently from one
started in the GUI: with ``nr_mpi`` above one and no ``mpirun``, every process
would run a full copy and write over the others' output.

The template substitution is kept identical on purpose. RELION's own
``relion_qsub.csh`` and the cluster templates people already have expect
``XXXmpinodesXXX``, ``XXXqueueXXX``, ``XXXcommandXXX`` and the rest, so a
template that works in the GUI works here.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

# The default when RELION_MPIRUN is unset, matching DEFAULTMPIRUN in
# pipeline_jobs.h.
DEFAULT_MPIRUN = 'mpirun'
SUBMISSION_SCRIPT = 'run_submit.script'

# Template placeholders (saveJobSubmissionScript).
PLACEHOLDERS = (
    'XXXmpinodesXXX',
    'XXXthreadsXXX',
    'XXXcoresXXX',
    'XXXdedicatedXXX',
    'XXXnodesXXX',
    'XXXnameXXX',
    'XXXerrfileXXX',
    'XXXoutfileXXX',
    'XXXqueueXXX',
    'XXXcommandXXX',
)


class QueueError(RuntimeError):
    """The submission script could not be prepared or submitted."""


@dataclass
class RunPlan:
    """How a job will be started, and the command(s) that do it."""

    argv: List[str]
    # 'local', 'mpi' or 'queue', for reporting.
    mode: str
    nr_mpi: int
    nr_threads: int
    # The program commands before mpirun wrapping and redirection.
    commands: List[str]
    # Set when mode == 'queue'.
    submission_script: Optional[Path] = None

    def describe(self) -> Dict[str, object]:
        return {
            'mode': self.mode,
            'nr_mpi': self.nr_mpi,
            'nr_threads': self.nr_threads,
            'commands': self.commands,
            'submission_script': (
                str(self.submission_script) if self.submission_script else None
            ),
            'argv': self.argv,
        }


def mpirun_executable() -> str:
    """``RELION_MPIRUN`` or ``mpirun``, as ``prepareFinalCommand`` uses."""
    return os.environ.get('RELION_MPIRUN') or DEFAULT_MPIRUN


def _as_int(value, default: int = 1) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def is_mpi_program(command: str) -> bool:
    """True for a program RELION knows how to run under ``mpirun``.

    ``prepareFinalCommand`` looks for ``relion_`` and ``_mpi`` in the command
    text. The trailing backtick in its check is because the GUI's own command
    strings wrap the executable in backticks around ``which relion_x_mpi``; the web builders
    emit the bare name, so the test looks for the suffix instead.
    """
    first = command.split()[0] if command.split() else ''
    return first.startswith('relion_') and first.endswith('_mpi')


def normalise_commands(raw: Sequence[str]) -> List[str]:
    """Turn builder output into whole command strings.

    ``job_commands.build_command`` returns argv tokens for a single-program job
    (``["relion_x", "--t", "t.star", ...]``) and a single pre-joined string for
    the multi-program jobs (``["a && b"]``). The queue template needs one line
    per program, and the MPI wrapper needs a whole command to prefix, so a token
    list is joined and a chain is split on ``&&``.
    """
    if not raw:
        return []
    if len(raw) > 1:
        # Multi-program jobs (multibody, inimodel, pick) come back as one
        # command string per program already; keep them separate so the queue
        # template can put each on its own line.
        return [command for command in raw if command.strip()]
    only = raw[0]
    if '&&' not in only:
        return [only]
    return [part.strip() for part in only.split('&&') if part.strip()]


def build_run_plan(
    commands: Sequence[str],
    output_prefix: str,
    options: Dict[str, object],
    *,
    make_directory: bool = True,
) -> RunPlan:
    """Turn a builder's commands into what will actually be executed.

    Mirrors ``prepareFinalCommand``:

    * every command naming a ``relion_`` program gets
      ``--pipeline_control <output>`` appended;
    * with ``do_queue`` set, the commands go into a submission script built from
      ``qsubscript`` and the queue command is what starts it;
    * otherwise ``mpirun -n <nr_mpi>`` is prepended to each ``_mpi`` program when
      ``nr_mpi > 1``, the commands are joined with ``&&``, and the output is
      redirected to ``run.out`` / ``run.err``;
    * a local job asking for more processes than ``RELION_ERROR_LOCAL_MPI``
      allows is refused, as the GUI refuses it.
    """
    nr_mpi = max(1, _as_int(options.get('nr_mpi'), 1))
    nr_threads = max(1, _as_int(options.get('nr_threads'), 1))
    commands = normalise_commands(commands)

    # Step 1: the pipeline-control flag, added to RELION programs only.
    prepared: List[str] = []
    for command in commands:
        if 'relion_' in command:
            prepared.append(f'{command} --pipeline_control {output_prefix}')
        elif '--pipeline_control' not in command:
            # A non-RELION program (an external job, or a test double standing in
            # for one) has no pipeline_control handling of its own, but it still
            # needs the directory so it can write the sentinel files.
            prepared.append(f'{command} --pipeline_control {output_prefix}')
        else:
            prepared.append(command)

    if _truthy(options.get('do_queue')):
        script = write_submission_script(prepared, output_prefix, options, nr_mpi, nr_threads)
        qsub = str(options.get('qsub') or 'qsub').strip()
        return RunPlan(
            argv=[qsub, str(script)],
            mode='queue',
            nr_mpi=nr_mpi,
            nr_threads=nr_threads,
            commands=prepared,
            submission_script=script,
        )

    # Step 2: local execution. Refuse an unreasonable local MPI run first, which
    # is the last thing prepareFinalCommand checks.
    warning_limit = _as_int(
        os.environ.get('RELION_ERROR_LOCAL_MPI'),
        default=16,
    )
    if nr_mpi > warning_limit:
        raise QueueError(
            f'you are submitting a local job with {nr_mpi} MPI processes; that is '
            f'more than RELION_ERROR_LOCAL_MPI allows ({warning_limit}). Use a '
            f'queue, or raise RELION_ERROR_LOCAL_MPI.'
        )

    wrapped: List[str] = []
    for command in prepared:
        if nr_mpi > 1 and is_mpi_program(command):
            wrapped.append(f'{mpirun_executable()} -n {nr_mpi} {command}')
        else:
            wrapped.append(command)

    chained = ' && '.join(wrapped)
    # RELION redirects to run.out/run.err on the command line; the agent does it
    # in _pump_output instead, writing both the web log and those two files from
    # the process pipes. Doing it here as well would capture the output before
    # the web log ever sees it, leaving the live tail empty.

    return RunPlan(
        argv=['/bin/sh', '-c', chained],
        mode='mpi' if nr_mpi > 1 else 'local',
        nr_mpi=nr_mpi,
        nr_threads=nr_threads,
        commands=prepared,
    )


def write_submission_script(
    commands: Sequence[str],
    output_prefix: str,
    options: Dict[str, object],
    nr_mpi: int,
    nr_threads: int,
) -> Path:
    """Render the queue template, as ``saveJobSubmissionScript`` does.

    The template is the user's own (``qsubscript``), and the placeholders are the
    ones RELION defines, so an existing cluster template works unchanged.
    """
    template_text = str(options.get('qsubscript') or '').strip()
    if not template_text:
        raise QueueError(
            'submitting to a queue needs a submission script template '
            '(qsubscript); set it, or RELION_QSUB_TEMPLATE in the environment'
        )

    template = Path(template_text)
    if not template.is_file():
        raise QueueError(
            f'the submission script template does not exist: {template}'
        )

    dedicated = max(1, _as_int(options.get('min_dedicated'), 1))
    cores = nr_mpi * nr_threads
    nodes = math.ceil(cores / dedicated)

    replacing = {
        'XXXmpinodesXXX': str(nr_mpi),
        'XXXthreadsXXX': str(nr_threads),
        'XXXcoresXXX': str(cores),
        'XXXdedicatedXXX': str(dedicated),
        'XXXnodesXXX': str(nodes),
        'XXXnameXXX': output_prefix,
        'XXXerrfileXXX': f'{output_prefix}run.err',
        'XXXoutfileXXX': f'{output_prefix}run.out',
        'XXXqueueXXX': str(options.get('queuename') or ''),
    }
    # Optional extra replacement values (qsub_extra1..N), as the GUI offers.
    for index in range(1, 10):
        name = f'qsub_extra{index}'
        if name in options:
            replacing[f'XXXextra{index}XXX'] = str(options.get(name) or '')

    script_lines: List[str] = []
    for line in template.read_text(encoding='utf-8', errors='replace').splitlines():
        for key, value in replacing.items():
            line = line.replace(key, value)
        if 'XXXcommandXXX' not in line:
            script_lines.append(line)
            continue
        # One template line becomes one line per MPI command, mirroring the
        # loop in saveJobSubmissionScript.
        template_line = line
        for command in commands:
            if 'relion_' in command and (is_mpi_program(command) or nr_mpi == 1):
                script_lines.append(template_line.replace('XXXcommandXXX', command))
            else:
                script_lines.append(command)

    script = Path(output_prefix) / SUBMISSION_SCRIPT
    try:
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text('\n'.join(script_lines) + '\n', encoding='utf-8')
        # The template is usually a shell script; without this qsub may refuse
        # or the shell may not run it.
        script.chmod(0o755)
    except OSError as exc:
        raise QueueError(f'could not write {script}: {exc}') from exc
    return script


def submit_to_queue(script: Path, qsub_command: str) -> subprocess.CompletedProcess:
    """Start the submission script through the queue command.

    Runs in the job directory, which is where the template expects to be: it
    writes ``run.out``/``run.err`` relative to it.
    """
    try:
        return subprocess.run(
            [qsub_command, script.name],
            cwd=str(script.parent),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise QueueError(
            f'the queue command {qsub_command!r} was not found on PATH'
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise QueueError(f'{qsub_command} timed out') from exc


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ('yes', 'true', '1')
