#!/usr/bin/env python3
"""Extract the GUI-variable -> command-line translation from RELION's C++.

The desktop GUI never runs a command it composed itself: ``RelionJob::getCommands*``
turns a job's ``joboptions`` into the argv, and that function is the only correct
translation. 820 ``command +=`` statements, 505 of them reading an option, with
213 conditional blocks, is far too much to keep by hand — and the hand-written
copy in this package drifted three times (missing ``--o``, a hyphen where the C++
wants an underscore, a subcommand left out).

The same extraction approach already works for option metadata
(``generate_web_job_schema.py``), so this applies it to the translation itself.
Each job becomes an ordered list of steps:

``{"flag": "--t", "var": "in_tiltseries"}``
    emit ``--t <value>`` when the option is set
``{"flag": "--fourier", "var": "do_fourier", "kind": "flag"}``
    emit a bare ``--fourier`` when the option is true
``{"literal": "--theme classic"}``
    always emit this text
``{"executable": ["relion_x", "relion_x_mpi"]}``
    the program, chosen by nr_mpi
``{"subcommand": "cryoCARE:predict"}``
    a positional word the Typer apps need
``{"output": true}`` / ``{"output": "suffix"}``
    the job directory, optionally with a suffix

A step may carry ``when``: a condition stack copied from the enclosing ``if``
statements, so the generated builder reproduces the same branching.

Run it from the repository root:

    python scripts/generate_web_job_commands.py

It rewrites ``backend/recryo/generated_commands.py``. The test
``test_web_commands.py`` re-extracts and fails if the checked-in file is stale.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

WEBUI_ROOT = Path(__file__).resolve().parent.parent


def resolve_source_root(explicit=None) -> Path:
    """The RELION checkout to read the C++ sources from.

    Chosen in this order, so the script works from a sibling checkout, from
    anywhere with an argument, or in CI with the environment:

    1. ``--relion-source`` on the command line;
    2. ``RELION_SOURCE`` in the environment;
    3. a ``relion`` checkout beside this one;
    4. the parent directory, for the layout where this package sits inside it.

    The answer is what the caller needs to see: a wrong path fails here with a
    message naming the flag, instead of surfacing later as a FileNotFoundError
    on an arbitrary .cpp.
    """
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    if os.environ.get('RELION_SOURCE'):
        candidates.append(Path(os.environ['RELION_SOURCE']).expanduser())
    candidates += [
        WEBUI_ROOT.parent / 'relion',
        WEBUI_ROOT.parent,
        WEBUI_ROOT.parent.parent / 'relion',
    ]
    for candidate in candidates:
        if (candidate / 'src' / 'pipeline_jobs.cpp').is_file():
            return candidate.resolve()
    tried = '\n  '.join(str(c) for c in candidates)
    raise SystemExit(
        'could not find a RELION checkout. Pass --relion-source /path/to/relion '
        'or set RELION_SOURCE. Tried:\n  ' + tried
    )
_OUTPUT_DIR = WEBUI_ROOT / 'backend' / 'recryo'
OUTPUT = _OUTPUT_DIR / 'generated_commands.py'

# cpp function suffix -> web job id. Must match generate_web_job_schema.py.
JOBS: List[Tuple[str, str]] = [
    ('TomoImport', 'tomo_import'),
    ('Motioncorr', 'motioncorr'),
    ('Ctffind', 'ctffind'),
    ('TomoExcludeTiltImages', 'tomo_exclude_tilt_images'),
    ('TomoAlignTiltSeries', 'tomo_align_tilt_series'),
    ('TomoReconstructTomograms', 'tomo_reconstruct_tomograms'),
    ('TomoDenoiseTomograms', 'tomo_denoise_tomograms'),
    ('TomoPickTomograms', 'tomo_pick_tomograms'),
    ('TomoSubtomo', 'tomo_subtomo'),
    ('Inimodel', 'inimodel'),
    ('Class3D', 'class3d'),
    ('Autorefine', 'autorefine'),
    ('MultiBody', 'multibody'),
    ('Select', 'select'),
    ('TomoReconPart', 'tomo_recon_particle'),
    ('TomoCtfRefine', 'tomo_ctf_refine'),
    ('TomoAlign', 'tomo_align_frame'),
    ('Maskcreate', 'maskcreate'),
    ('Joinstar', 'joinstar'),
    ('Postprocess', 'postprocess'),
    ('Localres', 'localres'),
    ('ModelAngelo', 'modelangelo'),
    ('External', 'external'),
]


def strip_comments(text: str) -> str:
    """Remove C++ comments while leaving string literals intact.

    ``"frames/*.mrc"`` contains ``/*``, so a regex would eat the rest of the
    file; this walks the text and tracks quoting.
    """
    out: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in '"\'':
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                out.append(text[i])
                if text[i] == '\\' and i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == '/' and i + 1 < n and text[i + 1] == '/':
            while i < n and text[i] != '\n':
                i += 1
            continue
        if ch == '/' and i + 1 < n and text[i + 1] == '*':
            i += 2
            while i + 1 < n and not (text[i] == '*' and text[i + 1] == '/'):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


def function_body(source: str, signature: str) -> str:
    start = source.find(signature)
    if start < 0:
        return ''
    ends = [
        idx
        for idx in (
            source.find('\nvoid RelionJob::', start + 5),
            source.find('\nbool RelionJob::', start + 5),
        )
        if idx > 0
    ]
    return source[start : min(ends) if ends else len(source)]


# ------------------------------------------------------------------ parsing
_CONDITION = re.compile(r'^\s*if\s*\((.*)\)\s*$', re.S)
_ELSE = re.compile(r'^\s*else\b')


def _condition(cpp: str) -> Optional[dict]:
    """Translate a C++ condition into a form the evaluator can test."""
    expr = ' '.join(cpp.split())
    # A few blocks test two joboptions at once; express them as a list of
    # sub-conditions, all of which must hold (&& is the only operator used).
    if '&&' in expr:
        parts = [_condition(part) for part in expr.split('&&')]
        if all(parts):
            return {'all': parts}
        return None
    return _simple_condition(expr)


def _simple_condition(expr: str) -> Optional[dict]:
    """Translate a C++ condition into a form the Python emitter can test.

    Only the shapes ``getCommands*`` actually uses are handled; anything else is
    reported rather than guessed at, so a new construct in RELION shows up as a
    gap in the generated file instead of a silently wrong command.
    """
    match = re.fullmatch(
        r'joboptions\["(\w+)"\]\.getBoolean\(\)', expr
    )
    if match:
        return {'truthy': match.group(1)}

    match = re.fullmatch(
        r'!\s*joboptions\["(\w+)"\]\.getBoolean\(\)', expr
    )
    if match:
        return {'falsy': match.group(1)}

    match = re.fullmatch(
        r'joboptions\["(\w+)"\]\.getString\(\)\s*(?:==|\.length\(\)\s*>\s*0)'
        r'\s*(""|0)?',
        expr,
    )
    if match:
        if expr.endswith('""') or '.length() > 0' in expr:
            return {'set': match.group(1)}

    match = re.fullmatch(
        r'joboptions\["(\w+)"\]\.getNumber\(\w+\)\s*>\s*1', expr
    )
    if match:
        return {'mpi_gt_one': match.group(1)}

    match = re.fullmatch(r'is_continue', expr)
    if match:
        return {'continue': True}

    match = re.fullmatch(r'!is_continue', expr)
    if match:
        return {'not_continue': True}

    # `is_tomo` is a job attribute, not a joboption: the GUI sets it from the
    # --tomo flag, and every job in this front end is a tomography job.
    match = re.fullmatch(r'is_tomo', expr)
    if match:
        return {'is_tomo': True}

    match = re.fullmatch(r'!is_tomo', expr)
    if match:
        return {'is_tomo': False}
    return None


# Statements that carry no command content.
_IGNORED = (
    'commands.clear();',
    'commands.push_back(command);',
    'commands.push_back(command0);',
    'commands.push_back(command2);',
    'commands.push_back(command3);',
)


def _is_ignored(text: str) -> bool:
    stripped = text.strip()
    return (
        stripped.startswith('commands.push_back(')
        or stripped == 'commands.clear();'
        or stripped.startswith('commands.insert(')
    )


# Stands in for an escaped `\"` while a statement is being matched.
_QUOTE_SENTINEL = '\x00Q\x00'


def _restore_quotes(value: str) -> str:
    return value.replace(_QUOTE_SENTINEL, '"')


def _emit_step(line: str) -> Optional[dict]:
    """Turn one ``command += ...`` line into a step.

    The shapes below are the ones ``getCommands*`` uses. Anything left over is
    returned to the caller as unparsed, so a new construct upstream becomes a
    visible gap rather than a silently wrong argv.
    """
    # C++ writes `\"` inside a string literal for an embedded double quote.
    # Replacing it with `"` would merge with the surrounding quotes and break the
    # literal, so it becomes a sentinel that is restored when the step is
    # rendered. The value it wraps still needs quoting in the shell.
    text = line.strip().replace('\\"', _QUOTE_SENTINEL)

    # The program itself: `command="`which relion_x`";` / `"relion_x "`.
    match = re.match(
        r'command\w*\s*=\s*"`?which\s+(relion_\w+)`?\s*;', text
    )
    if match:
        return {'executable': [match.group(1)]}
    match = re.match(r'command\w*\s*=\s*"`?which\s+(relion_\w+)`?\s*"', text)
    if match:
        return {'executable': [match.group(1)]}
    # A literal program + subcommand: `command = "relion_python_tomo_pick ";`
    match = re.match(r'command\w*\s*=\s*"(relion_\w+)\s*"\s*;', text)
    if match:
        return {'executable': [match.group(1)]}
    # A positional subcommand written as its own statement:
    # `command = "relion_python_tomo_import SerialEM ";`
    match = re.match(r'command\w*\s*=\s*"(relion_\w+)\s+([A-Za-z][\w:.-]*)\s*"\s*;', text)
    if match:
        return {'executable': [match.group(1)], 'subcommand': match.group(2)}

    # `command += " --o " + outputname;` - the job directory.
    match = re.match(
        r'command\w*\s*\+=\s*"([^"]*)"\s*\+\s*outputname\s*;', text
    )
    if match:
        return {'literal_prefix': match.group(1), 'output': True}

    # `command += " --suffix " + outputname + "extra";`
    match = re.match(
        r'command\w*\s*\+=\s*"([^"]*)"\s*\+\s*outputname\s*\+\s*"([^"]*)"\s*;',
        text,
    )
    if match:
        return {
            'literal_prefix': match.group(1),
            'output': True,
            'literal_suffix': match.group(2),
        }

    # `command += " --flag \"" + joboptions["var"].getString() + "\"";`
    match = re.match(
        r'command\w*\s*\+=\s*"([^"]*)"\s*\+\s*joboptions\["(\w+)"\]\.getString\(\)\s*\+\s*"([^"]*)"\s*;',
        text,
    )
    if match:
        return {
            'literal_prefix': match.group(1),
            'var': match.group(2),
            'literal_suffix': match.group(3),
        }

    # `command += " --t " + joboptions["var"].getString();`
    match = re.match(
        r'command\w*\s*\+=\s*"([^"]*)"\s*\+\s*joboptions\["(\w+)"\]\.getString\(\)\s*;',
        text,
    )
    if match:
        return {'literal_prefix': match.group(1), 'var': match.group(2)}

    # A converted option: `integerToString(joboptions["var"].getNumber(...))`.
    match = re.match(
        r'command\w*\s*\+=\s*"([^"]*)"\s*\+\s*integerToString\('
        r'\s*joboptions\["(\w+)"\]\.getNumber\([^)]*\)\s*\*\s*[^)]*\)',
        text,
    )
    if match:
        return {'literal_prefix': match.group(1), 'var': match.group(2), 'convert': 'int'}
    match = re.match(
        r'command\w*\s*\+=\s*"([^"]*)"\s*\+\s*integerToString\(\s*'
        r'joboptions\["(\w+)"\]\.getNumber\([^)]*\)\s*\)',
        text,
    )
    if match:
        return {'literal_prefix': match.group(1), 'var': match.group(2), 'convert': 'int'}
    match = re.match(
        r'command\w*\s*\+=\s*"([^"]*)"\s*\+\s*floatToString\(\s*'
        r'joboptions\["(\w+)"\]\.getNumber\([^)]*\)\s*\*\s*[^)]*\)',
        text,
    )
    if match:
        return {'literal_prefix': match.group(1), 'var': match.group(2), 'convert': 'float'}

    # Any statement of the form: "prefix" + joboptions["var"].getString()
    # optionally followed by a literal or a variable suffix.
    match = re.match(
        r'command\w*\s*\+=\s*"([^"]*)"\s*\+\s*'
        r'joboptions\["(\w+)"\]\.getString\(\)(.*);$',
        text,
    )
    if match:
        step = {'literal_prefix': match.group(1), 'var': match.group(2)}
        tail = match.group(3).strip()
        # A trailing literal, single- or double-quoted.
        literal = re.fullmatch(r'\+\s*"([^"]*)"', tail) or re.fullmatch(
            r"\+\s*'([^']*)'", tail
        )
        if literal:
            step['literal_suffix'] = literal.group(1)
        return step

    # `command += " --o " + outputname + fn_run;` - output then a derived name.
    match = re.match(
        r'command\w*\s*\+=\s*"([^"]*)"\s*\+\s*outputname\s*\+\s*(\w+)\s*;',
        text,
    )
    if match:
        return {
            'literal_prefix': match.group(1),
            'output': True,
            'literal_suffix_var': match.group(2),
        }

    # A bare literal: `command += " --fourier ";`
    match = re.match(r'command\w*\s*\+=\s*"([^"]*)"\s*;', text)
    if match:
        literal = match.group(1)
        if not literal.strip():
            return None
        return {'literal': literal}

    return None


def extract_job(body: str) -> dict:
    """Walk one getCommands body, tracking the if-stack around each statement."""
    steps: List[dict] = []
    unknown: List[str] = []
    stack: List[Optional[dict]] = []

    lines = body.splitlines()
    # One entry per open `{`: the condition that guards it, or None for a block
    # with no condition (a bare group, or a loop body).
    stack: List[Optional[dict]] = []
    pending_condition: Optional[dict] = None
    # A block opened by `else {` is not guarded: the executable step already
    # distinguishes the two branches.
    pending_is_else = False

    for raw in lines:
        text = raw.strip()
        if not text:
            continue

        if text.startswith('if') and '(' in text:
            # `if (cond)` possibly followed by `{` on the same or the next line.
            inner = text[2:].strip()
            if inner.startswith('('):
                # Trim the trailing `{` if present and the matching paren.
                body_part = inner
                if body_part.endswith('{'):
                    body_part = body_part[:-1].strip()
                depth = 0
                end = None
                for index, ch in enumerate(body_part):
                    if ch == '(':
                        depth += 1
                    elif ch == ')':
                        depth -= 1
                        if depth == 0:
                            end = index
                            break
                if end is not None:
                    pending_condition = _condition(body_part[1:end])
                pending_is_else = False
            continue

        if text.startswith('else'):
            pending_is_else = True
            pending_condition = None
            # `else if (...)` is a new condition, handled on the next pass.
            if 'if' in text and '(' in text:
                inner = text[text.index('(') + 1 :]
                depth = 0
                end = None
                for index, ch in enumerate(inner):
                    if ch == '(':
                        depth += 1
                    elif ch == ')':
                        depth -= 1
                        if depth == 0:
                            end = index
                            break
                if end is not None:
                    pending_condition = _condition(inner[:end])
            continue

        if text == '{':
            stack.append(None if pending_is_else else pending_condition)
            pending_condition = None
            pending_is_else = False
            continue

        if text.startswith('}'):
            if stack:
                stack.pop()
            continue

        if text.startswith('command'):
            if _is_ignored(text):
                continue
            step = _emit_step(text)
            if step is None:
                unknown.append(text)
                continue
            # `command0`/`command2` are the second and third programs of a job;
            # record which one so the evaluator starts a new command there.
            name_match = re.match(r'(command\d+)\s*(?:\+|=)', text)
            if name_match and name_match.group(1) != 'command':
                step['program'] = name_match.group(1)
            conditions = [c for c in stack if c]
            if conditions:
                step['when'] = conditions
            steps.append(step)

    return {'steps': steps, 'unknown': unknown}


# Jobs that call the shared `getTomoInputCommmand`, and the flag it emits.
# The call is a single statement in the job's body, so the helper's own
# `command +=` lines are not in that body: the extractor would produce a command
# missing --ios/--i entirely, which is what the byte comparison caught.
SHARED_TOMO_INPUT: Dict[str, str] = {
    # job id -> the flag the helper emits when is_for_refine is true/false
    'inimodel': '--ios',
    'class3d': '--ios',
    'autorefine': '--ios',
    'tomo_subtomo': '--i',
    'tomo_ctf_refine': '--i',
    'tomo_align_frame': '--i',
    'tomo_recon_particle': '--i',
}


# Statements that read a value computed earlier in the same C++ function. These
# cannot be extracted from the statement alone, so each is declared with the
# transformation it performs. Written by hand, deliberately: there are few of
# them, and each is a real derivation rather than a mapping.
#
# The C++ computes these before emitting them, so the statement cannot be read on
# its own. Each entry names the flag, the option it comes from, the transform,
# and where it belongs relative to the extracted steps.
DERIVED_STEPS: List[Tuple[str, str, str, str, dict]] = [
    # `int gain_rot = index of joboptions["gain_rot"] in
    #  job_gain_rotation_options`, i.e. the number in the radio label. Emitted
    # right after --gpu.
    # Emitted right after --gainref, at the end of the MotionCor2 branch. The
    # extracted steps do not include --gainref (it only appears when
    # `do_own_motioncor` is off), so --bin_factor - the first statement after
    # that branch - is used as the anchor and the step is placed before it.
    ('motioncorr', '--gain_rot', 'gain_rot', 'radio_index', {'before': 'bin_factor'}),
    ('motioncorr', '--gain_flip', 'gain_flip', 'radio_index', {'before': 'bin_factor'}),
    # `int grouping_for_ps` comes from dose_for_ps / dose_rate, and is emitted
    # just before --only_do_unfinished, only when own implementation is used.
    (
        'motioncorr',
        '--grouping_for_ps',
        'group_for_ps',
        'int',
        {'after': 'group_frames', 'when': [{'truthy': 'do_save_ps'}]},
    ),
]


def extract(source: str) -> Dict[str, dict]:
    result: Dict[str, dict] = {}
    for cpp_name, web_id in JOBS:
        body = function_body(source, f'bool RelionJob::getCommands{cpp_name}Job(')
        job = extract_job(body)

        # Insert the shared helper's step where the job calls it. The helper is
        # invoked right after the executable and output are set, so its flag
        # belongs before the option-specific ones.
        if web_id in SHARED_TOMO_INPUT:
            flag = SHARED_TOMO_INPUT[web_id]
            step = {
                'literal_prefix': f' {flag} ',
                'var': 'in_optimisation',
                'when': [{'falsy': 'use_direct_entries'}],
            }
            # After the output step, before the job-specific options. C++ calls
            # the helper immediately after building the output argument.
            insert_at = 0
            for index, existing in enumerate(job['steps']):
                if existing.get('output'):
                    insert_at = index + 1
            job['steps'].insert(insert_at, step)

        # Attach the declared derivations as one block, in declaration order.
        # The C++ emits gain_rot then gain_flip, so keeping the declared order
        # matters as much as getting the values right.
        derived = [
            {
                'literal_prefix': f' {flag} ',
                'var': variable,
                'derive': transform,
                **extra,
            }
            for job_id, flag, variable, transform, extra in DERIVED_STEPS
            if job_id == web_id
        ]
        for step in derived:
            # A derived statement replaces the same variable's plain step when
            # the extractor already produced one.
            for index, existing in enumerate(job['steps']):
                if existing.get('var') == step['var']:
                    job['steps'].pop(index)
                    break
        # Insert in declaration order: several steps can share an anchor, and
        # inserting each right after the anchor reverses them.
        # Group by anchor so steps that share one keep their declared order.
        groups: List[tuple] = []
        for step in derived:
            anchor = (step.pop('after', None), step.pop('before', None))
            if groups and groups[-1][0] == anchor:
                groups[-1][1].append(step)
            else:
                groups.append((anchor, [step]))
        for (after, before), steps in groups:
            anchor = before or after
            insert_at = len(job['steps'])
            if anchor:
                for index, existing in enumerate(job['steps']):
                    if existing.get('var') == anchor:
                        insert_at = index if before else index + 1
                        break
            for offset, step in enumerate(steps):
                job['steps'].insert(insert_at + offset, step)

        result[web_id] = job
    return result


def render(jobs: Dict[str, dict]) -> str:
    lines = [
        '# SPDX-FileCopyrightText: 2026 elemeng',
        '# SPDX-License-Identifier: GPL-2.0-or-later',
        '#',
        "# This file is GENERATED from RELION's C++ sources and carries RELION's",
        '# option text. Regenerate it; do not edit it by hand.',
        '"""GUI-variable to command-line translation, extracted from RELION.',
        '',
        'Generated by ``scripts/generate_web_job_commands.py``; do not edit by',
        'hand. Regenerate after changing ``src/pipeline_jobs.cpp`` and let',
        '``test_web_commands.py`` confirm the file is in step with the sources.',
        '',
        'Each job is an ordered list of steps; see the generator docstring for the',
        'step kinds. ``unparsed`` lists statements the extractor could not express,',
        'so a construct added upstream shows up as a gap rather than a silently',
        'wrong command.',
        '"""',
        '',
        'COMMANDS = {',
    ]
    for _cpp, web_id in JOBS:
        job = jobs[web_id]
        lines.append(f'    {web_id!r}: {{')
        lines.append('        "steps": [')
        for step in job['steps']:
            rendered = ', '.join(
                f'{key!r}: {value!r}'
                for key, value in (
                    (k, _restore_quotes(v) if isinstance(v, str) else v)
                    for k, v in step.items()
                )
            )
            lines.append(f'            {{{rendered}}},')
        lines.append('        ],')
        lines.append(f'        "unparsed": {job["unknown"]!r},')
        lines.append('    },')
    lines.append('}')
    lines.append('')
    total = sum(len(job['steps']) for job in jobs.values())
    unparsed = sum(len(job['unknown']) for job in jobs.values())
    lines.append(f'# {total} command steps, {unparsed} statements not expressed.')
    lines.append(f'STEP_COUNT = {total}')
    lines.append(f'UNPARSED_COUNT = {unparsed}')
    lines.append('')
    return '\n'.join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument(
        '--report',
        action='store_true',
        help='print what could not be parsed and exit',
    )
    parser.add_argument(
        '--relion-source',
        metavar='DIR',
        help='RELION checkout to read the C++ sources from '
             '(default: $RELION_SOURCE, else a relion/ checkout beside this one)',
    )
    args = parser.parse_args(argv)

    root = resolve_source_root(args.relion_source)
    source = strip_comments((root / 'src' / 'pipeline_jobs.cpp').read_text(encoding='utf-8'))
    jobs = extract(source)

    if args.report:
        total = sum(len(job['steps']) for job in jobs.values())
        unparsed = sum(len(job['unknown']) for job in jobs.values())
        print(f'{total} steps extracted, {unparsed} statements not expressed')
        for web_id, job in jobs.items():
            for text in job['unknown']:
                print(f'  {web_id}: {text[:110]}')
        return 0

    text = render(jobs)
    if args.check:
        existing = OUTPUT.read_text(encoding='utf-8') if OUTPUT.exists() else ''
        if existing != text:
            print(
                f'{OUTPUT} is out of date; run '
                f'scripts/generate_web_job_commands.py',
                file=sys.stderr,
            )
            return 1
        return 0

    OUTPUT.write_text(text, encoding='utf-8')
    total = sum(len(job['steps']) for job in jobs.values())
    unparsed = sum(len(job['unknown']) for job in jobs.values())
    print(
        f'wrote {OUTPUT} ({len(JOBS)} jobs, {total} steps, '
        f'{unparsed} unparsed)'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
