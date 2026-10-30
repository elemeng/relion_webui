"""The CLI surface of each job, and the extra arguments the form does not cover.

The desktop job window shows a subset of what a program accepts: it hides a few
flags, renames others, and folds several into one widget. ``job.star`` records
only the GUI's variables, so those hidden flags are unreachable from a job
window - but the program still accepts them on the command line.

``generated_cli_options.py`` lists them, extracted from the parsers themselves.
This module turns that list into:

* what the advanced panel offers, with the flags the form already sets marked
  so the same value is not configured twice;
* the argv tokens for whatever the user adds, appended after the builder's own
  arguments - and, because ``IOParser::getOption`` keeps the value that appears
  last, an added flag intentionally overrides a form value;
* a warning when an added flag is not one the program knows, which the C++
  parser would only print to the log.
"""

from __future__ import annotations

import shlex
from typing import Dict, Iterable, List, Optional, Tuple
from .generated_cli_options import CLI_OPTIONS
from .generated_schema import JOBS as SCHEMA_JOBS

# The option the GUI and the C++ builders already reserve for free-form text.
OTHER_ARGS_VARIABLE = 'other_args'
# The request field carrying structured extra arguments. Kept separate from
# `other_args` so a value can be validated and rendered rather than pasted.
CLI_EXTRA_VARIABLE = 'cli_extra'

# Values used only to make a builder run, so the flags it emits can be read.
# They are never executed; the point is the flag list, not the paths.
_PLACEHOLDER = 'x'


def _placeholder_sets(job_type: str) -> List[Dict[str, object]]:
    """Several complete option sets, so every branch of a builder runs.

    One set cannot reach every conditional: a boolean that is on can hide the
    else-branch, and some combinations are rejected outright by a cross-option
    rule (``tomo_reconstruct_tomograms`` refuses Fourier inversion together with
    denoising). So the flags are collected as a union over all-off, all-on and
    one-hot per boolean. A builder that refuses a set is skipped; the panel then
    errs towards offering a flag, never towards hiding one.
    """
    schema = SCHEMA_JOBS.get(job_type)
    if schema is None:
        return []
    base: Dict[str, object] = {}
    booleans: List[str] = []
    for field in schema['fields']:
        variable = field['variable']
        kind = field.get('kind')
        if kind == 'boolean':
            base[variable] = False
            booleans.append(variable)
        elif field.get('default') not in (None, ''):
            base[variable] = field['default']
        else:
            base[variable] = _PLACEHOLDER
    # The queue is handled by the run planner, not by the job's builder, so it
    # must stay off or the command would be built for a scheduler.
    base['do_queue'] = False
    base[OTHER_ARGS_VARIABLE] = ''
    sets = [dict(base)]
    everything_on = dict(base)
    for name in booleans:
        everything_on[name] = True
    sets.append(everything_on)
    for name in booleans:
        variant = dict(base)
        variant[name] = True
        sets.append(variant)
    return sets


def flags_the_form_sets(job_type: str) -> set:
    """The flags the job's own builder can emit, over all placeholder sets.

    These are the ones a form field already controls. Anything outside this set
    is either conditional on a combination no placeholder set produced, or
    genuinely hidden by the GUI - both belong in the advanced panel, so treating
    them as available is the safe direction.
    """
    from . import job_commands

    if job_type not in job_commands.COMMAND_SPECS:
        return set()
    flags = set()
    for options in _placeholder_sets(job_type):
        try:
            commands = job_commands.build_command(job_type, options, '/tmp/job/')
        except (ValueError, KeyError, TypeError):
            continue
        for command in commands:
            # A multi-program builder returns whole strings; a single-program
            # one returns tokens. shlex handles the quoting in both.
            try:
                tokens = shlex.split(command, posix=False)
            except ValueError:
                tokens = command.split()
            for token in tokens:
                token = token.strip('"')
                if token.startswith('-') and ' ' not in token:
                    flags.add(token)
    return flags


def options_for(job_type: str) -> Tuple[List[dict], Optional[str]]:
    """The advanced-panel entries for one job, and why there are none.

    Every entry carries ``used_by_form`` so the UI can show a flag as already
    handled without hiding it (an override is legitimate).
    """
    # A variant (or an API alias) has no table of its own: its branch runs one
    # of the base job's programs, so the base job's flag list applies.
    from . import job_commands

    entry = CLI_OPTIONS.get(job_type)
    if entry is None:
        base = job_commands.base_job_type(job_type)
        entry = CLI_OPTIONS.get(base)
    if entry is None:
        return [], None
    if entry.get('unavailable'):
        return [], entry['unavailable']
    handled = flags_the_form_sets(job_type)
    options = []
    for option in entry['options']:
        item = dict(option)
        item['used_by_form'] = option['flag'] in handled
        options.append(item)
    return options, None


def parse_extra(value) -> List[dict]:
    """Normalise the request's ``cli_extra`` into ``{flag, value}`` rows.

    Accepts the structured form the UI sends, and a plain string (a CLI user
    pasting ``--foo bar``), so the API stays usable without the browser. In the
    string form a token starting with ``-`` opens a row and a following token
    that is not a flag becomes its value; a lone value token is kept so
    ``malformed`` can report it.
    """
    if value is None or value == '':
        return []
    if isinstance(value, str):
        rows: List[dict] = []
        for token in shlex.split(value):
            if token.startswith('-') or not rows or rows[-1]['value'] is not None:
                rows.append({'flag': token, 'value': None})
            else:
                rows[-1]['value'] = token
        return rows
    if isinstance(value, dict):
        value = [value]
    rows = []
    for item in value:
        if isinstance(item, str):
            rows.extend(parse_extra(item))
            continue
        if not isinstance(item, dict):
            continue
        flag = str(item.get('flag') or '').strip()
        if not flag:
            continue
        raw = item.get('value')
        rows.append(
            {
                'flag': flag,
                'value': None if raw is None or raw == '' else str(raw),
            }
        )
    return rows


def render_tokens(rows: Iterable[dict]) -> List[str]:
    """The argv tokens for parsed extra rows.

    A boolean flag is emitted alone; anything else is emitted as
    ``flag value``. A value containing whitespace is quoted so the argument
    reaches the program as one token.
    """
    tokens: List[str] = []
    for row in rows:
        flag = row['flag']
        value = row.get('value')
        if value is None:
            tokens.append(flag)
            continue
        tokens.append(flag)
        tokens.append(shlex.quote(value) if ' ' in value else value)
    return tokens


def render_text(rows: Iterable[dict]) -> str:
    """The rows as one command-line fragment, for ``other_args`` and job.star."""
    return ' '.join(render_tokens(rows))


def unknown_flags(job_type: str, rows: Iterable[dict]) -> List[str]:
    """Added flags the program does not declare.

    ``IOParser::checkForUnknownArguments`` only writes a warning to the job log,
    so a typo would otherwise look like it took effect. Reporting it before the
    job starts is the only place it can still be corrected cheaply.
    """
    entry = CLI_OPTIONS.get(job_type)
    if entry is None or entry.get('unavailable'):
        return []
    known = {option['flag'] for option in entry['options']}
    return [
        row['flag']
        for row in rows
        if row['flag'].startswith('-') and row['flag'] not in known
    ]


def malformed(rows: Iterable[dict]) -> List[str]:
    """Rows whose flag is not a flag.

    A token that does not start with ``-`` would be passed as a positional
    argument, which no RELION program reads, and a shell metacharacter there
    would change the command's meaning rather than configure it.
    """
    return [
        row['flag']
        for row in rows
        if not row['flag'].startswith('-')
    ]


def apply(job_type: str, arguments: Dict[str, object]) -> Tuple[Dict[str, object], List[str]]:
    """Fold ``cli_extra`` into ``other_args``.

    RELION already has exactly one channel for hand-written arguments
    (``other_args``, appended by every builder at the position its program
    expects), so the advanced panel writes into that channel rather than adding
    a second one. The returned dict is a copy; ``unknown`` lists flags the
    program does not declare.
    """
    rows = parse_extra(arguments.get(CLI_EXTRA_VARIABLE))
    if not rows:
        cleaned = dict(arguments)
        cleaned.pop(CLI_EXTRA_VARIABLE, None)
        return cleaned, []

    bad = malformed(rows)
    if bad:
        raise ValueError(
            'advanced arguments must each start with "-": ' + ', '.join(bad)
        )
    unknown = unknown_flags(job_type, rows)

    extra = render_text(rows)
    existing = str(arguments.get(OTHER_ARGS_VARIABLE) or '').strip()
    merged = f'{existing} {extra}'.strip() if existing else extra

    cleaned = dict(arguments)
    cleaned.pop(CLI_EXTRA_VARIABLE, None)
    cleaned[OTHER_ARGS_VARIABLE] = merged
    return cleaned, unknown
