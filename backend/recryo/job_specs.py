# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""Where each job goes in a RELION project, derived from the generated schema.

Everything here used to be a hand-copied table of C++ constants. It now comes
from ``generated_schema.py``, which the generator extracts from
``src/pipeline_jobs.h``, so a directory name or process label cannot drift from
what the GUI expects.

The remaining hand-written part is the *output nodes*: which files a job creates
and what node type label each carries. Those are interleaved with control flow
in ``getCommands*`` and are listed per job below, with the C++ function each one
comes from.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .generated_schema import JOBS as SCHEMA_JOBS
from . import job_commands
from .job_commands import COMMAND_SPECS


@dataclass(frozen=True)
class OutputNode:
    """One file a job writes, with the node type the GUI uses to route it."""

    filename: str
    type_label: str
    # A file that is only produced under some option, so the GUI should not be
    # surprised when it is absent.
    optional: bool = False


# Node type labels, resolved from src/pipeline_jobs.h.
LABEL_IMPORT_TOMOGRAMS = 'TomogramGroupMetadata.star.relion.tomo.import'
LABEL_IMPORT_TOMO_COORDS = 'ParticleGroupMetadata.star.relion.tomo.import'
LABEL_MOCORR_TOMOGRAMS = 'TomogramGroupMetadata.star.relion.tomo.motioncorr'
LABEL_MOCORR_MICS = 'MicrographGroupMetadata.star.relion.motioncorr'
LABEL_MOCORR_LOG = 'LogFile.pdf.relion.motioncorr'
LABEL_CTFFIND_TOMOGRAMS = 'TomogramGroupMetadata.star.relion.tomo.ctffind'
LABEL_CTFFIND_MICS = 'MicrographGroupMetadata.star.relion.ctf'
LABEL_CTFFIND_LOG = 'LogFile.pdf.relion.ctffind'
LABEL_CTFFIND_POWER_SPECTRA = 'Image2DGroupMetadata.star.relion.ctffind.power_spectra'
LABEL_EXCLUDE_TOMOGRAMS = 'TomogramGroupMetadata.star.relion.tomo.excludeimages'
LABEL_TILTALIGN_TOMOGRAMS = 'TomogramGroupMetadata.star.relion.tomo.aligntiltseries'
LABEL_TILTALIGN_LOG = 'LogFile.pdf.relion.tomo.aligntiltseries'
LABEL_RECONSTRUCT_TOMOGRAMS = 'TomogramGroupMetadata.star.relion.tomo.reconstruct'
LABEL_DENOISE_TOMOGRAMS = 'TomogramGroupMetadata.star.relion.tomo.denoise'
LABEL_TOMOPICK_PARTS = 'ParticleGroupMetadata.star.relion.tomo.manualpick.particles'
LABEL_TOMOPICK_OPTSET = 'TomoOptimisationSet.star.relion.tomo.manualpick'
LABEL_SUBTOMO_PARTS = 'ParticleGroupMetadata.star.relion.tomo.extract'
LABEL_SUBTOMO_OPTSET = 'TomoOptimisationSet.star.relion.tomo.extract'
LABEL_INIMOD_OPTSET = 'TomoOptimisationSet.star.relion.initialmodel'
LABEL_INIMOD_MAP = 'DensityMap.mrc.relion.initialmodel'
LABEL_CLASS3D_OPTSET = 'TomoOptimisationSet.star.relion.class3d'
LABEL_REFINE3D_OPTSET = 'TomoOptimisationSet.star.relion.refine3d'
LABEL_MULTIBODY_SEL_PARTS = (
    'ParticleGroupMetadata.star.relion.flexanalysis.eigenselected'
)
LABEL_MULTIBODY_FLEXLOG = 'LogFile.pdf.relion.flexanalysis'
LABEL_SELECT_OPT = 'OptimiserData.star.relion.select'
LABEL_SELECT_LOG = 'LogFile.pdf.relion.select'
LABEL_SELECT_PARTS = 'ParticleGroupMetadata.star.relion'
LABEL_SELECT_MICS = 'MicrographGroupMetadata.star.relion'
LABEL_SELECT_CLAVS = 'Image2DGroupMetadata.star.relion.classaverages'
LABEL_RECONSPART_MAP = 'DensityMap.mrc.relion.tomo.map.reconstruct'
LABEL_RECONSPART_HALFMAP = 'DensityMap.mrc.relion.tomo.halfmap.reconstruct'
LABEL_RECONSPART_OPTSET = 'TomoOptimisationSet.star.relion.tomo.reconstruct'
LABEL_CTFREFINE_OPTSET = 'TomoOptimisationSet.star.relion.tomo.ctfrefine'
LABEL_CTFREFINE_TOMOGRAMS = 'TomogramGroupMetadata.star.relion.tomo.ctfrefine'
LABEL_CTFREFINE_LOG = 'LogFile.pdf.relion.tomo.ctfrefine'
LABEL_FRAMEALIGN_OPTSET = 'TomoOptimisationSet.star.relion.tomo.polish'
LABEL_FRAMEALIGN_TOMOGRAMS = 'TomogramGroupMetadata.star.relion.tomo.polish'
LABEL_FRAMEALIGN_PARTS = 'ParticleGroupMetadata.star.relion.tomo.polish'
LABEL_FRAMEALIGN_TRAJS = 'TomoTrajectoryData.star.relion.polish'
LABEL_FRAMEALIGN_LOG = 'LogFile.pdf.relion.tomo.polish'
LABEL_MASK3D_MASK = 'Mask3D.mrc.relion'
LABEL_POST_MAP = 'DensityMap.mrc.relion.postprocess'
LABEL_POST_MASKED = 'DensityMap.mrc.relion.postprocess.masked'
LABEL_POST_LOG = 'LogFile.pdf.relion.postprocess'
LABEL_POST_POST = 'ProcessData.star.relion.postprocess'
LABEL_LOCRES_RESMAP = 'Image3D.mrc.resmap.localresmap'
LABEL_LOCRES_LOG = 'LogFile.pdf.relion.localres'
LABEL_LOCRES_FILTMAP = 'DensityMap.mrc.relion.localresfiltered'
LABEL_ATOMCOORDS_CPIPE = 'AtomCoords.cif'


# The nodes each getCommands* pushes, in the order the C++ does. Filenames are
# relative to the job directory.
OUTPUT_NODES: Dict[str, Tuple[OutputNode, ...]] = {
    # getCommandsTomoImportJob, SerialEM branch
    'tomo_import': (
        OutputNode('tilt_series.star', LABEL_IMPORT_TOMOGRAMS),
    ),
    # getCommandsMotioncorrJob: tomo mode emits the tilt-series set, SPA mode the
    # micrograph set. Both are registered so either input style works, but the
    # one the job did not write is optional.
    'motioncorr': (
        OutputNode('corrected_tilt_series.star', LABEL_MOCORR_TOMOGRAMS),
        OutputNode('corrected_micrographs.star', LABEL_MOCORR_MICS, optional=True),
        OutputNode('logfile.pdf', LABEL_MOCORR_LOG),
    ),
    # getCommandsCtffindJob
    'ctffind': (
        OutputNode('tilt_series_ctf.star', LABEL_CTFFIND_TOMOGRAMS),
        OutputNode('micrographs_ctf.star', LABEL_CTFFIND_MICS, optional=True),
        OutputNode('logfile.pdf', LABEL_CTFFIND_LOG),
        OutputNode('power_spectra_fits.star', LABEL_CTFFIND_POWER_SPECTRA, optional=True),
    ),
    # getCommandsTomoExcludeTiltImagesJob
    'tomo_exclude_tilt_images': (
        OutputNode('selected_tilt_series.star', LABEL_EXCLUDE_TOMOGRAMS),
    ),
    # getCommandsTomoAlignTiltSeriesJob: tomograms.star instead of
    # aligned_tilt_series.star when AreTomo also reconstructs.
    'tomo_align_tilt_series': (
        OutputNode('aligned_tilt_series.star', LABEL_TILTALIGN_TOMOGRAMS),
        OutputNode('logfile.pdf', LABEL_TILTALIGN_LOG),
    ),
    # getCommandsTomoReconstructTomogramsJob
    'tomo_reconstruct_tomograms': (
        OutputNode('tomograms.star', LABEL_RECONSTRUCT_TOMOGRAMS),
    ),
    # getCommandsTomoDenoiseTomogramsJob
    'tomo_denoise_tomograms': (
        OutputNode('tomograms.star', LABEL_DENOISE_TOMOGRAMS),
    ),
    # getCommandsTomoPickTomogramsJob
    'tomo_pick_tomograms': (
        OutputNode('particles.star', LABEL_TOMOPICK_PARTS),
        OutputNode('optimisation_set.star', LABEL_TOMOPICK_OPTSET),
    ),
    # getCommandsTomoSubtomoJob
    'tomo_subtomo': (
        OutputNode('optimisation_set.star', LABEL_SUBTOMO_OPTSET),
        OutputNode('particles.star', LABEL_SUBTOMO_PARTS),
    ),
    # getCommandsInimodelJob
    'inimodel': (
        OutputNode('run_optimisation_set.star', LABEL_INIMOD_OPTSET),
        OutputNode('initial_model.mrc', LABEL_INIMOD_MAP),
    ),
    # getCommandsClass3DJob
    'class3d': (
        OutputNode('run_optimisation_set.star', LABEL_CLASS3D_OPTSET),
    ),
    # getCommandsAutorefineJob
    'autorefine': (
        OutputNode('run_optimisation_set.star', LABEL_REFINE3D_OPTSET),
    ),
    # getCommandsMultiBodyJob
    'multibody': (
        OutputNode('analyse_sel_particles.star', LABEL_MULTIBODY_SEL_PARTS),
        OutputNode('analyse_logfile.pdf', LABEL_MULTIBODY_FLEXLOG),
    ),
    # getCommandsSelectJob, metadata-value branch (the one the web UI drives)
    # getTomoImportJob: the coordinates mode writes particles.star, the SerialEM
    # mode writes tilt_series.star.
    'tomo_import_serialem': (
        OutputNode('tilt_series.star', LABEL_IMPORT_TOMOGRAMS),
    ),
    'tomo_import_coordinates': (
        OutputNode('particles.star', LABEL_IMPORT_TOMO_COORDS),
    ),
    # getCommandsTomoAlignTiltSeriesJob: AreTomo2 with reconstruction writes
    # tomograms.star instead of aligned_tilt_series.star.
    'tomo_align_imod_fiducials': (
        OutputNode('aligned_tilt_series.star', LABEL_TILTALIGN_TOMOGRAMS),
        OutputNode('logfile.pdf', LABEL_TILTALIGN_LOG),
    ),
    'tomo_align_imod_patchtrack': (
        OutputNode('aligned_tilt_series.star', LABEL_TILTALIGN_TOMOGRAMS),
        OutputNode('logfile.pdf', LABEL_TILTALIGN_LOG),
    ),
    'tomo_align_aretomo': (
        OutputNode('aligned_tilt_series.star', LABEL_TILTALIGN_TOMOGRAMS),
        OutputNode('logfile.pdf', LABEL_TILTALIGN_LOG),
        # Written only when CTF estimation is also requested.
        OutputNode('power_spectra_fits.star', LABEL_CTFFIND_POWER_SPECTRA, optional=True),
    ),
    # getCommandsTomoDenoiseTomogramsJob: both modes write tomograms.star.
    'tomo_denoise_train': (
        OutputNode('tomograms.star', LABEL_DENOISE_TOMOGRAMS),
    ),
    'tomo_denoise_predict': (
        OutputNode('tomograms.star', LABEL_DENOISE_TOMOGRAMS),
    ),
    # getCommandsLocalresJob: ResMap writes its own map, RELION writes both.
    'localres_resmap': (
        OutputNode('half1_resmap.mrc', LABEL_LOCRES_RESMAP),
    ),
    'localres_relion': (
        OutputNode('relion_locres.mrc', LABEL_LOCRES_RESMAP),
        OutputNode('relion_locres_filtered.mrc', LABEL_LOCRES_FILTMAP),
        OutputNode('histogram.pdf', LABEL_LOCRES_LOG, optional=True),
    ),
    # getCommandsModelAngeloJob writes the same model in both modes; the HMM
    # search refines it in place.
    'modelangelo_build': (
        OutputNode('model.cif', LABEL_ATOMCOORDS_CPIPE),
    ),
    'modelangelo_hmm': (
        OutputNode('model.cif', LABEL_ATOMCOORDS_CPIPE),
    ),
    # getCommandsSelectJob. Each mode writes different files, so the nodes are
    # per-variant: the flow graph would otherwise claim a filament run produced
    # particles.star, which it does not.
    'select': (
        OutputNode('particles.star', LABEL_SELECT_PARTS),
    ),
    'select_values': (
        OutputNode('particles.star', LABEL_SELECT_PARTS),
        OutputNode('micrographs.star', LABEL_SELECT_MICS, optional=True),
    ),
    'select_discard': (
        OutputNode('particles.star', LABEL_SELECT_PARTS),
        OutputNode('micrographs.star', LABEL_SELECT_MICS, optional=True),
    ),
    'select_split': (
        OutputNode('particles.star', LABEL_SELECT_PARTS),
        OutputNode('micrographs.star', LABEL_SELECT_MICS, optional=True),
    ),
    'select_duplicates': (
        OutputNode('particles.star', LABEL_SELECT_PARTS),
    ),
    # The filament branch writes an optimiser set and a log, not particles.
    'select_filaments': (
        OutputNode('run_optimiser.star', LABEL_SELECT_OPT),
        OutputNode('logfile.pdf', LABEL_SELECT_LOG),
    ),
    'select_class_ranker': (
        OutputNode('particles.star', LABEL_SELECT_PARTS),
        OutputNode('class_averages.star', LABEL_SELECT_CLAVS),
        OutputNode('rank_optimiser.star', LABEL_SELECT_OPT),
    ),
    # getCommandsTomoReconPartJob
    'tomo_recon_particle': (
        OutputNode('merged.mrc', LABEL_RECONSPART_MAP),
        OutputNode('half1.mrc', LABEL_RECONSPART_HALFMAP),
        OutputNode('optimisation_set.star', LABEL_RECONSPART_OPTSET),
    ),
    # getCommandsTomoCtfRefineJob
    'tomo_ctf_refine': (
        OutputNode('optimisation_set.star', LABEL_CTFREFINE_OPTSET),
        OutputNode('tomograms.star', LABEL_CTFREFINE_TOMOGRAMS),
        OutputNode('logfile.pdf', LABEL_CTFREFINE_LOG),
    ),
    # getCommandsTomoAlignJob
    'tomo_align_frame': (
        OutputNode('optimisation_set.star', LABEL_FRAMEALIGN_OPTSET),
        OutputNode('tomograms.star', LABEL_FRAMEALIGN_TOMOGRAMS),
        OutputNode('particles.star', LABEL_FRAMEALIGN_PARTS),
        OutputNode('motion.star', LABEL_FRAMEALIGN_TRAJS),
        OutputNode('logfile.pdf', LABEL_FRAMEALIGN_LOG),
    ),
    # getCommandsMaskcreateJob
    'maskcreate': (
        OutputNode('mask.mrc', LABEL_MASK3D_MASK),
    ),
    # getCommandsJoinstarJob
    'joinstar': (
        OutputNode('join_particles.star', LABEL_SELECT_PARTS),
    ),
    # getCommandsPostprocessJob
    'postprocess': (
        OutputNode('postprocess.mrc', LABEL_POST_MAP),
        OutputNode('postprocess_masked.mrc', LABEL_POST_MASKED),
        OutputNode('logfile.pdf', LABEL_POST_LOG),
        OutputNode('postprocess.star', LABEL_POST_POST),
    ),
    # getCommandsLocalresJob
    'localres': (
        OutputNode('relion_locres.mrc', LABEL_LOCRES_RESMAP),
        OutputNode('relion_locres_filtered.mrc', LABEL_LOCRES_FILTMAP),
        OutputNode('histogram.pdf', LABEL_LOCRES_LOG),
    ),
    # getCommandsModelAngeloJob
    'modelangelo': (
        OutputNode('model.cif', LABEL_ATOMCOORDS_CPIPE),
    ),
    # getCommandsExternalJob creates nothing RELION knows about.
    'external': (),
}


@dataclass(frozen=True)
class JobSpec:
    """Everything needed to make one web job look native to the GUI."""

    job_type: str
    executable: str
    process_type_label: str
    directory_name: str
    output_nodes: Tuple[OutputNode, ...]
    # The option holding the job's single input node, when it has one. Jobs that
    # take an optimisation set (refine, extract, and every tomo refine step)
    # carry it here too, so the same code registers their input edge.
    input_variable: Optional[str] = None
    input_node_label: Optional[str] = None
    # The C++ job this is a branch of; the job itself for a whole job.
    base_job_type: Optional[str] = None
    # Which branch of the base job's builder this variant pins, when it is one.
    branch: Optional[str] = None
    # Variants keep their base job's option table, so the form can be limited to
    # the fields the branch uses while the labels and help text still come from
    # the one C++ job window.
    variant_fields: Optional[Tuple[str, ...]] = None
    # Input options a variant accepts, in priority order. A branch that takes
    # either a particles or a micrographs STAR file lists both; the first one
    # present in the arguments becomes the pipeline input edge.
    input_candidates: Tuple[str, ...] = ()

    @property
    def default_output_name(self) -> str:
        """The primary output, used when a single node has to be named."""
        for node in self.output_nodes:
            if not node.optional:
                return node.filename
        return ''

    @property
    def schema(self) -> dict:
        return SCHEMA_JOBS[self.base_job_type or self.job_type]

    @property
    def tab_title(self) -> str:
        return self.schema['title']


def _spec(job_type: str) -> JobSpec:
    base = job_commands.base_job_type(job_type)
    schema = SCHEMA_JOBS[base]
    command_spec = COMMAND_SPECS[job_type]
    variant_fields = job_commands.variant_fields(job_type)
    node_types = {
        field['variable']: field.get('node_type')
        for field in schema['fields']
        if field.get('kind') == 'input_node'
    }

    # Which option carries the job's main input node.
    #
    # A variant knows its own branch's inputs in priority order (`select_values`
    # takes fn_data before fn_mic), and a branch can accept either of two, so the
    # first one present in the actual arguments wins at submit time. A whole job
    # takes the first node-typed field it requires, so optional extra inputs (a
    # mask, a second reference) are not mistaken for the main one.
    input_variable = None
    input_node_label = None
    declared_inputs = job_commands.variant_inputs(job_type)
    if declared_inputs:
        input_variable = declared_inputs[0]
        input_node_label = node_types.get(input_variable)
    else:
        for field in schema['fields']:
            if field.get('kind') != 'input_node':
                continue
            if field['variable'] in command_spec.required:
                input_variable = field['variable']
                input_node_label = field.get('node_type')
                break
    return JobSpec(
        job_type=job_type,
        executable=command_spec.executable,
        process_type_label=schema['process_label'],
        directory_name=schema['directory'],
        # A variant writes a different set of files from its base job, so its
        # own entry wins when one exists.
        output_nodes=OUTPUT_NODES.get(job_type, OUTPUT_NODES.get(base, ())),
        input_variable=input_variable,
        input_node_label=input_node_label,
        base_job_type=base,
        branch=command_spec.branch,
        variant_fields=variant_fields,
        input_candidates=declared_inputs,
    )


JOB_SPECS: Dict[str, JobSpec] = {
    job_type: _spec(job_type)
    for job_type in COMMAND_SPECS
}


def get_spec(job_type: str) -> JobSpec:
    spec = JOB_SPECS.get(job_type)
    if spec is None:
        raise KeyError(
            f'no pipeline mapping for job type {job_type!r}; '
            f'known types: {sorted(JOB_SPECS)}'
        )
    return spec


def process_name_for(spec: JobSpec, job_number: int) -> str:
    """``Tomograms/job013`` for the given number."""
    return f'{spec.directory_name}/job{job_number:03d}'


def output_node_names(spec: JobSpec, process_name: str) -> List[Tuple[str, str]]:
    """``[(node name, type label), ...]`` for a job that has just finished.

    Node names are project-relative, matching what the GUI writes.
    """
    return [
        (f'{process_name}/{node.filename}', node.type_label)
        for node in spec.output_nodes
    ]
