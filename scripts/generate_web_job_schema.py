#!/usr/bin/env python3
"""Generate the web UI's job schema from RELION's own C++ sources.

The web front end shows a form per job and has to build the exact argv that
``RelionJob::getCommands*`` would build. Copying those ~3000 lines of C++ by
hand would drift the moment RELION changes, so the option metadata (label,
default, range, help text, radio choices) and the window layout (which tab each
option appears under, in what order) are extracted from the sources instead:

  src/pipeline_jobs.cpp   ``joboptions["x"] = JobOption(...)`` -> metadata
  src/gui_jobwindow.cpp   ``place("x")`` under ``tabN->label()`` -> layout

What is *not* generated is the argv translation, because it is not data: the
same option reaches the command line as ``--binned_angpix`` for a C++ program
and ``--tomogram-star-file`` for a Python one, some flags only exist under a
condition, and several jobs prepend a subcommand. Those live in
``job_commands.py`` and are checked against these generated option names.

Run it from the repository root:

    python scripts/generate_web_job_schema.py

It rewrites ``backend/recryo/generated_schema.py``. The test
``test_web_schema.py`` re-runs the extraction and fails if the checked-in file
is stale, so a RELION source change that alters an option cannot silently go
unnoticed.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

# This script lives in webui/scripts/, so the RELION checkout it reads is the
# repository it sits inside; RELION_SOURCE points it elsewhere when the agent is
# developed outside a RELION checkout.
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
OUTPUT = WEBUI_ROOT / 'backend' / 'recryo' / 'generated_schema.py'

# Job name in the C++ source -> (web id, GUI tab title, process type constant).
# The tab titles are what gui_mainwindow.cpp adds to the browser list, so they
# are also the labels the front end shows.
JOBS = [
    ('TomoImport', 'tomo_import', 'Import'),
    ('Motioncorr', 'motioncorr', 'Motion correction'),
    ('Ctffind', 'ctffind', 'CTF estimation'),
    ('TomoExcludeTiltImages', 'tomo_exclude_tilt_images', 'Exclude tilt-images'),
    ('TomoAlignTiltSeries', 'tomo_align_tilt_series', 'Align tilt-series'),
    ('TomoReconstructTomograms', 'tomo_reconstruct_tomograms', 'Reconstruct tomograms'),
    ('TomoDenoiseTomograms', 'tomo_denoise_tomograms', 'Denoise tomograms'),
    ('TomoPickTomograms', 'tomo_pick_tomograms', 'Pick tomograms'),
    ('TomoSubtomo', 'tomo_subtomo', 'Extract subtomos'),
    ('Inimodel', 'inimodel', '3D initial reference'),
    ('Class3D', 'class3d', '3D classification'),
    ('Autorefine', 'autorefine', '3D auto-refine'),
    ('MultiBody', 'multibody', '3D multi-body'),
    ('Select', 'select', 'Subset selection'),
    ('TomoReconPart', 'tomo_recon_particle', 'Reconstruct particle'),
    ('TomoCtfRefine', 'tomo_ctf_refine', 'CTF refinement'),
    ('TomoAlign', 'tomo_align_frame', 'Bayesian polishing'),
    ('Maskcreate', 'maskcreate', 'Mask creation'),
    ('Joinstar', 'joinstar', 'Join star files'),
    ('Postprocess', 'postprocess', 'Post-processing'),
    ('Localres', 'localres', 'Local resolution'),
    ('ModelAngelo', 'modelangelo', 'ModelAngelo building'),
    ('External', 'external', 'External'),
]

# The pipeline constants the GUI derives a job's type and directory from. These
# are what `Pipeline::read` uses to decide which job window to open, so the web
# side must use exactly the same strings.
#
# The tomo jobs' directory names come from PROC_TOMO_*_DIRNAME and the SPA ones
# from PROC_*_DIRNAME; `reconstruct tomograms` is "Tomograms", not the job's own
# name, which is an easy mistake to make.
PIPELINE_CONSTANTS = {
    'TomoImport': ('PROC_TOMO_IMPORT', 'PROC_TOMO_IMPORT_DIRNAME', 'PROC_TOMO_IMPORT_LABELNEW'),
    'Motioncorr': ('PROC_MOTIONCORR', 'PROC_MOTIONCORR_DIRNAME', 'PROC_MOTIONCORR_LABELNEW'),
    'Ctffind': ('PROC_CTFFIND', 'PROC_CTFFIND_DIRNAME', 'PROC_CTFFIND_LABELNEW'),
    'TomoExcludeTiltImages': (
        'PROC_TOMO_EXCLUDE_TILT_IMAGES', 'PROC_TOMO_EXCLUDE_TILT_IMAGES_DIRNAME',
        'PROC_TOMO_EXCLUDE_TILT_IMAGES_LABELNEW'),
    'TomoAlignTiltSeries': (
        'PROC_TOMO_ALIGN_TILTSERIES', 'PROC_TOMO_ALIGN_TILTSERIES_DIRNAME',
        'PROC_TOMO_ALIGN_TILTSERIES_LABELNEW'),
    'TomoReconstructTomograms': (
        'PROC_TOMO_RECONSTRUCT_TOMOGRAM', 'PROC_TOMO_RECONSTRUCT_TOMOGRAM_DIRNAME',
        'PROC_TOMO_RECONSTRUCT_TOMOGRAM_LABELNEW'),
    'TomoDenoiseTomograms': (
        'PROC_TOMO_DENOISE_TOMOGRAM', 'PROC_TOMO_DENOISE_DIRNAME',
        'PROC_TOMO_DENOISE_LABELNEW'),
    'TomoPickTomograms': (
        'PROC_TOMO_PICK_TOMOGRAM', 'PROC_TOMO_PICK_DIRNAME',
        'PROC_TOMO_PICK_LABELNEW'),
    'TomoSubtomo': ('PROC_TOMO_SUBTOMO', 'PROC_TOMO_SUBTOMO_DIRNAME',
                    'PROC_TOMO_SUBTOMO_LABELNEW'),
    'Inimodel': ('PROC_INIMODEL', 'PROC_INIMODEL_DIRNAME', 'PROC_INIMODEL_LABELNEW'),
    'Class3D': ('PROC_3DCLASS', 'PROC_3DCLASS_DIRNAME', 'PROC_3DCLASS_LABELNEW'),
    'Autorefine': ('PROC_3DAUTO', 'PROC_3DAUTO_DIRNAME', 'PROC_3DAUTO_LABELNEW'),
    'MultiBody': ('PROC_MULTIBODY', 'PROC_MULTIBODY_DIRNAME', 'PROC_MULTIBODY_LABELNEW'),
    'Select': ('PROC_CLASSSELECT', 'PROC_CLASSSELECT_DIRNAME', 'PROC_CLASSSELECT_LABELNEW'),
    'TomoReconPart': ('PROC_TOMO_RECONSTRUCT', 'PROC_TOMO_RECONSTRUCT_DIRNAME',
                      'PROC_TOMO_RECONSTRUCT_LABELNEW'),
    'TomoCtfRefine': ('PROC_TOMO_CTFREFINE', 'PROC_TOMO_CTFREFINE_DIRNAME',
                      'PROC_TOMO_CTFREFINE_LABELNEW'),
    'TomoAlign': ('PROC_TOMO_ALIGN', 'PROC_TOMO_ALIGN_DIRNAME',
                  'PROC_TOMO_ALIGN_LABELNEW'),
    'Maskcreate': ('PROC_MASKCREATE', 'PROC_MASKCREATE_DIRNAME', 'PROC_MASKCREATE_LABELNEW'),
    'Joinstar': ('PROC_JOINSTAR', 'PROC_JOINSTAR_DIRNAME', 'PROC_JOINSTAR_LABELNEW'),
    'Postprocess': ('PROC_POST', 'PROC_POST_DIRNAME', 'PROC_POST_LABELNEW'),
    'Localres': ('PROC_RESMAP', 'PROC_RESMAP_DIRNAME', 'PROC_RESMAP_LABELNEW'),
    'ModelAngelo': ('PROC_MODELANGELO', 'PROC_MODELANGELO_DIRNAME',
                    'PROC_MODELANGELO_LABELNEW'),
    'External': ('PROC_EXTERNAL', 'PROC_EXTERNAL_DIRNAME', 'PROC_EXTERNAL_LABELNEW'),
}

# The C++ window initialiser does not always match the job name.
WINDOW_OVERRIDES = {
    'TomoAlignTiltSeries': 'initialiseTomoAlignTiltseriesWindow',
    # The C++ abbreviates these, so the default name-based lookup found no
    # window at all and the job's form had only the Run tab.
    'TomoReconPart': 'initialiseTomoReconParWindow',
    'Localres': 'initialiseLocresWindow',
}


def _function_body(source: str, signature: str) -> str:
    """Body of a top-level ``RelionJob::`` or ``JobWindow::`` function."""
    start = source.find(signature)
    if start < 0:
        return ''
    ends = [
        index
        for index in (
            source.find('\nvoid RelionJob::', start + 5),
            source.find('\nbool RelionJob::', start + 5),
            source.find('\nvoid JobWindow::', start + 5),
            source.find('\nbool JobWindow::', start + 5),
        )
        if index > 0
    ]
    return source[start : min(ends) if ends else len(source)]


def _strip_comments(text: str) -> str:
    """Remove C++ comments, leaving string and character literals intact.

    A regex is not enough here: patterns like ``"frames/*.mrc"`` contain ``/*``
    inside a string literal, and a naive strip would treat the rest of the file
    as a comment and delete thousands of lines.
    """
    out: List[str] = []
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character == '"' or character == "'":
            quote = character
            out.append(character)
            index += 1
            while index < length:
                inner = text[index]
                out.append(inner)
                index += 1
                if inner == '\\' and index < length:
                    out.append(text[index])
                    index += 1
                    continue
                if inner == quote:
                    break
            continue
        if character == '/' and index + 1 < length:
            following = text[index + 1]
            if following == '/':
                while index < length and text[index] != '\n':
                    index += 1
                continue
            if following == '*':
                index += 2
                while index + 1 < length and not (
                    text[index] == '*' and text[index + 1] == '/'
                ):
                    index += 1
                index += 2
                continue
        out.append(character)
        index += 1
    return ''.join(out)


def _split_arguments(raw: str) -> List[str]:
    """Split a C++ argument list on commas that are not inside quotes."""
    parts: List[str] = []
    current: List[str] = []
    depth = 0
    in_string = False
    escape = False
    for character in raw:
        if escape:
            current.append(character)
            escape = False
            continue
        if character == '\\':
            current.append(character)
            escape = True
            continue
        if character == '"':
            in_string = not in_string
            current.append(character)
            continue
        if not in_string:
            if character in '([':
                depth += 1
            elif character in ')]':
                depth -= 1
            elif character == ',' and depth == 0:
                parts.append(''.join(current).strip())
                current = []
                continue
        current.append(character)
    tail = ''.join(current).strip()
    if tail:
        parts.append(tail)
    return parts


_STRING_LITERAL = re.compile(r'^"(.*)"$', re.S)


def _parse_string(argument: str) -> Optional[str]:
    """A C++ string literal, with adjacent literals and (std::string) casts."""
    argument = argument.strip()
    argument = re.sub(r'^\(?\s*std::string\s*\)?\s*', '', argument).strip()
    if argument == '""':
        return ''
    pieces = re.findall(r'"((?:[^"\\]|\\.)*)"', argument)
    if not pieces:
        return None
    joined = ''.join(pieces)
    return joined.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')


def _parse_number(argument: str) -> Optional[float]:
    argument = argument.strip().rstrip('f')
    try:
        return float(argument)
    except ValueError:
        return None


def _parse_bool(argument: str) -> Optional[bool]:
    argument = argument.strip()
    if argument == 'true':
        return True
    if argument == 'false':
        return False
    return None


def _radio_vectors(source: str) -> Dict[str, List[str]]:
    """``job_xxx_options`` initialiser lists in pipeline_jobs.h."""
    vectors: Dict[str, List[str]] = {}
    for match in re.finditer(
        r'static const std::vector<std::string>\s+(\w+)\s*\{(.*?)\};', source, re.S
    ):
        vectors[match.group(1)] = re.findall(r'"((?:[^"\\]|\\.)*)"', match.group(2))
    return vectors


def _label_macros(source_h: str) -> Dict[str, str]:
    """``LABEL_FOO`` -> the string value it stands for."""
    return dict(
        re.findall(r'#define\s+(LABEL_\w+)\s+"([^"]*)"', source_h)
    )


def _scalar_macros(source_h: str) -> Dict[str, str]:
    """Simple ``#define NAME 42`` / ``#define NAME "text"`` constants.

    The common run options take their bounds and defaults from macros that can be
    overridden at configure time or by environment variables
    (``DEFAULTMPIMAX``, ``DEFAULTNRTHREADS``, ...). Resolving them here keeps the
    form's ranges the same as the GUI's, instead of hard-coding 64 and 16.
    """
    macros: Dict[str, str] = {}
    for match in re.finditer(
        r'#define\s+([A-Z][A-Z0-9_]+)\s+("([^"]*)"|[-0-9.]+|true|false)',
        source_h,
    ):
        macros[match.group(1)] = match.group(2)
    return macros


def _resolve_macro(text: str, values: Dict[str, str]) -> str:
    """Replace a leading simple macro with its literal value.

    ``qsub_nrmpi_val`` and ``mpi_max`` are local variables in the C++, not
    macros, so they cannot be resolved from the header; those are left as-is and
    the caller keeps the option without a numeric bound. ``DEFAULTMPIMAX`` and
    friends are macros and are resolved here.
    """
    text = text.strip()
    if text in values:
        return values[text]
    return text


def _parse_joboption(
    variable: str,
    raw: str,
    radios: Dict[str, List[str]],
    labels: Dict[str, str],
    values: Optional[Dict[str, str]] = None,
    depth: int = 1,
) -> dict:
    """Turn one ``JobOption(...)`` argument list into a form field description."""
    values = values or {}
    args = _split_arguments(raw)
    if len(args) < 2:
        return {}

    label = _parse_string(args[0])
    field: dict = {
        'variable': variable,
        'label': label if label is not None else variable,
    }

    # Node-typed input: (label, node_type_macro, depth, default, pattern, help).
    # The node type is a LABEL_* macro, so the string-literal parse fails and
    # the argument has to be recognised by name instead.
    if len(args) == 6 and args[1].strip() in labels:
        field.update(
            kind='input_node',
            node_type=labels[args[1].strip()],
            default=_parse_string(args[3]) or '',
            pattern=_parse_string(args[4]) or '',
            help=_parse_string(args[5]) or '',
        )
        return field

    # Radio: (label, vector, index, help)
    if len(args) == 4 and args[1].strip() in radios:
        choices = radios[args[1].strip()]
        try:
            selected = int(args[2])
        except ValueError:
            selected = 0
        field.update(
            kind='radio',
            choices=choices,
            default=choices[selected] if 0 <= selected < len(choices) else '',
            help=_parse_string(args[3]) or '',
        )
        return field

    # Slider: (label, default, min, max, step, help)
    if len(args) == 6:
        default = _parse_number(_resolve_macro(args[1], values))
        minimum = _parse_number(_resolve_macro(args[2], values))
        maximum = _parse_number(_resolve_macro(args[3], values))
        step = _parse_number(_resolve_macro(args[4], values))
        if None not in (default, minimum, maximum, step):
            field.update(
                kind='slider',
                default=default,
                min=minimum,
                max=maximum,
                step=step,
                help=_parse_string(args[5]) or '',
            )
            return field

    # Boolean: (label, bool, help). The default can be a local that was
    # resolved to a macro ('false'), so accept the resolved text too.
    if len(args) == 3:
        boolean = _parse_bool(_resolve_macro(args[1], values))
        if boolean is not None:
            field.update(kind='boolean', default=boolean, help=_parse_string(args[2]) or '')
            return field

    # File name: (label, default, pattern, directory, help). The default can be
    # std::string(local), where the local holds a macro value.
    if len(args) == 5:
        default_text = _parse_string(args[1])
        if default_text is None:
            inner = re.match(
                r'\(?\s*std::string\s*\)?\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*$',
                args[1].strip(),
            )
            if inner:
                resolved = _resolve_macro(inner.group(1), values)
                default_text = resolved.strip('"') if resolved != inner.group(1) else None
        field.update(
            kind='path',
            default=default_text or '',
            pattern=_parse_string(args[2]) or '',
            help=_parse_string(args[4]) or '',
        )
        return field

    # Plain string: (label, default, help). The default can be
    # std::string(DEFAULTQUEUENAME), whose macro has to be resolved.
    raw_default = args[1].strip()
    default_text = _parse_string(raw_default)
    if default_text is None:
        # `std::string(default_queue)` or `std::string(DEFAULTQUEUENAME)`; the
        # name may be a resolved local holding the quoted macro value.
        inner = re.match(
            r'\(?\s*std::string\s*\)?\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*$',
            raw_default,
        )
        if inner:
            resolved = _resolve_macro(inner.group(1), values)
            if resolved.startswith('"') and resolved.endswith('"'):
                default_text = resolved[1:-1]
            elif resolved != inner.group(1):
                default_text = resolved
    field.update(
        kind='string',
        default=default_text or '',
        help=_parse_string(args[-1]) or '',
    )
    return field


def extract_options(source: str, source_h: str) -> Dict[str, List[dict]]:
    radios = _radio_vectors(source_h)
    labels = _label_macros(source_h)
    values = _scalar_macros(source_h)
    result: Dict[str, List[dict]] = {}

    # The tomo refine jobs get their input options from a shared helper rather
    # than from their own initialiser, so parse it once and prepend it to every
    # job that calls addTomoInputOptions().
    shared = _function_body(source, 'void RelionJob::addTomoInputOptions(')
    shared_fields: List[dict] = []
    for match in re.finditer(
        r'joboptions\["(\w+)"\]\s*=\s*JobOption\((.*?)\);', shared, re.S
    ):
        parsed = _parse_joboption(
            match.group(1), match.group(2), radios, labels, values
        )
        if parsed:
            shared_fields.append(parsed)

    # RelionJob::initialise() adds the options every job shares: thread count,
    # MPI process count, and the queue settings. They are not in any
    # initialise*Job, so a job-only scan misses them entirely.
    run_body = _function_body(source, 'void RelionJob::initialise(int _job_type)')
    # The run options' defaults and bounds are locals that fall back to a macro
    # when no environment variable is set, e.g.
    #   int mpi_max = (getenv(...) == NULL) ? DEFAULTMPIMAX : textToInteger(...)
    #   const char qsub_nrmpi_val = (t ? atoi(t) : DEFAULTNRMPI);
    # The form uses the macro value, which is RELION's documented default.
    local_defaults: Dict[str, str] = {}
    # Any `x = (cond) ? MACRO : other;` or `x = MACRO;` assignment: the macro is
    # the compiled-in default used when the environment variable is absent.
    for match in re.finditer(
        r'\b(\w+)\s*=\s*(?:\([^;]*?\)\s*)?\?[^;]*?:\s*([A-Z][A-Z0-9_]+)\s*;',
        run_body,
        re.S,
    ):
        local_defaults[match.group(1)] = values.get(match.group(2), '')
    for match in re.finditer(
        r'\b(\w+)\s*=\s*\([^;?]*\)\s*\?\s*([A-Z][A-Z0-9_]+)\s*:', run_body
    ):
        local_defaults.setdefault(match.group(1), values.get(match.group(2), ''))
    # `x = (getenv(...) ? atoi(getenv(...)) : MACRO);` - the ternary is inside
    # the parentheses and there is no trailing semicolon inside the group.
    for match in re.finditer(
        r'\b(\w+)\s*=\s*\(\s*\w+\s*\?[^;:]*?:\s*([A-Z][A-Z0-9_]+)\s*\)\s*;',
        run_body,
    ):
        local_defaults.setdefault(match.group(1), values.get(match.group(2), ''))
    for match in re.finditer(
        r'\b(\w+)\s*=\s*\([^;?]*\?[^;]*?:\s*([A-Z][A-Z0-9_]+)\s*\)', run_body
    ):
        local_defaults.setdefault(match.group(1), values.get(match.group(2), ''))
    for match in re.finditer(
        r'\b(\w+)\s*=\s*([A-Z][A-Z0-9_]+)\s*;', run_body
    ):
        local_defaults.setdefault(match.group(1), values.get(match.group(2), ''))
    # `char default_qsub[] = DEFAULTQSUBLOCATION;` then used via default_qsub.
    for match in re.finditer(
        r'\b(\w+)\s*\[\s*\]\s*=\s*([A-Z][A-Z0-9_]+)\s*;', run_body
    ):
        local_defaults.setdefault(match.group(1), values.get(match.group(2), ''))
    # `if (ptr == NULL) { ptr = local; }` - the getenv fallback RELION uses for
    # the submission template, where the real default lives in a local array.
    for match in re.finditer(
        r'if\s*\(\s*(\w+)\s*==\s*NULL\s*\)\s*\{\s*\1\s*=\s*(\w+)\s*;\s*\}',
        run_body,
    ):
        target, source_name = match.group(1), match.group(2)
        if source_name in local_defaults:
            local_defaults[target] = local_defaults[source_name]
    run_values = {**values, **{k: v for k, v in local_defaults.items() if v}}
    run_fields: List[dict] = []
    for match in re.finditer(
        r'joboptions\["(\w+)"\]\s*=\s*JobOption\((.*?)\);', run_body, re.S
    ):
        parsed = _parse_joboption(
            match.group(1), match.group(2), radios, labels, run_values
        )
        if parsed:
            parsed['shared'] = True
            run_fields.append(parsed)

    for cpp_name, _, _ in JOBS:
        body = _function_body(source, f'void RelionJob::initialise{cpp_name}Job()')
        fields: List[dict] = []
        seen = set()
        # The run options come first, as they do in the GUI's Run tab.
        for parsed in run_fields:
            seen.add(parsed['variable'])
            fields.append(parsed)
        if 'addTomoInputOptions(' in body:
            for parsed in shared_fields:
                if parsed['variable'] in seen:
                    continue
                seen.add(parsed['variable'])
                fields.append(parsed)
        for match in re.finditer(
            r'joboptions\["(\w+)"\]\s*=\s*JobOption\((.*?)\);', body, re.S
        ):
            variable = match.group(1)
            if variable in seen:
                continue
            seen.add(variable)
            parsed = _parse_joboption(
                variable, match.group(2), radios, labels, values
            )
            if parsed:
                fields.append(parsed)
        result[cpp_name] = fields
    return result


def extract_layout(source: str) -> Dict[str, List[dict]]:
    """Tab title plus the ordered option variables placed on it."""
    result: Dict[str, List[dict]] = {}
    for cpp_name, _, _ in JOBS:
        function = WINDOW_OVERRIDES.get(
            cpp_name, f'initialise{cpp_name}Window'
        )
        body = _function_body(source, f'void JobWindow::{function}()')
        tabs: List[dict] = []
        current: Optional[dict] = None
        gates: List[dict] = []
        for line in body.splitlines():
            stripped = line.strip()
            match = re.match(r'tab(\d+)->label\("([^"]*)"\)', stripped)
            if match:
                current = {'index': int(match.group(1)), 'title': match.group(2), 'fields': []}
                tabs.append(current)
                continue
            if current is None:
                continue
            # A group gate: place("do_x", TOGGLE_DEACTIVATE, groupN) makes the
            # fields added while groupN->begin() is open depend on do_x being
            # Yes (or No, when actually_activate is true). `guientries[..]
            # .cb_menu_i()` at groupN->end() applies that initial state.
            match = re.match(
                r'place\("([^"]+)",\s*TOGGLE_DEACTIVATE,\s*(group\d+)'
                r'(?:,\s*(true|false))?',
                stripped,
            )
            if match:
                gate = {
                    'controller': match.group(1),
                    'group': match.group(2),
                    'active_when': 'false' if match.group(3) == 'true' else 'true',
                }
                gates.append(gate)
                current['fields'].append(match.group(1))
                current.setdefault('gates', []).append(gate)
                continue
            opened = re.search(r'(group\d+)->begin\(\)', stripped)
            if opened:
                for gate in gates:
                    if gate['group'] == opened.group(1):
                        gate['open'] = True
                continue
            if re.search(r'\bgroup\d+->end\(\)', stripped):
                # Close the gate whose fields just finished.
                for gate in reversed(gates):
                    if gate.get('open'):
                        gate['fields'] = list(gate.get('fields', []))
                        gate.pop('open', None)
                        break
                continue
            match = re.match(r'place\("([^"]+)"', stripped)
            if match:
                variable = match.group(1)
                current['fields'].append(variable)
                for gate in gates:
                    if gate.get('open'):
                        gate.setdefault('fields', []).append(variable)
                continue
            # place2/place3 take several variables plus one shared display label;
            # only the variables are fields.
            match = re.match(
                r'place3\("([^"]+)",\s*"([^"]+)",\s*"([^"]+)",\s*"([^"]*)"',
                stripped,
            )
            if match:
                current['fields'].extend(
                    [match.group(1), match.group(2), match.group(3)]
                )
                continue
            match = re.match(
                r'place2\("([^"]+)",\s*"([^"]+)",\s*"([^"]*)"', stripped
            )
            if match:
                current['fields'].extend([match.group(1), match.group(2)])
                continue
            match = re.match(r'placeTomoInput\(([^)]*)\)', stripped)
            if match:
                # The GUI expands this into the optimisation set plus the
                # individual components it was told the job can take
                # (JobWindow::placeTomoInput, gui_jobwindow.cpp:2558). Recording
                # one placeholder lost `in_optimisation`, so a tomography refine
                # job had no way to name its input at all.
                flags = [
                    part.strip() == 'true'
                    for part in match.group(1).split(',')
                ]
                expanded = ['in_optimisation', 'use_direct_entries']
                for name, wanted in zip(
                    ('in_particles', 'in_tomograms', 'in_trajectories', 'in_manifolds'),
                    flags,
                ):
                    if wanted:
                        expanded.append(name)
                current['fields'].extend(expanded)
                for gate in gates:
                    if gate.get('open'):
                        gate.setdefault('fields', []).extend(expanded)
        result[cpp_name] = tabs
    return result


def extract_pipeline_constants(source_h: str) -> Dict[str, dict]:
    """Resolve each job's type constant, directory name and process label.

    The GUI reads ``rlnPipeLineProcessTypeLabel`` and, for older pipelines, the
    directory prefix, so both have to match its own constants exactly.
    ``get_proc_type`` falls back to the directory name when the label is
    unknown, which is why a wrong directory name still "works" until a job is
    cleaned or undeleted.
    """
    defines = dict(re.findall(r'#define\s+(\w+)\s+"([^"]*)"', source_h))
    result: Dict[str, dict] = {}
    for cpp_name, _, _ in JOBS:
        constants = PIPELINE_CONSTANTS.get(cpp_name)
        if constants is None:
            continue
        type_constant, dirname_constant, label_constant = constants
        result[cpp_name] = {
            'process_type': type_constant,
            'directory': defines.get(dirname_constant, ''),
            'process_label': defines.get(label_constant, ''),
        }
    return result


def render(
    options: Dict[str, List[dict]],
    layout: Dict[str, List[dict]],
    pipeline: Dict[str, dict],
) -> str:
    lines = [
        '"""Job option metadata extracted from RELION\'s C++ sources.',
        '',
        'Generated by ``scripts/generate_web_job_schema.py``; do not edit by hand.',
        'Run the generator after changing ``src/pipeline_jobs.cpp`` or',
        '``src/gui_jobwindow.cpp``, and let ``test_web_schema.py`` confirm the file',
        'is in step with the sources.',
        '',
        'Every job is a dict with ``fields`` (form widgets and their defaults),',
        '``tabs`` (which fields the desktop job window groups together, in order)',
        'and the pipeline constants the GUI uses to identify the job. The names',
        'here are the GUI\'s variable names, which is also what ``job.star``',
        'stores; ``job_commands.py`` maps them to command-line flags.',
        '"""',
        '',
        'JOBS = {',
    ]
    for cpp_name, web_id, title in JOBS:
        fields = options.get(cpp_name, [])
        tabs = list(layout.get(cpp_name, []))
        # The desktop GUI builds a Run tab at runtime (JobWindow::setupRunTab)
        # rather than in each initialise*Window, so it is not in the extracted
        # layout. Append it so the thread/MPI/queue controls have a home that
        # matches where a RELION user looks for them.
        run_variables = [
            field['variable']
            for field in fields
            if field.get('shared') and field['variable'] != 'other_args'
        ]
        if run_variables:
            # setupRunTab puts the queue options in a group controlled by
            # do_queue, so they are only live when a queue is being used.
            queue_fields = [
                variable
                for variable in (
                    'queuename', 'qsub', 'qsubscript', 'min_dedicated',
                )
                if variable in run_variables
            ]
            run_tab = {
                'index': 99,
                'title': 'Run',
                'fields': [*run_variables, 'other_args'],
            }
            if queue_fields:
                run_tab['gates'] = [
                    {
                        'controller': 'do_queue',
                        'group': 'queue_group',
                        'active_when': 'true',
                        'fields': queue_fields,
                    }
                ]
            tabs.append(run_tab)
        constants = pipeline.get(cpp_name, {})
        lines.append(f'    {web_id!r}: {{')
        lines.append(f'        "cpp_name": {cpp_name!r},')
        lines.append(f'        "title": {title!r},')
        lines.append(f'        "directory": {constants.get("directory", "")!r},')
        lines.append(f'        "process_label": {constants.get("process_label", "")!r},')
        lines.append(f'        "process_type": {constants.get("process_type", "")!r},')
        lines.append('        "fields": [')
        for field in fields:
            rendered = ', '.join(
                f'{key!r}: {value!r}' for key, value in field.items()
            )
            lines.append(f'            {{{rendered}}},')
        lines.append('        ],')
        lines.append('        "tabs": [')
        for tab in tabs:
            gate_text = ''
            if tab.get('gates'):
                gates = [
                    {
                        'controller': gate['controller'],
                        'group': gate['group'],
                        'active_when': gate['active_when'],
                        'fields': gate.get('fields', []),
                    }
                    for gate in tab['gates']
                ]
                gate_text = f', "gates": {gates!r}'
            lines.append(
                f'            {{"index": {tab["index"]}, "title": {tab["title"]!r}, '
                f'"fields": {tab["fields"]!r}{gate_text}}},'
            )
        lines.append('        ],')
        lines.append('    },')
    lines.append('}')
    lines.append('')
    lines.append('# Total number of options per job, handy for the coverage test.')
    lines.append(
        'FIELD_COUNTS = {'
        + ', '.join(
            f'{web_id!r}: {len(options.get(cpp_name, []))}'
            for cpp_name, web_id, _ in JOBS
        )
        + '}'
    )
    lines.append('')
    return '\n'.join(lines)


def main(argv: Optional[List[str]] = None) -> int:
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

    root = resolve_source_root(arguments.relion_source)
    source = _strip_comments((root / 'src' / 'pipeline_jobs.cpp').read_text(encoding='utf-8'))
    source_h = (root / 'src' / 'pipeline_jobs.h').read_text(encoding='utf-8')
    window_source = _strip_comments((root / 'src' / 'gui_jobwindow.cpp').read_text(encoding='utf-8'))

    options = extract_options(source, source_h)
    layout = extract_layout(window_source)
    pipeline = extract_pipeline_constants(source_h)
    text = render(options, layout, pipeline)

    if arguments.check:
        existing = OUTPUT.read_text(encoding='utf-8') if OUTPUT.exists() else ''
        if existing != text:
            print(
                f'{OUTPUT} is out of date; run scripts/generate_web_job_schema.py',
                file=sys.stderr,
            )
            return 1
        return 0

    OUTPUT.write_text(text, encoding='utf-8')
    total = sum(len(fields) for fields in options.values())
    print(f'wrote {OUTPUT} ({len(JOBS)} jobs, {total} options)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
