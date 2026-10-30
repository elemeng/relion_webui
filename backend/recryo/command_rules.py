# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""Execute the command rules extracted from RELION's ``getCommands*``.

``generated_commands.py`` holds the rules; this module evaluates them. Keeping
the two apart matters because the generator rewrites its output wholesale, while
the evaluation logic is hand-written and must survive regeneration.
"""

from __future__ import annotations

from typing import Dict, List

from .generated_commands import COMMANDS


def _flag_value(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ('yes', 'true', '1')


def _present(value) -> bool:
    return value is not None and str(value).strip() != ''


def _condition_holds(condition: dict, options: dict, out_prefix: str) -> bool:
    """Evaluate one extracted condition against a concrete option set."""
    for test, argument in condition.items():
        if test == 'truthy':
            if not _flag_value(options.get(argument)):
                return False
        elif test == 'falsy':
            if _flag_value(options.get(argument)):
                return False
        elif test == 'set':
            if not _present(options.get(argument)):
                return False
        elif test == 'mpi_gt_one':
            try:
                if int(float(options.get(argument) or 1)) <= 1:
                    return False
            except (TypeError, ValueError):
                return False
        elif test == 'continue':
            if not options.get('is_continue'):
                return False
        elif test == 'not_continue':
            if options.get('is_continue'):
                return False
        elif test == 'is_tomo':
            # Every job in this front end is a tomography job.
            if bool(argument) is not True:
                return False
        elif test == 'all':
            if not all(
                _condition_holds(condition, options, out_prefix)
                for condition in argument
            ):
                return False
        else:
            return False
    return True


# The radio labels RELION defines; `gain_rot`'s value is the index of the chosen
# label in this list (getCommandsMotioncorrJob, pipeline_jobs.cpp:1615).
_GAIN_ROTATION = ['No rotation (0)', '90 degrees (1)', '180 degrees (2)', '270 degrees (3)']
_GAIN_FLIP = ['No flipping (0)', 'Flip upside down (1)', 'Flip left to right (2)']


def _radio_index(raw, choices) -> str:
    """The number RELION derives for a radio option.

    The C++ searches the option's label in the choice list and uses the index;
    the label itself already ends in "(n)", so both agree. Falling back to the
    trailing number keeps a hand-written value working.
    """
    text = str(raw).strip()
    if text in choices:
        return str(choices.index(text))
    if '(' in text and ')' in text:
        return text[text.rfind('(') + 1 : text.rfind(')')].strip()
    return text


def _choose_executable(names: List[str], options: dict) -> str:
    """Pick the serial or MPI name from the C++ if/else pair."""
    try:
        nr_mpi = int(float(options.get('nr_mpi') or 1))
    except (TypeError, ValueError):
        nr_mpi = 1
    return names[1] if nr_mpi > 1 and len(names) > 1 else names[0]


def _is_same_program(a: str | None, b: str) -> bool:
    """True when two executable names are the serial/MPI variants of one program."""
    if not a or not b:
        return False
    left = a[: -len('_mpi')] if a.endswith('_mpi') else a
    right = b[: -len('_mpi')] if b.endswith('_mpi') else b
    return left == right


def _render_value(raw, convert) -> str:
    """Render an option the way ``JobOption::getString()`` would.

    RELION stores every option as a string and ``getString()`` returns it
    unchanged, so a slider set to 10.0 is passed as "10.0", not "10". Values that
    arrive as Python floats are formatted the same way; ``convert == 'int'``
    marks the few statements that call ``integerToString`` explicitly.
    """
    if convert == 'radio_index':
        choices = _GAIN_ROTATION if 'gain_rot' in str(raw) else None
        # The caller passes the variable name; treat the value as its label.
        return _radio_index(raw, _GAIN_FLIP if raw in _GAIN_FLIP else _GAIN_ROTATION)
    if convert == 'int':
        try:
            return str(int(float(raw)))
        except (TypeError, ValueError):
            return str(raw)
    if isinstance(raw, bool):
        return '1' if raw else '0'
    if isinstance(raw, float) and raw == int(raw):
        # The GUI's sliders are floats, and job.star holds "10.0"; keep that.
        return f'{raw:.1f}'
    return str(raw)


def build(job_type: str, options: dict, out_prefix: str) -> List[str]:
    """Render one job's argv from the extracted rules.

    ``out_prefix`` is the job directory with a trailing slash, matching what
    RELION passes to ``getCommands`` as ``outputname``.
    """
    job = COMMANDS.get(job_type)
    if job is None:
        raise KeyError(job_type)

    commands: List[str] = []
    current: List[str] = []
    executable = None
    # A job can run several programs (multibody, select, modelangelo). A new
    # executable step starts a new command, so the pieces already collected are
    # flushed rather than overwritten.
    def flush():
        if current and any(part.strip() for part in current):
            commands.append(' '.join(current))
        current.clear()

    for step in job['steps']:
        if not all(
            _condition_holds(condition, options, out_prefix)
            for condition in step.get('when', ())
        ):
            continue

        if 'executable' in step:
            chosen = _choose_executable(step['executable'], options)
            if chosen == executable:
                # The serial and MPI names come from one if/else, so the second
                # of the pair must not start a new command.
                continue
            if current and _is_same_program(executable, chosen):
                # A new executable that is the MPI (or serial) twin of the one
                # running: keep the command already built.
                executable = chosen
                current[0] = chosen
                continue
            flush()
            executable = chosen
            current.append(chosen)
            continue

        if 'subcommand' in step:
            current.append(step['subcommand'])
            continue

        if 'literal' in step:
            current.append(step['literal'])
            continue

        prefix = step.get('literal_prefix', '')

        if step.get('output'):
            # `command += " --o " + outputname` - the job directory, optionally
            # followed by a derived name and/or a literal.
            value = prefix + out_prefix
            if 'literal_suffix_var' in step:
                value += str(options.get(step['literal_suffix_var'], ''))
            value += step.get('literal_suffix', '')
            current.append(value)
            continue

        if 'var' in step:
            raw = options.get(step['var'])
            if not _present(raw):
                continue
            rendered = _render_value(
                raw, step.get('derive') or step.get('convert')
            )
            current.append(prefix + rendered + step.get('literal_suffix', ''))
            continue

    flush()
    # A literal step carries its own surrounding spaces; joining with a space
    # keeps flags separated without doubling the padding.
    return [' '.join(command.split()) for command in commands]
