# Coverage against the desktop GUI

This records what the web agent can do compared with `relion_maingui --tomo`,
what it deliberately does differently, and what is still missing. It exists so
"is it as complete as the GUI yet?" has an answer that can be checked rather
than argued about.

Last checked against RELION 5.1 (`src/pipeline_jobs.h`, `gui_mainwindow.cpp`).

## Job types

The desktop tomography GUI has 23 job tabs (`gui_mainwindow.cpp:408-660`).
All 23 are implemented. Every one has the real option form, the correct
executable and flags, and pipeline registration.

Six of the 23 are split further: a job window whose modes are mutually exclusive
becomes one job type per mode, so the picker offers 34 job types in total. See
"Jobs with mutually exclusive modes are split" below for why, and for the
guarantee that every variant still registers as its base job's process.

| Base job | Job types |
| --- | --- |
| `tomo_import` | `tomo_import_serialem`, `tomo_import_coordinates` |
| `tomo_align_tilt_series` | `tomo_align_imod_fiducials`, `tomo_align_imod_patchtrack`, `tomo_align_aretomo` |
| `tomo_denoise_tomograms` | `tomo_denoise_train`, `tomo_denoise_predict` |
| `select` | `select_values`, `select_discard`, `select_split`, `select_duplicates`, `select_filaments`, `select_class_ranker` |
| `localres` | `localres_resmap`, `localres_relion` |
| `modelangelo` | `modelangelo_build`, `modelangelo_hmm` |

| Tab | Web id | Executable | Command source |
| --- | --- | --- | --- |
| Import | `tomo_import` | `relion_python_tomo_import` | `getCommandsTomoImportJob` |
| Motion correction | `motioncorr` | `relion_run_motioncorr` | `getCommandsMotioncorrJob` |
| CTF estimation | `ctffind` | `relion_run_ctffind` | `getCommandsCtffindJob` |
| Exclude tilt-images | `tomo_exclude_tilt_images` | `relion_python_tomo_exclude_tilt_images` | `getCommandsTomoExcludeTiltImagesJob` |
| Align tilt-series | `tomo_align_tilt_series` | `relion_align_tiltseries` | `getCommandsTomoAlignTiltSeriesJob` |
| Reconstruct tomograms | `tomo_reconstruct_tomograms` | `relion_tomo_reconstruct_tomogram` | `getCommandsTomoReconstructTomogramsJob` |
| Denoise tomograms | `tomo_denoise_tomograms` | `relion_python_tomo_denoise` | `getCommandsTomoDenoiseTomogramsJob` |
| Pick tomograms | `tomo_pick_tomograms` | `relion_python_tomo_pick` | `getCommandsTomoPickTomogramsJob` |
| Extract subtomos | `tomo_subtomo` | `relion_tomo_subtomo` | `getCommandsTomoSubtomoJob` |
| 3D initial reference | `inimodel` | `relion_refine` | `getCommandsInimodelJob` |
| 3D classification | `class3d` | `relion_refine` | `getCommandsClass3DJob` |
| 3D auto-refine | `autorefine` | `relion_refine` | `getCommandsAutorefineJob` |
| 3D multi-body | `multibody` | `relion_refine` | `getCommandsMultiBodyJob` |
| Subset selection | `select` | `relion_star_handler` | `getCommandsSelectJob` |
| Reconstruct particle | `tomo_recon_particle` | `relion_tomo_reconstruct_particle` | `getCommandsTomoReconPartJob` |
| CTF refinement | `tomo_ctf_refine` | `relion_tomo_refine_ctf` | `getCommandsTomoCtfRefineJob` |
| Bayesian polishing | `tomo_align_frame` | `relion_tomo_align` | `getCommandsTomoAlignJob` |
| Mask creation | `maskcreate` | `relion_mask_create` | `getCommandsMaskcreateJob` |
| Join star files | `joinstar` | `relion_star_handler` | `getCommandsJoinstarJob` |
| Post-processing | `postprocess` | `relion_postprocess` | `getCommandsPostprocessJob` |
| Local resolution | `localres` | `relion_postprocess` | `getCommandsLocalresJob` |
| ModelAngelo building | `modelangelo` | `relion_python_modelangelo` | `getCommandsModelAngeloJob` |
| External | `external` | user-supplied | `getCommandsExternalJob` |

645 options in total, with the GUI's labels, defaults, ranges, radio choices and
help text, laid out in the GUI's own tabs.

## Every job can be started

The end-to-end requirement is "pick a job, fill its form, press Run, and have it
appear in the project". That works for all 23: each builder passes its output
flag, its subcommand where the program has one, its input node, and the
`--pipeline_control` directory. `test_web_commands.py` pins each of those
properties, and the tests run a shell script in place of the executable to prove
the lifecycle (directory, sentinels, `run.out`, pipeline status, node links).

The one honest caveat: no RELION binary was available while this was written, so
the commands were checked against the C++ argument parsers' rules rather than by
executing the real programs. The rules were read from the sources
(`args.cpp`, each Typer `cli.py`), and the format of the written STAR files was
validated with `starfile` and RELION's column conventions.

## Every flag, not just the ones the GUI shows

The desktop job window shows a renamed, merged subset of each program's flags, so
some values a program accepts cannot be set from any job window. Every form now
has an expandable **Advanced arguments** panel listing the program's real flags,
read from its own argument parser:

- C++ programs: `parser.getOption(...)` / `parser.checkOption(...)` in
  `motioncorr_runner.cpp`, `ml_optimiser.cpp`, the JAZ
  `tomography/programs/*.cpp`, and the rest (`webui/scripts/generate_web_cli_options.py`);
- Python programs: their Typer signatures.

1183 flags are extracted across the 23 jobs. Flags the form already controls are
marked, and may still be added — `IOParser::getOption` keeps the last occurrence,
so that is how a form value is overridden. Added flags travel in `other_args`,
which every builder already appends at the position its program expects, so the
command's shape does not change. An unknown flag is reported while the user is
still editing (`/api/job-validate`), because at run time
`checkForUnknownArguments` only writes a warning to the log.

`job.star` records `other_args` as well, so the GUI shows the same job and a
clone of it runs the same command.

### The extractor is a drift detector, not a generator

`webui/scripts/generate_web_job_commands.py` extracts 528 of the 564 `command +=`
statements from `getCommands*`. The builders are nevertheless hand-written,
because the remaining statements read local variables (`fn_sym`, `fn_out`,
`maps[i]`) and the multi-program jobs branch on `||` conditions — a mechanical
translation would have to invent rules for those, and the result would be harder
to trust than code that names the C++ line it follows.

Current state, measured: 528 of 564 `command +=` statements are extracted
(93.6%), and of the 46 the extractor cannot express, **41 are produced by the
hand-written builders anyway**. The five that are not are Select's interactive
`relion_display` branch (`--allow_save`, `--fn_parts`, `--fn_imgs`), which opens
a GUI window and is therefore not a web job at all.

Auditing that gap list is what found the last round of real defects, all now
fixed and pinned by tests:

- `--ref1`/`--ref2` were never emitted, although `in_halfmaps` is a form field —
  a refine job silently fell back to the optimisation set's reference;
- `--mask`/`--fsc` were ignored on both tomo refine jobs;
- the GUI's angular-sampling *interval* was passed through as `--healpix_order`
  instead of being converted to a healpix order;
- the helical block omitted `--helical_z_percentage` (and its /100 scaling),
  the symmetry-search flags, and the angular ranges' /3 scaling;
- `tomo_recon_particle`'s helical branch, three extra `relion_helix_toolbox`
  runs, was missing entirely;
- multibody passed sampling through instead of converting it, and did not scale
  `--offset_step` by the oversampling level.

`test_web_command_drift.py` therefore uses the extraction in the direction that
matters: **a builder may emit fewer flags than the C++, but never one the C++
does not pass.** That single invariant is what every command-line bug found here
had violated (`--ios` on a JAZ program, hyphenated flags on a C++ program,
ResMap flags on `relion_postprocess`, `--v` on model_angelo), and all four were
silent at run time because an unknown option is only warned about. When upstream
changes a flag, this test fails and names the builder to fix.

## Jobs with mutually exclusive modes are split

Some C++ job windows hold several mutually exclusive modes: the builder counts
its switches, refuses the job outright when more than one is on, and picks a
different program (sometimes a different flag style) per mode.
`getCommandsSelectJob` is the clearest case —

| switch | program | what it does |
| --- | --- | --- |
| `do_select_values` | `relion_star_handler --select` | keep rows by metadata value |
| `do_discard` | `relion_star_handler --discard_on_stats` | drop outliers by image statistics |
| `do_split` | `relion_star_handler --split` | split into subsets |
| `do_remove_duplicates` | `relion_star_handler --remove_duplicates` | remove duplicate particles |
| `do_filaments` | `relion_filament_selection` | dendrogram filament clustering |
| `do_class_ranker` | `relion_class_ranker` | automatic 2D class selection |

Presenting that as one form means five switches of which at most one may be on,
most of them irrelevant to whatever the user actually wanted, and — the reason
this was changed — every extracted command rule ends up guarded by conditions
that are false for the mode being run. That is precisely why the translation
could not be parsed automatically.

Each mode is therefore its own job type: `select_values`, `select_discard`,
`select_split`, `select_duplicates`, `select_filaments`, `select_class_ranker`.
A variant's form carries only its own branch's options, its command has no dead
branches, and the picker lists it directly under "Subset selection".

**The official pipeline identity does not split.** Every variant keeps its base
job's directory and process label (`Select/` + `relion.select`, `Denoise/` +
`relion.denoisetomo`, …), because RELION's `get_proc_type` strips a label's
suffixes. So the desktop GUI reads a `select_split` job as the same kind of
process it would have created itself, and `job.star` carries the full base job's
option table with the variant's switch set to `Yes` — the window opens on the
mode that ran, not on whichever mode its defaults pick.

`test_web_commands.py` and `test_web_command_drift.py` check all of it: the
shared identity, the `job.star` switch, the per-variant output nodes, and that
every switch the C++ tests has a job type. That last one fails when upstream
adds a mode, so the split cannot silently fall behind.

Two cases need a note:

- **`tomo_import`** crosses a flag style. Its coordinates mode runs a C++ utility
  with underscore flags; its SerialEM mode is a Typer app with hyphens and
  `--output-directory`. Each variant therefore carries its own `flag_style`,
  `executable` and output flag rather than inheriting all of them.
- **`modelangelo`** is not exclusive. The HMM search is an extra program after
  model building, so `modelangelo_build` is the build command and
  `modelangelo_hmm` is that command followed by the search.

The un-suffixed name of each split job (`select`, `localres`, …) is kept as an
API alias that pins the branch that name used to build, so a stored request
keeps producing the same command; the picker hides the alias so the same job is
not offered twice.

The one mode with no web equivalent is Select's interactive
`relion_display --gui` branch: it opens a GUI window, which a headless job cannot
drive.

## Bidirectional compatibility

The two programs share files, not an API, so compatibility has to be shown in
both directions. `webui/scripts/relion_compat_check/run.sh` compiles a tool from
RELION's own `metadata_table.cpp` and three writers, then checks:

- the agent writes `default_pipeline.star`, `job.star`,
  `RELION_OUTPUT_NODES.star` and the mini-pipeline, and RELION's parser reads
  every table;
- RELION writes a pipeline, and the agent reads it back with the right counter
  and job list.

It runs in a few seconds and needs only a C++ compiler: it compiles
`metadata_*.cpp` and their dependencies, not the whole of RELION.

Four defects came out of this check, all of which made the two sides
incompatible while the unit tests stayed green:

1. **A single-object table has no `loop_`.** RELION's `MetaDataTable::write`
   emits one object as repeated `_label value` lines; `starfile` returns that as
   a `dict`, and the reader only accepted a `DataFrame`. A project with one job
   therefore read as empty: no jobs, no nodes, job counter reset to 1. Files with
   several jobs use `loop_` and parsed fine, which is why the tests missed it.
2. **Output nodes live in `output_nodes`, not `pipeline_nodes`.**
   `getOutputNodesFromStarFile` reads that exact table name; anything else is
   ignored, so the GUI never learned which nodes a job produced.
3. **The note goes beside the job directory.** The GUI builds
   `<process name>note.txt`, and a process name has no trailing slash, so the
   file belongs at `Tomograms/job001note.txt` - not `Tomograms/job001/note.txt`,
   where the note editor would never look.
4. **`.Nodes` entries are touched files, not symlinks.** The GUI lists that
   directory to offer input files. A symlink lists too, but is removed by
   `git clean` and `rsync`, and RELION also creates a second path for nodes with
   `type_depth > 1`, which was missing.

Delete now also writes `<job>deleted_pipeline.star`, the file RELION's own
Undelete dialog opens.

## What is at parity

- **Job option forms** — same tabs, same order, same widget kinds, same defaults,
  same help text, generated from `gui_jobwindow.cpp`.
- **Project registration** — the GUI's directory names (`Tomograms/`, not
  `ReconstructTomograms/`), process labels, node types, `job.star`,
  `RELION_OUTPUT_NODES.star`, `.Nodes` symlinks, and the per-job mini-pipeline.
- **Lifecycle** — sentinel files, abort handshake, status written back to the
  pipeline and the mini-pipeline, job counter advanced.
- **Project list** — add, remove, switch; now also create.
- **Job counter and numbering** — `<Type>/job<NNN>` with three digits below 1000.
- **Job actions** — alias, note, gentle/harsh clean, delete, undelete, mark
  finished/failed. Alias and clean use RELION's own implementation; see below.
- **Field gating and cross-option validation** — the same greyed-out groups and
  the same contradictory-option refusals, taken from the C++.
- **Threads, MPI and queues** — the Run tab with `nr_threads`, `nr_mpi`,
  `do_queue` and the queue settings, with the same defaults and ranges as the
  GUI, and the same two execution shapes (see below).
- **Continue** — a checkpoint in `fn_cont` runs a refinement with `--continue`
  and without the image input, matching RELION's `is_continue` branch.
- **3D view** — rotatable max-intensity projection, rendered server-side.
- **Scheduler** — RELION's own, started detached and stoppable.
- **Export / import** — the same archive layout RELION uses.
- **Reading existing projects** — every job the GUI would show, with its status.

## Job actions

The "Job actions" menu is implemented, and most of it is *delegated* rather than
reimplemented, because the behaviour already exists in RELION:

| Action | Where it lives | What the web UI does |
| --- | --- | --- |
| Alias | `PipeLine::setAliasJob` (library) | Reimplemented: stores `<Type>/<alias>/`, makes the `<Type>/<alias>` → `<Type>/jobNNN` symlink, validates the alias the way the C++ does, rebuilds `.Nodes`. |
| Note | `<jobdir>/<jobname>note.txt` | Reads and writes that file directly; the GUI's note editor shows the same text. |
| Gentle / harsh clean | `PipeLine::cleanupJob`, exposed as `relion_pipeliner --gentle_clean/--harsh_clean` | Delegated: shells out to `relion_pipeliner`, so the per-job-type cleanup rules are RELION's own. |
| Delete | `PipeLine::deleteJobGetNodesAndProcesses` (library) | Reimplemented: removes the process and its nodes, moves the directory to `Trash/<Type>/jobNNN`. |
| Undelete | `PipeLine::undeleteJob` (library) | Reimplemented: reads `job.star` and `RELION_OUTPUT_NODES.star` back and re-registers the job. |
| Mark finished / failed | Sentinel files | Writes `RELION_JOB_EXIT_SUCCESS` / `_FAILURE`, as the GUI does. |

`relion_pipeliner` exposes alias, clean and the scheduler on the command line,
but delete, undelete and mark-status are library calls with no CLI flag, so those
three are the only ones written here.

## Field gating and validation

Both are also RELION rules, extracted rather than invented:

- **Gating** — `place("do_x", TOGGLE_DEACTIVATE, groupN)` plus the later
  `groupN->begin()` marks a set of fields that go grey when `do_x` is No. The
  generator records 41 such groups, so the web form greys the same fields the
  desktop window greys.
- **Validation** — the C++ refuses contradictory options from inside
  `getCommands*` (102 error messages). The web side reproduces the subset that
  changes the command's shape (exactly one alignment method, denoising versus
  Fourier inversion, one cryoCARE mode, GPU versus skipped alignment, subset
  splitting without a size, and so on) and checks them before submitting, so the
  error appears in the form rather than in a job log after a directory and
  pipeline entry have been created. `POST /api/job-validate` is what the form
  calls as you edit.

## Threads, MPI and queues

These follow `prepareFinalCommand` and `saveJobSubmissionScript`, because the two
execution shapes have to match RELION's or a job behaves differently here:

- **Threads** — `nr_threads` is passed as `--j` (or `--j_out` for some jobs), and
  is part of the core count a queue script asks for.
- **MPI** — with `nr_mpi > 1` RELION does not wrap the serial program; it runs
  the `_mpi` variant instead (`relion_tomo_reconstruct_tomogram` becomes
  `relion_tomo_reconstruct_tomogram_mpi`, `relion_refine` becomes
  `relion_refine_mpi`, and so on for the 14 jobs that have one) and prefixes it
  with `mpirun -n N`. Substituting the binary is the part that is easy to get
  wrong: running the serial program under mpirun starts N full copies that
  overwrite each other's output. A local run above `RELION_ERROR_LOCAL_MPI`
  processes is refused, as the GUI refuses it.
- **Queues** — with `do_queue` set, the commands are written into
  `run_submit.script` from the user's `qsubscript` template, with RELION's own
  placeholders (`XXXmpinodesXXX`, `XXXthreadsXXX`, `XXXcoresXXX`,
  `XXXnodesXXX`, `XXXqueueXXX`, `XXXcommandXXX`, …), and started with `qsub`.
  An existing cluster template therefore works unchanged.
- **Queued job status** — the submit command returning 0 means the cluster
  accepted the job, not that it ran. A queued job stays `queued` (and the
  pipeline `Scheduled`) until the program writes its sentinel, which a watcher
  polls for; only then does the pipeline become `Succeeded` or `Failed`.

## 3D viewing and picking

The desktop GUI renders volumes in napari on the client. A browser cannot hold a
multi-gigabyte tomogram, so the view is produced server-side instead: the viewer
sends yaw/pitch and receives a PNG projection from that angle.

- The volume is binned until it fits the requested frame before the projection,
  so the per-frame cost stays bounded.
- The ray is built in the volume frame with an explicit `Rz(yaw)·Rx(pitch)`
  rotation, and `mip` (maximum intensity) is the default because it keeps
  membranes and particles legible; `mean` is the smoother alternative.
- Dragging fetches low-resolution frames and the settled view re-renders at full
  size, which is what makes rotation usable over a LAN.
- Clicking places a point, saved as an `rlnCoordinateX/Y/Z` table — the format
  `relion_tomo_import_coordinates` and the pick job's input STAR already use.

This is not napari: there is no interactive sphere/filament placement, and a
pick lands on the mid-plane because a projection has no depth buffer. For
annotation-heavy work the desktop picker is still the right tool.

## Scheduler

`relion_pipeliner --RunJobs --schedule` is started detached, so it is RELION's
scheduler with its own dependency checks, repeat bookkeeping and log. Stopping it
removes the `RUNNING_PIPELINER_<name>` file the scheduler polls, which is exactly
what the GUI's "Stop running scheduled jobs" does. The UI polls its status and
shows the log tail.

## Export and import

Export writes `ExportJobs/<name>/` with scheduled jobs renamed to
`<Type>/expNNN`, copying `job.star`, `note.txt`, `RELION_OUTPUT_NODES.star` and
the mini-pipeline with the paths rewritten — the same archive RELION builds.
Import renumbers them into the receiving project from its own counter. RELION's
own export works from `run.job`; that file is not written here, so `job.star` is
used, which RELION prefers on read anyway.

## What is missing

1. **Interactive sphere / filament picking.** The 3D view can place single
   points; it cannot trace filaments or fit spheres the way napari does, because
   that needs the volume client-side and a live depth buffer.
2. **`Overwrite` (re-run in place).** `Continue` is supported — giving `fn_cont`
   runs a refinement from a checkpoint without `fn_img` — but re-running an
   existing job into its own directory is not exposed.
3. **Validation coverage.** The cross-option rules and the filesystem checks
   (input exists, half-map filename) are in place. What is left are checks that
   depend on the *content* of an input file (whether a STAR has
   `rlnTomoReconstructedTomogram`) and on run-time state; those surface in the
   job log.
4. **The napari programs.** `relion_python_tomo_view` and the picker's
   interactive mode open napari windows and are not exposed.

## Deliberate differences

- **No plaintext-password storage.** Login is a username and password submitted
  to the agent, which asks the local `sshd` to verify them over a single
  connection and never keeps the password.
- **`--no-auth` refuses a non-loopback bind.** The check is in `_cli.py`, because
  an unauthenticated job runner on a network is a remote-execution endpoint.
- **Dry run does not touch the project.** The desktop "Check command" similarly
  only prints.
- **External jobs run a user-named program.** This is what `relion.external`
  means upstream; the per-user agent model is what makes it acceptable.
