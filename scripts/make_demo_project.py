#!/usr/bin/env python3
"""Build a synthetic RELION cryo-ET project for looking at the web UI.

Writes a real ``default_pipeline.star``: processes, nodes, input/output edges,
statuses, plus ``job.star`` and ``run.out`` in each job directory. The app reads
it exactly as it reads a real project, so what you see is what the pipeline
parser produced - not mock data.

    python scripts/make_demo_project.py /tmp/devproj
    ./scripts/dev_web.sh -p /tmp/devproj
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'backend'))

from relion_webui import job_artifacts, pipeline  # noqa: E402
from relion_webui import job_specs  # noqa: E402


# (job type, label used in the tree, status, input node names, log lines)
PLAN = [
    ('tomo_import', '48 tilt-series', 'Succeeded', [], [
        'Reading 48 tilt-series from /data/gold/TS_*.star',
        'Wrote tilt_series.star',
        'Finished in 00:00:41',
    ]),
    ('tomo_align_aretomo', 'aligned, 4.2 nm', 'Succeeded', ['tomo_import'], [
        'AlignTiltSeries: patch tracking over 48 tomograms',
        'mean residual tilt 0.83 degrees',
        'Wrote tilt_series.star',
        'Finished in 00:12:08',
    ]),
    ('ctffind', 'median 3.1 A', 'Succeeded', ['tomo_align_aretomo'], [
        'CTFFIND4 on 48 tilt-series',
        'Estimated defocus for 1,440 tilt images',
        'Finished in 00:04:12',
    ]),
    ('tomo_reconstruct_tomograms', '48 tomograms, bin 4', 'Failed', ['ctffind'], [
        'reconstruct: reading tilt_series.star',
        'ERROR: tomogram 17 has fewer than 5 usable tilts after CTF rejection',
        'ERROR: stopping; no partial output written',
    ]),
    ('tomo_reconstruct_tomograms', '48 tomograms, bin 4', 'Succeeded', ['ctffind'], [
        'reconstruct: weighted back-projection, 48 tomograms',
        'box 2560 x 2560 x 800, binning 4',
        'Finished in 01:02:47',
    ]),
    ('tomo_denoise_train', 'epoch 41/100', 'Running', ['reconstruct-ok'], [
        'Training on 48 tomograms, patch 96',
        'epoch   1/100  loss 0.0412',
        'epoch  20/100  loss 0.0093',
        'epoch  40/100  loss 0.0061',
        'epoch  41/100  loss 0.0059  (18m 22s elapsed)',
    ]),
    ('tomo_denoise_predict', '48 denoised', 'Succeeded', ['denoise-train'], [
        'Applying model to 48 tomograms',
        'Finished in 00:06:44',
    ]),
    ('tomo_pick_tomograms', 'waiting on the model', 'Scheduled', ['denoise-train'], []),
    ('tomo_subtomo', 'no picks yet', 'Scheduled', ['picks'], []),
    ('tomo_recon_particle', 'still setting up', 'Aborted', ['import'], [
        'reconstruct: reading optimisation set',
        'Aborted by user (meng) after 00:02:10',
    ]),
]


def make_project(root: Path, force: bool) -> Path:
    if root.exists():
        if not force:
            raise SystemExit(f'{root} exists; pass --force to replace it')
        shutil.rmtree(root)
    root.mkdir(parents=True)
    (root / 'default_pipeline.star').touch()

    state = pipeline.read_pipeline(root)
    # node name -> pipeline.Node, so later jobs can consume earlier outputs
    made: dict[str, pipeline.Node] = {}
    consumed: dict[str, list[str]] = {}

    for index, (job_type, label, status, inputs, log) in enumerate(PLAN, start=1):
        spec = job_specs.get_spec(job_type)
        process = pipeline.Process(
            name=f'{spec.directory_name}/job{index:03d}',
            alias=label,
            type_label=spec.process_type_label,
            status=status,
        )

        # Inputs: a job consumes the output nodes of its upstream jobs. The
        # first output node of each upstream job is the one the spec advertises.
        in_nodes = []
        for key in inputs:
            node = made.get(key)
            if node is not None:
                in_nodes.append(node)
                consumed.setdefault(key, []).append(process.name)

        # Outputs: one node per declared filename, so the canvas has real edges.
        out_nodes = [
            pipeline.Node(
                name=f'{process.name}/{output.filename}',
                type_label=output.type_label,
                type_depth=spec.output_nodes.index(output) if False else 0,
            )
            for output in spec.output_nodes
        ]
        # job005 is the successful reconstruction; give it a volume filename so
        # the node is recognised as a volume and the dialog renders it.
        if index == 5 and out_nodes:
            out_nodes[0].name = f'{process.name}/tomograms.mrc'
            out_nodes[0].type_label = 'DensityMap.mrc.relion.tomo.map'

        # Aliases the PLAN uses to refer to this job's main output.
        key = {
            1: 'tomo_import', 2: 'tomo_align_aretomo', 3: 'ctffind',
            4: 'reconstruct-failed', 5: 'reconstruct-ok',
            6: 'denoise-train', 7: 'denoise-predict', 8: 'picks',
        }.get(index)
        if key and out_nodes:
            made[key] = out_nodes[0]
        if index == 1 and out_nodes:
            made['import'] = out_nodes[0]

        pipeline.register_process(state, process, in_nodes, out_nodes)
        directory = root / process.name
        directory.mkdir(parents=True, exist_ok=True)
        # A log so the Log tab and the dialog have something to show.
        (directory / 'run.out').write_text('\n'.join(log) + ('\n' if log else ''))
        # job.star so the desktop GUI could open it, and so the UI has the
        # option table to read back.
        # Record a plausible option set, including one or two values that
        # differ from the form's defaults, so the parameters tab has something
        # to say about custom values.
        job_artifacts.write_job_star(
            directory,
            spec.process_type_label,
            demo_options(spec, root, index),
            is_tomo=True,
        )

    _write_tomogram(root)
    _write_tilt_series(root)
    pipeline.write_pipeline(root, state)
    return root


def demo_options(spec, root: Path, index: int) -> dict:
    """A small, plausible option table for a demo job."""
    options: dict = {'fn_in': str(root / 'tilt_series.star')}
    schema = None
    try:
        from relion_webui import job_commands

        schema = job_commands.form_schema(spec.job_type)
    except (KeyError, ImportError):
        schema = None

    if schema is None:
        return options

    # Everything at its default, then two deliberate departures.
    for field in schema['fields']:
        if 'default' in field and field['variable'] not in options:
            options[field['variable']] = field['default']

    overrides = {
        'fn_in': str(root / 'tilt_series.star'),
        'nr_mpi': 4,
        'nr_threads': 8,
    }
    for key, value in overrides.items():
        if key in {field['variable'] for field in schema['fields']}:
            options[key] = value
    return options


def _write_tilt_series(
    root: Path,
    *,
    n_series: int = 4,
    n_tilts: int = 12,
    pixel_size: float = 0.675,
) -> None:
    """Write the global tilt-series STAR and one per-series file.

    Every job's ``fn_in`` points at ``tilt_series.star``, so without these files
    the demo project names a dataset that does not exist: the tilt browser says
    "file not found" and the import job's options point nowhere. Four series of
    twelve tilts is enough to exercise the list, the image viewer and the
    per-image selection without writing real images.
    """
    series_dir = root / 'tilt_series'
    series_dir.mkdir(parents=True, exist_ok=True)
    images_dir = series_dir / 'images'
    images_dir.mkdir(parents=True, exist_ok=True)

    global_columns = [
        '_rlnTomoName',
        '_rlnTomoTiltSeriesStarFile',
        '_rlnVoltage',
        '_rlnSphericalAberration',
        '_rlnAmplitudeContrast',
        '_rlnMicrographOriginalPixelSize',
        '_rlnTomoHand',
        '_rlnTomoTiltSeriesPixelSize',
    ]
    global_rows: list[str] = []
    for index in range(1, n_series + 1):
        name = f'TS_{index:02d}'
        relative = f'tilt_series/{name}.star'
        tilts: list[str] = []
        for tilt in range(n_tilts):
            angle = -60 + tilt * (120 / (n_tilts - 1))
            # _rlnImageSelected 1: a fresh set has every tilt kept, and a
            # demo that opens at 0/12 looks like the selection was lost.
            tilts.append(
                f'images/{name}_{tilt:02d}.mrc {angle:.1f} 0.0 0.0 1'
            )
            _write_tilt_image(images_dir / f'{name}_{tilt:02d}.mrc', angle)
        (series_dir / f'{name}.star').write_text(
            '\n'.join(
                [
                    '',
                    '# version 30001',
                    '',
                    'data_' + name,
                    '',
                    'loop_',
                    '_rlnMicrographName #1',
                    '_rlnTomoNominalStageTiltAngle #2',
                    '_rlnDefocusU #3',
                    '_rlnDefocusV #4',
                    '_rlnImageSelected #5',
                    *tilts,
                    '',
                ]
            ),
            encoding='utf-8',
        )
        global_rows.append(
            f'{name} {relative} 300.0 2.7 0.1 {pixel_size} 1 {pixel_size}'
        )

    (root / 'tilt_series.star').write_text(
        '\n'.join(
            [
                '',
                '# version 30001',
                '',
                'data_global',
                '',
                'loop_',
                *[
                    f'{column} #{position}'
                    for position, column in enumerate(global_columns, start=1)
                ],
                *global_rows,
                '',
            ]
        ),
        encoding='utf-8',
    )


def _write_tilt_image(path: Path, angle: float, size: int = 64) -> None:
    """A small 2D MRC per tilt, so the image viewer renders something real.

    A single plane (`nz = 1`), which is what a tilt image is: the reader
    rejects a stack outright. The bar's angle follows the tilt, so moving the
    viewer's slider visibly changes the picture - the difference between a
    working viewer and a stale image.
    """
    y, x = np.mgrid[0:size, 0:size]
    image = np.zeros((size, size), dtype=np.float32)
    centre = size // 2
    radians = np.radians(angle)
    across = (y - centre) * np.cos(radians) + (x - centre) * np.sin(radians)
    image[np.abs(across) < 2.5] = 0.75
    image[(y - centre) ** 2 + (x - centre) ** 2 < 36] = 1.0

    header = np.zeros(256, dtype=np.int32)
    header[0:3] = (size, size, 1)
    header[3] = 2  # mode 2 = float32
    header[7:10] = (size, size, 1)
    header[10:13] = size // 2
    header[13:16] = (size, size, 1)
    with open(path, 'wb') as handle:
        handle.write(header.astype('<i4').tobytes())
        handle.write(image.astype('<f4').tobytes())


def _write_tomogram(root: Path, size: int = 192) -> None:
    """Write a small tomogram so the Volumes tab renders something.

    A real MRC, not a placeholder: the agent reads it through the same
    ``/api/render`` path a real project uses, so the preview either works or
    the UI reports why. Three spheres make the central slice obviously non-empty,
    which is also how the "slice at index 0 is blank" trap was found.
    """
    import struct

    out = root / 'Tomograms' / 'job005' / 'tomograms.mrc'
    out.parent.mkdir(parents=True, exist_ok=True)

    z, y, x = np.mgrid[0:size, 0:size, 0:size]
    volume = np.zeros((size, size, size), dtype=np.float32)
    centre = size // 2
    for cz, cy, cx, r in [
        (centre, centre - 38, centre - 38, size // 7),
        (centre, centre + 40, centre + 22, size // 9),
        (centre + 22, centre - 12, centre + 52, size // 12),
    ]:
        volume[((z - cz) ** 2 + (y - cy) ** 2 + (x - cx) ** 2) <= r * r] += 1.0

    header = np.zeros(256, dtype=np.int32)
    header[0:3] = size
    header[3] = 2  # mode 2 = float32
    header[7:10] = size
    header[10:13] = size // 2
    header[13:16] = size
    with open(out, 'wb') as handle:
        handle.write(header.astype('<i4').tobytes())
        handle.write(volume.astype('<f4').tobytes())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path, nargs='?', default=Path('/tmp/relion-demo'))
    parser.add_argument('--force', action='store_true', help='replace an existing directory')
    args = parser.parse_args()
    root = make_project(args.path.expanduser(), args.force)
    print(f'wrote {root}')
    print(f'  {sum(1 for _ in (root).glob("*/job*"))} job directories')
    print(f'  run: ./scripts/dev_web.sh -p {root}')


if __name__ == '__main__':
    main()
