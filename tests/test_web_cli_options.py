"""The advanced-argument panel must match what each program really accepts.

The panel's whole value is that it lists flags the GUI never shows, so a wrong
entry is worse than no entry: it looks like configuration and is silently
ignored. These tests pin three things:

* the generated flag list is in step with the parsers (regenerate on drift);
* every flag a job's builder emits is a flag the program declares, in the right
  spelling for that program's parser;
* the extra arguments reach the command as ``other_args``, which is where every
  builder already places them, and never as a second position in the argv.

The third point is the one that cannot be checked by reading the C++: it is a
property of this package's builders, and it failed before (a hyphen where the
C++ wants an underscore is accepted with a warning and then ignored).
"""

import importlib.util
import shlex
import sys
from pathlib import Path

import pytest

from _relion_source import relion_source

from relion_webui import cli_options, job_commands
from relion_webui.generated_cli_options import CLI_OPTIONS
from relion_webui.generated_schema import JOBS

WEBUI_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = WEBUI_ROOT / 'scripts' / 'generate_web_cli_options.py'

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


def _load_generator():
    spec = importlib.util.spec_from_file_location('relion_web_cli_generator', GENERATOR)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _arguments(job_type: str, **overrides) -> dict:
    """Defaults from the base job, plus a value for every required option.

    A variant has no option table of its own, so the base job's defaults are
    used and the variant's branch switch (in the spec's static table) is applied
    by build_command.
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


def _emitted_flags(job_type: str) -> set:
    command = job_commands.build_command(job_type, _arguments(job_type), OUT)
    flags = set()
    for part in command:
        for token in shlex.split(part, posix=False):
            token = token.strip('"')
            if token.startswith('--'):
                flags.add(token.split('=')[0])
    return flags


# ------------------------------------------------------------- generation
def test_generated_cli_options_are_up_to_date():
    """Re-extract and compare; the file must be what the generator would write."""
    if not GENERATOR.is_file():
        pytest.skip(f'{GENERATOR} is missing')
    if relion_source() is None:
        pytest.skip('no RELION checkout; set RELION_SOURCE to compare against it')
    generator = _load_generator()
    root = generator.resolve_source_root(relion_source())
    expected = generator.render(generator.collect(root))
    actual = generator.OUTPUT.read_text(encoding='utf-8')
    assert actual == expected, (
        'generated_cli_options.py is stale; run '
        'python scripts/generate_web_cli_options.py'
    )


def test_every_job_is_covered():
    """Every job resolves to a CLI table.

    A variant has none of its own: its branch runs one of the base job's
    programs, so it reports the base job's flags.
    """
    for job_type in job_commands.COMMAND_SPECS:
        base = job_commands.base_job_type(job_type)
        assert base in CLI_OPTIONS, f'{job_type} (base {base}) has no CLI option table'


def test_unavailable_jobs_say_why():
    for job_type, entry in CLI_OPTIONS.items():
        if entry['options']:
            continue
        assert entry.get('unavailable'), (
            f'{job_type} has no extracted options and no reason'
        )


def test_extracted_options_have_a_flag_and_a_kind():
    for job_type, entry in CLI_OPTIONS.items():
        for option in entry['options']:
            assert option['flag'].startswith('--'), (job_type, option)
            assert option['kind'] in ('boolean', 'value'), (job_type, option)
            assert option['program'], (job_type, option)


# ------------------------------------------------------------------ binding
@pytest.mark.parametrize('job_type', job_commands.JOB_ORDER)
def test_emitted_flags_are_declared(job_type):
    """A flag the builder emits must be one the program declares.

    The reverse does not hold and must not: a builder emits the subset the GUI
    exposes, and the panel exists precisely for the rest.
    """
    entry = CLI_OPTIONS.get(job_type) or {}
    if not entry.get('options') or entry.get('unavailable'):
        return
    declared = {option['flag'] for option in entry['options']}
    # Flags the C++ shared helpers add (the optimisation-set input, the theme
    # and output flags, pipeline control) are not in any single parser listing.
    shared = {
        '--i', '--ios', '--o', '--p', '--t', '--mot', '--man',
        '--theme', '--pipeline_control', '--continue', '--only_do_unfinished',
        '--j', '--gpu',
    }
    for flag in _emitted_flags(job_type):
        if flag in shared:
            continue
        assert flag in declared, (
            f'{job_type} emits {flag}, which its program does not declare'
        )


@pytest.mark.parametrize(
    'job_type, option, expected',
    [
        ('tomo_import_coordinates', 'in_coords', '--remove_substring'),
        ('motioncorr', 'do_own_motioncor', '--use_own'),
        ('tomo_reconstruct_tomograms', 'do_fourier', '--fourier'),
    ],
)
def test_flags_use_the_program_spelling(job_type, option, expected):
    """C++ programs take underscores even when the job's other program does not.

    ``tomo_import`` is the case that matters: its SerialEM branch is a Typer app
    (hyphens) while its coordinates branch is a C++ program (underscores).
    """
    flags = _emitted_flags(job_type)
    if job_type == 'tomo_import_coordinates':
        command = job_commands.build_command(
            job_type,
            _arguments(job_type, remove_substring='_rec'),
            OUT,
        )
        flags = {
            token.strip('"').split('=')[0]
            for part in command
            for token in shlex.split(part, posix=False)
            if token.startswith('--')
        }
    assert expected in flags


# ------------------------------------------------------- extra arguments
def test_extra_arguments_land_in_other_args():
    merged, unknown = cli_options.apply(
        'motioncorr',
        {'cli_extra': [{'flag': '--skip_logfile'}, {'flag': '--max_io_threads', 'value': '4'}]},
    )
    assert unknown == []
    assert merged['other_args'] == '--skip_logfile --max_io_threads 4'
    assert 'cli_extra' not in merged


def test_extra_arguments_append_to_an_existing_string():
    merged, _ = cli_options.apply(
        'motioncorr',
        {'other_args': '--skip_logfile', 'cli_extra': '--max_io_threads 4'},
    )
    assert merged['other_args'] == '--skip_logfile --max_io_threads 4'


def test_unknown_flag_is_reported_not_refused():
    merged, unknown = cli_options.apply('motioncorr', {'cli_extra': '--bogus 1'})
    assert unknown == ['--bogus']
    assert merged['other_args'] == '--bogus 1'


def test_a_stray_value_is_refused():
    with pytest.raises(ValueError):
        cli_options.apply('motioncorr', {'cli_extra': 'stray'})


def test_extra_arguments_reach_the_command():
    command = job_commands.build_command(
        'motioncorr',
        _arguments('motioncorr', cli_extra=[{'flag': '--skip_logfile'}]),
        OUT,
    )
    joined = ' '.join(command)
    assert '--skip_logfile' in joined
    # The flag must be last, which is what makes an override take effect:
    # IOParser::getOption keeps the value that appears last.
    assert joined.strip().endswith('--skip_logfile')


def test_extra_flag_overrides_a_form_value():
    """A later occurrence wins, so the panel can correct a form field."""
    command = job_commands.build_command(
        'motioncorr',
        _arguments(
            'motioncorr',
            bfactor='150',
            cli_extra=[{'flag': '--bfactor', 'value': '300'}],
        ),
        OUT,
    )
    joined = ' '.join(command)
    assert joined.index('--bfactor 150') < joined.index('--bfactor 300')


def test_panel_marks_flags_the_form_sets():
    options, _unavailable = cli_options.options_for('motioncorr')
    by_flag = {option['flag']: option for option in options}
    # The form controls these; a duplicate control would be confusing.
    assert by_flag['--bin_factor']['used_by_form']
    assert not by_flag['--max_io_threads']['used_by_form']
    assert not by_flag['--skip_logfile']['used_by_form']


def test_unavailable_job_explains_itself():
    options, unavailable = cli_options.options_for('external')
    assert options == []
    assert unavailable
