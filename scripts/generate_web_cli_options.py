#!/usr/bin/env python3
"""Extract every command-line option each job's program accepts.

The web form exposes the options the desktop GUI shows, which is a subset of
what the programs take: the GUI hides a few, renames others, and folds several
into one widget. ``--printCommand`` output cannot be inverted to recover them,
so the flags are read from the parsers themselves:

* C++ programs declare their flags with
  ``parser.getOption("--x", "usage", "default")`` / ``parser.checkOption(...)``
  (``src/args.h``);
* Python programs declare them as Typer parameters,
  ``name: Type = typer.Option(default, help=...)``.

The result is shipped to the job form as the "advanced arguments" list, so a
flag the GUI never shows is still reachable without editing source. Values the
form does set are marked so the panel does not offer a second control for the
same flag.

Run after changing any parser:

    python scripts/generate_web_cli_options.py
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

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
OUTPUT = WEBUI_ROOT / 'backend' / 'recryo' / 'generated_cli_options.py'


def _strip_comments(text: str) -> str:
    """Remove C++ comments, leaving string and character literals intact.

    A naive ``/*`` search eats ``"frames/*.mrc"``, which is a real default in
    ``pipeline_jobs.cpp``; tracking quotes is the only way to be sure.
    """
    out: List[str] = []
    i = 0
    n = len(text)
    in_line_comment = False
    in_block_comment = False
    quote: Optional[str] = None
    while i < n:
        char = text[i]
        nxt = text[i + 1] if i + 1 < n else ''
        if in_line_comment:
            if char == '\n':
                in_line_comment = False
                out.append(char)
            i += 1
            continue
        if in_block_comment:
            if char == '*' and nxt == '/':
                in_block_comment = False
                i += 2
                continue
            if char == '\n':
                out.append(char)
            i += 1
            continue
        if quote:
            out.append(char)
            if char == '\\' and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if char == quote:
                quote = None
            i += 1
            continue
        if char in ('"', "'"):
            quote = char
            out.append(char)
            i += 1
            continue
        if char == '/' and nxt == '/':
            in_line_comment = True
            i += 2
            continue
        if char == '/' and nxt == '*':
            in_block_comment = True
            i += 2
            continue
        out.append(char)
        i += 1
    return ''.join(out)


def _call_arguments(source: str, start: int) -> Tuple[List[str], int]:
    """Split the arguments of the call whose ``(`` opens at ``start``.

    Returns the top-level arguments and the index just past the closing
    parenthesis. Nested calls, parentheses inside string literals and braces are
    all tracked, because a default such as
    ``"a (b)"`` would otherwise truncate the list.
    """
    assert source[start] == '('
    depth = 0
    quote: Optional[str] = None
    current: List[str] = []
    args: List[str] = []
    i = start
    while i < len(source):
        char = source[i]
        if quote:
            current.append(char)
            if char == '\\' and i + 1 < len(source):
                current.append(source[i + 1])
                i += 2
                continue
            if char == quote:
                quote = None
            i += 1
            continue
        if char in ('"', "'"):
            quote = char
            current.append(char)
            i += 1
            continue
        if char == '(':
            depth += 1
            if depth > 1:
                current.append(char)
            i += 1
            continue
        if char == ')':
            depth -= 1
            if depth == 0:
                if ''.join(current).strip():
                    args.append(''.join(current).strip())
                return args, i + 1
            current.append(char)
            i += 1
            continue
        if char == ',' and depth == 1:
            args.append(''.join(current).strip())
            current = []
            i += 1
            continue
        current.append(char)
        i += 1
    return args, len(source)


def _string_literal(token: str) -> Optional[str]:
    token = token.strip()
    match = re.fullmatch(r'"((?:[^"\\]|\\.)*)"', token, re.DOTALL)
    if not match:
        return None
    return match.group(1).encode().decode('unicode_escape')


def _bool_literal(token: str) -> Optional[bool]:
    token = token.strip()
    if token == 'true':
        return True
    if token == 'false':
        return False
    return None


# Which parser source each job's program reads its flags with. A job that runs
# several programs lists both, in the order the job runs them.
CPP_SOURCES: Dict[str, List[str]] = {
    'motioncorr': ['src/motioncorr_runner.cpp'],
    'ctffind': ['src/ctffind_runner.cpp'],
    'tomo_align_tilt_series': ['src/align_tiltseries_runner.cpp'],
    'tomo_reconstruct_tomograms': [
        'src/jaz/tomography/programs/reconstruct_tomogram.cpp',
    ],
    'tomo_subtomo': ['src/jaz/tomography/programs/subtomo.cpp'],
    'inimodel': ['src/ml_optimiser.cpp', 'src/apps/align_symmetry.cpp'],
    'class3d': ['src/ml_optimiser.cpp'],
    'autorefine': ['src/ml_optimiser.cpp'],
    'multibody': [
        'src/ml_optimiser.cpp',
        'src/flex_analyser.cpp',
    ],
    'select': ['src/apps/star_handler.cpp'],
    'joinstar': ['src/apps/star_handler.cpp'],
    'tomo_recon_particle': [
        # The program's own flags, plus the ones it inherits from the shared
        # RefinementProgram base (--b, --j, --o, the optimisation set).
        'src/jaz/tomography/programs/reconstruct_particle.cpp',
        'src/jaz/tomography/programs/refinement.cpp',
    ],
    'tomo_ctf_refine': [
        'src/jaz/tomography/programs/ctf_refinement.cpp',
        'src/jaz/tomography/programs/refinement.cpp',
    ],
    'tomo_align_frame': [
        'src/jaz/tomography/programs/align.cpp',
        'src/jaz/tomography/programs/refinement.cpp',
    ],
    'maskcreate': ['src/apps/mask_create.cpp'],
    'postprocess': ['src/postprocessing.cpp'],
    'localres': ['src/postprocessing.cpp'],
    'tomo_import': ['src/jaz/tomography/apps/import_coordinates.cpp'],
}

# Python programs: (module file, subcommand or None). Typer derives the flag
# from the parameter name, so the extracted flags are hyphenated.
TYPER_SOURCES: Dict[str, List[str]] = {
    'tomo_import': [
        'src/tomography_python_programs/import_tilt_series/serialem.py',
        'src/tomography_python_programs/import_tilt_series/stub_alternative.py',
    ],
    'tomo_exclude_tilt_images': [
        'src/tomography_python_programs/exclude_tilt_images/_cli.py',
    ],
    'tomo_denoise_tomograms': [
        'src/tomography_python_programs/denoise/cryoCARE/train.py',
        'src/tomography_python_programs/denoise/cryoCARE/predict.py',
    ],
    'tomo_pick_tomograms': [
        'src/tomography_python_programs/pick/particles.py',
        'src/tomography_python_programs/pick/spheres.py',
        'src/tomography_python_programs/pick/filaments.py',
        'src/tomography_python_programs/get_particle_poses/particles.py',
        'src/tomography_python_programs/get_particle_poses/spheres.py',
        'src/tomography_python_programs/get_particle_poses/filaments.py',
    ],
}

# Programs whose options live outside this repository or are free-form, so the
# panel cannot offer a list. Recorded so the UI can say why rather than show an
# empty box.
UNEXTRACTABLE = {
    'modelangelo': 'options are defined by the model_angelo Python package',
    'external': 'the command line is written by the user',
}


def extract_cpp_options(path: Path) -> List[dict]:
    """Every flag a C++ program registers, in source order, with its section."""
    source = _strip_comments(path.read_text(encoding='utf-8', errors='replace'))
    headings = [
        (m.start(), _string_literal(m.group(1)) or '')
        for m in re.finditer(r'parser\s*\.\s*addSection\s*\(\s*("(?:[^"\\]|\\.)*")', source)
    ]
    options: List[dict] = []
    pattern = re.compile(r'parser\s*\.\s*(getOption|checkOption|addOption)\s*\(')
    for match in pattern.finditer(source):
        args, _end = _call_arguments(source, match.end() - 1)
        if not args:
            continue
        flag = _string_literal(args[0])
        if not flag or not flag.startswith('--'):
            continue
        kind = match.group(1)
        section = ''
        for offset, name in headings:
            if offset < match.start():
                section = name
            else:
                break
        entry = {
            'program': path.stem,
            'flag': flag,
            'kind': 'boolean' if kind == 'checkOption' else 'value',
            'usage': _string_literal(args[1]) if len(args) > 1 else None,
            'default': None,
            'required': kind == 'getOption' and len(args) <= 2,
            'hidden': False,
            'section': section,
        }
        if kind == 'getOption' and len(args) > 2:
            default = _string_literal(args[2])
            entry['default'] = default
            entry['required'] = default is None
            if len(args) > 3:
                entry['hidden'] = _bool_literal(args[3]) is True
        if kind == 'addOption':
            entry['kind'] = 'value'
            if len(args) > 2:
                default = _string_literal(args[2])
                if default is None or default == 'NULL':
                    entry['required'] = True
                else:
                    entry['default'] = default
            if len(args) > 3:
                entry['hidden'] = _bool_literal(args[3]) is True
        options.append(entry)
    return options


def _annotation_name(node) -> str:
    """The bare type name of an annotation, so ``Optional[bool]`` reads ``bool``."""
    text = ast.unparse(node) if hasattr(ast, 'unparse') else ''
    match = re.search(r'([A-Za-z_][A-Za-z0-9_]*)', text)
    return match.group(1) if match else ''


def extract_typer_options(path: Path) -> List[dict]:
    """Every Typer parameter of every command in a module.

    Typer builds the flag from the parameter name (``cache_size`` becomes
    ``--cache-size``), so the extracted flag is the hyphenated name, not
    whatever help text happens to say. Plain Python defaults are options too:
    ``cache_size: int = 3`` is reachable as ``--cache-size`` without a
    ``typer.Option`` wrapper, so both spellings are collected.
    """
    tree = ast.parse(path.read_text(encoding='utf-8', errors='replace'))
    options: List[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        arguments = node.args
        positional = list(arguments.posonlyargs) + list(arguments.args)
        defaults = [None] * (len(positional) - len(arguments.defaults)) + list(
            arguments.defaults
        )
        for parameter, default in zip(positional, defaults):
            if parameter.arg in ('self', 'cls', 'ctx'):
                continue
            annotation = _annotation_name(parameter.annotation) if parameter.annotation else ''
            if annotation in ('Typer', 'Context', 'OptionInfo'):
                continue
            call = default
            if isinstance(call, ast.Call):
                func_name = ''
                if isinstance(call.func, ast.Attribute):
                    func_name = call.func.attr
                elif isinstance(call.func, ast.Name):
                    func_name = call.func.id
                if func_name not in ('Option', 'Argument'):
                    continue
                value = None
                if call.args:
                    try:
                        value = ast.literal_eval(call.args[0])
                    except (ValueError, SyntaxError):
                        value = None
                usage = None
                for keyword in call.keywords:
                    if keyword.arg == 'help':
                        try:
                            usage = ast.literal_eval(keyword.value)
                        except (ValueError, SyntaxError):
                            usage = None
                declared_hyphen = None
                for keyword in call.keywords:
                    if keyword.arg in ('param_decls',):
                        try:
                            declared_hyphen = ast.literal_eval(keyword.value)
                        except (ValueError, SyntaxError):
                            declared_hyphen = None
            elif default is None:
                # No default at all is a required argument, but Typer only
                # makes it a flag through Option(...), so anything else here is
                # a positional. Skip.
                continue
            else:
                try:
                    value = ast.literal_eval(default)
                except (ValueError, SyntaxError):
                    continue
                usage = None
                declared_hyphen = None
            required = value is Ellipsis
            flag = '--' + parameter.arg.replace('_', '-')
            if isinstance(declared_hyphen, str) and declared_hyphen.startswith('--'):
                flag = declared_hyphen
            kind = 'boolean' if annotation == 'bool' or isinstance(value, bool) else 'value'
            options.append(
                {
                    'program': node.name,
                    'flag': flag,
                    'kind': kind,
                    'usage': usage,
                    'default': (
                        None
                        if required or value is None or value is Ellipsis
                        else str(value)
                    ),
                    'required': required,
                    'hidden': False,
                    'section': '',
                }
            )
    return options


def _dedupe(options: Iterable[dict]) -> List[dict]:
    """Keep the first declaration of each flag.

    ``ml_optimiser`` declares the same flag in several branches; the program
    accepts it once, and the first declaration carries the clearest usage text.
    """
    seen = set()
    unique: List[dict] = []
    for option in options:
        if option['flag'] in seen:
            continue
        seen.add(option['flag'])
        unique.append(option)
    return unique


def collect(root: Path) -> Dict[str, dict]:
    jobs: Dict[str, dict] = {}
    for job_type, sources in {**CPP_SOURCES}.items():
        options: List[dict] = []
        for relative in sources:
            path = root / relative
            if not path.is_file():
                raise SystemExit(f'{relative} is missing; cannot extract {job_type}')
            options.extend(extract_cpp_options(path))
        jobs.setdefault(job_type, {'sources': [], 'options': []})
        jobs[job_type]['sources'].extend(sources)
        jobs[job_type]['options'].extend(_dedupe(options))
    for job_type, sources in TYPER_SOURCES.items():
        options = []
        for relative in sources:
            path = root / relative
            if not path.is_file():
                raise SystemExit(f'{relative} is missing; cannot extract {job_type}')
            options.extend(extract_typer_options(path))
        entry = jobs.setdefault(job_type, {'sources': [], 'options': []})
        entry['sources'].extend(sources)
        entry['options'].extend(_dedupe(options))
    for job_type, reason in UNEXTRACTABLE.items():
        entry = jobs.setdefault(job_type, {'sources': [], 'options': []})
        entry['unavailable'] = reason
    return jobs


HEADER = '''# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is GENERATED from RELION's C++ sources and carries RELION's
# option text. Regenerate it; do not edit it by hand.
"""Command-line options each job's program accepts.

Generated by ``scripts/generate_web_cli_options.py``; do not edit by hand.

The desktop job window shows a subset of these flags, so this table is what
lets the web form offer the rest. ``kind`` is ``boolean`` for flags that take no
value and ``value`` otherwise; ``default`` is the parser's own default and
``required`` marks an option the program refuses to run without.

A job whose options cannot be read from a source file records ``unavailable``
with the reason, so the form can say so instead of showing an empty panel.
"""

'''


def render(jobs: Dict[str, dict]) -> str:
    lines = [HEADER, 'CLI_OPTIONS = {']
    for job_type in sorted(jobs):
        entry = jobs[job_type]
        lines.append(f'    {job_type!r}: {{')
        lines.append(f'        "sources": {entry["sources"]!r},')
        if 'unavailable' in entry:
            lines.append(f'        "unavailable": {entry["unavailable"]!r},')
        lines.append('        "options": [')
        for option in entry['options']:
            lines.append('            ' + repr(option) + ',')
        lines.append('        ],')
        lines.append('    },')
    lines.append('}')
    lines.append('')
    return '\n'.join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--check',
        action='store_true',
        help='exit non-zero if the checked-in file is out of date',
    )
    parser.add_argument(
        '--relion-source',
        metavar='DIR',
        help='RELION checkout to read the C++ sources from '
             '(default: $RELION_SOURCE, else a relion/ checkout beside this one)',
    )
    arguments = parser.parse_args(argv)

    jobs = collect(resolve_source_root(arguments.relion_source))
    text = render(jobs)
    total = sum(len(entry['options']) for entry in jobs.values())

    if arguments.check:
        existing = OUTPUT.read_text(encoding='utf-8') if OUTPUT.exists() else ''
        if existing != text:
            print(
                f'{OUTPUT} is out of date; run '
                f'{Path(__file__).name}',
                file=sys.stderr,
            )
            return 1
        return 0

    OUTPUT.write_text(text, encoding='utf-8')
    # RELION_SOURCE may point outside this project, so a path relative to it
    # would not always be expressible; show the file relative to webui/ instead.
    try:
        shown = OUTPUT.relative_to(WEBUI_ROOT)
    except ValueError:
        shown = OUTPUT
    print(f'wrote {shown}: {total} options over {len(jobs)} jobs')
    for job_type in sorted(jobs):
        entry = jobs[job_type]
        print(f'  {job_type:28} {len(entry["options"]):4} options')
    return 0


if __name__ == '__main__':
    sys.exit(main())
