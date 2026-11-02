# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""RELION job options as FlowGram form definitions.

The node inspector renders a form from a table of *data*: which field, which
editor, what choices. That table used to be built in TypeScript
(``flowgram-app/src/lib/relion-job-definitions.ts``), which meant the mapping
from RELION's option kinds to FlowGram's editors existed twice - once here for
the projection's default values, once there for the form. It lives here now, so
the agent is the only thing that knows what a RELION option looks like.

The mapping:

    RELION kind    FlowGram editor   why
    ------------   ---------------   ------------------------------------
    boolean        value(boolean)    typed constant row; the value editor's
                                     boolean mode is the stock control
    radio          select            choices map 1:1
    slider         number            min/max exist on the field
    path           path-input        monospace path + filesystem browser
    input_node     node-input        the project's .Nodes picker
    string         value(string)     plain text; multiline when help says so

Fields are grouped into sections from RELION's own job-window tabs, with the
resource tab (MPI, threads, queue) renamed and forced last so a form reads
I/O -> algorithm -> resources.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

RESOURCE_TAB = 'Run'
RESOURCE_SECTION = '计算资源'
COMPUTE_TAB = 'Compute'
IO_TAB = 'I/O'

#: A flag list longer than this is not worth a tip; it is in `other_args`.
MULTILINE_HINTS = ('comma', 'separated', 'one per line')


def _clean_label(label: str) -> str:
    """RELION labels carry a trailing colon; FlowGram's form adds its own."""
    return label.rstrip().removesuffix(':').strip()


def _choices(field: Mapping[str, Any]) -> Optional[List[List[str]]]:
    """``options`` arrives as ``{value,label}`` objects or as bare strings."""
    options = field.get('options') or []
    if not options:
        return None
    pairs: List[List[str]] = []
    for option in options:
        if isinstance(option, str):
            pairs.append([option, option])
        else:
            value = str(option.get('value') or option.get('label') or '')
            pairs.append([value, str(option.get('label') or value)])
    return pairs


def _looks_multiline(field: Mapping[str, Any]) -> bool:
    """Long free text gets a multiline editor.

    RELION's help text is the only signal available, and a path or a regex is
    never that.
    """
    help_text = str(field.get('help') or '').lower()
    return any(hint in help_text for hint in MULTILINE_HINTS)


def field_definition(field: Mapping[str, Any], required: bool) -> Dict[str, Any]:
    """One RELION option as a FlowGram field definition."""
    help_text = str(field.get('help') or '').strip() or None
    variable = str(field['variable'])
    base: Dict[str, Any] = {
        # RELION options live under `data.inputsValues`, the same place
        # FlowGram's own nodes keep theirs. A bare key would make the form
        # write `data.<variable>` while the canvas reads
        # `data.inputsValues.<variable>` - an edit would silently do nothing.
        'key': f'inputsValues.{variable}',
        'variable': variable,
        'label': _clean_label(str(field.get('label') or variable)),
        # RELION's help is a paragraph per option; inline it buries the value,
        # so it travels as `tip` (hover next to the label).
        'tip': help_text,
        'section': field.get('section'),
        'defaultValue': field.get('default'),
    }
    if required:
        base['required'] = True

    kind = field.get('kind')
    if kind == 'boolean':
        return {**base, 'editor': 'value', 'type': 'boolean'}

    if kind == 'radio':
        choices = _choices(field)
        # Without choices a select renders empty; fall back to text so the
        # field stays editable rather than silently broken.
        if choices:
            return {**base, 'editor': 'select', 'choices': choices}
        return {**base, 'editor': 'value', 'type': 'string'}

    if kind == 'slider':
        definition = {**base, 'editor': 'number', 'type': 'number'}
        if isinstance(field.get('min'), (int, float)):
            definition['min'] = field['min']
        if isinstance(field.get('max'), (int, float)):
            definition['max'] = field['max']
        return definition

    if kind == 'input_node':
        # A slot picker over the project's .Nodes, typed by node_type.
        return {
            **base,
            'editor': 'node-input',
            'nodeType': field.get('node_type'),
            'placeholder': field.get('pattern'),
        }

    if kind == 'path':
        return {**base, 'editor': 'path-input', 'placeholder': field.get('pattern')}

    definition = {**base, 'editor': 'value', 'type': 'string'}
    if _looks_multiline(field):
        definition['multiline'] = True
    return definition


def sections(job: Mapping[str, Any]) -> tuple[Dict[str, str], List[str]]:
    """Which section each variable belongs to, and the reading order.

    The schema already groups every variable under a tab ("I/O", the
    algorithm's tab, "Run"). The order is fixed here rather than inherited from
    the C++ table: I/O first, the algorithm tabs in their own order, RELION's
    Compute tab second-to-last, and the resource tab last.
    """
    section_of: Dict[str, str] = {}
    io: List[str] = []
    middle: List[str] = []
    compute: List[str] = []
    has_resources = False
    for tab in job.get('tabs') or []:
        title = tab.get('title')
        if title == RESOURCE_TAB:
            title = RESOURCE_SECTION
        elif title == COMPUTE_TAB:
            title = '计算'
        for variable in tab.get('fields') or []:
            section_of.setdefault(str(variable), str(title))
        if title == RESOURCE_SECTION:
            has_resources = True
        elif tab.get('title') == IO_TAB:
            if title not in io:
                io.append(str(title))
        elif tab.get('title') == COMPUTE_TAB:
            if title not in compute:
                compute.append(str(title))
        elif str(title) not in middle:
            middle.append(str(title))
    order = [*io, *middle, *compute]
    if has_resources:
        order.append(RESOURCE_SECTION)
    return section_of, order


def advanced_tip(job: Mapping[str, Any]) -> Optional[str]:
    """The flags this form does not cover, for the `other_args` tip.

    The Run tab's other_args line is RELION's advanced panel. Naming the flags
    the form does not already expose - with their usage - turns it from a free
    box into a discoverable one.
    """
    flags = [
        f"{option['flag']}  {option['usage']}" if option.get('usage') else str(option['flag'])
        for option in job.get('cli_options') or []
        if not option.get('hidden') and not option.get('used_by_form')
    ]
    if not flags:
        return None
    return '除表单外的可用参数（写入 other_args）：\n  ' + '\n  '.join(flags)


def node_definition(job: Mapping[str, Any]) -> Dict[str, Any]:
    """One job type as a node definition the inspector can register."""
    required = set(job.get('required') or [])
    section_of, order = sections(job)
    rank = {title: index for index, title in enumerate(order)}
    extra = advanced_tip(job)

    fields = []
    for index, field in enumerate(job.get('fields') or []):
        definition = field_definition(field, str(field['variable']) in required)
        variable = definition['variable']
        section = section_of.get(variable)
        fields.append(
            (
                -1 if section is None else rank.get(section, len(order)),
                index,
                definition,
                section,
            )
        )
    fields.sort(key=lambda item: (item[0], item[1]))

    result = []
    for _, _, definition, section in fields:
        if definition['variable'] == 'other_args' and extra:
            definition['tip'] = '\n\n'.join(
                part for part in (definition.get('tip'), extra) if part
            )
        definition['section'] = section
        result.append(definition)

    return {
        'fields': result,
        # A RELION job's outputs are pipeline nodes, not a value this node
        # computes, so there is nothing honest to declare here.
        'aliases': [str(job.get('title') or job['job_type']), str(job['job_type'])],
    }


def definitions_for(schemas: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Every job type's form definition, keyed by node type id."""
    return {
        f"relion.{schema['job_type']}": node_definition(schema) for schema in schemas
    }
