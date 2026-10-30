"""Build the argv for each job the way RELION's own ``getCommands*`` does.

``generated_schema.py`` supplies the option metadata and the form layout, but it
cannot supply the argv translation: the mapping is code, not data. Each
``getCommands*Job`` in ``src/pipeline_jobs.cpp`` interleaves value flags,
conditionally emitted booleans, subcommands and derived output names, so the
rules are written out per job here.

Every builder follows the C++ body closely and names the lines it comes from in
a comment, so a future reader can diff the two. ``test_web_commands.py`` checks
the invariants that the C++ enforces (required inputs, subcommand presence,
option-name style) so a typo cannot silently produce a broken command.

Two conventions differ between program families and are easy to get wrong:

* C++ programs take underscore flags (``--binned_angpix``); Python/Typer ones
  take hyphens (``--tomogram-star-file``). Writing a hyphen to a C++ program is
  not an error at run time - ``IOParser`` only warns about unknown arguments -
  so the value is silently ignored and the job runs with a default. The
  ``flag_style`` field records which each job needs.
* Typer apps with several subcommands (``relion_python_tomo_pick`` and friends)
  reject a call with no subcommand, so ``subcommand`` is part of the argv.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .generated_schema import JOBS as SCHEMA_JOBS
from . import cli_options

# Where a job's own output directory goes on the command line, and which
# spelling the program expects.
OUTPUT_STYLE_C_STAR = '--o'                     # C++: argument is a prefix
OUTPUT_STYLE_PYTHON_DIR = '--output-directory'  # Typer: a directory

# Jobs that serve a single-particle and a tomography branch, and so accept an
# optimisation set (`--ios`) in place of a particles STAR file (`--i`).
TOMO_INPUT_JOBS = frozenset({'inimodel', 'class3d', 'autorefine', 'multibody'})


@dataclass(frozen=True)
class CommandSpec:
    """How one job type becomes a command line."""

    job_type: str
    cpp_name: str
    executable: str
    # Set when the program is a Typer app with subcommands.
    subcommand: Optional[str] = None
    # 'underscore' for C++ programs, 'hyphen' for Typer programs.
    flag_style: str = 'underscore'
    # Program that reads the job's inputs directly; used for the pipeline node.
    output_style: str = OUTPUT_STYLE_C_STAR
    # Options that are mandatory for the job to run at all, as GUI variable
    # names. Checked before anything is written so the error names the field.
    required: Tuple[str, ...] = ()
    # Options whose value is never passed as a flag (handled specially).
    internal: Tuple[str, ...] = ()
    # Options the GUI sets but which the web form does not ask for.
    static: Dict[str, object] = field(default_factory=dict)
    # The MPI variant, used instead of `executable` when nr_mpi > 1. RELION's
    # getCommands* swaps the program itself rather than wrapping a
    # non-MPI binary in mpirun, so the substituted name has to be here.
    mpi_executable: Optional[str] = None
    # Options holding a continuation checkpoint. When one is set the job runs
    # with --continue instead of --i, and the input it would otherwise require
    # (typically fn_img) is no longer needed - RELION's `is_continue` branch.
    continue_variable: Optional[str] = 'fn_cont'
    # Required options that are waived when continuing.
    waived_when_continued: Tuple[str, ...] = ()
    # Which branch of a multi-mode C++ builder this job type is.
    #
    # ``getCommandsSelectJob`` is a chain of mutually exclusive modes that pick
    # different programs, outputs and flags. Exposing the chain as one form
    # gives the user five booleans of which at most one may be true, and it
    # leaves most of the extracted command rules guarded by conditions that are
    # always false - which is what made the mapping unparsable. Each mode is
    # therefore its own job type with ``branch`` naming it, so the builder can
    # emit one branch with no dead conditions.
    #
    # A branch never changes the official pipeline identity: every variant keeps
    # the ``Select/`` directory and the ``relion.select`` process label, because
    # RELION's ``get_proc_type`` strips the label's suffixes
    # (``relion.select.onvalue`` -> ``relion.select``). The GUI therefore reads
    # every variant as the same kind of process, which is what keeps the two
    # directions compatible.
    # The C++ job a variant pins, when it is one.
    branch: Optional[str] = None
    # Set on a job type kept only for API compatibility: it builds exactly the
    # same command as another job type, so the picker hides it but a client that
    # already submits this name keeps working.
    alias_of: Optional[str] = None


# --------------------------------------------------------------------- specs
# One entry per tab in `relion_maingui --tomo`. The executable and subcommand
# come from the `command = ...` line of the matching getCommands*Job, and the
# flag style from whether that program is C++ or a Typer wrapper.
COMMAND_SPECS: Dict[str, CommandSpec] = {
    'tomo_import': CommandSpec(
        job_type='tomo_import',
        cpp_name='TomoImport',
        executable='relion_python_tomo_import',
        subcommand='SerialEM',
        flag_style='hyphen',
        output_style=OUTPUT_STYLE_PYTHON_DIR,
        required=('movie_files', 'mdoc_files'),
        # do_coords selects the coordinates branch, which is its own job type
        # now; these stay internal so the SerialEM form does not show them.
        internal=('do_coords', 'in_coords', 'remove_substring', 'remove_substring2',
                  'is_center', 'scale_factor', 'add_factor'),
        continue_variable=None,
        # Kept as an API alias: the SerialEM import was the only mode this job
        # type ever built as a default, so that is what the name keeps meaning.
        branch='serialem',
        static={'do_coords': False},
        alias_of='tomo_import_serialem',
    ),
    'motioncorr': CommandSpec(
        job_type='motioncorr',
        cpp_name='Motioncorr',
        executable='relion_run_motioncorr',
        required=('input_star_mics',),
        mpi_executable='relion_run_motioncorr_mpi',
        continue_variable=None,
    ),
    'ctffind': CommandSpec(
        job_type='ctffind',
        cpp_name='Ctffind',
        executable='relion_run_ctffind',
        required=('input_star_mics',),
        mpi_executable='relion_run_ctffind_mpi',
        continue_variable=None,
    ),
    'tomo_exclude_tilt_images': CommandSpec(
        job_type='tomo_exclude_tilt_images',
        cpp_name='TomoExcludeTiltImages',
        executable='relion_python_tomo_exclude_tilt_images',
        flag_style='hyphen',
        output_style=OUTPUT_STYLE_PYTHON_DIR,
        required=('in_tiltseries',),
    ),
    'tomo_align_tilt_series': CommandSpec(
        job_type='tomo_align_tilt_series',
        cpp_name='TomoAlignTiltSeries',
        executable='relion_align_tiltseries',
        required=('in_tiltseries',),
        mpi_executable='relion_align_tiltseries_mpi',
        # AreTomo2, the mode the web UI used by default; the other two methods
        # are separate job types now.
        branch='aretomo',
        static={'do_aretomo2': True, 'do_imod_fiducials': False,
                'do_imod_patchtrack': False},
        alias_of='tomo_align_aretomo',
    ),
    'tomo_reconstruct_tomograms': CommandSpec(
        job_type='tomo_reconstruct_tomograms',
        cpp_name='TomoReconstructTomograms',
        executable='relion_tomo_reconstruct_tomogram',
        required=('in_tiltseries', 'xdim', 'ydim', 'zdim'),
        mpi_executable='relion_tomo_reconstruct_tomogram_mpi',
    ),
    'tomo_denoise_tomograms': CommandSpec(
        job_type='tomo_denoise_tomograms',
        cpp_name='TomoDenoiseTomograms',
        executable='relion_python_tomo_denoise',
        subcommand='cryoCARE:predict',
        flag_style='hyphen',
        output_style=OUTPUT_STYLE_PYTHON_DIR,
        required=('in_tomoset',),
        # Predict was this job type's default branch; train is its own job now.
        branch='predict',
        static={'do_cryocare_train': False, 'do_cryocare_predict': True},
        alias_of='tomo_denoise_predict',
    ),
    'tomo_pick_tomograms': CommandSpec(
        job_type='tomo_pick_tomograms',
        cpp_name='TomoPickTomograms',
        executable='relion_python_tomo_pick',
        subcommand='particles',
        flag_style='hyphen',
        output_style=OUTPUT_STYLE_PYTHON_DIR,
        required=('in_tomoset',),
    ),
    'tomo_subtomo': CommandSpec(
        job_type='tomo_subtomo',
        cpp_name='TomoSubtomo',
        executable='relion_tomo_subtomo',
        required=('in_optimisation',),
        mpi_executable='relion_tomo_subtomo_mpi',
    ),
    'inimodel': CommandSpec(
        job_type='inimodel',
        cpp_name='Inimodel',
        executable='relion_refine',
        required=('fn_img',),
        mpi_executable='relion_refine_mpi',
        waived_when_continued=('fn_img',),
    ),
    'class3d': CommandSpec(
        job_type='class3d',
        cpp_name='Class3D',
        executable='relion_refine',
        required=('fn_img',),
        mpi_executable='relion_refine_mpi',
        waived_when_continued=('fn_img',),
    ),
    'autorefine': CommandSpec(
        job_type='autorefine',
        cpp_name='Autorefine',
        executable='relion_refine',
        required=('fn_img',),
        mpi_executable='relion_refine_mpi',
        waived_when_continued=('fn_img',),
    ),
    'multibody': CommandSpec(
        job_type='multibody',
        cpp_name='MultiBody',
        executable='relion_refine',
        required=('fn_in', 'fn_bodies'),
        mpi_executable='relion_refine_mpi',
        waived_when_continued=('fn_in',),
    ),
    'select': CommandSpec(
        job_type='select',
        cpp_name='Select',
        executable='relion_star_handler',
        required=(),
        # Kept as an API name for clients that already submit `select`. It is the
        # value-selection mode, so it pins the same switch `select_values` does;
        # otherwise the two names would build the same command while telling the
        # GUI different things in job.star. The picker lists the variant only,
        # because offering both would show the same job twice.
        branch='values',
        static={'do_select_values': True},
        alias_of='select_values',
    ),
    'tomo_recon_particle': CommandSpec(
        job_type='tomo_recon_particle',
        cpp_name='TomoReconPart',
        executable='relion_tomo_reconstruct_particle',
        required=('in_optimisation',),
        mpi_executable='relion_tomo_reconstruct_particle_mpi',
    ),
    'tomo_ctf_refine': CommandSpec(
        job_type='tomo_ctf_refine',
        cpp_name='TomoCtfRefine',
        executable='relion_tomo_refine_ctf',
        required=('in_optimisation',),
        mpi_executable='relion_tomo_refine_ctf_mpi',
    ),
    'tomo_align_frame': CommandSpec(
        job_type='tomo_align_frame',
        cpp_name='TomoAlign',
        executable='relion_tomo_align',
        required=('in_optimisation',),
        mpi_executable='relion_tomo_align_mpi',
    ),
    'maskcreate': CommandSpec(
        job_type='maskcreate',
        cpp_name='Maskcreate',
        executable='relion_mask_create',
        required=('fn_in',),
    ),
    'joinstar': CommandSpec(
        job_type='joinstar',
        cpp_name='Joinstar',
        executable='relion_star_handler',
        required=('fn_part1', 'fn_part2'),
    ),
    'postprocess': CommandSpec(
        job_type='postprocess',
        cpp_name='Postprocess',
        executable='relion_postprocess',
        required=('fn_in',),
        mpi_executable='relion_postprocess_mpi',
    ),
    'localres': CommandSpec(
        job_type='localres',
        cpp_name='Localres',
        executable='relion_postprocess',
        required=('fn_in',),
        mpi_executable='relion_postprocess_mpi',
        # RELION's own local resolution, which is the branch that runs a RELION
        # program and so the only one a headless job can default to.
        branch='relion',
        static={'do_resmap_locres': False, 'do_relion_locres': True},
        alias_of='localres_relion',
    ),
    'modelangelo': CommandSpec(
        job_type='modelangelo',
        cpp_name='ModelAngelo',
        executable='relion_python_modelangelo',
        # The C++ picks `build`, `build_no_seq` or `hmm_search` from the
        # options, so there is no single subcommand to record here; the builder
        # owns the choice.
        subcommand=None,
        flag_style='hyphen',
        output_style=OUTPUT_STYLE_PYTHON_DIR,
        required=('fn_map',),
        # Model building without the HMM search, which is the job this name
        # always built; the HMM step is its own job type now.
        branch='build',
        static={'do_hhmer': False},
        alias_of='modelangelo_build',
    ),
    'external': CommandSpec(
        job_type='external',
        cpp_name='External',
        executable='relion_external',
        required=('fn_exe',),
        # The executable is whatever the user typed into fn_exe.
        continue_variable=None,
    ),
}


# ------------------------------------------------------------------ variants
# Job types that are one branch of a multi-mode C++ builder, keyed by the base
# job they belong to. Each entry says:
#
#   * the branch name the builder switches on;
#   * the program that branch runs (which differs from the base job's);
#   * the options it needs, and the input field the form should show.
#
# They are separate job types rather than booleans on one form because the modes
# are mutually exclusive in the C++ (`c > 1` is an error), so a single form has
# to present five switches of which at most one may be on - and every extracted
# command rule ends up guarded by a condition that is false for the mode the
# user actually chose. Splitting makes each variant's command unconditional and
# its form show only the options that branch uses.
#
# The pipeline identity does not split: `job_specs` maps every variant back to
# the base job's directory and process label, which is safe because RELION's
# `get_proc_type` strips label suffixes.
BRANCH_VARIANTS: Dict[str, Tuple[Dict[str, object], ...]] = {
    'select': (
        {
            'job_type': 'select_values',
            'cpp_name': 'Select',
            'executable': 'relion_star_handler',
            'branch': 'values',
            'title': 'Select (metadata values)',
            'fields': ('fn_data', 'fn_mic', 'select_label', 'select_minval', 'select_maxval'),
            # Value selection needs the metadata label; the input is either a
            # particles or a micrographs STAR file, which the builder enforces.
            'required': ('select_label',),
            'inputs': ('fn_data', 'fn_mic'),
            'output': 'particles.star',
            # Sets the option the C++ branch tests, so job.star agrees with the
            # command that runs.
            'static': {'do_select_values': True},
        },
        {
            'job_type': 'select_discard',
            'cpp_name': 'Select',
            'executable': 'relion_star_handler',
            'branch': 'discard',
            'title': 'Select (discard on stats)',
            'fields': ('fn_data', 'fn_mic', 'discard_label', 'discard_sigma'),
            'required': (),
            'inputs': ('fn_data', 'fn_mic'),
            'output': 'particles.star',
            'static': {'do_discard': True},
        },
        {
            'job_type': 'select_split',
            'cpp_name': 'Select',
            'executable': 'relion_star_handler',
            'branch': 'split',
            'title': 'Select (split into subsets)',
            'fields': ('fn_data', 'fn_mic', 'do_random', 'split_size', 'nr_split'),
            'required': (),
            'inputs': ('fn_data', 'fn_mic'),
            'output': 'particles.star',
            'static': {'do_split': True},
        },
        {
            'job_type': 'select_duplicates',
            'cpp_name': 'Select',
            'executable': 'relion_star_handler',
            'branch': 'duplicates',
            'title': 'Select (remove duplicates)',
            'fields': ('fn_data', 'duplicate_threshold', 'image_angpix'),
            'required': ('fn_data',),
            'inputs': ('fn_data',),
            'output': 'particles.star',
            'static': {'do_remove_duplicates': True},
        },
        {
            'job_type': 'select_filaments',
            'cpp_name': 'Select',
            'executable': 'relion_filament_selection',
            'branch': 'filaments',
            'title': 'Select (filament dendrogram)',
            'fields': ('fn_model', 'dendrogram_threshold', 'dendrogram_minclass'),
            'required': ('fn_model',),
            'inputs': ('fn_model',),
            'output': 'run_optimiser.star',
            'static': {'do_filaments': True},
        },
        {
            'job_type': 'select_class_ranker',
            'cpp_name': 'Select',
            'executable': 'relion_class_ranker',
            'branch': 'class_ranker',
            'title': 'Select (automatic class selection)',
            'fields': ('fn_model', 'rank_threshold', 'select_nr_parts', 'select_nr_classes'),
            'required': ('fn_model',),
            'inputs': ('fn_model',),
            'output': 'particles.star',
            'static': {'do_class_ranker': True},
        },
    ),
    # `getCommandsTomoImportJob` runs a C++ utility for coordinates and a Typer
    # app for SerialEM movies. The two branches use different flag styles, so
    # each needs its own spec rather than one form with a mode switch.
    'tomo_import': (
        {
            'job_type': 'tomo_import_serialem',
            'cpp_name': 'TomoImport',
            'executable': 'relion_python_tomo_import',
            'subcommand': 'SerialEM',
            'flag_style': 'hyphen',
            'output_style': OUTPUT_STYLE_PYTHON_DIR,
            'branch': 'serialem',
            'title': 'Import (SerialEM tilt-series)',
            'fields': (
                'movie_files', 'mdoc_files', 'optics_group_name', 'prefix',
                'angpix', 'kV', 'Cs', 'Q0', 'dose_rate',
                'dose_is_per_movie_frame', 'tilt_axis_angle', 'mtf_file',
                'flip_tiltseries_hand', 'images_are_motion_corrected',
            ),
            'required': ('movie_files', 'mdoc_files'),
            'inputs': (),
            'output': 'tilt_series.star',
            'static': {'do_coords': False},
        },
        {
            'job_type': 'tomo_import_coordinates',
            'cpp_name': 'TomoImport',
            'executable': 'relion_tomo_import_coordinates',
            'flag_style': 'underscore',
            'branch': 'coordinates',
            'title': 'Import (particle coordinates)',
            'fields': (
                'in_coords', 'remove_substring', 'remove_substring2',
                'is_center', 'scale_factor', 'add_factor',
            ),
            'required': ('in_coords',),
            'inputs': ('in_coords',),
            'output': 'particles.star',
            'static': {'do_coords': True},
        },
    ),
    # The two cryoCARE modes are separate subcommands of one Typer app, and each
    # rejects the other's options, so they cannot share a form.
    'tomo_denoise_tomograms': (
        {
            'job_type': 'tomo_denoise_train',
            'cpp_name': 'TomoDenoiseTomograms',
            'executable': 'relion_python_tomo_denoise',
            'subcommand': 'cryoCARE:train',
            'flag_style': 'hyphen',
            'output_style': OUTPUT_STYLE_PYTHON_DIR,
            'branch': 'train',
            'title': 'Denoise (cryoCARE train)',
            'fields': (
                'in_tomoset', 'cryocare_path', 'gpu_ids',
                'tomograms_for_training', 'number_training_subvolumes',
                'subvolume_dimensions',
            ),
            'required': ('in_tomoset', 'tomograms_for_training'),
            'inputs': ('in_tomoset',),
            'output': 'tomograms.star',
            'static': {'do_cryocare_train': True, 'do_cryocare_predict': False},
        },
        {
            'job_type': 'tomo_denoise_predict',
            'cpp_name': 'TomoDenoiseTomograms',
            'executable': 'relion_python_tomo_denoise',
            'subcommand': 'cryoCARE:predict',
            'flag_style': 'hyphen',
            'output_style': OUTPUT_STYLE_PYTHON_DIR,
            'branch': 'predict',
            'title': 'Denoise (cryoCARE predict)',
            'fields': (
                'in_tomoset', 'cryocare_path', 'gpu_ids', 'care_denoising_model',
                'ntiles_x', 'ntiles_y', 'ntiles_z', 'denoising_tomo_name',
            ),
            'required': ('in_tomoset', 'care_denoising_model'),
            'inputs': ('in_tomoset',),
            'output': 'tomograms.star',
            'static': {'do_cryocare_train': False, 'do_cryocare_predict': True},
        },
    ),
    # Three alignment methods, of which the C++ permits exactly one. They differ
    # in flags, required executables and what they write, so each is its own job.
    'tomo_align_tilt_series': (
        {
            'job_type': 'tomo_align_imod_fiducials',
            'cpp_name': 'TomoAlignTiltSeries',
            'executable': 'relion_align_tiltseries',
            'branch': 'imod_fiducials',
            'title': 'Align tilt-series (IMOD fiducials)',
            'fields': (
                'in_tiltseries', 'tomogram_thickness', 'fiducial_diameter',
                'fn_batchtomo_exe',
            ),
            'required': ('in_tiltseries',),
            'inputs': ('in_tiltseries',),
            'output': 'aligned_tilt_series.star',
            'static': {
                'do_imod_fiducials': True,
                'do_imod_patchtrack': False,
                'do_aretomo2': False,
            },
        },
        {
            'job_type': 'tomo_align_imod_patchtrack',
            'cpp_name': 'TomoAlignTiltSeries',
            'executable': 'relion_align_tiltseries',
            'branch': 'imod_patchtrack',
            'title': 'Align tilt-series (IMOD patch tracking)',
            'fields': (
                'in_tiltseries', 'tomogram_thickness', 'patch_size',
                'patch_overlap', 'fn_batchtomo_exe',
            ),
            'required': ('in_tiltseries',),
            'inputs': ('in_tiltseries',),
            'output': 'aligned_tilt_series.star',
            'static': {
                'do_imod_fiducials': False,
                'do_imod_patchtrack': True,
                'do_aretomo2': False,
            },
        },
        {
            'job_type': 'tomo_align_aretomo',
            'cpp_name': 'TomoAlignTiltSeries',
            'executable': 'relion_align_tiltseries',
            'branch': 'aretomo',
            'title': 'Align tilt-series (AreTomo2)',
            'fields': (
                'in_tiltseries', 'tomogram_thickness', 'fn_aretomo_exe',
                'do_aretomo_tiltcorrect', 'aretomo_tiltcorrect_angle',
                'do_aretomo_ctf', 'do_aretomo_phaseshift',
                'do_aretomo_reconstruct', 'do_skip_aretomo_align',
                'aretomo_VolZ', 'aretomo_OutBin', 'other_aretomo_args', 'gpu_ids',
            ),
            'required': ('in_tiltseries',),
            'inputs': ('in_tiltseries',),
            'output': 'aligned_tilt_series.star',
            'static': {
                'do_imod_fiducials': False,
                'do_imod_patchtrack': False,
                'do_aretomo2': True,
            },
        },
    ),
    # HMM search is an extra program after model building, not a replacement, so
    # the two variants differ by one switch and by which options they ask for.
    'modelangelo': (
        {
            'job_type': 'modelangelo_build',
            'cpp_name': 'ModelAngelo',
            'executable': 'relion_python_modelangelo',
            'subcommand': None,
            'flag_style': 'hyphen',
            'output_style': OUTPUT_STYLE_PYTHON_DIR,
            'branch': 'build',
            'title': 'ModelAngelo (build model)',
            'fields': (
                'fn_map', 'p_seq', 'd_seq', 'r_seq', 'fn_modelangelo_exe', 'gpu_id',
            ),
            'required': ('fn_map',),
            'inputs': ('fn_map',),
            'output': 'model.cif',
            'static': {'do_hhmer': False},
        },
        {
            'job_type': 'modelangelo_hmm',
            'cpp_name': 'ModelAngelo',
            'executable': 'relion_python_modelangelo',
            'subcommand': None,
            'flag_style': 'hyphen',
            'output_style': OUTPUT_STYLE_PYTHON_DIR,
            'branch': 'hmm',
            'title': 'ModelAngelo (build + HMM search)',
            'fields': (
                'fn_map', 'p_seq', 'd_seq', 'r_seq', 'fn_modelangelo_exe', 'gpu_id',
                'fn_lib', 'alphabet', 'F1', 'F2', 'F3', 'E',
            ),
            'required': ('fn_map', 'fn_lib'),
            'inputs': ('fn_map',),
            'output': 'model.cif',
            'static': {'do_hhmer': True},
        },
    ),
    # ResMap and Relion's own local resolution are exclusive and take entirely
    # different programs and flags.
    'localres': (
        {
            'job_type': 'localres_resmap',
            'cpp_name': 'Localres',
            'executable': 'relion_postprocess',
            'branch': 'resmap',
            'title': 'Local resolution (ResMap)',
            'fields': (
                'fn_in', 'fn_mask', 'angpix', 'fn_resmap', 'pval',
                'minres', 'maxres', 'stepres',
            ),
            'required': ('fn_in', 'fn_mask', 'fn_resmap'),
            'inputs': ('fn_in',),
            'output': 'half1_resmap.mrc',
            'static': {'do_resmap_locres': True, 'do_relion_locres': False},
        },
        {
            'job_type': 'localres_relion',
            'cpp_name': 'Localres',
            'executable': 'relion_postprocess',
            'branch': 'relion',
            'title': 'Local resolution (RELION)',
            'fields': (
                'fn_in', 'angpix', 'adhoc_bfac', 'fn_mtf', 'fn_mask',
            ),
            'required': ('fn_in',),
            'inputs': ('fn_in',),
            'output': 'relion_locres.mrc',
            'static': {'do_resmap_locres': False, 'do_relion_locres': True},
        },
    ),
}


def _register_variants() -> None:
    """Create one ``CommandSpec`` per declared branch variant.

    A variant inherits what it does not override from its base job, so the
    common case (same program, different branch switch) is one line. The
    overrides exist because some branches change more than the switch:
    ``tomo_import``'s coordinates mode runs a C++ utility with underscore flags
    while its SerialEM mode runs a Typer app with hyphens, and the denoise modes
    are two different Typer subcommands of one program.
    """
    for base, variants in BRANCH_VARIANTS.items():
        base_spec = COMMAND_SPECS[base]
        for variant in variants:
            # Fields a variant may override on top of the base job's spec.
            # `executable` is required, so it is read from the variant directly;
            # the rest inherit when the variant says nothing.
            overrides = {
                key: variant[key]
                for key in (
                    'subcommand', 'flag_style', 'output_style',
                    'mpi_executable', 'continue_variable',
                )
                if key in variant
            }
            COMMAND_SPECS[variant['job_type']] = CommandSpec(
                job_type=variant['job_type'],
                cpp_name=variant['cpp_name'],
                executable=variant['executable'],
                branch=str(variant['branch']),
                required=tuple(variant.get('required', ())),
                internal=tuple(variant.get('internal', base_spec.internal)),
                static=dict(variant.get('static', {})),
                continue_variable=base_spec.continue_variable,
                **overrides,
            )


_register_variants()


# The order the desktop GUI lists the tabs in, so the web UI matches it.
JOB_ORDER: Tuple[str, ...] = (
    'tomo_import',
    'motioncorr',
    'ctffind',
    'tomo_exclude_tilt_images',
    'tomo_align_tilt_series',
    'tomo_reconstruct_tomograms',
    'tomo_denoise_tomograms',
    'tomo_pick_tomograms',
    'tomo_subtomo',
    'inimodel',
    'class3d',
    'autorefine',
    'multibody',
    'select',
    'tomo_recon_particle',
    'tomo_ctf_refine',
    'tomo_align_frame',
    'maskcreate',
    'joinstar',
    'postprocess',
    'localres',
    'modelangelo',
    'external',
)


def _insert_variants() -> Tuple[str, ...]:
    """``JOB_ORDER`` with each variant right after the job it is a branch of.

    The picker then reads "Select (metadata values)" directly under "Subset
    selection", which is where a RELION user looks for it.
    """
    order: List[str] = []
    for job_type in JOB_ORDER:
        order.append(job_type)
        for variant in BRANCH_VARIANTS.get(job_type, ()):
            order.append(str(variant['job_type']))
    return tuple(order)


JOB_ORDER = _insert_variants()


# Stages of a cryo-ET workflow, in the order a project passes through them.
#
# RELION's job list is a flat sequence (gui_mainwindow.cpp fills one browser),
# so this grouping is ours: it follows the pipeline the jobs form rather than
# any label in the C++. Anything not listed falls back to "Other", so a job type
# added later still appears.
JOB_CATEGORIES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        'Import and preprocess',
        ('tomo_import', 'motioncorr', 'ctffind', 'tomo_exclude_tilt_images'),
    ),
    ('Align and reconstruct', ('tomo_align_tilt_series', 'tomo_reconstruct_tomograms')),
    ('Enhance and pick', ('tomo_denoise_tomograms', 'tomo_pick_tomograms')),
    ('Subtomogram averaging', ('tomo_subtomo', 'inimodel', 'class3d', 'autorefine', 'multibody')),
    ('Refine and reconstruct', ('tomo_recon_particle', 'tomo_ctf_refine', 'tomo_align_frame')),
    ('Select', ('select',)),
    # Order follows JOB_ORDER, where mask creation comes before post-processing.
    ('Post-process and interpret', ('maskcreate', 'postprocess', 'localres', 'modelangelo')),
    ('Utilities', ('joinstar', 'external')),
)


def job_categories() -> List[dict]:
    """Group the selectable job types for the picker.

    Each entry carries the base type *and* its variants, in `JOB_ORDER` order,
    so a variant appears under the job it is a mode of. Every selectable type
    lands in exactly one category; the assertion at the end is what keeps that
    true when a job is added.
    """
    groups: List[dict] = []
    placed: set = set()

    for title, bases in JOB_CATEGORIES:
        members: List[str] = []
        for base in bases:
            for job_type in selectable_job_types():
                if base_job_type(job_type) == base and job_type not in placed:
                    members.append(job_type)
                    placed.add(job_type)
        if members:
            groups.append({'title': title, 'job_types': members})

    leftover = [t for t in selectable_job_types() if t not in placed]
    if leftover:
        groups.append({'title': 'Other', 'job_types': leftover})

    assert placed | set(leftover) == set(selectable_job_types()), (
        'every job type must be in exactly one category'
    )
    return groups


def selectable_job_types() -> Tuple[str, ...]:
    """``JOB_ORDER`` without the API-only aliases.

    This is what the picker shows: every desktop job, plus each branch variant
    under the job it belongs to. An alias (``select`` for ``select_values``)
    builds the identical command, so listing both would offer the same job
    twice under two names.
    """
    return tuple(
        job_type
        for job_type in JOB_ORDER
        if COMMAND_SPECS[job_type].alias_of is None
    )


def base_job_type(job_type: str) -> str:
    """The C++ job a variant belongs to, or the job itself.

    Variants share their base job's pipeline identity (directory, process label,
    output-node conventions), so any code that only needs "what kind of process
    is this" asks here rather than testing every variant name.
    """
    for base, variants in BRANCH_VARIANTS.items():
        if any(str(variant['job_type']) == job_type for variant in variants):
            return base
    return job_type


def is_variant(job_type: str) -> bool:
    return base_job_type(job_type) != job_type


def variant_fields(job_type: str) -> Optional[Tuple[str, ...]]:
    """The form fields a variant uses, or None when it is not a variant.

    A variant form shows only its own branch's options, so the user is not asked
    to fill in four other modes' settings.
    """
    base = base_job_type(job_type)
    if base == job_type:
        return None
    for variant in BRANCH_VARIANTS.get(base, ()):
        if str(variant['job_type']) == job_type:
            return tuple(variant.get('fields', ()))
    return None


def variant_inputs(job_type: str) -> Tuple[str, ...]:
    """The input options a variant accepts, in priority order.

    Empty for a whole job, whose input is derived from what it requires.
    """
    base = base_job_type(job_type)
    if base == job_type:
        return ()
    for variant in BRANCH_VARIANTS.get(base, ()):
        if str(variant['job_type']) == job_type:
            return tuple(variant.get('inputs', ()))
    return ()


# ---------------------------------------------------------------- conversion
def flag(name: str, style: str) -> str:
    """``binned_angpix`` -> ``--binned_angpix`` or ``--binned-angpix``."""
    if style == 'hyphen':
        return '--' + name.replace('_', '-')
    return '--' + name


def _is_true(value) -> bool:
    """Values the GUI stores for a boolean, as Python may hand them over."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ('yes', 'true', '1')


def _present(value) -> bool:
    return value is not None and str(value).strip() != ''


def _positive(value) -> bool:
    """True for a number greater than zero, as ``getNumber() > 0`` tests."""
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _emit(command: List[str], name: str, value, style: str) -> None:
    """Append ``--flag value`` when the value is set."""
    if not _present(value):
        return
    command.extend([flag(name, style), str(value)])


def _emit_bool(command: List[str], name: str, value, style: str) -> None:
    """Append a bare ``--flag`` when the option is true.

    RELION's C++ programs treat presence as true and absence as false, and the
    Typer wrappers declare ``typer.Option(False)``, so a false value must be
    omitted rather than passed as ``--flag false``.
    """
    if _is_true(value):
        command.append(flag(name, style))


def _emit_int(command: List[str], name: str, value, style: str) -> None:
    """Emit an integer-valued option without a trailing ``.0``.

    The schema stores every slider as a float, because that is how RELION's
    ``JobOption`` constructor takes it. Typer's ``int`` options reject ``"5.0"``,
    so whole numbers are rendered without a decimal part.
    """
    if not _present(value):
        return
    try:
        number = float(value)
        rendered = str(int(number)) if number == int(number) else str(number)
    except (TypeError, ValueError):
        rendered = str(value)
    command.extend([flag(name, style), rendered])


def _merged(spec: CommandSpec, options: Dict[str, object]) -> Dict[str, object]:
    """The option set the builder sees: the caller's, plus the spec's fixed ones.

    ``static`` is applied last on purpose. A variant's static values are the
    switch that selects its branch (``do_split`` for ``select_split``), and the
    form - which carries the base job's defaults - sends every other mode as
    false. Applying the caller's options over ``static`` would let those false
    defaults unset the branch; the branch switch is a property of the job type,
    not something a form field may override.
    """
    merged = dict(options)
    merged.update(spec.static)
    return merged


# ------------------------------------------------------------------ builders
def _build_tomo_import(spec: CommandSpec, options: Dict[str, object], out: str) -> List[str]:
    """``getCommandsTomoImportJob`` (pipeline_jobs.cpp:6563).

    Two branches, now two job types: ``tomo_import_serialem`` runs the Typer app
    (hyphenated flags, ``--output-directory``) and ``tomo_import_coordinates``
    runs a C++ utility (underscore flags, ``--o``). Each spec carries its own
    executable and flag style, so one builder serves both.
    """
    if spec.branch == 'coordinates':
        return _build_tomo_import_coordinates(spec, options, out)

    style = spec.flag_style
    command = [spec.executable, spec.subcommand or '']
    # --tilt-image-movie-pattern is passed twice by the C++ (once early, once
    # with the optics group); only the last occurrence wins, so emit it once.
    _emit(command, 'tilt_image_movie_pattern', options.get('movie_files'), style)
    _emit(command, 'mdoc_file_pattern', options.get('mdoc_files'), style)
    _emit(command, 'nominal_tilt_axis_angle', options.get('tilt_axis_angle'), style)
    _emit(command, 'nominal_pixel_size', options.get('angpix'), style)
    _emit(command, 'voltage', options.get('kV'), style)
    _emit(command, 'spherical_aberration', options.get('Cs'), style)
    _emit(command, 'amplitude_contrast', options.get('Q0'), style)
    _emit(command, 'optics_group_name', options.get('optics_group_name'), style)
    # The two dose spellings are mutually exclusive in the C++ as well.
    if _is_true(options.get('dose_is_per_movie_frame')):
        _emit(command, 'dose_per_movie_frame', options.get('dose_rate'), style)
    else:
        _emit(command, 'dose_per_tilt_image', options.get('dose_rate'), style)
    _emit(command, 'prefix', options.get('prefix'), style)
    _emit(command, 'mtf_file', options.get('mtf_file'), style)
    _emit_bool(command, 'invert_defocus_handedness',
               options.get('flip_tiltseries_hand'), style)
    _emit_bool(command, 'images_are_motion_corrected',
               options.get('images_are_motion_corrected'), style)
    command.extend([spec.output_style, out])
    _emit_other_args(command, options)
    return command


def _build_tomo_import_coordinates(
    spec: CommandSpec, options: Dict[str, object], out: str
) -> List[str]:
    """The coordinates branch of ``getCommandsTomoImportJob``.

    This branch runs a C++ program (``relion_tomo_import_coordinates``) rather
    than the SerialEM Typer app, so it needs underscore flags even though the
    job's other branch is hyphenated.
    """
    style = spec.flag_style
    command = [spec.executable]
    _emit(command, 'i', options.get('in_coords'), style)
    command.extend(['--o', out])
    _emit(command, 'remove_substring', options.get('remove_substring'), style)
    _emit(command, 'remove_substring2', options.get('remove_substring2'), style)
    _emit_bool(command, 'centered', options.get('is_center'), style)
    _emit(command, 'scale_factor', options.get('scale_factor'), style)
    _emit(command, 'add_factor', options.get('add_factor'), style)
    return command


def _build_motioncorr(spec: CommandSpec, options: Dict[str, object], out: str) -> List[str]:
    """``getCommandsMotioncorrJob`` (pipeline_jobs.cpp:1524)."""
    style = spec.flag_style
    command = [spec.executable]
    _emit(command, 'i', options.get('input_star_mics'), style)
    command.extend(['--o', out])
    _emit_bool(command, 'even_odd_split', options.get('do_even_odd_split'), style)
    # The C++ always passes --use_own here because the GUI drives its own
    # executable; the web UI does the same.
    command.append('--use_own')
    _emit_int(command, 'j', options.get('nr_threads'), style)
    _emit_bool(command, 'float16', options.get('do_float16'), style)
    _emit_bool(command, 'use_motioncor2', options.get('do_own_motioncor'), style)
    _emit(command, 'motioncor2_exe', options.get('fn_motioncor2_exe'), style)
    if _present(options.get('other_motioncor2_args')):
        command.extend(
            ['--other_motioncor2_args', f" {options['other_motioncor2_args']} "]
        )
    _emit(command, 'gpu', options.get('gpu_ids'), style)
    _emit(command, 'defect_file', options.get('fn_defect'), style)
    _emit_int(command, 'bin_factor', options.get('bin_factor'), style)
    _emit(command, 'bfactor', options.get('bfactor'), style)
    _emit_int(command, 'patch_x', options.get('patch_x'), style)
    _emit_int(command, 'patch_y', options.get('patch_y'), style)
    _emit_int(command, 'eer_grouping', options.get('eer_grouping'), style)
    _emit_int(command, 'group_frames', options.get('group_frames'), style)
    _emit(command, 'gainref', options.get('fn_gain_ref'), style)
    # gain_rot / gain_flip are radio labels like "90 degrees (1)"; the C++
    # converts them to the integer in brackets.
    _emit(command, 'gain_rot', _index_from_radio(options.get('gain_rot')), style)
    _emit(command, 'gain_flip', _index_from_radio(options.get('gain_flip')), style)
    _emit_bool(command, 'dose_weighting', options.get('do_dose_weighting'), style)
    _emit_bool(command, 'save_noDW', options.get('do_save_noDW'), style)
    if _is_true(options.get('do_save_ps')):
        _emit_int(command, 'grouping_for_ps', options.get('group_for_ps'), style)
    _emit_other_args(command, options)
    return command


def _build_ctffind(spec: CommandSpec, options: Dict[str, object], out: str) -> List[str]:
    """``getCommandsCtffindJob`` (pipeline_jobs.cpp:1762)."""
    style = spec.flag_style
    command = [spec.executable]
    _emit(command, 'i', options.get('input_star_mics'), style)
    command.extend(['--o', out])
    # The C++ uses the capitalised CTFFIND4 spellings for these.
    for name, variable in (
        ('Box', 'box'),
        ('ResMin', 'resmin'),
        ('ResMax', 'resmax'),
        ('dFMin', 'dfmin'),
        ('dFMax', 'dfmax'),
        ('FStep', 'dfstep'),
        ('dAst', 'dast'),
        ('localsearch_nominal_defocus', 'localsearch_nominal_defocus'),
        ('exp_factor_dose', 'exp_factor_dose'),
    ):
        _emit(command, name, options.get(variable), style)
    _emit_bool(command, 'use_noDW', options.get('do_save_noDW'), style)
    _emit_bool(command, 'do_phaseshift', options.get('do_phaseshift'), style)
    if _is_true(options.get('do_phaseshift')):
        _emit(command, 'phase_min', options.get('phase_min'), style)
        _emit(command, 'phase_max', options.get('phase_max'), style)
        _emit(command, 'phase_step', options.get('phase_step'), style)
    # fn_ctffind_exe is set by the GUI from a fixed location; only emit when
    # the caller supplied one, so the program uses its own default otherwise.
    _emit(command, 'ctffind_exe', options.get('fn_ctffind_exe'), style)
    _emit(command, 'ctfWin', options.get('ctf_win'), style)
    _emit_bool(command, 'is_ctffind4', True, style)
    _emit_bool(command, 'fast_search', options.get('do_fast_search'), style)
    _emit_bool(command, 'use_given_ps', options.get('use_given_ps'), style)
    _emit_other_args(command, options)
    return command


def _build_tomo_exclude(spec: CommandSpec, options: Dict[str, object], out: str) -> List[str]:
    """``getCommandsTomoExcludeTiltImagesJob`` (pipeline_jobs.cpp:7145)."""
    style = spec.flag_style
    command = [spec.executable]
    _emit(command, 'tilt_series_star_file', options.get('in_tiltseries'), style)
    _emit_int(command, 'cache_size', options.get('cache_size'), style)
    command.extend([spec.output_style, out])
    _emit_other_args(command, options)
    return command


def _build_tomo_align_tilt_series(
    spec: CommandSpec, options: Dict[str, object], out: str
) -> List[str]:
    """``getCommandsTomoAlignTiltSeriesJob`` (pipeline_jobs.cpp:6677).

    Exactly one alignment method must be chosen - the C++ counts the three
    switches and refuses anything but one - so each method is its own job type
    and the builder switches on the spec's branch rather than on option values.
    A form therefore cannot present an invalid combination at all.
    """
    style = spec.flag_style
    command = [spec.executable]
    _emit(command, 'i', options.get('in_tiltseries'), style)
    command.extend(['--o', out])
    _emit(command, 'tomogram_thickness', options.get('tomogram_thickness'), style)

    if spec.branch == 'imod_fiducials':
        command.append('--imod_fiducials')
        _emit(command, 'fiducial_diameter', options.get('fiducial_diameter'), style)
        _emit(command, 'batchtomo_exe', options.get('fn_batchtomo_exe'), style)
    elif spec.branch == 'imod_patchtrack':
        command.append('--imod_patchtrack')
        _emit(command, 'patch_size', options.get('patch_size'), style)
        _emit(command, 'patch_overlap', options.get('patch_overlap'), style)
        _emit(command, 'batchtomo_exe', options.get('fn_batchtomo_exe'), style)
    else:
        command.append('--aretomo2')
        _emit(command, 'aretomo_exe', options.get('fn_aretomo_exe'), style)
        _emit_bool(command, 'aretomo_tiltcorrect',
                   options.get('do_aretomo_tiltcorrect'), style)
        if _is_true(options.get('do_aretomo_tiltcorrect')):
            _emit(command, 'aretomo_tiltcorrect_angle',
                  options.get('aretomo_tiltcorrect_angle'), style)
        _emit_bool(command, 'aretomo_ctf', options.get('do_aretomo_ctf'), style)
        if _is_true(options.get('do_aretomo_ctf')):
            _emit_bool(command, 'aretomo_phaseshift',
                       options.get('do_aretomo_phaseshift'), style)
        _emit_bool(command, 'aretomo_reconstruct',
                   options.get('do_aretomo_reconstruct'), style)
        if _is_true(options.get('do_aretomo_reconstruct')):
            _emit_bool(command, 'aretomo_only_reconstruct',
                       options.get('do_skip_aretomo_align'), style)
            _emit(command, 'aretomo_VolZ', options.get('aretomo_VolZ'), style)
            _emit(command, 'aretomo_OutBin', options.get('aretomo_OutBin'), style)
        if _present(options.get('other_aretomo_args')):
            command.extend(
                ['--other_wrapper_args', f" {options['other_aretomo_args']} "]
            )
        _emit(command, 'gpu', options.get('gpu_ids'), style)
    _emit_other_args(command, options)
    return command


def _build_tomo_reconstruct(
    spec: CommandSpec, options: Dict[str, object], out: str
) -> List[str]:
    """``getCommandsTomoReconstructTomogramsJob`` (pipeline_jobs.cpp:6817)."""
    style = spec.flag_style
    command = [spec.executable]
    _emit(command, 't', options.get('in_tiltseries'), style)
    command.extend(['--o', out])
    _emit(command, 'tn', options.get('tomo_name'), style)
    _emit_bool(command, 'generate_split_tomograms',
               options.get('generate_split_tomograms'), style)
    _emit(command, 'w', options.get('xdim'), style)
    _emit(command, 'h', options.get('ydim'), style)
    _emit(command, 'd', options.get('zdim'), style)
    _emit(command, 'binned_angpix', options.get('binned_angpix'), style)
    # Fourier inversion: --fourier plus the two sub-flags, all in one block.
    if _is_true(options.get('do_fourier')):
        command.append('--fourier')
        _emit_bool(command, 'ctf_intact_first_peak',
                   options.get('ctf_intact_first_peak'), style)
        _emit_bool(command, 'skip_fourier_wiener',
                   options.get('do_skip_wiener'), style)
    if _is_true(options.get('do_proj')):
        command.append('--do_proj')
        _emit(command, 'centre_proj', options.get('centre_proj'), style)
        _emit(command, 'thickness_proj', options.get('thickness_proj'), style)
    # The C++ emits the offset only when non-zero.
    offset = options.get('tiltangle_offset')
    if _present(offset) and str(offset) not in ('0', '0.0', '0.00'):
        _emit(command, 'tiltangle_offset', offset, style)
    _emit_int(command, 'j', options.get('nr_threads'), style)
    _emit_other_args(command, options)
    return command


def _build_tomo_denoise(spec: CommandSpec, options: Dict[str, object], out: str) -> List[str]:
    """``getCommandsTomoDenoiseTomogramsJob`` (pipeline_jobs.cpp:6930).

    cryoCARE training and prediction are two subcommands with disjoint options,
    and the C++ refuses a job with both or neither. Each is its own job type, so
    the builder reads the branch from the spec instead of the options.
    """
    style = spec.flag_style
    command = [spec.executable, spec.subcommand or 'cryoCARE:predict']
    _emit(command, 'tomogram_star_file', options.get('in_tomoset'), style)
    _emit(command, 'cryocare_path', options.get('cryocare_path'), style)
    command.extend([spec.output_style, out])
    _emit(command, 'gpu', options.get('gpu_ids'), style)
    if spec.branch == 'train':
        # Training options only exist on the train subcommand.
        _emit(command, 'training_tomograms', options.get('tomograms_for_training'), style)
        _emit(command, 'number_training_subvolumes',
              options.get('number_training_subvolumes'), style)
        _emit(command, 'subvolume_sidelength',
              options.get('subvolume_dimensions'), style)
    else:
        _emit(command, 'tomogram_name', options.get('denoising_tomo_name'), style)
        _emit(command, 'model_file', options.get('care_denoising_model'), style)
        # --n-tiles takes three values.
        if all(_present(options.get(key)) for key in ('ntiles_x', 'ntiles_y', 'ntiles_z')):
            command.extend(
                [
                    flag('n_tiles', style),
                    str(options['ntiles_x']),
                    str(options['ntiles_y']),
                    str(options['ntiles_z']),
                ]
            )
    _emit_other_args(command, options)
    return command


def _build_tomo_pick(spec: CommandSpec, options: Dict[str, object], out: str) -> List[str]:
    """``getCommandsTomoPickTomogramsJob`` (pipeline_jobs.cpp:7050).

    The desktop GUI runs two programs (the picker, then the pose exporter). The
    architecture does not have an interactive picker, so the web job is the
    "particles from star" route (command0) plus the pose export (command2).
    That pair is what turns an existing annotated STAR file into particles.star,
    which is the only picking the web UI can drive without napari.
    """
    style = spec.flag_style
    # The pick mode is the subcommand, so it overrides the spec's default.
    mode = str(options.get('pick_mode') or spec.subcommand or 'particles')
    commands: List[str] = []

    if mode == 'particles' and _present(options.get('in_star_file')):
        get_poses = ['relion_python_tomo_get_particle_poses', 'particles-from-star']
        _emit(get_poses, 'tomograms_file', options.get('in_tomoset'), style)
        _emit(get_poses, 'annotations_directory', out + 'annotations', style)
        _emit(get_poses, 'in_star_file', options.get('in_star_file'), style)
        commands.append(_join(get_poses))

    picker = [spec.executable, mode]
    _emit(picker, 'tilt_series_star_file', options.get('in_tomoset'), style)
    _emit(picker, 'output_directory', out, style)
    commands.append(_join(picker))

    exporter = ['relion_python_tomo_get_particle_poses', mode]
    _emit(exporter, 'tilt_series_star_file', options.get('in_tomoset'), style)
    _emit(exporter, 'annotations_directory', out + 'annotations', style)
    _emit(exporter, 'output_directory', out, style)
    if mode != 'particles':
        _emit(exporter, 'spacing_angstroms', options.get('particle_spacing'), style)
    commands.append(_join(exporter))

    # Multiple programs are chained with && by RELION's own final command.
    return [' && '.join(commands)]


def _build_tomo_subtomo(spec: CommandSpec, options: Dict[str, object], out: str) -> List[str]:
    """``getCommandsTomoSubtomoJob`` (pipeline_jobs.cpp:7199)."""
    style = spec.flag_style
    command = [spec.executable, '--theme', 'classic', '--o', out]
    _emit_optimisation_inputs(command, options, style, flag='i')
    _emit(command, 'box_size_A', options.get('box_size_angst'), style)
    _emit(command, 'precrop_size_A', options.get('precrop_size_angst'), style)
    _emit(command, 'binned_size_pix', options.get('binned_size_pix'), style)
    # subtomo_format radio chooses between 2D stacks and real subtomos.
    fmt = str(options.get('subtomo_format') or '')
    if fmt == '2D stacks':
        command.append('--stack2d')
    elif fmt == '3D subtomos':
        command.append('--real_subtomo')
    _emit_bool(command, 'float16', options.get('do_float16'), style)
    _emit_int(command, 'j', options.get('nr_threads'), style)
    _emit_other_args(command, options)
    return command


def _build_inimodel(spec: CommandSpec, options: Dict[str, object], out: str) -> List[str]:
    """``getCommandsInimodelJob`` (pipeline_jobs.cpp:3613).

    Two programs: relion_refine --denovo_3dref, then relion_align_symmetry.
    """
    style = spec.flag_style
    command = [spec.executable]
    _emit(command, 'continue', options.get('fn_cont'), style)
    command.extend(['--o', out + 'run'])
    _emit_int(command, 'iter', options.get('nr_iter'), style)
    command.extend(['--grad', '--denovo_3dref'])
    _emit(command, 'sigma_tilt', options.get('sigma_tilt'), style)
    _emit_tomo_or_image_input(command, options, style, is_tomo=True)
    command.append('--ctf')
    _emit_bool(command, 'ctf_intact_first_peak',
               options.get('ctf_intact_first_peak'), style)
    _emit_int(command, 'K', options.get('nr_classes'), style)
    symmetry = options.get('sym_name')
    _emit(command, 'sym', symmetry if _present(symmetry) else 'C1', style)
    _emit_bool(command, 'flatten_solvent', options.get('do_flatten_solvent'), style)
    _emit_bool(command, 'zero_mask', options.get('do_zero_mask'), style)
    _emit_scratch_options(command, options, style)
    _emit(command, 'pad', 1, style)
    _emit(command, 'particle_diameter', options.get('particle_diameter'), style)
    command.extend(
        ['--oversampling', '1', '--healpix_order', '1', '--offset_range', '6',
         '--offset_step', '2', '--auto_sampling']
    )
    _emit(command, 'tau2_fudge', options.get('tau_fudge'), style)
    _emit_int(command, 'j', options.get('nr_threads'), style)
    _emit(command, 'gpu', options.get('gpu_ids'), style)
    _emit_other_args(command, options)

    align = ['relion_align_symmetry']
    _emit(align, 'i', out + 'run_it025_model.mrc', style)
    _emit(align, 'o', out + 'initial_model.mrc', style)
    align.extend(['--sym', 'C1', '--apply_sym', '--select_largest_class'])
    return [_join(command), _join(align)]


def _build_class3d(spec: CommandSpec, options: Dict[str, object], out: str) -> List[str]:
    """``getCommandsClass3DJob`` (pipeline_jobs.cpp:3830)."""
    style = spec.flag_style
    command = [spec.executable]
    _emit(command, 'continue', options.get('fn_cont'), style)
    command.extend(['--o', out + 'run'])
    _emit(command, 'sigma_tilt', options.get('sigma_tilt'), style)
    _emit_tomo_or_image_input(command, options, style, is_tomo=True)
    _emit(command, 'ref', options.get('fn_ref'), style)
    _emit_bool(command, 'firstiter_cc', options.get('do_firstiter_cc'), style)
    _emit_bool(command, 'trust_ref_size', options.get('do_trust_ref_size'), style)
    _emit(command, 'ini_high', options.get('ini_high'), style)
    _emit_scratch_options(command, options, style)
    _emit(command, 'pad', 1, style)
    _emit_bool(command, 'ctf', options.get('do_ctf_correction'), style)
    _emit_bool(command, 'ctf_intact_first_peak',
               options.get('ctf_intact_first_peak'), style)
    _emit_int(command, 'iter', options.get('nr_iter'), style)
    _emit(command, 'tau2_fudge', options.get('tau_fudge'), style)
    _emit(command, 'particle_diameter', options.get('particle_diameter'), style)
    _emit_bool(command, 'fast_subsets', options.get('do_fast_subsets'), style)
    _emit_int(command, 'K', options.get('nr_classes'), style)
    _emit_bool(command, 'flatten_solvent', options.get('do_flatten_solvent'), style)
    _emit_bool(command, 'zero_mask', options.get('do_zero_mask'), style)
    _emit(command, 'strict_highres_exp', options.get('highres_limit'), style)
    _emit(command, 'solvent_mask', options.get('fn_mask'), style)
    _emit_bool(command, 'skip_align', options.get('do_skip_align'), style)
    # Angular sampling: the GUI shows a sampling interval, and the C++ turns it
    # into a healpix order plus one oversampling level.
    _emit_sampling(command, options, style)
    if _present(options.get('sigma_angles')):
        try:
            sigma_ang = float(options['sigma_angles']) / 3.0
            _emit(command, 'sigma_ang', sigma_ang, style)
        except (TypeError, ValueError):
            pass
    _emit(command, 'relax_sym', options.get('relax_sym'), style)
    _emit(command, 'offset_range', options.get('offset_range'), style)
    _emit(command, 'offset_step', options.get('offset_step'), style)
    _emit_bool(command, 'allow_coarser_sampling',
               options.get('do_allow_coarser'), style)
    _emit(command, 'sym', options.get('sym_name') or 'C1', style)
    command.extend(['--norm', '--scale'])
    _emit_helical_options(command, options, style)
    _emit_int(command, 'j', options.get('nr_threads'), style)
    _emit(command, 'gpu', options.get('gpu_ids'), style)
    _emit_other_args(command, options)
    return command


def _build_autorefine(spec: CommandSpec, options: Dict[str, object], out: str) -> List[str]:
    """``getCommandsAutorefineJob`` (pipeline_jobs.cpp:4452)."""
    style = spec.flag_style
    command = [spec.executable]
    _emit(command, 'continue', options.get('fn_cont'), style)
    command.extend(['--o', out + 'run'])
    command.extend(['--auto_refine', '--split_random_halves'])
    _emit(command, 'sigma_tilt', options.get('sigma_tilt'), style)
    _emit_tomo_or_image_input(command, options, style, is_tomo=True)
    _emit(command, 'ref', options.get('fn_ref'), style)
    _emit_bool(command, 'firstiter_cc', options.get('do_firstiter_cc'), style)
    _emit_bool(command, 'trust_ref_size', options.get('do_trust_ref_size'), style)
    _emit(command, 'ini_high', options.get('ini_high'), style)
    _emit_scratch_options(command, options, style)
    _emit(command, 'pad', 1, style)
    command.extend(['--auto_ignore_angles', '--auto_resol_angles'])
    _emit_bool(command, 'ctf', options.get('do_ctf_correction'), style)
    _emit_bool(command, 'ctf_intact_first_peak',
               options.get('ctf_intact_first_peak'), style)
    _emit(command, 'particle_diameter', options.get('particle_diameter'), style)
    _emit_bool(command, 'flatten_solvent', options.get('do_flatten_solvent'), style)
    _emit_bool(command, 'zero_mask', options.get('do_zero_mask'), style)
    _emit(command, 'solvent_mask', options.get('fn_mask'), style)
    _emit_bool(command, 'solvent_correct_fsc',
               options.get('do_solvent_fsc'), style)
    _emit_sampling(command, options, style, auto_local=True)
    _emit(command, 'sym', options.get('sym_name') or 'C1', style)
    command.extend(['--norm', '--scale'])
    _emit_helical_options(command, options, style)
    _emit_int(command, 'j', options.get('nr_threads'), style)
    _emit(command, 'gpu', options.get('gpu_ids'), style)
    _emit_other_args(command, options)
    return command


def _build_multibody(spec: CommandSpec, options: Dict[str, object], out: str) -> List[str]:
    """``getCommandsMultiBodyJob`` (pipeline_jobs.cpp:4905).

    Three programs: relion_refine, relion_flex_analyse, then the movie analysis.
    """
    style = spec.flag_style
    refine = [spec.executable]
    if _present(options.get('fn_cont')):
        _emit(refine, 'continue', options.get('fn_cont'), style)
    else:
        _emit(refine, 'continue', options.get('fn_in'), style)
    refine.extend(['--o', out + 'run'])
    refine.extend(
        ['--solvent_correct_fsc', '--multibody_masks',
         str(options.get('fn_bodies') or '')]
    )
    # Same sampling arithmetic as the other relion_refine jobs: the GUI's
    # interval becomes a healpix order, and multibody always searches locally.
    _emit_sampling(refine, options, style, auto_local=True)
    _emit(refine, 'offset_range', options.get('offset_range'), style)
    # The offset step is scaled by the oversampling level, as the C++ does.
    step = _scaled(options.get('offset_step'), 2.0 ** OVERSAMPLING)
    if step is not None:
        _emit(refine, 'offset_step', step, style)
    _emit_scratch_options(refine, options, style)
    _emit(refine, 'pad', 1, style)
    _emit(refine, 'j', options.get('nr_threads'), style)
    _emit(refine, 'gpu', options.get('gpu_ids'), style)
    _emit_other_args(refine, options)

    flex = ['relion_flex_analyse']
    flex.extend(['--model', out + 'run_model.star', '--data', out + 'run_data.star'])
    _emit(flex, 'bodies', options.get('fn_bodies'), style)
    flex.extend(['--o', out + 'analyse', '--do_maps'])
    _emit(flex, 'k', options.get('nr_movies'), style)
    _emit(flex, 'select_eigenvalue', options.get('select_eigenval'), style)
    _emit(flex, 'select_eigenvalue_min', options.get('eigenval_min'), style)
    _emit(flex, 'select_eigenvalue_max', options.get('eigenval_max'), style)
    return [_join(refine), _join(flex)]


def _build_select(spec: CommandSpec, options: Dict[str, object], out: str) -> List[str]:
    """``getCommandsSelectJob`` (pipeline_jobs.cpp:2788).

    The C++ is a chain of mutually exclusive modes, each choosing a different
    program *and* a different output name, and it refuses a job that picks more
    than one:

        do_filaments        -> relion_filament_selection  (optimiser.star in)
        do_remove_duplicates-> relion_star_handler        (particles in)
        do_select_values    -> relion_star_handler --select       \\
        do_discard          -> relion_star_handler --discard_on_stats  > one mode
        do_split            -> relion_star_handler --split        /
        do_class_ranker     -> relion_class_ranker        (2D/3D class selection)
        otherwise           -> relion_display --gui       (interactive, GUI only)

    Each of those is exposed as its own job type (``select_values``,
    ``select_discard``, ``select_split``, ``select_duplicates``,
    ``select_filaments``, ``select_class_ranker``), so the form asks only for the
    options that mode uses and the command has no dead branches. ``select``
    itself is the value-selection mode, which is what it always built in
    practice - but it used to leave ``do_select_values`` false, so ``job.star``
    told the desktop GUI a different story from the command that ran.

    The one mode with no web equivalent is the interactive ``relion_display``
    branch: it opens a GUI, which a headless job cannot do.
    """
    style = spec.flag_style
    mode = spec.branch or 'values'

    if mode == 'filaments':
        command = ['relion_filament_selection']
        _emit(command, 'i', options.get('fn_model'), style)
        command.extend(['-o', out])
        _emit(command, 't', options.get('dendrogram_threshold'), style)
        _emit(command, 'c', options.get('dendrogram_minclass'), style)
        _emit_other_args(command, options)
        return command

    if mode == 'duplicates':
        command = [spec.executable]
        _emit(command, 'i', options.get('fn_data'), style)
        command.extend(['--o', out + 'particles.star'])
        _emit(command, 'remove_duplicates', options.get('duplicate_threshold'), style)
        # The C++ only passes the pixel size when it is positive.
        if _positive(options.get('image_angpix')):
            _emit(command, 'image_angpix', options.get('image_angpix'), style)
        _emit_other_args(command, options)
        return command

    if mode == 'class_ranker':
        command = ['relion_class_ranker']
        _emit(command, 'opt', options.get('fn_model'), style)
        # --o takes the job directory, then the two output names are fixed.
        command.extend(
            [
                '--o', out,
                '--fn_sel_parts', 'particles.star',
                '--fn_sel_classavgs', 'class_averages.star',
            ]
        )
        # The C++ passes at most one of the two minimum-count options.
        if _positive(options.get('select_nr_parts')):
            _emit(command, 'select_min_nr_particles',
                  options.get('select_nr_parts'), style)
        elif _positive(options.get('select_nr_classes')):
            _emit(command, 'select_min_nr_classes',
                  options.get('select_nr_classes'), style)
        command.extend(['--fn_root', 'rank', '--do_granularity_features',
                        '--auto_select'])
        _emit(command, 'min_score', options.get('rank_threshold'), style)
        _emit_other_args(command, options)
        return command

    # Value selection, discarding on statistics, and splitting all run the same
    # program; only the flags differ. The input is a micrograph or particle STAR
    # file, never an optimiser.star, and each gets its own output name.
    command = [spec.executable]
    if _present(options.get('fn_mic')):
        _emit(command, 'i', options.get('fn_mic'), style)
        output = out + 'micrographs.star'
    else:
        _emit(command, 'i', options.get('fn_data'), style)
        output = out + 'particles.star'
    command.extend(['--o', output])

    if mode == 'discard':
        command.append('--discard_on_stats')
        _emit(command, 'discard_label', options.get('discard_label'), style)
        _emit(command, 'discard_sigma', options.get('discard_sigma'), style)
    elif mode == 'split':
        command.append('--split')
        if _is_true(options.get('do_random')):
            command.append('--random_order')
        # Each of the two is passed only when positive; -1 means "unset" here.
        if _positive(options.get('nr_split')):
            _emit_int(command, 'nr_split', options.get('nr_split'), style)
        if _positive(options.get('split_size')):
            _emit_int(command, 'size_split', options.get('split_size'), style)
    else:
        # `values`: the metadata-filter mode.
        _emit(command, 'select', options.get('select_label'), style)
        _emit(command, 'minval', options.get('select_minval'), style)
        _emit(command, 'maxval', options.get('select_maxval'), style)
    _emit_other_args(command, options)
    return command


def _build_tomo_recon_particle(
    spec: CommandSpec, options: Dict[str, object], out: str
) -> List[str]:
    """``getCommandsTomoReconPartJob`` (pipeline_jobs.cpp:7597)."""
    style = spec.flag_style
    command = [spec.executable, '--theme', 'classic', '--o', out]
    _emit_optimisation_inputs(command, options, style, flag='i')
    _emit(command, 'b', options.get('box_size'), style)
    _emit(command, 'bin', options.get('binning'), style)
    _emit(command, 'nr_helical_asu', options.get('helical_nr_asu'), style)
    _emit(command, 'helical_twist', options.get('helical_twist'), style)
    _emit(command, 'helical_rise', options.get('helical_rise'), style)
    _emit_int(command, 'j', options.get('nr_threads'), style)
    _emit_int(command, 'j_out', options.get('nr_threads'), style)
    command.extend(['--j_in', '1'])
    _emit(command, 'sym', options.get('sym_name') or 'C1', style)
    _emit_other_args(command, options)
    if not _is_true(options.get('do_helix')):
        return command

    # The helical branch then imposes the symmetry on each reconstructed map.
    commands = [_join(command)]
    for name in ('half1', 'half2', 'merged'):
        impose = ['relion_helix_toolbox', '--impose']
        _emit(impose, 'i', out + name + '.mrc', style)
        _emit(impose, 'o', out + 'helical_' + name + '.mrc', style)
        _emit(impose, 'twist', options.get('helical_twist'), style)
        _emit(impose, 'rise', options.get('helical_rise'), style)
        z_percent = _scaled(options.get('helical_z_percentage'), 0.01)
        if z_percent is not None:
            _emit(impose, 'z_percentage', z_percent, style)
        _emit(impose, 'cyl_outer_diameter',
              options.get('helical_tube_outer_diameter'), style)
        commands.append(_join(impose))
    return commands


def _build_tomo_ctf_refine(
    spec: CommandSpec, options: Dict[str, object], out: str
) -> List[str]:
    """``getCommandsTomoCtfRefineJob`` (pipeline_jobs.cpp:7303)."""
    style = spec.flag_style
    command = [spec.executable, '--theme', 'classic', '--o', out]
    _emit_optimisation_inputs(command, options, style, flag='i')
    # An explicit half-map pair overrides what the optimisation set carries.
    _emit_half_maps(command, options, style)
    _emit(command, 'mask', options.get('in_refmask'), style)
    _emit(command, 'fsc', options.get('in_post'), style)
    _emit(command, 'b', options.get('box_size'), style)
    _emit_bool(command, 'do_defocus', options.get('do_defocus'), style)
    if _is_true(options.get('do_defocus')) and _present(options.get('focus_range')):
        # The C++ passes -focus_range as d0 to build a symmetric search range.
        command.extend(['--d0', '-' + str(options['focus_range'])])
        _emit(command, 'd1', options.get('focus_range'), style)
    if _is_true(options.get('do_defocus')):
        command.extend(['--do_reg_defocus', '--lambda', str(options.get('lambda') or '')])
    _emit_bool(command, 'do_scale', options.get('do_scale'), style)
    if _is_true(options.get('do_even_aberrations')):
        command.append('--do_even_aberrations')
        _emit_int(command, 'ne', options.get('nr_even_aberr'), style)
    if _is_true(options.get('do_odd_aberrations')):
        command.append('--do_odd_aberrations')
        _emit_int(command, 'no', options.get('nr_odd_aberr'), style)
    if _is_true(options.get('is_continue')):
        command.append('--only_do_unfinished')
    if _is_true(options.get('is_continue')):
        command.append('--only_do_unfinished')
    _emit_int(command, 'j', options.get('nr_threads'), style)
    _emit_other_args(command, options)
    return command


def _build_tomo_align_frame(
    spec: CommandSpec, options: Dict[str, object], out: str
) -> List[str]:
    """``getCommandsTomoAlignJob`` (pipeline_jobs.cpp:7441)."""
    style = spec.flag_style
    command = [spec.executable, '--theme', 'classic', '--o', out]
    _emit_optimisation_inputs(command, options, style, flag='i')
    _emit_half_maps(command, options, style)
    _emit(command, 'mask', options.get('in_refmask'), style)
    _emit(command, 'fsc', options.get('in_post'), style)
    _emit(command, 'b', options.get('box_size'), style)
    _emit(command, 'r', options.get('max_error'), style)
    _emit_bool(command, 'shift_only', options.get('do_shift_only'), style)
    _emit_bool(command, 'shift_only_by_particles',
               options.get('do_shift_only_by_particles'), style)
    if _is_true(options.get('do_motion')):
        command.append('--motion')
        _emit(command, 's_vel', options.get('sigma_vel'), style)
        _emit(command, 's_div', options.get('sigma_div'), style)
        _emit_bool(command, 'sq_exp_ker', options.get('do_sq_exp_ker'), style)
    if _is_true(options.get('is_continue')):
        command.append('--only_do_unfinished')
    _emit_int(command, 'j', options.get('nr_threads'), style)
    _emit_other_args(command, options)
    return command


def _build_maskcreate(spec: CommandSpec, options: Dict[str, object], out: str) -> List[str]:
    """``getCommandsMaskcreateJob`` (pipeline_jobs.cpp:4998)."""
    style = spec.flag_style
    command = [spec.executable]
    _emit(command, 'i', options.get('fn_in'), style)
    command.extend(['--o', out + 'mask.mrc'])
    _emit(command, 'lowpass', options.get('lowpass_filter'), style)
    _emit(command, 'angpix', options.get('angpix'), style)
    _emit(command, 'ini_threshold', options.get('inimask_threshold'), style)
    _emit(command, 'extend_inimask', options.get('extend_inimask'), style)
    _emit(command, 'width_soft_edge', options.get('width_mask_edge'), style)
    _emit_helical_mask(command, options, style)
    _emit_int(command, 'j', options.get('nr_threads'), style)
    _emit_other_args(command, options)
    return command


def _build_joinstar(spec: CommandSpec, options: Dict[str, object], out: str) -> List[str]:
    """``getCommandsJoinstarJob`` (pipeline_jobs.cpp:5073)."""
    style = spec.flag_style
    parts = [
        options.get('fn_part1'), options.get('fn_part2'),
        options.get('fn_part3'), options.get('fn_part4'),
    ]
    inputs = ' '.join(str(item) for item in parts if _present(item))
    if not inputs.strip():
        raise ValueError('joinstar needs at least one input STAR file')
    command = [spec.executable, '--combine', '--i', f' {inputs} ']
    command.extend(['--check_duplicates', 'rlnImageName'])
    command.extend(['--o', out + 'join_particles.star'])
    _emit_other_args(command, options)
    return command


def _build_postprocess(spec: CommandSpec, options: Dict[str, object], out: str) -> List[str]:
    """``getCommandsPostprocessJob`` (pipeline_jobs.cpp:5365)."""
    style = spec.flag_style
    command = [spec.executable]
    _emit(command, 'mask', options.get('fn_mask'), style)
    _emit(command, 'i', options.get('fn_in'), style)
    command.extend(['--o', out + 'postprocess'])
    _emit(command, 'angpix', options.get('angpix'), style)
    _emit(command, 'mtf', options.get('fn_mtf'), style)
    _emit(command, 'mtf_angpix', options.get('mtf_angpix'), style)
    if _is_true(options.get('do_auto_bfac')):
        command.append('--auto_bfac')
        _emit(command, 'autob_lowres', options.get('autob_lowres'), style)
    else:
        _emit(command, 'adhoc_bfac', options.get('adhoc_bfac'), style)
    _emit_bool(command, 'skip_fsc_weighting',
               options.get('do_skip_fsc_weighting'), style)
    _emit(command, 'low_pass', options.get('low_pass'), style)
    _emit_other_args(command, options)
    return command


def _build_localres(spec: CommandSpec, options: Dict[str, object], out: str) -> List[str]:
    """``getCommandsLocalresJob`` (pipeline_jobs.cpp:5510).

    Two mutually exclusive branches, now two job types (``localres_resmap`` and
    ``localres_relion``) because the C++ refuses a job with both or neither
    selected:

    * ResMap (``do_resmap_locres``) runs the user's ResMap executable with
      ResMap's own flags (``--pVal``, ``--minRes``, ``--stepRes``).
    * Relion (``do_relion_locres``) runs ``relion_postprocess --locres``, which
      knows nothing about those flags and reads its own.

    Emitting one branch's flags to the other program is silent: an unknown
    argument is only a warning, so the job would run with local resolution off
    its default grid.
    """
    style = spec.flag_style
    # The branch is fixed by the job type, so there is no "both or neither"
    # state to reject: the form cannot express one.
    resmap = spec.branch == 'resmap'

    command: List[str] = []
    if resmap:
        executable = str(options.get('fn_resmap') or '').strip()
        if not executable:
            raise ValueError('please provide an executable for the ResMap program')
        if not _present(options.get('fn_mask')):
            raise ValueError(
                'please provide an input mask for ResMap local-resolution estimation'
            )
        command.append(executable)
        command.append('--maskVol=' + str(options.get('fn_mask')))
        command.append(
            '--noguiSplit ' + out + 'half1.mrc ' + out + 'half2.mrc'
        )
        command.append('--vxSize=' + str(options.get('angpix')))
        command.append('--pVal=' + str(options.get('pval')))
        command.append('--minRes=' + str(options.get('minres')))
        command.append('--maxRes=' + str(options.get('maxres')))
        command.append('--stepRes=' + str(options.get('stepres')))
        _emit_other_args(command, options)
        return command

    command = [spec.executable, '--locres']
    _emit(command, 'i', options.get('fn_in'), style)
    command.extend(['--o', out + 'relion'])
    _emit(command, 'angpix', options.get('angpix'), style)
    _emit(command, 'adhoc_bfac', options.get('adhoc_bfac'), style)
    _emit(command, 'mtf', options.get('fn_mtf'), style)
    _emit(command, 'mask', options.get('fn_mask'), style)
    _emit_other_args(command, options)
    return command


def _build_modelangelo(
    spec: CommandSpec, options: Dict[str, object], out: str
) -> List[str]:
    """``getCommandsModelAngeloJob`` (pipeline_jobs.cpp:5803).

    Unlike the other split jobs, these two modes are not alternatives: the C++
    always builds a model and then, only if ``do_hhmer`` is set, runs a second
    ``hmm_search`` program in the same job. So ``modelangelo_build`` is the
    build command and ``modelangelo_hmm`` is that command plus the search.

    model_angelo's own CLI takes single-dash flags (``-i``, ``-f``, ``-o``,
    ``-a``, ``-v``, ``-d``) plus the double-dash HMM parameters. Routing them
    through the job's underscore/hyphen style produced ``--v``/``--d``/``--a``,
    which model_angelo rejects.
    """
    build = [spec.executable]
    # `build <seq flags>` and `build_no_seq` are alternatives, so emitting both
    # would pass an unknown subcommand.
    if any(_present(options.get(key)) for key in ('p_seq', 'd_seq', 'r_seq')):
        build.append('build')
        for flag, variable in (('-pf', 'p_seq'), ('-df', 'd_seq'), ('-rf', 'r_seq')):
            if _present(options.get(variable)):
                build.extend([flag, str(options[variable])])
    else:
        build.append('build_no_seq')
    build.extend(['-v', str(options.get('fn_map'))])
    build.extend(['-o', out])
    build.extend(['-d', str(options.get('gpu_id'))])
    _emit_other_args(build, options)

    if spec.branch != 'hmm':
        return build

    if not _present(options.get('fn_lib')):
        raise ValueError('the HMM search needs a sequence library (fn_lib)')
    search = [spec.executable, 'hmm_search']
    for flag, variable in (('-i', 'out'), ('-f', 'fn_lib'), ('-o', 'out'),
                           ('-a', 'alphabet')):
        value = out if variable == 'out' else options.get(variable)
        if _present(value):
            search.extend([flag, str(value)])
    for name in ('F1', 'F2', 'F3', 'E'):
        if _present(options.get(name)):
            search.extend(['--' + name, str(options[name])])
    _emit_other_args(search, options)
    return [_join(build), _join(search)]


def _build_external(spec: CommandSpec, options: Dict[str, object], out: str) -> List[str]:
    """``getCommandsExternalJob`` (pipeline_jobs.cpp:6265)."""
    style = spec.flag_style
    executable = str(options.get('fn_exe') or '').strip()
    if not executable:
        raise ValueError('external job needs fn_exe (the program to run)')
    command = [executable]
    command.extend(['--o', out])
    for name, variable in (
        ('in_movies', 'in_mov'),
        ('in_mics', 'in_mic'),
        ('in_parts', 'in_part'),
        ('in_coords', 'in_coords'),
        ('in_3dref', 'in_3dref'),
        ('in_mask', 'in_mask'),
    ):
        _emit(command, name, options.get(variable), style)
    # Up to ten user-defined --label value pairs.
    for index in range(1, 11):
        label = options.get(f'param{index}_label')
        value = options.get(f'param{index}_value')
        if _present(label) and _present(value):
            command.extend(['--' + str(label), str(value)])
    _emit_int(command, 'j', options.get('nr_threads'), style)
    _emit_other_args(command, options)
    return command


BUILDERS = {
    'tomo_import': _build_tomo_import,
    'motioncorr': _build_motioncorr,
    'ctffind': _build_ctffind,
    'tomo_exclude_tilt_images': _build_tomo_exclude,
    'tomo_align_tilt_series': _build_tomo_align_tilt_series,
    'tomo_reconstruct_tomograms': _build_tomo_reconstruct,
    'tomo_denoise_tomograms': _build_tomo_denoise,
    'tomo_pick_tomograms': _build_tomo_pick,
    'tomo_subtomo': _build_tomo_subtomo,
    'inimodel': _build_inimodel,
    'class3d': _build_class3d,
    'autorefine': _build_autorefine,
    'multibody': _build_multibody,
    'select': _build_select,
    'tomo_recon_particle': _build_tomo_recon_particle,
    'tomo_ctf_refine': _build_tomo_ctf_refine,
    'tomo_align_frame': _build_tomo_align_frame,
    'maskcreate': _build_maskcreate,
    'joinstar': _build_joinstar,
    'postprocess': _build_postprocess,
    'localres': _build_localres,
    'modelangelo': _build_modelangelo,
    'external': _build_external,
}

# Every select variant runs through the same builder, which switches on the
# spec's branch.
for _variant_base, _variant_specs in BRANCH_VARIANTS.items():
    for _variant in _variant_specs:
        BUILDERS[str(_variant['job_type'])] = BUILDERS[_variant_base]


# ------------------------------------------------------------------ helpers
def _join(parts: Sequence[str]) -> str:
    return ' '.join(part for part in parts if part)


def _emit_other_args(command: List[str], options: Dict[str, object]) -> None:
    extra = options.get('other_args')
    if _present(extra):
        command.append(str(extra))


def _emit_scratch_options(
    command: List[str], options: Dict[str, object], style: str
) -> None:
    """The five options the SPA refine jobs share for scratch handling."""
    command.extend(['--dont_combine_weights_via_disc', '--no_parallel_disc_io',
                    '--preread_images'])
    _emit(command, 'scratch_dir', options.get('scratch_dir'), style)
    _emit_int(command, 'pool', options.get('nr_pool'), style)


# The GUI's angular-sampling choices, in the order pipeline_jobs.h declares
# them. `getHealPixOrder` returns index+1 for the chosen label, and the C++
# always passes one oversampling level on top (`iover = 1`), so the healpix
# order it emits is index+1-1 = the index. Keeping the table here reproduces
# that arithmetic instead of guessing a number.
SAMPLING_OPTIONS = (
    '30 degrees', '15 degrees', '7.5 degrees', '3.7 degrees', '1.8 degrees',
    '0.9 degrees', '0.5 degrees', '0.2 degrees', '0.1 degrees',
)
OVERSAMPLING = 1


def _healpix_order(sampling) -> Optional[int]:
    """``"7.5 degrees"`` -> 2, as ``getHealPixOrder`` then minus iover."""
    text = str(sampling or '').strip()
    if text in SAMPLING_OPTIONS:
        return SAMPLING_OPTIONS.index(text) + 1 - OVERSAMPLING
    return None


def _emit_sampling(
    command: List[str],
    options: Dict[str, object],
    style: str,
    *,
    auto_local: bool = False,
) -> None:
    """The angular-sampling block the C++ emits for a non-skipped alignment.

    ``--oversampling`` is always 1 there, and ``--healpix_order`` is the chosen
    sampling's healpix order minus that oversampling. Auto-refine additionally
    repeats the order for its local searches.
    """
    _emit_int(command, 'oversampling', OVERSAMPLING, style)
    order = _healpix_order(options.get('sampling'))
    if order is not None:
        _emit_int(command, 'healpix_order', order, style)
        if auto_local:
            _emit_int(command, 'auto_local_healpix_order', order, style)


def _emit_helical_options(
    command: List[str], options: Dict[str, object], style: str
) -> None:
    """The helical block of ``getCommandsClass3DJob``/``AutorefineJob``.

    The C++ divides the GUI's percentages and search ranges by 100 and by 3
    respectively, so the values passed here are not the ones the form shows.
    """
    if not _is_true(options.get('do_helix')):
        return
    command.append('--helix')
    _emit(command, 'helical_inner_diameter',
          options.get('helical_tube_inner_diameter'), style)
    _emit(command, 'helical_outer_diameter',
          options.get('helical_tube_outer_diameter'), style)
    _emit(command, 'helical_nr_asu', options.get('helical_nr_asu'), style)
    _emit(command, 'helical_twist_initial', options.get('helical_twist_initial'), style)
    _emit(command, 'helical_rise_initial', options.get('helical_rise_initial'), style)
    # The GUI stores a percentage; the programs take a fraction.
    z_percent = _scaled(options.get('helical_z_percentage'), 0.01)
    if z_percent is not None:
        _emit(command, 'helical_z_percentage', z_percent, style)

    if _is_true(options.get('do_local_search_helical_symmetry')):
        command.append('--helical_symmetry_search')
        _emit(command, 'helical_twist_min', options.get('helical_twist_min'), style)
        _emit(command, 'helical_twist_max', options.get('helical_twist_max'), style)
        step = _positive_or_none(options.get('helical_twist_inistep'))
        if step is not None:
            _emit(command, 'helical_twist_inistep', step, style)
        _emit(command, 'helical_rise_min', options.get('helical_rise_min'), style)
        _emit(command, 'helical_rise_max', options.get('helical_rise_max'), style)
        rise_step = _positive_or_none(options.get('helical_rise_inistep'))
        if rise_step is not None:
            _emit(command, 'helical_rise_inistep', rise_step, style)
    if _is_true(options.get('keep_tilt_prior_fixed')):
        command.append('--helical_keep_tilt_prior_fixed')

    # The angular search ranges are only used when alignment is not skipped and
    # no local angular search was requested; the C++ clamps each to 0..90 and
    # passes a third of it.
    if (_is_true(options.get('dont_skip_align'))
            and not _is_true(options.get('do_local_ang_searches'))):
        for flag, variable in (
            ('sigma_tilt', 'range_tilt'),
            ('sigma_psi', 'range_psi'),
            ('sigma_rot', 'range_rot'),
        ):
            value = _clamped_third(options.get(variable))
            if value is not None:
                _emit(command, flag, value, style)
        distance = _positive_or_none(options.get('helical_range_distance'))
        if distance is not None:
            _emit(command, 'helical_sigma_distance', distance / 3.0, style)
            _emit(command, 'helical_nstart', options.get('helical_nstart'), style)


def _scaled(value, factor) -> Optional[float]:
    """``value * factor``, or None when the value is not a number."""
    try:
        return float(value) * factor
    except (TypeError, ValueError):
        return None


def _positive_or_none(value) -> Optional[float]:
    """The number when it is > 0, which is when the C++ emits it."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _clamped_third(value) -> Optional[float]:
    """The C++'s ``clamp(value, 0, 90) / 3`` for an angular search range."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(90.0, number)) / 3.0


def _emit_half_maps(
    command: List[str], options: Dict[str, object], style: str
) -> None:
    """``--ref1``/``--ref2`` from the one half-map the form asks for.

    The C++ takes a single half-map name and derives the other by swapping
    ``half1``/``half2`` in it (`FileName::getTheOtherHalf`). Reproducing that
    here means the form keeps asking for one file, as the GUI does.
    """
    value = options.get('in_halfmaps')
    if not _present(value):
        return
    other = _other_half(str(value))
    if other is None:
        raise ValueError(
            "cannot find 'half' in the half-map filename, so the other "
            f"half-map cannot be derived: {Path(str(value)).name}"
        )
    _emit(command, 'ref1', value, style)
    _emit(command, 'ref2', other, style)


def _other_half(name: str) -> Optional[str]:
    """``run_half1.mrc`` -> ``run_half2.mrc``, as getTheOtherHalf does."""
    for first, second in (('half1', 'half2'), ('half2', 'half1'),
                          ('half_1', 'half_2'), ('half_2', 'half_1')):
        if first in name:
            return name.replace(first, second, 1)
    return None


def _emit_helical_mask(
    command: List[str], options: Dict[str, object], style: str
) -> None:
    if not _is_true(options.get('do_helix')):
        return
    command.append('--helix')
    # The mask job divides the percentage by 100 as well.
    z_percent = _scaled(options.get('helical_z_percentage'), 0.01)
    if z_percent is not None:
        _emit(command, 'z_percentage', z_percent, style)


def _emit_optimisation_inputs(
    command: List[str],
    options: Dict[str, object],
    style: str,
    flag: str = 'ios',
) -> None:
    """``addTomoInputOptions`` / ``getTomoInputCommmand`` (pipeline_jobs.cpp:6409).

    The flag spelling is the C++'s ``is_for_refine`` argument, and it differs by
    program family:

    * the refine jobs (inimodel, class3d, autorefine, multibody) call
      ``getTomoInputCommmand(true, ...)``, which emits ``--ios`` because
      ``relion_refine`` reads a RELION optimisation set;
    * the JAZ tomography programs call ``getTomoInputCommmand(false, ...)``,
      which emits ``--i`` because ``OptimisationSet::read`` takes the set through
      ``parser.getOption("--i", "Optimisation set", "")``.

    Getting this backwards is silent: ``IOParser`` only warns about an unknown
    argument, so the program would run with no input at all.
    """
    _emit(command, flag, options.get('in_optimisation'), style)


def _emit_tomo_or_image_input(
    command: List[str],
    options: Dict[str, object],
    style: str,
    *,
    is_tomo: bool,
) -> None:
    """The input of a job that serves both a SPA and a tomography branch.

    ``inimodel``/``class3d``/``autorefine``/``multibody`` are shared with the
    single-particle GUI, and the C++ chooses the branch from the job's
    tomography flag:

    * a tomography job with an optimisation set gets ``--ios <set>``, because
      ``relion_refine`` reads ``rlnTomoParticleName`` and the tomogram links
      from that file;
    * otherwise it gets ``--i <particles.star>`` or ``--continue <model.star>``.

    The web UI registers these as tomography jobs, so the first branch is the
    one that applies whenever ``in_optimisation`` is set.
    """
    if is_tomo and _present(options.get('in_optimisation')):
        _emit_optimisation_inputs(command, options, style)
        return
    _emit(command, 'i', options.get('fn_img'), style)


def _as_mpi_count(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 1


def _index_from_radio(value) -> Optional[int]:
    """``"90 degrees (1)"`` -> ``1``; already-numeric values pass through."""
    if not _present(value):
        return None
    text = str(value)
    if '(' in text and ')' in text:
        inner = text[text.rfind('(') + 1 : text.rfind(')')].strip()
        try:
            return int(inner)
        except ValueError:
            pass
    try:
        return int(float(text))
    except ValueError:
        return None


# ---------------------------------------------------------------- validation
@dataclass(frozen=True)
class ValidationRule:
    """One cross-option check the C++ performs before building the command.

    RELION reports these from inside ``getCommands*`` (102 error messages across
    the jobs), so the web UI has to make the same checks *before* submitting;
    otherwise the failure only appears in the job's log after the directory and
    pipeline entry already exist.

    Only the rules that change the command's shape are here. The many "empty
    field for input STAR file" checks are covered by ``CommandSpec.required``,
    and the "cannot find 'half' in the filename" checks are properties of the
    input file rather than of the form.
    """

    job_type: str
    message: str
    # All of these must hold for the rule to fire.
    when: Dict[str, object]
    # Human-readable name of the option to point the user at.
    fields: Tuple[str, ...] = ()


def _flag(value) -> bool:
    return _is_true(value)


def _set(value) -> bool:
    return _present(value)


# Each entry quotes the C++ message it reproduces, so a reader can find it.
VALIDATION_RULES: Tuple[ValidationRule, ...] = (
    # "you should (only) select ONE of the alignment methods"
    ValidationRule(
        job_type='tomo_align_tilt_series',
        message=(
            'select exactly one alignment method: IMOD fiducials, IMOD '
            'patch tracking, or AreTomo'
        ),
        when={
            'count_true': ('do_imod_fiducials', 'do_imod_patchtrack', 'do_aretomo2'),
            'count_gt': 1,
        },
        fields=('do_imod_fiducials', 'do_imod_patchtrack', 'do_aretomo2'),
    ),
    # "you cannot generate tomograms for denoising with the Fourier-inversion
    #  from odd/even frames method! Disable at least one of them."
    ValidationRule(
        job_type='tomo_reconstruct_tomograms',
        message=(
            'denoising tomograms cannot use the Fourier-inversion method; turn '
            'off one of "Generate tomograms for denoising?" or "Use '
            'Fourier-inversion?"'
        ),
        when={'all_true': ('generate_split_tomograms', 'do_fourier')},
        fields=('generate_split_tomograms', 'do_fourier'),
    ),
    # "cryoCARE training is enabled but the tomograms to train on have not been
    #  specified."
    ValidationRule(
        job_type='tomo_denoise_tomograms',
        message='training needs the tomograms to train on',
        when={'all_true': ('do_cryocare_train',), 'empty': ('tomograms_for_training', '')},
        fields=('tomograms_for_training',),
    ),
    # "cryoCARE predict is enabled but path to the denoising model ... has not
    #  been specified."
    ValidationRule(
        job_type='tomo_denoise_tomograms',
        message='prediction needs the denoising model from a training job',
        when={
            'all_true': ('do_cryocare_predict',),
            'empty': ('care_denoising_model', ''),
        },
        fields=('care_denoising_model',),
    ),
    # "You must state desired GPU ID." - checked only on the predict path,
    # which is the branch the web builder takes when training is off.
    ValidationRule(
        job_type='tomo_denoise_tomograms',
        message='state the GPU to use',
        when={
            'all_true': ('do_cryocare_predict',),
            'empty': ('gpu_ids', ''),
        },
        fields=('gpu_ids',),
    ),
    # "you cannot use GPUs when skipping image alignments."
    ValidationRule(
        job_type='class3d',
        message='GPUs cannot be used when image alignment is skipped',
        when={'all_true': ('use_gpu',), 'all_false': ('dont_skip_align',)},
        fields=('use_gpu', 'dont_skip_align'),
    ),
    # "Per-particle motion and shift only corrections cannot be applied
    #  simultaneously."
    ValidationRule(
        job_type='tomo_align_frame',
        message='per-particle motion and shift-only correction are exclusive',
        when={
            'all_true': ('do_motion', 'do_shift_only_by_particles'),
        },
        fields=('do_motion', 'do_shift_only_by_particles'),
    ),
    # "per-tomogram scale estimation and per-frame scale estimation are mutually
    #  exclusive"
    ValidationRule(
        job_type='tomo_ctf_refine',
        message='per-tomogram and per-frame scale estimation are exclusive',
        when={'all_true': ('do_scale', 'do_scale_frame')},
        fields=('do_scale', 'do_scale_frame'),
    ),
    # "When splitting the input STAR file into subsets, set nr_split and/or
    #  split_size to a positive value"
    ValidationRule(
        job_type='select',
        message='splitting needs a positive subset size or number of subsets',
        when={
            'all_true': ('do_split',),
            'not_positive': ('nr_split', 'split_size'),
        },
        fields=('nr_split', 'split_size'),
    ),
    # "the maximum eigenvalue should be larger than the minimum one!"
    ValidationRule(
        job_type='multibody',
        message='the maximum eigenvalue must be larger than the minimum',
        when={
            'all_true': ('do_select',),
            'min_ge_max': ('eigenval_min', 'eigenval_max'),
        },
        fields=('eigenval_min', 'eigenval_max'),
    ),
    # "empty field for first or second input STAR file..."
    ValidationRule(
        job_type='joinstar',
        message='give at least two STAR files to combine',
        when={'missing_second': ('fn_part1', 'fn_part2')},
        fields=('fn_part1', 'fn_part2'),
    ),
    # "you need to provide a library to perform the HMM search against."
    ValidationRule(
        job_type='modelangelo',
        message='the HMM search needs a sequence library',
        when={'all_true': ('do_hhmer',), 'empty': ('fn_lib', '')},
        fields=('fn_lib',),
    ),
    # "empty field for the external executable script..."
    ValidationRule(
        job_type='external',
        message='give the program to run',
        when={'empty': ('fn_exe', '')},
        fields=('fn_exe',),
    ),
)


# Inputs that must name an existing file, and inputs whose name must contain
# "half" because the pair is resolved from it. These are the checks the C++ makes
# that depend on the filesystem rather than on the option combination.
FILE_INPUTS: Dict[str, Tuple[str, ...]] = {
    'postprocess': ('fn_in',),
    'localres': ('fn_in',),
    'tomo_ctf_refine': ('fn_in',),
    'tomo_align_frame': ('fn_in',),
    'maskcreate': ('fn_in',),
    'multibody': ('fn_in', 'fn_bodies'),
    'class3d': ('fn_img',),
    'autorefine': ('fn_img',),
    'inimodel': ('fn_img',),
    'tomo_pick_tomograms': ('in_tomoset',),
    'tomo_denoise_tomograms': ('in_tomoset',),
    'tomo_reconstruct_tomograms': ('in_tiltseries',),
    'tomo_align_tilt_series': ('in_tiltseries',),
    'tomo_exclude_tilt_images': ('in_tiltseries',),
    'motioncorr': ('input_star_mics',),
    'ctffind': ('input_star_mics',),
    'tomo_subtomo': ('in_optimisation',),
    'tomo_recon_particle': ('in_optimisation',),
}

# Jobs whose input is a half-map pair resolved by the "half" substring in the
# filename, as `getCommandsPostprocessJob` and friends require.
HALF_MAP_INPUTS: Dict[str, Tuple[str, ...]] = {
    'postprocess': ('fn_in',),
    'localres': ('fn_in',),
}


def validate_inputs(
    job_type: str, options: Dict[str, object], base: Optional[Path] = None
) -> List[str]:
    """Checks that need the filesystem: the input exists, and is what it claims.

    RELION reports "empty field for input STAR file" and "cannot find 'half' in
    the halfmap filename" from inside ``getCommands*``. Those cannot be answered
    by looking at the option values alone, so they are checked here against the
    project directory, and only when the input looks like a real path (a
    wildcard or an unset value is left to the program).
    """
    problems: List[str] = []

    for variable in FILE_INPUTS.get(job_type, ()):
        value = options.get(variable)
        if not _present(value):
            continue  # required-ness is CommandSpec's job
        text = str(value)
        if any(token in text for token in ('*', '?')):
            continue  # a pattern is resolved by the program
        candidate = Path(text)
        if not candidate.is_absolute():
            if base is None:
                # Without a project directory a relative path cannot be
                # resolved, so existence is left to the program rather than
                # reported as missing.
                continue
            candidate = Path(base) / candidate
        if not candidate.exists():
            problems.append(f'{variable}: {text} does not exist')

    for variable in HALF_MAP_INPUTS.get(job_type, ()):
        value = options.get(variable)
        if not _present(value):
            continue
        text = Path(str(value)).name.lower()
        if 'half' not in text:
            problems.append(
                f'{variable}: the filename must contain "half" so the other '
                f'half-map can be found ({Path(str(value)).name})'
            )

    return problems


def validate(job_type: str, options: Dict[str, object]) -> List[str]:
    """Cross-option problems that would make RELION refuse the job.

    Returns one message per problem, phrased for a user. An empty list means the
    option set passes the checks the C++ performs before building the command.
    """
    problems: List[str] = []
    # A variant's branch switch lives in its spec's `static` table, not in the
    # caller's options, so the rules that test that switch need it merged in
    # first - otherwise `do_cryocare_predict` reads as unset on a predict job.
    spec = COMMAND_SPECS.get(job_type)
    merged = _merged(spec, options) if spec is not None else dict(options)
    for rule in VALIDATION_RULES:
        if rule.job_type != base_job_type(job_type):
            continue
        if _applies(rule, merged):
            problems.append(rule.message)
    problems.extend(_variant_problems(job_type, merged))
    return problems


def _variant_problems(job_type: str, options: Dict[str, object]) -> List[str]:
    """Checks that belong to a variant's branch rather than to the whole job.

    A variant has already fixed the mode, so what is left is whether the input
    and values that mode needs are present. These are the C++ messages the
    branch would report, moved to editing time.
    """
    base = base_job_type(job_type)
    if variant_fields(job_type) is None:
        return []
    if base == 'localres' and job_type == 'localres_resmap':
        # The ResMap branch needs an external executable and a mask; the C++
        # reports both from inside getCommands, and the form should say so while
        # the user is still editing.
        problems: List[str] = []
        if not _present(options.get('fn_resmap')):
            problems.append('please provide an executable for the ResMap program')
        if not _present(options.get('fn_mask')):
            problems.append(
                'please provide an input mask for ResMap local-resolution estimation'
            )
        return problems

    if base != 'select':
        # The other split jobs state their branch's requirements through
        # `CommandSpec.required`, which is checked before this runs; only the
        # cross-option rules below need restating per branch.
        return []
    problems: List[str] = []
    has_input = any(_present(options.get(name)) for name in ('fn_data', 'fn_mic', 'fn_model'))

    if job_type == 'select_filaments':
        if _present(options.get('fn_mic')) or _present(options.get('fn_data')):
            problems.append(
                'filament selection only works on an optimiser STAR file'
            )
        if not _present(options.get('fn_model')):
            problems.append('filament selection needs an optimiser STAR file')
        return problems

    if job_type == 'select_class_ranker':
        if not _present(options.get('fn_model')):
            problems.append(
                'automatic class selection needs an optimiser STAR file'
            )
        if _flag(options.get('do_regroup')) or _flag(options.get('do_recenter')):
            problems.append(
                'regrouping and recentering are not implemented by the class ranker'
            )
        return problems

    if job_type == 'select_duplicates':
        if _present(options.get('fn_mic')) or _present(options.get('fn_model')):
            problems.append('duplicate removal only works on a particles STAR file')
        if not _present(options.get('fn_data')):
            problems.append('duplicate removal needs a particles STAR file')
        return problems

    # values / discard / split
    if _present(options.get('fn_model')):
        problems.append(
            'value selection and subset splitting only work on micrograph or '
            'particle STAR files'
        )
    if not has_input:
        problems.append('please select an input file')
    if job_type == 'select_split':
        if not _positive(options.get('nr_split')) and not _positive(options.get('split_size')):
            problems.append(
                'splitting needs a positive subset size or number of subsets'
            )
    return problems


def _applies(rule: ValidationRule, options: Dict[str, object]) -> bool:
    """Evaluate a rule's conditions against a concrete option set."""
    for test, argument in rule.when.items():
        if test == 'count_true':
            continue  # resolved by count_gt / count_ne below
        if test in ('count_gt', 'count_ne'):
            continue
        if test == 'all_true':
            if not all(_flag(options.get(name)) for name in argument):
                return False
        elif test == 'empty':
            name, _sentinel = argument
            if _present(options.get(name)):
                return False
        elif test == 'all_false':
            if any(_flag(options.get(name)) for name in argument):
                return False
        elif test == 'not_positive':
            positive = False
            for name in argument:
                try:
                    if float(options.get(name)) > 0:
                        positive = True
                except (TypeError, ValueError):
                    continue
            if positive:
                return False
        elif test == 'min_ge_max':
            first, second = argument
            try:
                if float(options.get(first)) < float(options.get(second)):
                    return False
            except (TypeError, ValueError):
                return False
        elif test == 'missing_second':
            first, second = argument
            if _present(options.get(first)) and _present(options.get(second)):
                return False

    names = rule.when.get('count_true')
    if names:
        count = sum(1 for name in names if _flag(options.get(name)))
        if 'count_gt' in rule.when and not count > rule.when['count_gt']:
            return False
        if 'count_ne' in rule.when and count == rule.when['count_ne']:
            return False
    return True


# --------------------------------------------------------------------- entry
def build_command(
    job_type: str,
    options: Dict[str, object],
    output_prefix: str,
    executable: Optional[str] = None,
) -> List[str]:
    """Return the argv for ``job_type``.

    ``output_prefix`` is the job directory with a trailing slash, matching what
    RELION's ``outputname`` is inside ``getCommands*``. Programs that want a
    directory rather than a prefix get it without the slash.

    A single-element list is the common case. Multi-program jobs (pick,
    multibody, inimodel) return one element holding a ``&&`` chain, which is
    what RELION's ``prepareFinalCommand`` produces and what ``/bin/sh -c``
    expects.
    """
    spec = COMMAND_SPECS.get(job_type)
    if spec is None:
        raise ValueError(
            f'unsupported job type {job_type!r}; '
            f'choose from {sorted(COMMAND_SPECS)}'
        )
    # The advanced panel's flags travel in `other_args`, which every builder
    # appends where its program expects them. Folding here rather than only in
    # JobManager means a caller that builds a command directly gets the same
    # argv as one that submits through the API.
    options, _unknown = cli_options.apply(job_type, options)
    builder = BUILDERS[job_type]
    merged = _merged(spec, options)

    # A continuation is a complete input in its own right: RELION's
    # `if (is_continue)` branch uses --continue and never reads fn_img, so the
    # image input must not be demanded when a checkpoint is given.
    continuing = bool(
        spec.continue_variable and _present(merged.get(spec.continue_variable))
    )
    required = spec.required
    if continuing:
        required = tuple(
            name for name in required if name not in spec.waived_when_continued
        )
        merged['is_continue'] = True
    # The refine jobs accept a tomography optimisation set instead of a
    # particles STAR file: `getTomoInputCommmand(true, ...)` emits --ios and
    # never reads fn_img, so demanding fn_img would reject a valid tomo job.
    if job_type in TOMO_INPUT_JOBS and _present(merged.get('in_optimisation')):
        required = tuple(name for name in required if name != 'fn_img')
    missing = [
        name
        for name in required
        if not _present(merged.get(name))
    ]
    if missing:
        raise ValueError(
            f'{job_type} is missing required option(s): {", ".join(missing)}'
        )

    problems = validate(job_type, merged)
    if problems:
        raise ValueError('; '.join(problems))

    # RELION swaps in the _mpi binary when nr_mpi > 1 instead of wrapping the
    # serial program in mpirun, so the same substitution happens here.
    if not executable and spec.mpi_executable and _as_mpi_count(merged.get('nr_mpi')) > 1:
        executable = spec.mpi_executable

    if executable:
        # A caller-supplied program replaces the primary one, which is how a
        # deployment switches to an MPI variant and how tests stand a shell
        # script in for the real executable.
        spec = CommandSpec(
            job_type=spec.job_type,
            cpp_name=spec.cpp_name,
            executable=executable,
            subcommand=spec.subcommand,
            flag_style=spec.flag_style,
            output_style=spec.output_style,
            required=spec.required,
            internal=spec.internal,
            static=spec.static,
            mpi_executable=spec.mpi_executable,
        )
    return builder(spec, merged, output_prefix)


def command_specs() -> Dict[str, CommandSpec]:
    return dict(COMMAND_SPECS)


def form_schema(job_type: str) -> dict:
    """The form description for one job, with command-line notes attached.

    The generated schema knows every GUI option; this adds what the form needs
    to know about the command line (flag style, subcommand, which fields this
    backend actually honours) so the UI can explain itself rather than hiding
    options that silently do nothing.

    ``cli_options`` is the program's full flag list, which is larger than the
    form: it is what the advanced panel offers.

    A variant (``select_split`` and friends) reports its base job's option
    metadata but only the fields its branch uses, plus its own title. The picker
    can then list "Select (split into subsets)" as a distinct job while the
    option descriptions still come from the one C++ job window.
    """
    base = base_job_type(job_type)
    if base not in SCHEMA_JOBS:
        raise KeyError(f'unknown job type {job_type!r}')
    from . import cli_options as cli_options_module

    job = SCHEMA_JOBS[base]
    spec = COMMAND_SPECS.get(job_type)
    variant = variant_fields(job_type)
    allowed = set(variant) if variant is not None else None
    fields = []
    for field in job['fields']:
        if allowed is not None and field['variable'] not in allowed:
            continue
        entry = dict(field)
        if spec is not None:
            entry['internal'] = field['variable'] in spec.internal
        fields.append(entry)

    # Trim the base job's tabs to the fields this variant kept, and always keep
    # the shared Run tab, which every job has.
    run_tab = next((tab for tab in job['tabs'] if tab['title'] == 'Run'), None)
    tabs = []
    for tab in job['tabs']:
        kept = [name for name in tab['fields'] if allowed is None or name in allowed]
        if kept:
            tabs.append({**tab, 'fields': kept})
    if run_tab is not None and not any(tab['title'] == 'Run' for tab in tabs):
        tabs.append(run_tab)

    extended, unavailable = cli_options_module.options_for(base)
    return {
        'job_type': job_type,
        'title': job['title'] if variant is None else _variant_title(base, job_type),
        'cpp_name': job['cpp_name'],
        'executable': spec.executable if spec else None,
        'subcommand': spec.subcommand if spec else None,
        'flag_style': spec.flag_style if spec else None,
        'required': list(spec.required) if spec else [],
        'fields': fields,
        'tabs': tabs,
        'cli_options': extended,
        'cli_options_unavailable': unavailable,
        # The C++ job this form is a branch of, so the UI can group variants
        # under one entry instead of pretending they are unrelated jobs.
        'base_job_type': base,
        'branch': spec.branch if spec else None,
    }


def _variant_title(base: str, job_type: str) -> str:
    for variant in BRANCH_VARIANTS.get(base, ()):
        if str(variant['job_type']) == job_type:
            return str(variant.get('title') or job_type)
    return job_type


def all_form_schemas() -> List[dict]:
    """Every job type the picker offers, in the desktop GUI's order.

    Variants are included: each reports its base job's option metadata limited
    to its own branch's fields, so the picker can offer them as separate jobs.
    API-only aliases are excluded, since they would duplicate another entry.
    """
    return [form_schema(job_type) for job_type in selectable_job_types()]
