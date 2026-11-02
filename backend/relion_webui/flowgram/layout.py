# SPDX-FileCopyrightText: 2026 elemeng
# SPDX-License-Identifier: GPL-2.0-or-later
"""Lay a RELION pipeline out as the FlowGram canvas draws it.

A direct port of ``frontend/src/lib/layout.ts``, which the main branch's canvas
uses. It is duplicated rather than shared because the two UIs are separate
apps with separate build pipelines, and the algorithm is pure geometry with no
dependencies - the risk of drift is a comment in both files, not a silent
behaviour change.

RELION's pipeline is a DAG *except* for refinement, which loops
(``Refine -> CTF refine -> Refine``). Longest-path ranking diverges on a cycle -
measured on a 13-job cryo-ET pipeline it produced 26 columns for 15 jobs - so
the graph is condensed into strongly connected components first, ranked as a
DAG, and each component is drawn as one node.

The result is deliberately simple: fixed-width columns, so edges have a
predictable run, and the whole graph fits one world coordinate space.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Sequence, Set, Tuple

NODE_WIDTH = 250
NODE_HEIGHT = 94
COLUMN_GAP = 48
ROW_GAP = 17
PADDING = 29


@dataclass
class LayoutNode:
    """One box on the canvas. More than one process means a condensed cycle."""

    processes: List[str]
    primary: str
    x: float
    y: float
    width: float
    height: float


@dataclass
class LayoutEdge:
    source: str
    target: str
    path: str


@dataclass
class Layout:
    nodes: List[LayoutNode] = field(default_factory=list)
    edges: List[LayoutEdge] = field(default_factory=list)
    width: float = 0
    height: float = 0
    #: Process name -> the node it was placed in.
    node_of: Dict[str, LayoutNode] = field(default_factory=dict)


def process_edges(graph: Mapping[str, object]) -> List[Tuple[str, str]]:
    """Collapse the edge list into process -> process dependencies.

    ``graph['edges']`` carries ``outputs`` (process -> file node) and ``inputs``
    (file node -> process). A node with no producer is external input, not an
    edge, so it is skipped.
    """
    edges = graph['edges']  # type: ignore[index]
    # The agent returns these as ``{'process': .., 'node': ..}`` dicts (it is
    # FastAPI's JSON of the pipeline's edge tables), not as tuples.
    producer_by_node = {}
    for edge in edges['outputs']:  # type: ignore[index]
        producer_by_node[edge['node']] = edge['process']
    seen: Set[Tuple[str, str]] = set()
    out: List[Tuple[str, str]] = []
    for edge in edges['inputs']:  # type: ignore[index]
        node, process = edge['node'], edge['process']
        producer = producer_by_node.get(node)
        if not producer or producer == process:
            continue
        key = (producer, process)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def strong_components(names: Sequence[str], successors: Mapping[str, Sequence[str]]) -> List[List[str]]:
    """Tarjan's algorithm, written with an explicit stack.

    A real pipeline can be deep enough to overflow a recursive implementation,
    and a layout failure must not blank the canvas.
    """
    index: Dict[str, int] = {}
    low: Dict[str, int] = {}
    on_stack: Set[str] = set()
    stack: List[str] = []
    components: List[List[str]] = []
    counter = 0

    for root in names:
        if root in index:
            continue
        work: List[dict] = [{'node': root, 'next': 0}]
        while work:
            frame = work[-1]
            v = frame['node']

            if frame['next'] == 0:
                index[v] = counter
                low[v] = counter
                counter += 1
                stack.append(v)
                on_stack.add(v)

            children = successors.get(v, ())
            if frame['next'] < len(children):
                w = children[frame['next']]
                frame['next'] += 1
                if w not in index:
                    work.append({'node': w, 'next': 0})
                elif w in on_stack:
                    low[v] = min(low[v], index[w])
                continue

            work.pop()
            if work:
                parent = work[-1]['node']
                low[parent] = min(low[parent], low[v])
            if low[v] == index[v]:
                members: List[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    members.append(w)
                    if w == v:
                        break
                components.append(members)
    return components


def layout_pipeline(graph: Mapping[str, object]) -> Layout:
    """Place every process, condensing cycles into single nodes."""
    names = [process['name'] for process in graph['processes']]  # type: ignore[index]
    edges = process_edges(graph)

    successors: Dict[str, List[str]] = {name: [] for name in names}
    for source, target in edges:
        successors.get(source, []).append(target)

    # 1. Condense cycles, so refinement loops become one node.
    components = strong_components(names, successors)
    component_of = {
        name: position
        for position, members in enumerate(components)
        for name in members
    }

    predecessors: Dict[int, Set[int]] = {i: set() for i in range(len(components))}
    for source, target in edges:
        a = component_of[source]
        b = component_of[target]
        if a != b:
            predecessors[b].add(a)

    # 2. Longest-path rank on the condensation: a job sits one column right of
    #    its deepest dependency, which is what makes the flow read left-to-right.
    rank = [0] * len(components)
    changed = True
    passes = 0
    while changed and passes <= len(components):
        changed = False
        passes += 1
        for i in range(len(components)):
            nxt = 0
            for predecessor in predecessors[i]:
                nxt = max(nxt, rank[predecessor] + 1)
            if nxt != rank[i]:
                rank[i] = nxt
                changed = True

    # 3. Order within a column by the mean column of its predecessors, which
    #    keeps fan-out readable without a full crossing-minimisation pass.
    columns: Dict[int, List[int]] = {}
    for i in range(len(components)):
        columns.setdefault(rank[i], []).append(i)
    rank_order = sorted(columns)
    column_of_rank = {value: position for position, value in enumerate(rank_order)}

    def barycentre(i: int) -> float:
        preds = list(predecessors[i])
        if not preds:
            return 0.0
        return sum(column_of_rank[rank[p]] for p in preds) / len(preds)

    for key in rank_order:
        columns[key].sort(key=lambda i: (barycentre(i), i))

    # 4. Place: fixed column width, rows centred vertically in their column.
    tallest = max((len(members) for members in columns.values()), default=1)
    width = (
        PADDING * 2
        + max(1, len(rank_order)) * NODE_WIDTH
        + max(0, len(rank_order) - 1) * COLUMN_GAP
    )
    height = max(320, PADDING * 2 + tallest * NODE_HEIGHT + (tallest - 1) * ROW_GAP)

    layout = Layout(width=width, height=height)
    for column_index, rank_key in enumerate(rank_order):
        members = columns[rank_key]
        total = len(members) * NODE_HEIGHT + (len(members) - 1) * ROW_GAP
        y = max(PADDING, (height - total) / 2)
        for component in members:
            processes = sorted(components[component])
            # Label a condensed loop with the job that closes it (the last one
            # started), which is the one the user last touched.
            node = LayoutNode(
                processes=processes,
                primary=processes[-1],
                x=PADDING + column_index * (NODE_WIDTH + COLUMN_GAP),
                y=y,
                width=NODE_WIDTH,
                height=NODE_HEIGHT,
            )
            layout.nodes.append(node)
            for name in processes:
                layout.node_of[name] = node
            y += NODE_HEIGHT + ROW_GAP

    # 5. Edges, de-duplicated after condensation and drawn as horizontal-tangent
    #    curves so they leave and enter nodes parallel to the reading direction.
    drawn: Set[Tuple[str, str]] = set()
    for source, target in edges:
        a = layout.node_of.get(source)
        b = layout.node_of.get(target)
        if a is None or b is None or a is b:
            continue
        key = (a.primary, b.primary)
        if key in drawn:
            continue
        drawn.add(key)

        x1, y1 = a.x + a.width, a.y + a.height / 2
        x2, y2 = b.x, b.y + b.height / 2
        mid = (x1 + x2) / 2
        layout.edges.append(
            LayoutEdge(
                source=a.primary,
                target=b.primary,
                path=f'M{x1:g} {y1:g} C{mid:g} {y1:g}, {mid:g} {y2:g}, {x2:g} {y2:g}',
            )
        )
    return layout
