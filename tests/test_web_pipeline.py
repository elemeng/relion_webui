"""Tests for RELION pipeline compatibility.

These check the property the user asked for: a project created by RELION can be
loaded, new jobs can be submitted into it, and the official GUI can read the
result back. The GUI itself is not invoked; instead the pipeline file is
re-parsed and its structure checked against the layout documented in
``src/pipeliner.cpp``.
"""

import asyncio
from pathlib import Path

import pandas as pd
import pytest
import starfile

from recryo import job_artifacts, job_commands, job_specs, pipeline
from recryo.jobs import JobManager
from recryo.paths import PathGuard


def write_reference_pipeline(project: Path, *, job_counter: int = 8) -> Path:
    """Write a pipeline shaped like one the official GUI produces.

    Mirrors ``PipeLine::write``: five tables, the GUI's column names, and the
    status labels from ``procstatus_type2label``.
    """
    project.mkdir(parents=True, exist_ok=True)
    path = project / pipeline.PIPELINE_FILENAME
    text = f"""
# version 50001

data_pipeline_general

loop_
_rlnPipeLineJobCounter #1
{job_counter}

data_pipeline_processes

loop_
_rlnPipeLineProcessName #1
_rlnPipeLineProcessAlias #2
_rlnPipeLineProcessTypeLabel #3
_rlnPipeLineProcessStatusLabel #4
Import/job001 None relion.importtomo Succeeded
AlignTiltSeries/job004 None relion.aligntiltseries Succeeded
Tomograms/job007 None relion.reconstructtomograms Succeeded

data_pipeline_nodes

loop_
_rlnPipeLineNodeName #1
_rlnPipeLineNodeTypeLabel #2
_rlnPipeLineNodeTypeLabelDepth #3
Import/job001/tilt_series.star TomogramGroupMetadata.star.relion.tomo.import 1
AlignTiltSeries/job004/aligned_tilt_series.star TomogramGroupMetadata.star.relion.tomo.aligntiltseries 1
Tomograms/job007/tomograms.star TomogramGroupMetadata.star.relion.tomo.reconstruct 1

data_pipeline_input_edges

loop_
_rlnPipeLineEdgeFromNode #1
_rlnPipeLineEdgeProcess #2
Import/job001/tilt_series.star AlignTiltSeries/job004
AlignTiltSeries/job004/aligned_tilt_series.star Tomograms/job007

data_pipeline_output_edges

loop_
_rlnPipeLineEdgeProcess #1
_rlnPipeLineEdgeToNode #2
Import/job001 Import/job001/tilt_series.star
AlignTiltSeries/job004 AlignTiltSeries/job004/aligned_tilt_series.star
Tomograms/job007 Tomograms/job007/tomograms.star
"""
    path.write_text(text.lstrip(), encoding='utf-8')
    return path


@pytest.fixture
def project(tmp_path):
    write_reference_pipeline(tmp_path, job_counter=8)
    (tmp_path / 'tilt_series.star').write_text('# placeholder\n')
    return tmp_path


# ---------------------------------------------------------------- reading
def test_read_existing_project(project):
    result = pipeline.read_pipeline(project)
    assert result.job_counter == 8
    assert [p.name for p in result.processes] == [
        'Import/job001',
        'AlignTiltSeries/job004',
        'Tomograms/job007',
    ]
    assert result.find_process('Tomograms/job007').type_label == (
        'relion.reconstructtomograms'
    )
    assert result.find_process('Tomograms/job007').status == 'Succeeded'
    assert len(result.nodes) == 3
    assert len(result.input_edges) == 2
    assert len(result.output_edges) == 3


def test_next_job_number_continues_counter(project):
    result = pipeline.read_pipeline(project)
    assert result.next_job_number() == 8


def test_job_number_parsing(project):
    result = pipeline.read_pipeline(project)
    assert result.job_number_of('Tomograms/job007') == 7
    assert result.job_number_of('Weird/name') == 0


def test_missing_pipeline_is_reported(tmp_path):
    with pytest.raises(FileNotFoundError):
        pipeline.read_pipeline(tmp_path)


# ---------------------------------------------------------------- writing
def test_write_then_read_roundtrip(project):
    original = pipeline.read_pipeline(project)
    pipeline.write_pipeline(project, original)
    again = pipeline.read_pipeline(project)
    assert again.job_counter == original.job_counter
    assert [p.name for p in again.processes] == [p.name for p in original.processes]
    assert [p.status for p in again.processes] == [p.status for p in original.processes]
    assert [n.name for n in again.nodes] == [n.name for n in original.nodes]
    assert again.input_edges == original.input_edges
    assert again.output_edges == original.output_edges


def test_written_pipeline_uses_gui_column_names(project):
    p = pipeline.read_pipeline(project)
    pipeline.write_pipeline(project, p)
    star = starfile.read(project / pipeline.PIPELINE_FILENAME, always_dict=True)
    assert set(star) == {
        'pipeline_general',
        'pipeline_processes',
        'pipeline_nodes',
        'pipeline_input_edges',
        'pipeline_output_edges',
    }
    processes = star['pipeline_processes']
    assert list(processes.columns) == [
        'rlnPipeLineProcessName',
        'rlnPipeLineProcessAlias',
        'rlnPipeLineProcessTypeLabel',
        'rlnPipeLineProcessStatusLabel',
    ]
    nodes = star['pipeline_nodes']
    assert list(nodes.columns) == [
        'rlnPipeLineNodeName',
        'rlnPipeLineNodeTypeLabel',
        'rlnPipeLineNodeTypeLabelDepth',
    ]


def test_written_statuses_are_gui_labels(project):
    p = pipeline.read_pipeline(project)
    pipeline.update_process_status(p, 'Import/job001', pipeline.STATUS_ABORTED)
    pipeline.write_pipeline(project, p)
    again = pipeline.read_pipeline(project)
    assert again.find_process('Import/job001').status == 'Aborted'
    assert again.find_process('Import/job001').status in pipeline.VALID_STATUSES


def test_invalid_status_is_rejected(project):
    p = pipeline.read_pipeline(project)
    with pytest.raises(ValueError):
        pipeline.update_process_status(p, 'Import/job001', 'Finished')


def test_register_process_advances_counter(project):
    p = pipeline.read_pipeline(project)
    pipeline.register_process(
        p,
        pipeline.Process(
            name='ReconstructTomograms/job008',
            type_label='relion.reconstructtomograms',
            status=pipeline.STATUS_RUNNING,
        ),
        output_nodes=[
            pipeline.Node(
                'ReconstructTomograms/job008/tomograms.star',
                'TomogramGroupMetadata.star.relion.tomo.reconstruct',
            )
        ],
    )
    assert p.job_counter == 9
    assert p.find_process('ReconstructTomograms/job008') is not None
    assert p.find_node('ReconstructTomograms/job008/tomograms.star') is not None
    assert (
        'ReconstructTomograms/job008',
        'ReconstructTomograms/job008/tomograms.star',
    ) in p.output_edges


def test_duplicate_process_is_refused(project):
    p = pipeline.read_pipeline(project)
    with pytest.raises(ValueError):
        pipeline.register_process(
            p,
            pipeline.Process(name='Import/job001', type_label='relion.importtomo'),
        )


def test_lock_is_held_and_released(project):
    with pipeline.pipeline_lock(project):
        assert (project / pipeline.LOCK_DIRECTORY).is_dir()
    assert not (project / pipeline.LOCK_DIRECTORY).exists()


def test_lock_blocks_concurrent_writer(project):
    with pipeline.pipeline_lock(project):
        with pytest.raises(pipeline.PipelineLockedError):
            with pipeline.pipeline_lock(project, timeout=0.2):
                pass


# ------------------------------------------------------------ job.star
def test_job_star_matches_gui_schema(tmp_path):
    job_dir = tmp_path / 'ReconstructTomograms' / 'job008'
    path = job_artifacts.write_job_star(
        job_dir,
        'relion.reconstructtomograms',
        {'in_tiltseries': 'tilt_series.star', 'xdim': 4000, 'do_fourier': 'true'},
    )
    star = starfile.read(path, always_dict=True)
    assert 'job' in star and 'joboptions_values' in star
    header = star['job']
    assert header.iloc[0]['rlnJobTypeLabel'] == 'relion.reconstructtomograms'
    assert int(header.iloc[0]['rlnJobIsTomo']) == 1

    values = star['joboptions_values']
    assert list(values.columns) == ['rlnJobOptionVariable', 'rlnJobOptionValue']
    mapping = dict(zip(values['rlnJobOptionVariable'], values['rlnJobOptionValue']))
    assert mapping['in_tiltseries'] == 'tilt_series.star'
    assert mapping['xdim'] == '4000'


def test_job_star_quotes_values_with_spaces(tmp_path):
    path = job_artifacts.write_job_star(
        tmp_path / 'job', 'relion.reconstructtomograms', {'other_args': '--a 1 --b 2'}
    )
    text = path.read_text()
    assert '"--a 1 --b 2"' in text
    # And it must parse back to the original value.
    star = starfile.read(path, always_dict=True)
    values = dict(
        zip(
            star['joboptions_values']['rlnJobOptionVariable'],
            star['joboptions_values']['rlnJobOptionValue'],
        )
    )
    assert values['other_args'] == '--a 1 --b 2'


def test_output_nodes_file(tmp_path):
    path = job_artifacts.write_output_nodes(
        tmp_path / 'job',
        [('ReconstructTomograms/job008/tomograms.star', 'TomogramGroupMetadata.star.relion.tomo.reconstruct')],
    )
    star = starfile.read(path, always_dict=True)
    # RELION reads this file as `output_nodes`; any other table name is
    # silently ignored by getOutputNodesFromStarFile.
    assert 'output_nodes' in star
    nodes = star['output_nodes']
    assert nodes.iloc[0]['rlnPipeLineNodeName'] == (
        'ReconstructTomograms/job008/tomograms.star'
    )
    assert nodes.iloc[0]['rlnPipeLineNodeTypeLabel'] == (
        'TomogramGroupMetadata.star.relion.tomo.reconstruct'
    )


def test_output_nodes_omitted_when_empty(tmp_path):
    assert job_artifacts.write_output_nodes(tmp_path / 'job', []) is None


def test_option_renaming():
    remapped = job_artifacts.remap_job_options(
        {'t': 'in.star', 'w': 1000, 'unknown': 'kept'},
        {'t': 'in_tiltseries', 'w': 'xdim'},
    )
    assert remapped == {'in_tiltseries': 'in.star', 'xdim': 1000, 'unknown': 'kept'}


# ------------------------------------------------------------ run.out
def test_run_out_read_back(tmp_path):
    """The only log a finished job has; the Log tab reads it from here."""
    (tmp_path / 'run.out').write_text('step 1\nstep 2\n', encoding='utf-8')
    assert job_artifacts.read_run_out(tmp_path) == 'step 1\nstep 2\n'


def test_run_out_absent_is_none(tmp_path):
    assert job_artifacts.read_run_out(tmp_path) is None


def test_run_out_truncates_to_the_tail(tmp_path):
    """A long log keeps its end, where the error is, and says what it dropped."""
    filler = 'x' * 400
    lines = [f'{index:06d} {filler}' for index in range(2000)]
    (tmp_path / 'run.out').write_text('\n'.join(lines), encoding='utf-8')
    text = job_artifacts.read_run_out(tmp_path)
    assert text.startswith('[... ')
    assert 'bytes omitted ...]' in text
    assert text.rstrip().endswith(f'001999 {filler}')
    assert len(text.encode()) < job_artifacts.LOG_TAIL_BYTES + 200


def test_run_out_survives_a_truncated_utf8_sequence(tmp_path):
    """A job killed mid-write must still show its log, not a decode error."""
    # One complete line, then half of a multi-byte character.
    (tmp_path / 'run.out').write_bytes(b'ok\n' + 'é'.encode()[:1])
    text = job_artifacts.read_run_out(tmp_path)
    assert text.startswith('ok\n')
    assert text.count('\ufffd') == 1


def test_write_touches_pipeline_has_changed(project):
    """The GUI watches this marker to refresh without waiting for its poll."""
    marker = project / pipeline.PIPELINE_HAS_CHANGED_FILENAME
    if marker.exists():
        marker.unlink()
    p = pipeline.read_pipeline(project)
    pipeline.write_pipeline(project, p)
    assert marker.exists()


def test_marker_name_matches_relion():
    # From PIPELINE_HAS_CHANGED in src/pipeliner.h.
    assert pipeline.PIPELINE_HAS_CHANGED_FILENAME == '.pipeline_has_changed'


def test_mini_pipeline_contains_job_nodes_and_edges(tmp_path):
    """Each job directory gets ``<jobname>job_pipeline.star`` like RELION's."""
    job_dir = tmp_path / 'ReconstructTomograms' / 'job010'
    job_dir.mkdir(parents=True)
    process = pipeline.Process(
        'ReconstructTomograms/job010', 'None', 'relion.reconstructtomograms',
        pipeline.STATUS_RUNNING,
    )
    input_node = pipeline.Node(
        'tilt_series.star', 'TomogramGroupMetadata.star.relion.tomo.import'
    )
    output_node = pipeline.Node(
        'ReconstructTomograms/job010/tomograms.star',
        'TomogramGroupMetadata.star.relion.tomo.reconstruct',
    )
    path = pipeline.write_mini_pipeline(
        job_dir, process, input_nodes=[input_node], output_nodes=[output_node]
    )

    # The GUI names it after the job's last path component.
    assert path is not None
    assert path.name == 'job010job_pipeline.star'

    star = starfile.read(path, always_dict=True)
    assert star['pipeline_processes'].iloc[0]['rlnPipeLineProcessName'] == (
        'ReconstructTomograms/job010'
    )
    assert len(star['pipeline_nodes']) == 2
    assert star['pipeline_input_edges'].iloc[0]['rlnPipeLineEdgeFromNode'] == (
        'tilt_series.star'
    )
    assert star['pipeline_output_edges'].iloc[0]['rlnPipeLineEdgeToNode'] == (
        'ReconstructTomograms/job010/tomograms.star'
    )


def test_mini_pipeline_skipped_without_directory(tmp_path):
    assert pipeline.write_mini_pipeline(tmp_path / 'missing', pipeline.Process('X/job001')) is None


def test_column_indices_are_sequential(tmp_path):
    """STAR column markers must count 1,2,3..., which RELION's writer does.

    Duplicated ``#1`` markers parsed fine in practice but produced files that
    differ from what the GUI emits, so they are checked structurally.
    """
    from recryo import job_artifacts as artifacts

    proj = tmp_path / 'proj'
    proj.mkdir()
    p = pipeline.Pipeline(job_counter=2)
    pipeline.register_process(
        p,
        pipeline.Process(
            'ReconstructTomograms/job001', 'None', 'relion.reconstructtomograms',
            pipeline.STATUS_SUCCEEDED,
        ),
        input_nodes=[pipeline.Node('in.star', 'TomogramGroupMetadata.star.relion.tomo.import')],
        output_nodes=[
            pipeline.Node(
                'ReconstructTomograms/job001/tomograms.star',
                'TomogramGroupMetadata.star.relion.tomo.reconstruct',
            )
        ],
    )
    pipeline.write_pipeline(proj, p)
    artifacts.write_job_star(
        proj / 'ReconstructTomograms' / 'job001',
        'relion.reconstructtomograms',
        {'in_tiltseries': 'in.star'},
    )
    artifacts.write_output_nodes(
        proj / 'ReconstructTomograms' / 'job001',
        [('ReconstructTomograms/job001/tomograms.star', 'TomogramGroupMetadata.star.relion.tomo.reconstruct')],
    )

    for path in (
        proj / pipeline.PIPELINE_FILENAME,
        proj / 'ReconstructTomograms' / 'job001' / 'job.star',
        proj / 'ReconstructTomograms' / 'job001' / 'RELION_OUTPUT_NODES.star',
    ):
        text = path.read_text()
        # Reset the counter at every loop_ and check the run is 1..n.
        current = 0
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith('loop_'):
                current = 0
                continue
            if stripped.startswith('_'):
                current += 1
                assert stripped.endswith(f'#{current}'), (path.name, stripped)




def test_node_directory_grouping():
    assert job_artifacts.node_directory_name(
        'TomogramGroupMetadata.star.relion.tomo.reconstruct',
        'ReconstructTomograms/job008/tomograms.star',
    ) == 'TomogramGroupMetadata/ReconstructTomograms/job008/tomograms.star'


# ------------------------------------------------------- job specs
def test_all_specs_have_gui_labels():
    for spec in job_specs.JOB_SPECS.values():
        # ModelAngelo's label really is bare "modelangelo" in pipeline_jobs.h.
        assert spec.process_type_label
        assert spec.directory_name
        # External jobs are the one case the GUI knows nothing about; every
        # other job has at least one output the GUI can route.
        if spec.job_type != 'external':
            assert spec.output_nodes


def test_process_name_uses_the_gui_directory():
    """The directory comes from PROC_*_DIRNAME, not from the job's own name.

    Reconstruct-tomograms writes to ``Tomograms/``, which is easy to get wrong
    and makes the GUI's cleanup and undelete logic look in the wrong place.
    """
    spec = job_specs.get_spec('tomo_reconstruct_tomograms')
    assert job_specs.process_name_for(spec, 13) == 'Tomograms/job013'
    assert spec.directory_name == 'Tomograms'
    assert spec.process_type_label == 'relion.reconstructtomograms'


def test_every_job_has_a_pipeline_spec():
    """A job the UI offers must be registrable in a project."""
    for job_type in job_commands.JOB_ORDER:
        spec = job_specs.get_spec(job_type)
        assert spec.process_type_label
        assert spec.directory_name
        assert spec.executable


def test_unknown_spec_is_rejected():
    with pytest.raises(KeyError):
        job_specs.get_spec('not_a_job')


# ------------------------------------------- end-to-end submission
def test_submit_registers_job_in_existing_project(project, tmp_path):
    """The core requirement: submit into a real project and read it back."""
    executable = project / 'fake_relion_reconstruct'
    executable.write_text(
        '#!/bin/sh\n'
        # The job directory is the value after --pipeline_control, which is not
        # at a fixed position because the program's own flags come first.
        'prev=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "--pipeline_control" ]; then ctl="$a"; fi\n'
        '  prev="$a"\n'
        'done\n'
        'echo reconstructed\n'
        'touch "$ctl/RELION_JOB_EXIT_SUCCESS"\n'
    )
    executable.chmod(0o755)

    manager = JobManager(
        PathGuard([project]),
        executable_overrides={'tomo_reconstruct_tomograms': str(executable)},
    )
    if True:
        async def run():
            job = await manager.submit(
                'tomo_reconstruct_tomograms',
                {
                    'in_tiltseries': 'tilt_series.star',
                    'xdim': 4000,
                    'ydim': 4000,
                    'zdim': 2000,
                    'binned_angpix': 10,
                },
                project / 'Tomograms' / 'job008',
                project_directory=project,
            )
            for _ in range(200):
                if job.status != 'running':
                    break
                await asyncio.sleep(0.05)
            return job

        job = asyncio.run(run())
        asyncio.run(manager.shutdown())

        assert job.status == 'succeeded', job.as_dict()
        assert job.process_name == 'Tomograms/job008'

        # 1. The GUI can see the job and its final status.
        reloaded = pipeline.read_pipeline(project)
        process = reloaded.find_process('Tomograms/job008')
        assert process is not None
        assert process.type_label == 'relion.reconstructtomograms'
        assert process.status == pipeline.STATUS_SUCCEEDED
        assert reloaded.job_counter == 9

        # 2. The old jobs are untouched.
        assert reloaded.find_process('Tomograms/job007').status == 'Succeeded'

        # 3. The input edge connects the existing tilt-series node.
        assert (
            'tilt_series.star',
            'Tomograms/job008',
        ) in reloaded.input_edges

        # 4. The job directory carries job.star for the GUI job window.
        job_dir = project / 'Tomograms' / 'job008'
        assert (job_dir / 'job.star').is_file()
        star = starfile.read(job_dir / 'job.star', always_dict=True)
        assert star['job'].iloc[0]['rlnJobTypeLabel'] == 'relion.reconstructtomograms'
        options = dict(
            zip(
                star['joboptions_values']['rlnJobOptionVariable'],
                star['joboptions_values']['rlnJobOptionValue'],
            )
        )
        assert options['in_tiltseries'] == 'tilt_series.star'
        assert options['xdim'] == '4000'
        # Booleans are stored the way JobOption does, because getBoolean reads
        # "Yes"; writing "true" here would make the GUI see every flag as off.
        assert options['do_fourier'] == 'Yes'

        # 5. Output nodes are recorded for the GUI to re-check completion.
        assert (job_dir / 'RELION_OUTPUT_NODES.star').is_file()


def test_failed_job_is_recorded_as_failed(project):
    executable = project / 'fail_job'
    executable.write_text('#!/bin/sh\nexit 1\n')
    executable.chmod(0o755)

    manager = JobManager(
        PathGuard([project]),
        executable_overrides={'tomo_reconstruct_tomograms': str(executable)},
    )
    if True:
        async def run():
            job = await manager.submit(
                'tomo_reconstruct_tomograms',
                {
                    'in_tiltseries': 'tilt_series.star',
                    'xdim': 4000,
                    'ydim': 4000,
                    'zdim': 2000,
                },
                project / 'Tomograms' / 'job008',
                project_directory=project,
            )
            for _ in range(200):
                if job.status != 'running':
                    break
                await asyncio.sleep(0.05)
            return job

        job = asyncio.run(run())
        asyncio.run(manager.shutdown())

        reloaded = pipeline.read_pipeline(project)
        process = reloaded.find_process('Tomograms/job008')
        assert process.status == pipeline.STATUS_FAILED


# ------------------------------------------------------- reading RELION's files
# RELION's MetaDataTable::write has two shapes, and the reader has to accept
# both. `setIsList(true)` emits a single object as bare repeated `_label value`
# lines with no `loop_`; the normal form emits `loop_` with one row per object.
# A project with one job therefore looks different from one with several.
RELION_SINGLE_OBJECT = """
# version 50001

data_pipeline_general

_rlnPipeLineJobCounter                       7
 

# version 50001

data_pipeline_processes

_rlnPipeLineProcessName                   Tomograms/job006
_rlnPipeLineProcessAlias                  None
_rlnPipeLineProcessTypeLabel              relion.reconstructtomograms
_rlnPipeLineProcessStatusLabel            Succeeded
 

# version 50001

data_pipeline_nodes

_rlnPipeLineNodeName                      Tomograms/job006/tomograms.star
_rlnPipeLineNodeTypeLabel                 TomogramGroupMetadata.star.relion.tomo.reconstruct
_rlnPipeLineNodeTypeLabelDepth            1
 

# version 50001

data_pipeline_input_edges

_rlnPipeLineEdgeFromNode                  tilt_series.star
_rlnPipeLineEdgeProcess                   Tomograms/job006
 

# version 50001

data_pipeline_output_edges

_rlnPipeLineEdgeProcess                   Tomograms/job006
_rlnPipeLineEdgeToNode                    Tomograms/job006/tomograms.star
"""

RELION_MULTI_OBJECT = """
# version 50001

data_pipeline_general

_rlnPipeLineJobCounter                       9
 

# version 50001

data_pipeline_processes

loop_ 
_rlnPipeLineProcessName #1 
_rlnPipeLineProcessAlias #2 
_rlnPipeLineProcessTypeLabel #3 
_rlnPipeLineProcessStatusLabel #4 
Tomograms/job007       None relion.reconstructtomograms  Succeeded 
Denoise/job008       None relion.reconstructtomograms  Succeeded
"""


def test_reads_relions_single_object_pipeline(tmp_path):
    """A one-job project is written without `loop_`; that must still parse."""
    path = tmp_path / 'default_pipeline.star'
    path.write_text(RELION_SINGLE_OBJECT)
    result = pipeline.read_pipeline_file(path)
    assert result.job_counter == 7
    assert [p.name for p in result.processes] == ['Tomograms/job006']
    process = result.find_process('Tomograms/job006')
    assert process.type_label == 'relion.reconstructtomograms'
    assert process.status == 'Succeeded'
    assert result.find_node('Tomograms/job006/tomograms.star') is not None
    assert result.input_edges == [('tilt_series.star', 'Tomograms/job006')]


def test_reads_relions_multi_object_pipeline(tmp_path):
    """Several jobs come back as a `loop_`; both shapes must work."""
    path = tmp_path / 'default_pipeline.star'
    path.write_text(RELION_MULTI_OBJECT)
    result = pipeline.read_pipeline_file(path)
    assert result.job_counter == 9
    assert [p.name for p in result.processes] == [
        'Tomograms/job007',
        'Denoise/job008',
    ]


def test_next_job_number_continues_relions_counter(tmp_path):
    """A job submitted into a RELION project must not reuse its numbers."""
    path = tmp_path / 'default_pipeline.star'
    path.write_text(RELION_SINGLE_OBJECT)
    result = pipeline.read_pipeline_file(path)
    assert result.next_job_number() == 7


def test_our_output_nodes_table_is_what_relion_reads(tmp_path):
    """getOutputNodesFromStarFile reads `output_nodes`; other names are ignored."""
    path = job_artifacts.write_output_nodes(
        tmp_path / 'job',
        [('Tomograms/job001/tomograms.star', 'TomogramGroupMetadata.star.relion.tomo.reconstruct')],
    )
    star = starfile.read(path, always_dict=True)
    assert 'output_nodes' in star
    assert 'pipeline_nodes' not in star


def test_note_is_beside_the_job_directory(tmp_path):
    """The GUI builds `<process name>note.txt`, and a process name has no slash."""
    job_dir = tmp_path / 'Tomograms' / 'job001'
    job_dir.mkdir(parents=True)
    path = job_artifacts.write_notes(job_dir, 'relion.reconstructtomograms', ['cmd'])
    assert path == tmp_path / 'Tomograms' / 'job001note.txt'
    assert not (job_dir / 'note.txt').exists()
