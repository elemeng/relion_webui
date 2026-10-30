"""The argv each job produces must be one the real program accepts.

The earlier implementation built every command with one generic rule (replace
``_`` with ``-`` in each key), which produced commands that no program accepted:
``relion_tomo_reconstruct_tomogram`` got no ``--o``, the Typer apps got no
subcommand, and ``--binned_angpix`` became ``--binned-angpix``, which the C++
parser accepts with only a warning and then ignores.

These tests check the properties that made those failures invisible:

* every job emits the output flag its program actually reads;
* Python/Typer jobs carry their subcommand, and use hyphenated flags;
* C++ jobs use underscore flags, including ``--pipeline_control``;
* integers are rendered without a ``.0``, because Typer's ``int`` options
  reject it;
* booleans are omitted when false, since presence is what the C++ tests.

They do not run the programs (there is no build here); they encode the
contract the programs' own argument parsers enforce.
"""

import pytest

from relion_webui import job_commands
from relion_webui import job_specs
from relion_webui import pipeline as pipeline_module
from relion_webui.generated_schema import JOBS

# A valid-looking value for every required option, so each builder can run.
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

OUT = '/proj/Tomograms/job001/'


@pytest.fixture
def client(tmp_path):
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient

    from relion_webui.app import create_app

    app = create_app(roots=[tmp_path])
    with TestClient(app) as test_client:
        yield test_client


def _arguments(job_type: str, **overrides) -> dict:
    """Schema defaults, plus a plausible value for every required option.

    A variant form only carries its branch's fields, and a branch may take
    either of two inputs, so the base job's defaults are used and the variant's
    declared input candidates get a value in priority order.
    """
    base = job_commands.base_job_type(job_type)
    options = {
        field['variable']: field.get('default')
        for field in JOBS[base]['fields']
    }
    for name in job_commands.COMMAND_SPECS[job_type].required:
        options[name] = REQUIRED_VALUES.get(name, 'x')
    candidates = job_commands.variant_inputs(job_type)
    if candidates:
        # Only the first candidate is set; the builder picks the first present.
        options[candidates[0]] = REQUIRED_VALUES.get(candidates[0], 'x')
    if job_type.startswith('tomo_denoise'):
        # cryoCARE refuses to run without a GPU id, on both of its modes.
        options['gpu_ids'] = '0'
        # Prediction needs the model a training job produced. The validation
        # cases that test *that* rule pass their own options and check at the
        # validate() layer, so the default here does not hide it.
        options['care_denoising_model'] = 'model.tar.gz'
    if job_type == 'localres':
        # The two local-resolution branches are mutually exclusive and ResMap
        # needs an external executable, so the Relion branch is the one that can
        # be built without one.
        options['do_resmap_locres'] = False
        options['do_relion_locres'] = True
        options['fn_in'] = 'half1.mrc'
    options.update(overrides)
    return options


def _flat(job_type: str, **overrides) -> str:
    """The command as the shell would see it (chains joined by '&&')."""
    return ' '.join(
        job_commands.build_command(job_type, _arguments(job_type, **overrides), OUT)
    )


def _tokens(job_type: str, **overrides) -> list:
    command = job_commands.build_command(job_type, _arguments(job_type, **overrides), OUT)
    if len(command) == 1:
        return command[0].replace('&&', ' ').split()
    return command


# ------------------------------------------------------------- every job
@pytest.mark.parametrize('job_type', job_commands.JOB_ORDER)
def test_every_job_builds(job_type):
    command = job_commands.build_command(job_type, _arguments(job_type), OUT)
    assert command
    assert command[0].strip()
    assert ' ' + OUT in _flat(job_type) or OUT in _flat(job_type)


@pytest.mark.parametrize('job_type', job_commands.JOB_ORDER)
def test_output_directory_is_passed(job_type):
    """Each program must be told where to write; this was missing before."""
    text = _flat(job_type)
    spec = job_commands.COMMAND_SPECS[job_type]
    expected = (
        '--output-directory'
        if spec.output_style == job_commands.OUTPUT_STYLE_PYTHON_DIR
        else '--o'
    )
    if job_type.startswith('modelangelo') or job_type == 'select_filaments':
        # Programs whose own CLI takes a single-dash -o: model_angelo and
        # relion_filament_selection.
        assert ' -o ' in text
        return
    if job_type == 'localres_resmap':
        # The ResMap wrapper is told the output through --noguiSplit's
        # arguments and the job directory, and takes no --o at all.
        assert '--noguiSplit ' + OUT + 'half1.mrc' in text
        return
    assert expected in text, text


@pytest.mark.parametrize('job_type', job_commands.JOB_ORDER)
def test_python_jobs_carry_their_subcommand(job_type):
    """A Typer app with subcommands exits 2 when called without one."""
    spec = job_commands.COMMAND_SPECS[job_type]
    if spec.subcommand is None:
        pytest.skip('single-command program')
    tokens = _tokens(job_type)
    # tomopick encodes the mode as the subcommand, so any of them is valid.
    if job_type == 'tomo_pick_tomograms':
        expected = str(_arguments(job_type).get('pick_mode') or spec.subcommand)
    else:
        expected = spec.subcommand
    assert expected in tokens[:3], tokens[:5]


@pytest.mark.parametrize('job_type', job_commands.JOB_ORDER)
def test_flag_style_is_consistent(job_type):
    """C++ programs take underscores, Typer programs hyphens.

    A hyphen sent to a C++ program is only a warning, so the value is dropped
    and the job quietly runs with a default. That is the bug this guards.
    """
    spec = job_commands.COMMAND_SPECS[job_type]
    if spec.subcommand is None and spec.flag_style == 'underscore':
        # C++ program: no hyphenated multi-word flags at all.
        for token in _tokens(job_type):
            if token.startswith('--') and token not in (
                '--pipeline_control', '--output-directory', '--o',
            ):
                assert '-' not in token[2:], token


def test_pipeline_control_is_underscored():
    """The C++ parser checks for the literal string ``--pipeline_control``."""
    from relion_webui.jobs import JobManager
    from relion_webui.paths import PathGuard
    from pathlib import Path

    manager = JobManager(PathGuard([Path('/tmp')]))
    command = manager.build_command(
        'tomo_reconstruct_tomograms',
        _arguments('tomo_reconstruct_tomograms'),
        Path('/tmp/job'),
    )
    # build_command now returns the shell string the run planner produced, so the
    # flag is inside it rather than a separate argv element.
    text = ' '.join(command)
    assert '--pipeline_control' in text
    assert '--pipeline-control' not in text


# ----------------------------------------------------- specific flags
def test_reconstruct_passes_its_geometry():
    text = _flat(
        'tomo_reconstruct_tomograms',
        xdim=4000,
        ydim=4000,
        zdim=2000,
        binned_angpix=10,
    )
    for flag, value in (
        ('--w', '4000'),
        ('--h', '4000'),
        ('--d', '2000'),
        ('--binned_angpix', '10'),
    ):
        assert f'{flag} {value}' in text, (flag, text)


def test_reconstruct_omits_the_zero_tilt_offset():
    """The C++ only emits --tiltangle_offset when it is non-zero."""
    assert '--tiltangle_offset' not in _flat('tomo_reconstruct_tomograms')
    text = _flat('tomo_reconstruct_tomograms', tiltangle_offset=5)
    assert '--tiltangle_offset 5' in text


def test_reconstruct_fourier_block():
    """--fourier, --ctf_intact_first_peak and --skip_fourier_wiener go together.

    The C++ writes the two sub-flags only inside the ``if (do_fourier)`` block.
    """
    text = _flat('tomo_reconstruct_tomograms', do_fourier=True)
    assert '--fourier' in text
    assert '--ctf_intact_first_peak' in text
    plain = _flat('tomo_reconstruct_tomograms', do_fourier=False)
    assert '--fourier' not in plain
    assert '--skip_fourier_wiener' not in plain


def test_align_defaults_to_aretomo():
    """All three methods false would make the program refuse to run."""
    text = _flat('tomo_align_tilt_series')
    assert '--aretomo2' in text
    assert '--imod_fiducials' not in text


def test_align_emits_only_the_chosen_method():
    """Each alignment method is a job type, so only its own flags are emitted."""
    fiducials = _flat('tomo_align_imod_fiducials')
    assert '--imod_fiducials' in fiducials
    assert '--aretomo2' not in fiducials

    patchtrack = _flat('tomo_align_imod_patchtrack')
    assert '--imod_patchtrack' in patchtrack
    assert '--patch_size' in patchtrack
    assert '--aretomo2' not in patchtrack

    aretomo = _flat('tomo_align_aretomo')
    assert '--aretomo2' in aretomo
    assert '--imod_fiducials' not in aretomo


def test_import_uses_the_serialem_subcommand_and_hyphen_flags():
    text = _flat('tomo_import_serialem')
    assert 'relion_python_tomo_import SerialEM' in text
    assert '--tilt-image-movie-pattern' in text
    assert '--mdoc-file-pattern' in text
    # The GUI's dose option becomes one of two mutually exclusive flags.
    assert '--dose-per-tilt-image' in text
    assert '--dose-per-movie-frame' not in text


def test_import_coordinate_branch_uses_the_other_program():
    text = _flat('tomo_import_coordinates', in_coords='coords.star')
    assert text.startswith('relion_tomo_import_coordinates')
    assert '--i coords.star' in text


def test_denoise_predict_passes_the_model():
    text = _flat(
        'tomo_denoise_tomograms',
        care_denoising_model='model.tar.gz',
        ntiles_x=2,
        ntiles_y=2,
        ntiles_z=2,
    )
    assert 'cryoCARE:predict' in text
    assert '--model-file model.tar.gz' in text
    # --n-tiles takes three values in one flag.
    assert '--n-tiles 2 2 2' in text


def test_denoise_train_passes_the_training_options():
    text = _flat(
        'tomo_denoise_train',
        tomograms_for_training='TS_01:TS_02',
    )
    assert '--training-tomograms TS_01:TS_02' in text
    assert '--model-file' not in text


def test_pick_chains_picker_and_pose_export():
    """The picker opens a GUI, so a headless run needs the export step too."""
    text = _flat('tomo_pick_tomograms', pick_mode='spheres', particle_spacing=100)
    assert 'relion_python_tomo_pick spheres' in text
    assert 'relion_python_tomo_get_particle_poses spheres' in text
    assert '--spacing-angstroms 100' in text
    assert '&&' in text


def test_subtomo_uses_the_optimisation_set():
    """The JAZ tomo programs read the set through --i, not --ios.

    The refine jobs (inimodel/class3d/autorefine/multibody) are the ones that
    take --ios, because relion_refine reads a RELION optimisation set. The
    tomography programs take the same file through OptimisationSet::read, which
    asks for --i. Both spellings exist in the C++, so pinning --ios here was
    pinning the wrong half: the program would ignore the input and run on
    defaults.
    """
    text = _flat('tomo_subtomo')
    assert '--i optimisation_set.star' in text
    assert '--ios' not in text


def test_refine_jobs_use_the_ios_flag():
    """relion_refine takes --ios for the tomo optimisation set.

    The same builder serves a SPA job through --i, so the flag is chosen from
    which input is actually given, which is what the C++ does with is_tomo.
    """
    for job_type in ('inimodel', 'class3d', 'autorefine'):
        text = _flat(job_type, in_optimisation='optimisation_set.star', fn_img='')
        assert '--ios optimisation_set.star' in text, job_type
        # The second program of inimodel reads its own --i map, so only the
        # absence of the particles input is meaningful here.
        assert '--i particles.star' not in text, job_type


def test_refine_jobs_fall_back_to_a_particles_star():
    """Without an optimisation set, the SPA input is used."""
    for job_type in ('inimodel', 'class3d', 'autorefine'):
        text = _flat(job_type, in_optimisation='', fn_img='particles.star')
        assert '--i particles.star' in text, job_type
        assert '--ios' not in text, job_type


def test_subtomo_format_radio_maps_to_flags():
    assert '--stack2d' in _flat('tomo_subtomo', subtomo_format='2D stacks')
    assert '--real_subtomo' in _flat('tomo_subtomo', subtomo_format='3D subtomos')


def test_motioncorr_gain_rotation_is_numeric():
    """The GUI stores a radio label; MotionCor2 wants the number in brackets."""
    text = _flat('motioncorr', gain_rot='90 degrees (1)', gain_flip='Flip upside down (1)')
    assert '--gain_rot 1' in text
    assert '--gain_flip 1' in text


def test_postprocess_switches_between_auto_and_adhoc_bfactor():
    auto = _flat('postprocess', do_auto_bfac=True)
    assert '--auto_bfac' in auto
    assert '--adhoc_bfac' not in auto
    adhoc = _flat('postprocess', do_auto_bfac=False, adhoc_bfac=-50)
    assert '--adhoc_bfac -50' in adhoc
    assert '--auto_bfac' not in adhoc


def test_select_uses_the_metadata_value_path():
    text = _flat('select', fn_data='particles.star')
    assert '--i particles.star' in text
    assert '--select rlnCtfMaxResolution' in text
    assert '--minval' in text
    assert '--maxval' in text


def test_joinstar_combines_the_inputs():
    text = _flat('joinstar', fn_part1='a.star', fn_part2='b.star')
    assert '--combine' in text
    assert 'a.star' in text and 'b.star' in text
    assert '--check_duplicates rlnImageName' in text


def test_modelangelo_without_sequences_uses_build_no_seq():
    """`build <seq flags>` and `build_no_seq` are alternatives, not a prefix."""
    text = _flat('modelangelo_build')
    assert 'build_no_seq' in text
    assert 'modelangelo build ' not in text
    with_seq = _flat('modelangelo_build', p_seq='seq.fasta')
    assert 'build -pf seq.fasta' in with_seq
    assert 'build_no_seq' not in with_seq


def test_modelangelo_uses_model_angelo_single_dash_flags():
    """model_angelo's CLI is not a RELION parser; it takes -v/-o/-d."""
    text = _flat('modelangelo_build')
    assert ' -v ' in text
    assert ' -d ' in text
    assert '--v ' not in text
    assert '--d ' not in text


def test_external_requires_a_program():
    with pytest.raises(ValueError, match='fn_exe'):
        job_commands.build_command('external', {'fn_exe': ''}, OUT)


def test_external_forwards_labelled_parameters():
    text = _flat(
        'external',
        fn_exe='/usr/bin/tool',
        param1_label='threads',
        param1_value='4',
    )
    assert text.startswith('/usr/bin/tool')
    assert '--threads 4' in text


# ------------------------------------------------- integers and booleans
def test_integers_have_no_decimal_point():
    """Typer's ``int`` options reject ``--cache-size 5.0``."""
    text = _flat('tomo_exclude_tilt_images', cache_size=5)
    assert '--cache-size 5 ' in text + ' '
    assert '--cache-size 5.0' not in text


def test_false_booleans_are_omitted():
    """The C++ treats presence as true, so a false flag must not be emitted."""
    text = _flat('tomo_reconstruct_tomograms', generate_split_tomograms=False)
    assert '--generate_split_tomograms' not in text
    # Enabling it requires turning Fourier inversion off, which is the C++ rule.
    assert '--generate_split_tomograms' in _flat(
        'tomo_reconstruct_tomograms',
        generate_split_tomograms=True,
        do_fourier=False,
    )


def test_boolean_yes_no_strings_are_understood():
    """job.star holds ``Yes``/``No``; the same values arrive back from the GUI."""
    assert '--fourier' in _flat('tomo_reconstruct_tomograms', do_fourier='Yes')
    assert '--fourier' not in _flat('tomo_reconstruct_tomograms', do_fourier='No')


def test_missing_required_option_names_the_field():
    with pytest.raises(ValueError) as error:
        job_commands.build_command('tomo_reconstruct_tomograms', {}, OUT)
    message = str(error.value)
    assert 'in_tiltseries' in message


def test_unknown_job_type_is_rejected():
    with pytest.raises(ValueError, match='unsupported job type'):
        job_commands.build_command('not_a_job', {}, OUT)


# ------------------------------------------------------------------ form
def test_form_schema_describes_one_job():
    form = job_commands.form_schema('tomo_reconstruct_tomograms')
    assert form['title'] == 'Reconstruct tomograms'
    assert form['executable'] == 'relion_tomo_reconstruct_tomogram'
    assert form['flag_style'] == 'underscore'
    assert 'in_tiltseries' in form['required']
    variables = {field['variable'] for field in form['fields']}
    assert {'in_tiltseries', 'xdim', 'ydim', 'zdim'} <= variables
    titles = [tab['title'] for tab in form['tabs']]
    # The Run tab is appended from the shared options, as the GUI builds it at
    # runtime, so it comes after the job's own tabs.
    assert titles == ['I/O', 'Reconstruct', 'Run']


def test_form_schema_marks_internal_options():
    """Options the web UI handles itself are flagged, not silently shown."""
    form = job_commands.form_schema('tomo_import')
    internal = {
        field['variable'] for field in form['fields'] if field.get('internal')
    }
    assert 'do_coords' in internal


def test_all_form_schemas_cover_the_job_order():
    """The picker's list is JOB_ORDER minus the API-only aliases."""
    forms = job_commands.all_form_schemas()
    assert [form['job_type'] for form in forms] == list(
        job_commands.selectable_job_types()
    )
    # An alias builds the same command as the variant it points at, so listing
    # both would offer one job twice.
    for job_type, spec in job_commands.COMMAND_SPECS.items():
        if spec.alias_of is None:
            continue
        assert job_type not in [form['job_type'] for form in forms]
        assert spec.branch == job_commands.COMMAND_SPECS[spec.alias_of].branch


# ------------------------------------------------------------- validation
VALIDATION_CASES = [
    # (job, options, expected substring in the message)
    (
        'tomo_reconstruct_tomograms',
        {'generate_split_tomograms': True, 'do_fourier': True},
        'Fourier-inversion',
    ),
    # cryoCARE's two modes are separate job types now, so "both or neither" can
    # no longer be expressed; what remains is each branch's own requirements.
    (
        'tomo_denoise_train',
        {'in_tomoset': 't.star', 'gpu_ids': '0'},
        'tomograms to train on',
    ),
    (
        'tomo_denoise_predict',
        {'in_tomoset': 't.star', 'gpu_ids': '0'},
        'denoising model',
    ),
    (
        'tomo_denoise_predict',
        {'in_tomoset': 't.star', 'care_denoising_model': 'm.tar.gz'},
        'state the GPU',
    ),
    (
        'class3d',
        {'use_gpu': True, 'dont_skip_align': False},
        'GPUs cannot be used',
    ),
    (
        'tomo_align_frame',
        {'do_motion': True, 'do_shift_only_by_particles': True},
        'exclusive',
    ),
    (
        'select_split',
        {'fn_data': 'p.star', 'nr_split': -1, 'split_size': -1},
        'positive subset size',
    ),
    (
        'joinstar',
        {'fn_part1': 'a.star'},
        'at least two STAR files',
    ),
    (
        'localres_resmap',
        {'fn_in': 'half1.mrc', 'fn_mask': 'm.mrc'},
        'ResMap program',
    ),
    (
        'multibody',
        {'do_select': True, 'eigenval_min': 5, 'eigenval_max': 1},
        'maximum eigenvalue',
    ),
]


@pytest.mark.parametrize('job_type, options, expected', VALIDATION_CASES)
def test_validation_rule_fires(job_type, options, expected):
    """The same contradictions the C++ refuses, caught before submitting."""
    problems = job_commands.validate(job_type, options)
    assert any(expected in problem for problem in problems), problems


@pytest.mark.parametrize('job_type, options, expected', VALIDATION_CASES)
def test_validation_is_reflected_in_build(job_type, options, expected):
    """build_command refuses, so the API returns 400 rather than a bad job.

    A job may be rejected earlier by its required-option list, which is also a
    400 with a specific message; either is a correct refusal.
    """
    if job_type in (
        'joinstar', 'tomo_denoise_train', 'tomo_denoise_predict',
        'localres_resmap',
    ):
        # These checks are about an option being *absent*, and _arguments fills
        # every required option, so the rule cannot fire through the builder.
        # validate() is where the form sees them, so assert on the raw options.
        problems = job_commands.validate(job_type, dict(options))
        assert any(expected in problem for problem in problems), problems
        return
    merged = _arguments(job_type, **options)
    with pytest.raises(ValueError, match=expected):
        job_commands.build_command(job_type, merged, OUT)


def test_a_valid_combination_has_no_problems():
    assert job_commands.validate(
        'tomo_align_tilt_series', {'do_aretomo2': True}
    ) == []
    assert job_commands.validate(
        'tomo_reconstruct_tomograms',
        {'generate_split_tomograms': False, 'do_fourier': True},
    ) == []


def test_jobs_without_rules_never_complain():
    """A job with no validation rule reports nothing for an empty option set.

    Variants are excluded: they carry their own branch checks, which do fire on
    an empty set ("please select an input file"), and those are the point.
    """
    for job_type in job_commands.JOB_ORDER:
        if job_commands.is_variant(job_type):
            continue
        if any(rule.job_type == job_type for rule in job_commands.VALIDATION_RULES):
            continue
        assert job_commands.validate(job_type, {}) == []


# --------------------------------------------------- filesystem validation
def test_half_map_filename_is_checked():
    """RELION finds the other half-map from the "half" substring."""
    problems = job_commands.validate_inputs('postprocess', {'fn_in': 'map.mrc'})
    assert any('half' in problem for problem in problems)
    assert job_commands.validate_inputs(
        'postprocess', {'fn_in': 'run_half1_unfil.mrc'}
    ) == []


def test_missing_input_is_reported_relative_to_the_project(tmp_path):
    (tmp_path / 'present.star').write_text('')
    problems = job_commands.validate_inputs(
        'tomo_reconstruct_tomograms',
        {'in_tiltseries': 'present.star'},
        base=tmp_path,
    )
    assert problems == []
    problems = job_commands.validate_inputs(
        'tomo_reconstruct_tomograms',
        {'in_tiltseries': 'absent.star'},
        base=tmp_path,
    )
    assert any('does not exist' in problem for problem in problems)


def test_relative_path_without_a_base_is_not_called_missing():
    """Without a project the path cannot be resolved, so it is left alone."""
    assert job_commands.validate_inputs(
        'tomo_reconstruct_tomograms', {'in_tiltseries': 'in.star'}
    ) == []


def test_wildcards_are_left_to_the_program(tmp_path):
    assert job_commands.validate_inputs(
        'tomo_import', {'movie_files': 'frames/*.mrc'}, base=tmp_path
    ) == []


def test_validation_endpoint_checks_half_maps(client):
    response = client.post(
        '/api/job-validate',
        json={'job_type': 'postprocess', 'arguments': {'fn_in': 'map.mrc'}},
    )
    assert response.status_code == 200
    assert any('half' in problem for problem in response.json()['problems'])


def test_validation_endpoint_resolves_paths_against_the_project(client, tmp_path):
    project = tmp_path / 'proj'
    project.mkdir()
    pipeline_module.write_pipeline(project, pipeline_module.Pipeline())
    response = client.post(
        '/api/job-validate',
        json={
            'job_type': 'tomo_reconstruct_tomograms',
            'arguments': {'in_tiltseries': 'nope.star'},
            'project_directory': str(project),
        },
    )
    assert response.status_code == 200
    assert any('does not exist' in problem for problem in response.json()['problems'])


# ------------------------------------------------------- branch variants
def test_select_variants_share_the_official_process_identity():
    """Every Select variant is still one Select job to the desktop GUI.

    RELION's ``get_proc_type`` strips a label's suffixes, so ``relion.select``
    and a deeper label both resolve to the same process type; the variants
    therefore all keep the ``Select/`` directory and the base label.
    """
    base = job_specs.get_spec('select')
    for job_type in job_commands.JOB_ORDER:
        if job_commands.base_job_type(job_type) != 'select':
            continue
        spec = job_specs.get_spec(job_type)
        assert spec.directory_name == base.directory_name, job_type
        assert spec.process_type_label == base.process_type_label, job_type


def test_select_variants_run_their_own_program():
    expected = {
        'select_values': 'relion_star_handler',
        'select_discard': 'relion_star_handler',
        'select_split': 'relion_star_handler',
        'select_duplicates': 'relion_star_handler',
        'select_filaments': 'relion_filament_selection',
        'select_class_ranker': 'relion_class_ranker',
    }
    for job_type, executable in expected.items():
        text = _flat(job_type)
        assert text.startswith(executable + ' '), (job_type, text[:60])


def test_select_values_filters_on_metadata():
    text = _flat(
        'select_values',
        fn_data='particles.star',
        select_label='rlnCtfMaxResolution',
        select_minval='3',
        select_maxval='9',
    )
    assert '--select rlnCtfMaxResolution' in text
    assert '--minval 3' in text
    assert '--maxval 9' in text
    assert '--o ' in text and 'particles.star' in text


def test_select_discard_uses_image_statistics():
    text = _flat('select_discard', fn_data='p.star', discard_sigma='4')
    assert '--discard_on_stats' in text
    assert '--discard_label rlnImageName' in text
    assert '--discard_sigma 4' in text
    assert '--select ' not in text


def test_select_split_only_sends_positive_sizes():
    """The C++ passes neither flag when the value is -1."""
    text = _flat('select_split', fn_data='p.star', nr_split='3', split_size='-1')
    assert '--split' in text
    assert '--nr_split 3' in text
    assert '--size_split' not in text

    by_size = _flat('select_split', fn_data='p.star', nr_split='-1', split_size='500')
    assert '--size_split 500' in by_size
    assert '--nr_split' not in by_size


def test_select_duplicates_uses_star_handler():
    text = _flat('select_duplicates', fn_data='p.star', duplicate_threshold='30')
    assert '--remove_duplicates 30' in text
    # image_angpix is only passed when positive.
    assert '--image_angpix' not in text


def test_select_class_ranker_passes_one_minimum_count():
    """The C++ passes at most one of the two minimum-count options."""
    parts = _flat(
        'select_class_ranker',
        fn_model='run_optimiser.star',
        select_nr_parts='500',
        select_nr_classes='-1',
    )
    assert '--select_min_nr_particles 500' in parts
    assert '--select_min_nr_classes' not in parts
    assert '--auto_select' in parts


def test_variant_form_shows_only_its_own_fields():
    form = job_commands.form_schema('select_split')
    variables = {field['variable'] for field in form['fields']}
    assert {'fn_data', 'nr_split', 'split_size'} <= variables
    # Options belonging to the other modes must not be in this form.
    assert 'do_remove_duplicates' not in variables
    assert 'dendrogram_threshold' not in variables
    assert form['base_job_type'] == 'select'
    assert form['branch'] == 'split'


def test_variant_static_switch_reaches_job_star_options():
    """The pinned branch switch is written to job.star, not just the command."""
    arguments = _arguments('select_split', fn_data='p.star', nr_split='2')
    options = job_commands._merged(
        job_commands.COMMAND_SPECS['select_split'], arguments
    )
    assert options['do_split'] is True


def test_variant_job_star_carries_the_branch_and_full_option_set(tmp_path):
    """A variant's job.star must open on the mode that actually ran.

    The desktop GUI has one window per C++ job, so it reads the branch from the
    switch option. ``_gui_options`` fills the base job's whole table (the
    window would otherwise show mostly blanks) and then forces the variant's
    switch, which is what this checks end to end through the real writer.
    """
    from relion_webui import job_artifacts
    import starfile

    arguments = {'fn_data': 'particles.star', 'nr_split': '4', 'split_size': '-1'}
    arguments = job_commands._merged(
        job_commands.COMMAND_SPECS['select_split'], arguments
    )
    from relion_webui.jobs import JobManager
    options = JobManager._gui_options('select_split', arguments)

    assert options['do_split'] is True
    # Every other mode stays off, which is what the C++ refuses more than one of.
    assert options['do_select_values'] is False
    assert options['do_remove_duplicates'] is False
    assert options['do_filaments'] is False
    # The base job's full table is present.
    assert 'select_label' in options
    assert 'dendrogram_threshold' in options

    path = job_artifacts.write_job_star(
        tmp_path / 'Select' / 'job002', 'relion.select', options
    )
    star = starfile.read(path, always_dict=True)
    values = dict(
        zip(
            star['joboptions_values']['rlnJobOptionVariable'],
            star['joboptions_values']['rlnJobOptionValue'],
        )
    )
    assert values['do_split'] == 'Yes'
    assert values['do_select_values'] == 'No'
    assert star['job'].iloc[0]['rlnJobTypeLabel'] == 'relion.select'


def test_api_alias_builds_the_same_command_as_its_target():
    """`select` is kept only so existing clients keep working.

    It must produce byte-identical argv to `select_values`; if the two drifted,
    the deprecated name would silently run a different job.
    """
    arguments = {'fn_data': 'p.star', 'select_label': 'rlnCtfMaxResolution'}
    alias = job_commands.build_command('select', dict(arguments), OUT)
    target = job_commands.build_command('select_values', dict(arguments), OUT)
    assert ' '.join(alias) == ' '.join(target)


def test_api_alias_is_hidden_from_the_picker_but_accepted():
    from relion_webui import jobs

    assert 'select' in jobs.SUPPORTED_JOBS
    assert 'select' not in jobs.SELECTABLE_JOBS


def test_variant_output_nodes_match_what_the_branch_writes():
    """Each mode writes different files; the flow graph must not claim otherwise.

    The filament branch writes `run_optimiser.star` and a log, not
    `particles.star`, so registering the base job's node would tell the GUI a
    downstream job can consume a file that was never created.
    """
    filaments = job_specs.get_spec('select_filaments')
    names = {node.filename for node in filaments.output_nodes}
    assert names == {'run_optimiser.star', 'logfile.pdf'}

    split = job_specs.get_spec('select_split')
    assert 'particles.star' in {node.filename for node in split.output_nodes}

    ranker = job_specs.get_spec('select_class_ranker')
    assert {
        'particles.star',
        'class_averages.star',
        'rank_optimiser.star',
    } <= {node.filename for node in ranker.output_nodes}


# ------------------------------------------------- derived and paired values
def test_half_map_pair_is_derived_from_one_name():
    """The form asks for one half-map; the other is found by name.

    RELION's `FileName::getTheOtherHalf` swaps half1/half2, and both refine jobs
    pass the pair as --ref1/--ref2. Missing this meant a job ran without an
    explicit reference at all, silently using whatever the optimisation set had.
    """
    for job_type in ('tomo_ctf_refine', 'tomo_align_frame'):
        text = _flat(job_type, in_halfmaps='run_half1.mrc')
        assert '--ref1 run_half1.mrc' in text, job_type
        assert '--ref2 run_half2.mrc' in text, job_type

        # A name without "half" cannot be paired, and the C++ refuses it.
        with pytest.raises(ValueError, match='half'):
            _flat(job_type, in_halfmaps='reference.mrc')


def test_refine_jobs_pass_mask_and_fsc():
    text = _flat(
        'tomo_ctf_refine',
        in_refmask='mask.mrc',
        in_post='postprocess.star',
    )
    assert '--mask mask.mrc' in text
    assert '--fsc postprocess.star' in text


def test_angular_sampling_becomes_a_healpix_order():
    """The GUI shows an interval; the program takes a healpix order.

    `getHealPixOrder` returns index+1 for the chosen label and the C++ subtracts
    one oversampling level, so "30 degrees" (index 0) is order 0 and
    "7.5 degrees" (index 2) is order 2.
    """
    assert '--healpix_order 0' in _flat(
        'class3d', in_optimisation='os.star', sampling='30 degrees',
        do_skip_align=False,
    )
    assert '--healpix_order 2' in _flat(
        'class3d', in_optimisation='os.star', sampling='7.5 degrees',
        do_skip_align=False,
    )
    text = _flat('class3d', in_optimisation='os.star', do_skip_align=False)
    assert '--oversampling 1' in text


def test_autorefine_repeats_the_order_for_local_searches():
    text = _flat(
        'autorefine', in_optimisation='os.star', sampling='7.5 degrees',
        dont_skip_align=True,
    )
    assert '--healpix_order 2' in text
    assert '--auto_local_healpix_order 2' in text


def test_helical_block_divides_the_percentage():
    """`helical_z_percentage` is a percentage in the GUI and a fraction in argv."""
    text = _flat(
        'class3d', in_optimisation='os.star', do_helix=True,
        helical_z_percentage=30,
    )
    assert '--helix' in text
    assert '--helical_z_percentage 0.3' in text


def test_helical_symmetry_search_block():
    text = _flat(
        'class3d', in_optimisation='os.star', do_helix=True,
        do_local_search_helical_symmetry=True,
        helical_twist_min=0.1, helical_twist_max=5,
        helical_rise_min=1, helical_rise_max=6,
    )
    assert '--helical_symmetry_search' in text
    assert '--helical_twist_min 0.1' in text
    assert '--helical_rise_max 6' in text


def test_helical_angular_ranges_are_thirds():
    """The C++ clamps each range to 0..90 and passes a third of it."""
    text = _flat(
        'class3d', in_optimisation='os.star', do_helix=True,
        dont_skip_align=True, range_psi=30, range_rot=30,
    )
    assert '--sigma_psi 10.0' in text
    assert '--sigma_rot 10.0' in text


def test_tomo_recon_particle_imposes_helical_symmetry():
    """The helical branch adds one relion_helix_toolbox run per output map."""
    commands = job_commands.build_command(
        'tomo_recon_particle',
        _arguments('tomo_recon_particle', in_optimisation='os.star', do_helix=True),
        OUT,
    )
    assert len(commands) == 4  # reconstruct + half1, half2, merged
    imposed = [c for c in commands if 'relion_helix_toolbox' in c]
    assert len(imposed) == 3
    # The default is 20%, which the C++ passes as a fraction.
    assert all('--z_percentage 0.2' in c for c in imposed)


# ------------------------------------------------------------------ categories
def test_every_selectable_job_is_in_exactly_one_category():
    """The picker must not be able to hide a job.

    JOB_CATEGORIES is hand-written, so a job type added later would silently
    vanish from the picker if nothing checked coverage.
    """
    from relion_webui import job_commands as jc

    seen: list[str] = []
    for group in jc.job_categories():
        seen.extend(group['job_types'])

    assert len(seen) == len(set(seen)), 'a job type appears in two categories'
    assert set(seen) == set(jc.selectable_job_types())


def test_variants_sit_under_their_base_job():
    from relion_webui import job_commands as jc

    groups = {g['title']: g['job_types'] for g in jc.job_categories()}
    # Denoise train/predict are the two modes of one job.
    assert set(groups['Enhance and pick']) >= {
        'tomo_denoise_train',
        'tomo_denoise_predict',
    }
    # All six select modes are together, not scattered by name.
    assert len(groups['Select']) == 6
    assert all(t.startswith('select_') for t in groups['Select'])


def test_categories_keep_the_pickers_order():
    """Variants follow the job they belong to, as in the flat list."""
    from relion_webui import job_commands as jc

    for group in jc.job_categories():
        order = [jc.selectable_job_types().index(t) for t in group['job_types']]
        assert order == sorted(order), f'{group["title"]} is out of order'


def test_job_schema_endpoint_returns_categories():
    from fastapi.testclient import TestClient
    from relion_webui.app import create_app

    app = create_app(roots=['.'], projects=__import__('relion_webui.projects', fromlist=['ProjectRegistry']).ProjectRegistry(config_directory='/tmp/relion-cat-test'))
    with TestClient(app) as client:
        payload = client.get('/api/job-schema').json()

    assert payload['jobs'], 'schemas still returned'
    titles = [c['title'] for c in payload['categories']]
    assert titles[0] == 'Import and preprocess'
    assert 'Utilities' in titles
