"""The generated schema must match RELION's C++ sources.

``generated_schema.py`` is produced from ``src/pipeline_jobs.cpp`` and
``src/gui_jobwindow.cpp``. These tests re-run that extraction and compare, so a
RELION change that renames an option, alters a default, or moves a field to
another tab fails here instead of producing a job that silently ignores a value.

They also pin the properties the web layer relies on: every job has a pipeline
identity, every command spec's required options exist, and the option metadata
is complete enough for the form renderer.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

from _relion_source import relion_source

WEBUI_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = WEBUI_ROOT / 'scripts' / 'generate_web_job_schema.py'

from recryo import job_commands, job_specs  # noqa: E402
from recryo.generated_schema import (  # noqa: E402
    FIELD_COUNTS,
    JOBS,
)

# The options RelionJob::initialise adds to every job; a form showing only these
# is showing nothing of its own.
RUN_TAB_FIELDS = {
    'nr_mpi', 'nr_threads', 'do_queue', 'queuename', 'qsub', 'qsubscript',
    'min_dedicated', 'other_args',
}


def _load_generator():
    spec = importlib.util.spec_from_file_location('relion_web_schema_generator', GENERATOR)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def generator():
    if not GENERATOR.is_file():
        pytest.skip(f'{GENERATOR} is missing')
    if relion_source() is None:
        pytest.skip('no RELION checkout; set RELION_SOURCE to compare against it')
    return _load_generator()


def test_generated_schema_is_up_to_date(generator):
    """Re-extract and compare; the file must be what the generator would write."""
    root = generator.resolve_source_root(relion_source())
    source = generator._strip_comments(
        (root / 'src' / 'pipeline_jobs.cpp').read_text(encoding='utf-8')
    )
    source_h = (root / 'src' / 'pipeline_jobs.h').read_text(encoding='utf-8')
    window_source = generator._strip_comments(
        (root / 'src' / 'gui_jobwindow.cpp').read_text(encoding='utf-8')
    )
    expected = generator.render(
        generator.extract_options(source, source_h),
        generator.extract_layout(window_source),
        generator.extract_pipeline_constants(source_h),
    )
    actual = generator.OUTPUT.read_text(encoding='utf-8')
    assert actual == expected, (
        'generated_schema.py is stale; run '
        'python scripts/generate_web_job_schema.py'
    )


def test_every_job_has_options():
    for job_type, job in JOBS.items():
        assert job['fields'], f'{job_type} has no extracted options'
        assert job['title']


def test_field_counts_match_the_fields():
    for job_type, job in JOBS.items():
        assert FIELD_COUNTS[job_type] == len(job['fields'])


def test_fields_carry_what_a_form_needs():
    """Every field needs a variable, a label and a known widget kind."""
    kinds = {'slider', 'boolean', 'string', 'path', 'input_node', 'radio'}
    for job_type, job in JOBS.items():
        for field in job['fields']:
            assert field['variable'], job_type
            assert field['label'], (job_type, field['variable'])
            assert field['kind'] in kinds, (job_type, field['kind'])
            if field['kind'] == 'slider':
                # A slider without bounds cannot be rendered as a range.
                assert {'min', 'max', 'step'} <= set(field), (job_type, field)
            if field['kind'] == 'radio':
                assert field['choices'], job_type
                assert field['default'] in field['choices'], (job_type, field)


def test_layout_only_names_real_fields():
    """A tab may not reference a field the job does not have."""
    for job_type, job in JOBS.items():
        known = {field['variable'] for field in job['fields']}
        for tab in job['tabs']:
            for variable in tab['fields']:
                assert variable in known, (job_type, tab['title'], variable)


def test_place_tomo_input_is_expanded():
    """``placeTomoInput`` becomes real fields, not a placeholder.

    The GUI expands this helper into the optimisation set plus the individual
    components the job accepts (gui_jobwindow.cpp:2558). Keeping a placeholder
    hid ``in_optimisation`` from the form, so the tomography refine jobs had no
    way to name their input.
    """
    for job_type in ('tomo_subtomo', 'tomo_ctf_refine', 'tomo_align_frame',
                     'tomo_recon_particle', 'inimodel', 'class3d', 'autorefine'):
        variables = {field['variable'] for field in JOBS[job_type]['fields']}
        assert 'in_optimisation' in variables, job_type
        placed = {
            variable
            for tab in JOBS[job_type]['tabs']
            for variable in tab['fields']
        }
        assert 'in_optimisation' in placed, job_type


def test_every_job_places_its_own_options():
    """A job's form must show its options, not only the shared Run tab.

    ``generate_web_job_schema`` finds a job's window by name. When the C++
    abbreviates that name (``initialiseLocresWindow``,
    ``initialiseTomoReconParWindow``) the lookup silently returned nothing and
    the whole form collapsed to the Run tab, which looks like a job with no
    options rather than a broken extraction.
    """
    for job_type, job in JOBS.items():
        own = [
            variable
            for tab in job['tabs']
            for variable in tab['fields']
            if variable not in RUN_TAB_FIELDS
        ]
        assert own, f'{job_type} places no options of its own'


def test_pipeline_identity_matches_the_gui_constants():
    """Directory and label come from the C++ defines, not from the job name."""
    expected = {
        'tomo_reconstruct_tomograms': ('Tomograms', 'relion.reconstructtomograms'),
        'tomo_align_tilt_series': ('AlignTiltSeries', 'relion.aligntiltseries'),
        'tomo_denoise_tomograms': ('Denoise', 'relion.denoisetomo'),
        'tomo_pick_tomograms': ('Picks', 'relion.picktomo'),
        'tomo_subtomo': ('Extract', 'relion.pseudosubtomo'),
        'tomo_import': ('Import', 'relion.importtomo'),
        'tomo_recon_particle': ('Reconstruct', 'relion.reconstructparticletomo'),
        'tomo_ctf_refine': ('CtfRefine', 'relion.ctfrefinetomo'),
        'tomo_align_frame': ('Polish', 'relion.framealigntomo'),
        'motioncorr': ('MotionCorr', 'relion.motioncorr'),
        'ctffind': ('CtfFind', 'relion.ctffind'),
    }
    for job_type, (directory, label) in expected.items():
        assert JOBS[job_type]['directory'] == directory, job_type
        assert JOBS[job_type]['process_label'] == label, job_type


def test_every_command_spec_has_a_matching_schema_entry():
    """Every spec resolves to an option table.

    A variant has no entry of its own: it reports its base job's schema, limited
    to its branch's fields, so the comparison is on base job types.
    """
    assert {job_commands.base_job_type(name) for name in job_commands.COMMAND_SPECS} == set(JOBS)


def test_variants_share_their_base_schema():
    """A variant's form keeps the base job's option metadata."""
    for base, variants in job_commands.BRANCH_VARIANTS.items():
        for variant in variants:
            job_type = str(variant['job_type'])
            assert job_commands.base_job_type(job_type) == base
            known = {field['variable'] for field in JOBS[base]['fields']}
            declared = set(job_commands.variant_fields(job_type) or ())
            assert declared <= known, (
                f'{job_type} uses options the base job does not have: '
                f'{sorted(declared - known)}'
            )


def test_required_options_exist_in_the_schema():
    for job_type, spec in job_commands.COMMAND_SPECS.items():
        base = job_commands.base_job_type(job_type)
        variables = {field['variable'] for field in JOBS[base]['fields']}
        missing = [name for name in spec.required if name not in variables]
        assert not missing, f'{job_type} requires unknown options: {missing}'
        hidden = [name for name in spec.internal if name not in variables]
        assert not hidden, f'{job_type} marks unknown options internal: {hidden}'


def test_every_job_has_a_pipeline_spec():
    for job_type in job_commands.JOB_ORDER:
        assert job_type in job_specs.JOB_SPECS
        spec = job_specs.get_spec(job_type)
        assert spec.executable
        assert spec.process_type_label
        assert spec.directory_name


def test_job_order_covers_every_job():
    assert set(job_commands.JOB_ORDER) == set(job_commands.COMMAND_SPECS)
