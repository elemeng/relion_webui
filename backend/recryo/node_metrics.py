# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""What is running on a node, in one ssh round trip.

A node card has to answer three questions at a glance: how loaded is this
machine, which GPUs are busy, and which of *our* jobs are on it. Gathering that
means reading ``nvidia-smi``, ``/proc`` and the process table - three sources
that would be three round trips if each were its own command. On a LAN that is
still fast, but a card refresh every few seconds multiplies it, so the whole
snapshot is one shell snippet whose output is parsed locally.

**The remote script is deliberately POSIX-only and defensive.** Node images vary:
``nvidia-smi`` may be missing, the GPU query fields differ between driver
versions (``power.limit`` is ``[N/A]`` on many laptop GPUs), and a container may
have no ``/proc/meminfo``. Every section prints what it can and nothing fails the
whole snapshot - a node with no GPU still gets a card showing its CPU load.

**Mapping a process to a job** uses a fact the agent already guarantees: every
RELION program is invoked with ``--pipeline_control <job dir>`` (relion's own
convention, reproduced in ``job_queue.py``). So a process's command line names
the job directory, which is a project path plus ``<Type>/job<NNN>``. That is how a
queue row says "job006, 3 processes, 12 GB, 2 GPUs" instead of a bare PID.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import hosts as hosts_module

# One snapshot, as a single remote command.
#
# Each block is wrapped so a missing tool produces an empty section rather than
# a non-zero exit that would discard everything else.
_SNAPSHOT_SCRIPT = r'''
echo "@@LOAD@@"
cat /proc/loadavg 2>/dev/null || true
echo "@@CPU@@"
nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo 2>/dev/null || echo 0
echo "@@MEM@@"
awk '/^MemTotal:|^MemAvailable:/{print $1, $2}' /proc/meminfo 2>/dev/null || true
echo "@@UP@@"
awk '{print int($1)}' /proc/uptime 2>/dev/null || true
echo "@@GPU@@"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=index,uuid,name,memory.used,memory.total,utilization.gpu,power.draw,power.limit,temperature.gpu --format=csv,noheader,nounits 2>/dev/null || true
fi
echo "@@GPUAPPS@@"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-compute-apps=pid,used_memory,gpu_uuid --format=csv,noheader,nounits 2>/dev/null || true
fi
echo "@@PROCS@@"
ps -eo pid,ppid,pcpu,pmem,rss,args --no-headers 2>/dev/null \
  | grep -- '--pipeline_control' 2>/dev/null | grep -v grep || true
echo "@@END@@"
'''


@dataclass
class Gpu:
    index: int
    name: str = ''
    # nvidia-smi's stable identifier. Indices are not stable across a reboot or
    # a card entering/leaving a MIG configuration; a UUID is.
    uuid: str = ''
    memory_used_mb: Optional[float] = None
    memory_total_mb: Optional[float] = None
    utilization_percent: Optional[float] = None
    power_watts: Optional[float] = None
    power_limit_watts: Optional[float] = None
    temperature_c: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            'index': self.index,
            'name': self.name,
            'uuid': self.uuid,
            'memory_used_mb': self.memory_used_mb,
            'memory_total_mb': self.memory_total_mb,
            'utilization_percent': self.utilization_percent,
            'power_watts': self.power_watts,
            'power_limit_watts': self.power_limit_watts,
            'temperature_c': self.temperature_c,
        }


@dataclass
class NodeJob:
    """One group of processes on a node that belongs to a RELION job."""

    job_directory: str
    project: Optional[str] = None
    process_name: Optional[str] = None
    job_type: Optional[str] = None
    pids: List[int] = field(default_factory=list)
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    gpu_memory_mb: float = 0.0
    command: str = ''

    def as_dict(self) -> dict:
        return {
            'job_directory': self.job_directory,
            'project': self.project,
            'process_name': self.process_name,
            'job_type': self.job_type,
            'pids': self.pids,
            'cpu_percent': round(self.cpu_percent, 1),
            'memory_mb': round(self.memory_mb, 1),
            'gpu_memory_mb': round(self.gpu_memory_mb, 1),
            'command': self.command[:400],
        }


@dataclass
class NodeSnapshot:
    name: str
    reachable: bool = False
    problem: Optional[str] = None
    hostname: str = ''
    load_1: Optional[float] = None
    load_5: Optional[float] = None
    load_15: Optional[float] = None
    cpus: int = 0
    memory_total_mb: Optional[float] = None
    memory_available_mb: Optional[float] = None
    uptime_seconds: Optional[int] = None
    gpus: List[Gpu] = field(default_factory=list)
    jobs: List[NodeJob] = field(default_factory=list)
    # ``pid -> gpu index`` for processes holding a compute context. Empty when
    # nvidia-smi is absent, which is why the UI falls back to memory readings.
    gpu_tenants: Dict[int, int] = field(default_factory=dict)

    @property
    def load_percent(self) -> Optional[float]:
        if self.load_1 is None or not self.cpus:
            return None
        return round(100.0 * self.load_1 / self.cpus, 1)

    @property
    def memory_used_mb(self) -> Optional[float]:
        if self.memory_total_mb is None or self.memory_available_mb is None:
            return None
        return max(0.0, self.memory_total_mb - self.memory_available_mb)

    def as_dict(self) -> dict:
        busy = {
            index
            for index in self.gpu_tenants.values()
        }
        return {
            'name': self.name,
            'reachable': self.reachable,
            'problem': self.problem,
            'hostname': self.hostname,
            'load_1': self.load_1,
            'load_5': self.load_5,
            'load_15': self.load_15,
            'load_percent': self.load_percent,
            'cpus': self.cpus,
            'memory_total_mb': self.memory_total_mb,
            'memory_available_mb': self.memory_available_mb,
            'memory_used_mb': self.memory_used_mb,
            'uptime_seconds': self.uptime_seconds,
            'gpus': [
                {**gpu.as_dict(), 'has_compute_tenant': gpu.index in busy}
                for gpu in self.gpus
            ],
            'jobs': [job.as_dict() for job in self.jobs],
        }


# ------------------------------------------------------------------- parsing

def _number(text: Optional[str]) -> Optional[float]:
    """A float from nvidia-smi output, treating ``[N/A]``/``[Not Supported]`` as absent.

    Driver versions report unsupported fields as bracketed text, and a laptop GPU
    commonly has no power limit. Returning None lets the UI leave the figure out
    instead of showing a zero that reads as "no power".
    """
    if text is None:
        return None
    cleaned = text.strip()
    if not cleaned or cleaned.startswith('['):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _sections(output: str) -> Dict[str, List[str]]:
    """Split the snapshot into its ``@@NAME@@`` sections."""
    sections: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith('@@') and stripped.endswith('@@'):
            current = stripped.strip('@')
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)
    return sections


def parse_gpus(lines: List[str]) -> List[Gpu]:
    gpus: List[Gpu] = []
    for line in lines:
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(',')]
        if len(parts) < 3:
            continue
        try:
            index = int(parts[0])
        except ValueError:
            continue
        gpu = Gpu(index=index, uuid=parts[1], name=parts[2])
        if len(parts) > 3:
            gpu.memory_used_mb = _number(parts[3])
        if len(parts) > 4:
            gpu.memory_total_mb = _number(parts[4])
        if len(parts) > 5:
            gpu.utilization_percent = _number(parts[5])
        if len(parts) > 6:
            gpu.power_watts = _number(parts[6])
        if len(parts) > 7:
            gpu.power_limit_watts = _number(parts[7])
        if len(parts) > 8:
            gpu.temperature_c = _number(parts[8])
        gpus.append(gpu)
    return gpus


def parse_gpu_apps(lines: List[str]) -> Dict[int, float]:
    """pid -> GPU memory in MB, from ``nvidia-smi --query-compute-apps``."""
    usage: Dict[int, float] = {}
    for line in lines:
        parts = [part.strip() for part in line.split(',')]
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        memory = _number(parts[1])
        if memory is None:
            continue
        # A process using several GPUs appears once per GPU; sum them, because
        # the card shows what the job costs in total.
        usage[pid] = usage.get(pid, 0.0) + memory
    return usage


def parse_gpu_app_uuids(lines: List[str], gpus: List[Gpu]) -> Dict[int, str]:
    """pid -> the UUID of the GPU it holds a context on.

    This is what tells a *compute* tenant apart from the display server, which
    holds memory but appears in no compute-apps row. Without it, a workstation
    card running a desktop session looks permanently busy.
    """
    by_uuid = {gpu.uuid: gpu.index for gpu in gpus if gpu.uuid}
    on_gpu: Dict[int, str] = {}
    for line in lines:
        parts = [part.strip() for part in line.split(',')]
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        index = by_uuid.get(parts[2])
        if index is not None:
            on_gpu[pid] = index
    return on_gpu


def _job_from_command(command: str) -> tuple:
    """``(job_directory, job_type)`` from a ``--pipeline_control`` argument.

    Every RELION program is started with ``--pipeline_control <job dir>``, which
    is relion's own convention for its sentinel files. The directory is
    ``<project>/<Type>/job<NNN>``, so the type is the last component before the
    job name - which is what makes a queue row readable.
    """
    for index, token in enumerate(command.split()):
        if token == '--pipeline_control':
            tokens = command.split()
            if index + 1 < len(tokens):
                directory = tokens[index + 1].rstrip('/')
                return directory, directory
        if token.startswith('--pipeline_control='):
            directory = token.split('=', 1)[1].rstrip('/')
            return directory, directory
    return '', ''


def parse_processes(lines: List[str], gpu_usage: Dict[int, float]) -> List[NodeJob]:
    """Group the node's RELION processes by the job directory they name.

    One job commonly has several processes (MPI ranks, or a pipeline of
    programs), and the card must show one row per job, not per rank - so they are
    summed by directory.
    """
    by_directory: Dict[str, NodeJob] = {}
    for line in lines:
        if not line.strip():
            continue
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        try:
            pid = int(parts[0])
            cpu = float(parts[2])
            memory_mb = float(parts[4]) / 1024.0  # rss is in kB
        except ValueError:
            continue
        command = parts[5]
        directory, _ = _job_from_command(command)
        if not directory:
            continue
        job = by_directory.get(directory)
        if job is None:
            job = NodeJob(job_directory=directory)
            by_directory[directory] = job
        job.pids.append(pid)
        job.cpu_percent += cpu
        job.memory_mb += memory_mb
        job.gpu_memory_mb += gpu_usage.get(pid, 0.0)
        if not job.command:
            job.command = command

    for directory, job in by_directory.items():
        parts = directory.rstrip('/').split('/')
        if len(parts) >= 2:
            job.process_name = f'{parts[-2]}/{parts[-1]}'
            job.project = '/'.join(parts[:-2]) or None
            job.job_type = parts[-2]
    return sorted(
        by_directory.values(), key=lambda job: (job.project or '', job.process_name or '')
    )


def parse_snapshot(name: str, output: str) -> NodeSnapshot:
    sections = _sections(output)
    snapshot = NodeSnapshot(name=name, reachable=True)

    load = (sections.get('LOAD') or [''])[0].split()
    if len(load) >= 3:
        snapshot.load_1 = float(load[0])
        snapshot.load_5 = float(load[1])
        snapshot.load_15 = float(load[2])

    cpu_lines = sections.get('CPU') or []
    if cpu_lines:
        try:
            snapshot.cpus = int(cpu_lines[0].strip())
        except ValueError:
            snapshot.cpus = 0

    for line in sections.get('MEM') or []:
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            value = float(parts[1]) / 1024.0  # kB -> MB
        except ValueError:
            continue
        if parts[0].startswith('MemTotal'):
            snapshot.memory_total_mb = round(value, 1)
        elif parts[0].startswith('MemAvailable'):
            snapshot.memory_available_mb = round(value, 1)

    up = (sections.get('UP') or [''])[0].strip()
    if up:
        try:
            snapshot.uptime_seconds = int(float(up))
        except ValueError:
            pass

    snapshot.gpus = parse_gpus(sections.get('GPU') or [])
    app_lines = sections.get('GPUAPPS') or []
    usage = parse_gpu_apps(app_lines)
    snapshot.gpu_tenants = parse_gpu_app_uuids(app_lines, snapshot.gpus)
    snapshot.jobs = parse_processes(sections.get('PROCS') or [], usage)
    return snapshot


# ------------------------------------------------------------------ collecting

def collect(
    host: hosts_module.ExecutionHost, timeout: float = 6.0
) -> NodeSnapshot:
    """Snapshot one node.

    A local host runs the same script without ssh, so the agent's own card is
    accurate and there is no second code path for reading a machine's state.
    """
    if host.is_local:
        try:
            result = subprocess.run(
                ['/bin/sh', '-c', _SNAPSHOT_SCRIPT],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return NodeSnapshot(name=host.name, problem=str(exc))
    else:
        if shutil.which('ssh') is None:
            return NodeSnapshot(name=host.name, problem='ssh is not installed')
        try:
            result = subprocess.run(
                hosts_module.ssh_argv(host, _SNAPSHOT_SCRIPT),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return NodeSnapshot(
                name=host.name,
                problem=f'no answer within {timeout:g}s',
            )
        except OSError as exc:
            return NodeSnapshot(name=host.name, problem=str(exc))

    if result.returncode != 0 and '@@END@@' not in result.stdout:
        detail = (result.stderr or result.stdout or '').strip().splitlines()
        message = detail[-1] if detail else f'exit code {result.returncode}'
        return NodeSnapshot(name=host.name, problem=_explain(message, host))

    snapshot = parse_snapshot(host.name, result.stdout)
    snapshot.hostname = host.hostname or 'this machine'
    return snapshot


def _explain(message: str, host: hosts_module.ExecutionHost) -> str:
    if 'Permission denied' in message or 'publickey' in message:
        return (
            f'cannot log in to {host.name}: no ssh key. Install one with '
            f"'ssh-copy-id {host.ssh_target()}'"
        )
    if 'Could not resolve hostname' in message:
        return f'unknown hostname: {host.hostname}'
    if 'Connection refused' in message or 'timed out' in message:
        return f'cannot reach {host.hostname}'
    return message


def collect_many(
    hosts: List[hosts_module.ExecutionHost], timeout: float = 6.0
) -> List[NodeSnapshot]:
    """Snapshot several nodes concurrently.

    A node that is down or slow must not delay the cards for the nodes that are
    up, so each is collected in its own thread and the slowest sets the total.
    """
    if not hosts:
        return []
    if len(hosts) == 1:
        return [collect(hosts[0], timeout)]

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(len(hosts), 8)) as pool:
        futures = [pool.submit(collect, host, timeout) for host in hosts]
        return [future.result() for future in futures]
