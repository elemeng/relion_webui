#!/bin/sh
# Bidirectional format check against RELION's own STAR reader and writer.
#
# The web agent and the desktop GUI share files rather than an API, so "we can
# read what RELION writes" and "RELION can read what we write" have to be tested
# against RELION's code, not against our understanding of the format. This
# script compiles a small tool from RELION's `metadata_table.cpp` - the same
# parser the GUI uses - plus three writers, and runs both directions.
#
# It is the check that found two real bugs:
#   * RELION omits `loop_` for a single-object table and writes repeated
#     `_label value` lines, which `starfile` returns as a dict. The reader
#     silently dropped every table of a one-job pipeline.
#   * RELION reads output nodes from a table called `output_nodes`; we wrote
#     `pipeline_nodes`, so the GUI never learned which nodes a job produced.
#
# Usage: scripts/relion_compat_check/run.sh
# Needs a C++ compiler. Only metadata_*.cpp, strings, filename and CPlot2D are
# compiled, so no FFTW/CUDA/FLTK is required.

set -e

WEBUI_ROOT=$(cd "$(dirname "$0")/../.." && pwd)   # the webui project root
BACKEND="$WEBUI_ROOT/backend"
# This check compiles RELION's own STAR reader, so it needs RELION's sources.
# In a checkout they are the parent directory; RELION_SOURCE points elsewhere
# for a standalone webui clone.
ROOT=${RELION_SOURCE:-$(cd "$WEBUI_ROOT/.." && pwd)}
HERE="$WEBUI_ROOT/scripts/relion_compat_check"   # the C++ helpers live here
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

PYTHON=${PYTHON:-python3}
# The conda env used for development is not always importable here; callers can
# override PYTHON. Fall back to a python3 that has numpy/starfile.
if [ -z "${PYTHON:-}" ] && [ -x /tmp/rtweb/bin/python ]; then PYTHON=/tmp/rtweb/bin/python; fi

echo "building the RELION metadata tool from $ROOT/src ..."
g++ -std=c++17 -I"$ROOT" -o "$WORK/read" "$HERE/read.cpp" "$HERE/stub.cpp" \
    "$ROOT/src/metadata_table.cpp" "$ROOT/src/metadata_label.cpp" \
    "$ROOT/src/metadata_container.cpp" "$ROOT/src/strings.cpp" \
    "$ROOT/src/error.cpp" "$ROOT/src/filename.cpp" "$ROOT/src/CPlot2D.cpp"
g++ -std=c++17 -I"$ROOT" -o "$WORK/write_single" "$HERE/write_single.cpp" "$HERE/stub.cpp" \
    "$ROOT/src/metadata_table.cpp" "$ROOT/src/metadata_label.cpp" \
    "$ROOT/src/metadata_container.cpp" "$ROOT/src/strings.cpp" \
    "$ROOT/src/error.cpp" "$ROOT/src/filename.cpp" "$ROOT/src/CPlot2D.cpp"
g++ -std=c++17 -I"$ROOT" -o "$WORK/write_multi" "$HERE/write_multi.cpp" "$HERE/stub.cpp" \
    "$ROOT/src/metadata_table.cpp" "$ROOT/src/metadata_label.cpp" \
    "$ROOT/src/metadata_container.cpp" "$ROOT/src/strings.cpp" \
    "$ROOT/src/error.cpp" "$ROOT/src/filename.cpp" "$ROOT/src/CPlot2D.cpp"

status=0
check() {
    # check <file> <table>
    if "$WORK/read" "$1" "$2" > "$WORK/out.txt" 2>&1; then
        printf '  ok   %-46s %s\n' "$(basename "$1")" "$2"
        sed 's/^/       /' "$WORK/out.txt"
    else
        printf '  FAIL %-46s %s\n' "$(basename "$1")" "$2"
        sed 's/^/       /' "$WORK/out.txt"
        status=1
    fi
}

echo
echo "== direction 1: the agent writes, RELION reads =="
PYTHONPATH="$BACKEND" "$PYTHON" - "$WORK" <<'PY'
import asyncio
import sys
from pathlib import Path

from relion_webui.jobs import JobManager
from relion_webui.paths import PathGuard
from relion_webui import pipeline as pipeline_module

work = Path(sys.argv[1])
project = work / 'proj'
project.mkdir()
pipeline_module.write_pipeline(project, pipeline_module.Pipeline())

# A stand-in for relion_tomo_reconstruct_tomogram: it honours --o and writes the
# sentinel, which is all the lifecycle needs.
exe = work / 'reconstruct.sh'
exe.write_text(
    '#!/bin/sh\n'
    'prev=""; for a in "$@"; do\n'
    '  if [ "$prev" = "--pipeline_control" ]; then ctl="$a"; fi; prev="$a"; done\n'
    'out=""; while [ $# -gt 0 ]; do case "$1" in --o) out="$2"; shift 2;; *) shift;; esac; done\n'
    'mkdir -p "$out"\n'
    'echo "# tomograms" > "${out}tomograms.star"\n'
    'touch "${ctl}RELION_JOB_EXIT_SUCCESS"\n'
)
exe.chmod(0o755)

manager = JobManager(
    PathGuard([work]),
    executable_overrides={'tomo_reconstruct_tomograms': str(exe)},
)


async def run():
    job = await manager.submit(
        'tomo_reconstruct_tomograms',
        {
            'in_tiltseries': 'tilt_series.star',
            'xdim': 4000,
            'ydim': 4000,
            'zdim': 2000,
            'binned_angpix': 10,
            'do_fourier': True,
        },
        work / 'tmp',
        project_directory=project,
    )
    for _ in range(200):
        if job.status != 'running':
            break
        await asyncio.sleep(0.05)
    return job


job = asyncio.run(run())
asyncio.run(manager.shutdown())
print(f'  job finished as {job.status!r} at {job.process_name}')
PY

JOB="$WORK/proj/Tomograms/job001"
check "$WORK/proj/default_pipeline.star" pipeline_general
check "$WORK/proj/default_pipeline.star" pipeline_processes
check "$WORK/proj/default_pipeline.star" pipeline_nodes
check "$WORK/proj/default_pipeline.star" pipeline_input_edges
check "$WORK/proj/default_pipeline.star" pipeline_output_edges
check "$JOB/job.star" job
check "$JOB/job.star" joboptions_values
check "$JOB/RELION_OUTPUT_NODES.star" output_nodes
check "$JOB/job001job_pipeline.star" pipeline_processes
check "$JOB/job001job_pipeline.star" pipeline_nodes

# The GUI finds a note at `<process name>note.txt`, beside the job directory.
if [ -f "$WORK/proj/Tomograms/job001note.txt" ]; then
    echo "  ok   note at Tomograms/job001note.txt"
else
    echo "  FAIL note is not where the GUI looks (Tomograms/job001note.txt)"; status=1
fi
# .Nodes holds touched files, not symlinks.
if [ -f "$WORK/proj/.Nodes/TomogramGroupMetadata/Tomograms/job001/tomograms.star" ] &&
   [ ! -L "$WORK/proj/.Nodes/TomogramGroupMetadata/Tomograms/job001/tomograms.star" ]; then
    echo "  ok   .Nodes entry is a touched file"
else
    echo "  FAIL .Nodes entry is missing or is a symlink"; status=1
fi

echo
echo "== direction 1b: a queued (Scheduled) chain is still a RELION pipeline =="
# A queued job is placed in the pipeline before it runs, exactly as
# PipeLine::addJob(job, PROC_SCHEDULED) does, and a downstream job names its
# output as input. RELION's own reader must accept that pipeline and keep the
# Scheduled status - otherwise the desktop GUI would reject or misread a project
# with anything queued in it.
PYTHONPATH="$BACKEND" "$PYTHON" - "$WORK" <<'PY'
import sys
from pathlib import Path

from relion_webui import queue_system, pipeline as pipeline_module
from relion_webui.hosts import HostRegistry
from relion_webui.paths import PathGuard


class _NullManager:
    """Just enough for enqueue: reserving never starts anything."""

    hosts = HostRegistry()

    def get(self, job_id):
        return None

    async def submit(self, *args, **kwargs):
        raise AssertionError('enqueue must not submit')


work = Path(sys.argv[1])
project = work / 'queued'
project.mkdir()
pipeline_module.write_pipeline(project, pipeline_module.Pipeline())

queue = queue_system.JobQueue(
    _NullManager(), PathGuard([work]), config_directory=work / 'cfg'
)
first = queue.add(
    project,
    'tomo_reconstruct_tomograms',
    {'in_tiltseries': 'tilt_series.star', 'xdim': 100, 'ydim': 100, 'zdim': 100},
    host='local',
)
second = queue.add(
    project,
    'tomo_denoise_train',
    {'tomograms_for_training': 'train.star'},
    host='local',
    inputs={'in_tomoset': f'{first.id}:tomograms.star'},
)
assert first.process_name == 'Tomograms/job001', first.process_name
assert second.process_name == 'Denoise/job002', second.process_name
assert second.depends_on == [first.id]

# The job directory and job.star must exist before the job runs, because the
# rest of the package and the GUI both assume a job row names a real directory.
job_dir = project / first.process_name
assert (job_dir / 'job.star').is_file(), sorted(p.name for p in job_dir.iterdir())
print(f'  queued {first.process_name} -> {second.process_name}')
PY
check "$WORK/queued/default_pipeline.star" pipeline_processes
check "$WORK/queued/default_pipeline.star" pipeline_nodes
check "$WORK/queued/default_pipeline.star" pipeline_output_edges
check "$WORK/queued/Tomograms/job001/job.star" job
check "$WORK/queued/Tomograms/job001/job.star" joboptions_values
check "$WORK/queued/Tomograms/job001/RELION_OUTPUT_NODES.star" output_nodes
if grep -q 'Scheduled' "$WORK/queued/default_pipeline.star"; then
    echo "  ok   reserved jobs are Scheduled"
else
    echo "  FAIL reserved jobs are not marked Scheduled"; status=1
fi

echo
echo "== direction 2: RELION writes, the agent reads =="
"$WORK/write_single" "$WORK/one.star" > /dev/null
"$WORK/write_multi" "$WORK/two.star" > /dev/null
PYTHONPATH="$BACKEND" "$PYTHON" - "$WORK" <<'PY'
import sys
from pathlib import Path

from relion_webui import pipeline as pipeline_module

work = Path(sys.argv[1])
expected = {
    'one.star': (7, ['Tomograms/job006']),
    'two.star': (9, ['Tomograms/job007', 'Denoise/job008']),
}
failed = False
for name, (counter, names) in expected.items():
    result = pipeline_module.read_pipeline_file(work / name)
    actual = [process.name for process in result.processes]
    ok = result.job_counter == counter and actual == names
    tag = 'ok  ' if ok else 'FAIL'
    print(f'  {tag} {name:<12} counter={result.job_counter} jobs={actual}')
    failed = failed or not ok
raise SystemExit(1 if failed else 0)
PY
status=$?

echo
if [ "$status" -eq 0 ]; then
    echo "compatible in both directions"
else
    echo "INCOMPATIBLE - see the failures above"
fi
exit "$status"
