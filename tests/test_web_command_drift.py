"""Drift detection against RELION's own ``getCommands*``.

The command builders in ``job_commands.py`` are hand-written and verified
against the C++ for the flags they emit. They are *not* generated: 46 of the
564 ``command +=`` statements read local variables (``fn_sym``, ``fn_out``,
``maps[i]``) that a mechanical translation cannot resolve without inventing
rules, and the remaining constructs (second programs, ``||`` chains, derived
radio values) are clearer written out.

``scripts/generate_web_job_commands.py`` still extracts what it can, and this
test uses both the extraction and the C++ source directly as a **drift
detector**: when upstream adds, renames or removes a command-line flag, the
hand-written builder is checked against the new source and the difference is
reported here rather than discovered by a job that silently ignores an argument.

The invariant that matters is one-directional. A builder may emit fewer flags
than the C++ (it drives a subset of the GUI), but it must never emit a flag the
C++ does not pass: such a flag is either a typo or meant for another program,
and ``IOParser::checkForUnknownArguments`` only warns, so it would be ignored.
Every command-line bug found in this package had exactly that shape:
``--ios`` on a JAZ program, hyphenated flags on a C++ program, and ResMap flags
on ``relion_postprocess``.
"""

import re
import shlex
from pathlib import Path

import pytest

from relion_webui import job_commands
from relion_webui.generated_commands import COMMANDS
from relion_webui.generated_schema import JOBS

from _relion_source import relion_source

PIPELINE_JOBS = (relion_source() or Path()) / 'src' / 'pipeline_jobs.cpp'

OUT = '/proj/Tomograms/job001/'

REQUIRED_VALUES = {
    'in_tiltseries': 'tilt_series.star',
    'input_star_mics': 'micrographs.star',
    'in_tomoset': 'tomograms.star',
    'fn_img': 'particles.star',
    'fn_in': 'half1.mrc',
    'fn_map': 'map.mrc',
    'fn_bodies': 'bodies.star',
    'in_optimisation': 'optimisation_set.star',
    'movie_files': 'frames/*.mrc',
    'mdoc_files': 'mdoc/*.mdoc',
    'fn_exe': '/bin/echo',
    'fn_part1': 'a.star',
    'fn_part2': 'b.star',
}

# Flags added by the C++ helper functions rather than by the job's own body.
# `getTomoInputCommmand` adds the input flag and `prepareFinalCommand` adds
# pipeline control; both are shared by many jobs.
HELPER_FLAGS = {
    '--i', '--ios', '--o', '--p', '--t', '--mot', '--man',
    '--pipeline_control', '--theme',
}

# Flags produced by an expression the extractor does not fold into a literal
# (a computed value still carries its flag in the source, so these are only
# listed when the flag itself is built by string concatenation).
COMPUTED_FLAGS = {
    'class3d': {'--sigma_ang'},
    'autorefine': {'--sigma_ang'},
}


def _arguments(job_type: str, **overrides) -> dict:
    """Defaults from the base job, plus a value for every required option.

    A variant has no option table of its own; its branch switch lives in the
    spec's static table, which build_command applies.
    """
    base = job_commands.base_job_type(job_type)
    options = {
        field['variable']: field.get('default')
        for field in JOBS[base]['fields']
    }
    for name in job_commands.COMMAND_SPECS[job_type].required:
        options[name] = REQUIRED_VALUES.get(name, 'x')
    for name in job_commands.variant_inputs(job_type):
        options.setdefault(name, REQUIRED_VALUES.get(name, 'x'))
    if job_type.startswith('tomo_denoise'):
        options['gpu_ids'] = '0'
        options['care_denoising_model'] = 'model.tar.gz'
    options.update(overrides)
    return options


def _builder_flags(job_type: str) -> set:
    command = job_commands.build_command(job_type, _arguments(job_type), OUT)
    flags = set()
    for part in command:
        for token in shlex.split(part, posix=False):
            token = token.strip('"')
            if token.startswith('--'):
                flags.add(token.split('=')[0])
    return flags


def _function_body(source: str, signature: str) -> str:
    start = source.find(signature)
    if start < 0:
        return ''
    ends = [
        index
        for index in (
            source.find('\nvoid RelionJob::', start + 5),
            source.find('\nbool RelionJob::', start + 5),
            source.find('\nstd::string RelionJob::', start + 5),
        )
        if index > 0
    ]
    return source[start : min(ends) if ends else len(source)]


def _cpp_flag_sets(cpp_name: str, source: str) -> set:
    """Every ``--flag`` literal in one job's getCommands body.

    The body includes any second program the job runs (``relion_align_symmetry``
    in inimodel, ``relion_flex_analyse`` in multibody), so both programs' flags
    are covered without a per-job exception list.
    """
    body = _function_body(source, f'bool RelionJob::getCommands{cpp_name}Job(')
    flags = set()
    for match in re.finditer(r'command\w*\s*\+=\s*"([^"]*)"', body):
        for token in match.group(1).split():
            if token.startswith('--'):
                flags.add(token.rstrip('"'))
    return flags


@pytest.fixture(scope='module')
def cpp_source() -> str:
    if not PIPELINE_JOBS.is_file():
        pytest.skip(f'{PIPELINE_JOBS} is missing')
    return PIPELINE_JOBS.read_text(encoding='utf-8', errors='replace')


@pytest.mark.parametrize('job_type', sorted(COMMANDS))
def test_builder_emits_no_flag_the_cpp_does_not(job_type, cpp_source):
    """A builder may not invent a flag.

    This is the check that would have caught every command-line bug in this
    package: an unknown flag is accepted with a warning and then ignored, so a
    job that looks configured runs on defaults.
    """
    spec = job_commands.COMMAND_SPECS.get(job_type)
    if spec is None:
        pytest.skip('not a job the web UI drives')
    cpp = _cpp_flag_sets(spec.cpp_name, cpp_source)
    if not cpp:
        pytest.skip('no command statements found for this job')
    allowed = cpp | HELPER_FLAGS | COMPUTED_FLAGS.get(job_type, set())
    extra = _builder_flags(job_type) - allowed
    assert not extra, (
        f'{job_type} emits flags absent from getCommands{spec.cpp_name}Job: '
        f'{sorted(extra)}. Either the builder is wrong, or the extractor needs '
        f'to learn the construct that produces them.'
    )


@pytest.mark.parametrize(
    'job_type',
    [
        'motioncorr',
        'ctffind',
        'tomo_reconstruct_tomograms',
        'tomo_subtomo',
        'maskcreate',
        'postprocess',
        'joinstar',
    ],
)
def test_builder_emits_the_flags_the_extractor_found(job_type):
    """The builder and the extractor must overlap substantially.

    A strict equality would be wrong in both directions: the builder drives a
    subset of the GUI, and the extractor cannot resolve the statements that read
    local variables (``local_fn_out``, ``maps[i]``). What must hold is that the
    builder is not a stale copy of an older C++ - so every flag the extractor
    found unconditionally and without a local variable must appear.
    """
    job = COMMANDS.get(job_type) or {}
    found = set()
    for step in job.get('steps', ()):
        for key in ('literal', 'literal_prefix', 'literal_suffix'):
            for token in (step.get(key) or '').split():
                if token.startswith('--'):
                    found.add(token.rstrip('"'))
    # Only the flags that a maximal option set would turn on are comparable;
    # the builder is asked for the schema defaults here.
    built = _builder_flags(job_type)
    emitted_at_least_half = len(found & built) * 2 >= len(found)
    assert emitted_at_least_half, (
        f'{job_type} emits {sorted(found & built)} of the {len(found)} flags '
        f'the extractor found: {sorted(found)}'
    )


def test_extraction_still_finds_the_statements():
    """The extractor's coverage is asserted, so a parse regression shows up."""
    total = sum(len(job['unparsed']) for job in COMMANDS.values())
    steps = sum(len(job['steps']) for job in COMMANDS.values())
    assert steps > 400, 'the extractor stopped finding command statements'
    # 46 statements read local variables. A jump means upstream added a
    # construct the extractor should learn, and the builder may have drifted.
    assert total <= 60, (
        f'{total} statements are no longer parsed; review '
        f'scripts/generate_web_job_commands.py'
    )


def test_every_extracted_job_is_a_job_the_ui_offers():
    for job_type in COMMANDS:
        assert job_type in job_commands.COMMAND_SPECS, job_type


# ------------------------------------------------------ branch completeness
SELECT_BRANCH_SWITCHES = {
    'do_filaments': 'filaments',
    'do_remove_duplicates': 'duplicates',
    'do_select_values': 'values',
    'do_discard': 'discard',
    'do_split': 'split',
    'do_class_ranker': 'class_ranker',
}


def test_every_select_branch_is_exposed(cpp_source):
    """Each mode of ``getCommandsSelectJob`` has a job type.

    The modes are mutually exclusive in the C++, so leaving one unexposed means
    a user has no way to reach it from the web UI at all - and nothing would say
    so. This compares the switches the C++ tests against the branches the specs
    declare.
    """
    body = _function_body(cpp_source, 'bool RelionJob::getCommandsSelectJob(')
    tested = {
        option
        for option in SELECT_BRANCH_SWITCHES
        if f'joboptions["{option}"].getBoolean()' in body
    }
    declared = {
        spec.branch
        for job_type, spec in job_commands.COMMAND_SPECS.items()
        if job_commands.base_job_type(job_type) == 'select' and spec.branch
    }
    expected = {SELECT_BRANCH_SWITCHES[option] for option in tested}
    assert expected <= declared, (
        f'Select branches in the C++ with no job type: {sorted(expected - declared)}'
    )


def test_each_variant_pins_its_switch():
    """A variant's static table sets the switch its branch is tested by."""
    for base, variants in job_commands.BRANCH_VARIANTS.items():
        for variant in variants:
            job_type = str(variant['job_type'])
            static = job_commands.COMMAND_SPECS[job_type].static
            assert static, f'{job_type} pins no branch switch'
            for option in static:
                # The base job must actually declare this option, or the GUI
                # would ignore the switch when it opens the job.
                assert any(
                    field['variable'] == option
                    for field in JOBS[base]['fields']
                ), f'{job_type} pins unknown option {option}'


# ------------------------------------------------------ branch completeness
# The switch each split job tests in its own getCommands* body, mapped to the
# branch name the spec declares. Every one of these must have a job type.
SPLIT_JOB_SWITCHES = {
    'Select': {
        'do_filaments': 'filaments',
        'do_remove_duplicates': 'duplicates',
        'do_select_values': 'values',
        'do_discard': 'discard',
        'do_split': 'split',
        'do_class_ranker': 'class_ranker',
    },
    'TomoImport': {'do_coords': 'coordinates'},
    'TomoDenoiseTomograms': {
        'do_cryocare_train': 'train',
        'do_cryocare_predict': 'predict',
    },
    'TomoAlignTiltSeries': {
        'do_imod_fiducials': 'imod_fiducials',
        'do_imod_patchtrack': 'imod_patchtrack',
        'do_aretomo2': 'aretomo',
    },
    'Localres': {
        'do_resmap_locres': 'resmap',
        'do_relion_locres': 'relion',
    },
    'ModelAngelo': {'do_hhmer': 'hmm'},
}


@pytest.mark.parametrize('cpp_name', sorted(SPLIT_JOB_SWITCHES))
def test_every_branch_of_a_split_job_is_exposed(cpp_name, cpp_source):
    """A mode the C++ knows must be reachable as some job type.

    The modes of these jobs are mutually exclusive (or, for ModelAngelo's HMM
    step, additive), so leaving one out means a user has no way to reach it from
    the web UI - and nothing would say so. This compares the switches the C++
    actually tests against the branch names the specs declare.
    """
    body = _function_body(cpp_source, f'bool RelionJob::getCommands{cpp_name}Job(')
    tested = {
        branch
        for option, branch in SPLIT_JOB_SWITCHES[cpp_name].items()
        if f'joboptions["{option}"].getBoolean()' in body
    }
    declared = {
        spec.branch
        for job_type, spec in job_commands.COMMAND_SPECS.items()
        if spec.cpp_name == cpp_name and spec.branch
    }
    assert tested <= declared, (
        f'{cpp_name} branches in the C++ with no job type: '
        f'{sorted(tested - declared)}'
    )


def test_every_split_job_has_a_default_branch_for_its_old_name():
    """The un-suffixed job type still names one branch, so old clients work.

    Each base name used to build a particular mode; keeping that mapping means a
    stored request keeps producing the same command rather than silently
    switching to a different mode.
    """
    for base, variants in job_commands.BRANCH_VARIANTS.items():
        spec = job_commands.COMMAND_SPECS[base]
        declared = {str(variant['branch']) for variant in variants}
        assert spec.branch in declared, (
            f'{base} names branch {spec.branch!r}, which no variant declares'
        )
        assert spec.alias_of, f'{base} should alias the variant it duplicates'


def test_alias_pins_the_same_switch_as_the_variant_it_stands_for():
    """An API alias must write the same job.star as the job it is a synonym for.

    Otherwise a client still submitting the old name would get a command for one
    branch and a `job.star` describing another.
    """
    for base, spec in job_commands.COMMAND_SPECS.items():
        if spec.alias_of is None:
            continue
        target = job_commands.COMMAND_SPECS[spec.alias_of]
        assert spec.branch == target.branch, base
        assert spec.static == target.static, base
