/**
 * Layout invariants.
 *
 * `layoutPipeline` decides the graph's shape, and a wrong rank does not throw -
 * it silently produces a 26-column graph for a 15-job pipeline. These checks
 * run it on real cryo-ET shapes, including the refinement loop that breaks a
 * naive longest-path rank.
 *
 *   node --experimental-strip-types src/lib/layout.test.ts
 */
import assert from "node:assert/strict";

import {
  MIN_READABLE_SCALE,
  fitScale,
  layoutPipeline,
  naturalFitScale,
} from "./layout.ts";

let passed = 0;
function test(name: string, body: () => void) {
  try {
    body();
    passed += 1;
    console.log(`  ok   ${name}`);
  } catch (error) {
    console.error(`  FAIL ${name}`);
    console.error(`       ${(error as Error).message}`);
    process.exitCode = 1;
  }
}

/** A graph in the shape the agent's /api/project/graph returns. */
function graph(
  processes: string[],
  edges: [string, string][],
  labels: Record<string, string> = {},
) {
  const produced = new Map<string, string>();
  const inputs: { node: string; process: string }[] = [];
  const outputs: { process: string; node: string }[] = [];
  const nodes: { name: string; type_label: string }[] = [];

  for (const process of processes) {
    const node = `${process}/out.star`;
    produced.set(node, process);
    outputs.push({ process, node });
    nodes.push({ name: node, type_label: labels[process] ?? "relion.out" });
  }
  for (const [from, to] of edges) {
    const node = `${from}/out.star`;
    inputs.push({ node, process: to });
  }
  return {
    project_directory: "/tmp/x",
    job_counter: processes.length + 1,
    processes: processes.map((name, index) => ({
      name,
      alias: "None",
      type_label: labels[name] ?? "relion.out",
      status: "Succeeded",
      job_number: index + 1,
      directory: name.split("/")[0],
    })),
    nodes: [
      ...nodes,
      ...processes.map((process) => ({
        name: `${process}/in.star`,
        type_label: "relion.in",
      })),
    ],
    edges: { inputs, outputs },
  };
}

test("a chain lays out in one column per job", () => {
  const g = graph(
    ["A/job001", "B/job002", "C/job003"],
    [
      ["A/job001", "B/job002"],
      ["B/job002", "C/job003"],
    ],
  );
  const layout = layoutPipeline(g);
  assert.equal(layout.nodes.length, 3);
  const xs = layout.nodes.map((node) => node.x).sort((a, b) => a - b);
  assert.equal(new Set(xs).size, 3, "three distinct columns");
});

test("a fan-out shares one column", () => {
  const g = graph(
    ["A/job001", "B/job002", "C/job003"],
    [
      ["A/job001", "B/job002"],
      ["A/job001", "C/job003"],
    ],
  );
  const layout = layoutPipeline(g);
  const b = layout.nodeOf.get("B/job002")!;
  const c = layout.nodeOf.get("C/job003")!;
  assert.equal(b.x, c.x, "siblings share a column");
  assert.notEqual(b.y, c.y, "and do not overlap");
});

test("a refinement loop condenses to one node", () => {
  // Refine -> CTF refine -> Refine is the normal cryo-ET iteration, and it
  // makes longest-path ranking diverge without condensation.
  const g = graph(
    ["A/job001", "Refine/job002", "CtfRefine/job003", "Post/job004"],
    [
      ["A/job001", "Refine/job002"],
      ["Refine/job002", "CtfRefine/job003"],
      ["CtfRefine/job003", "Refine/job002"],
      ["Refine/job002", "Post/job004"],
    ],
  );
  const layout = layoutPipeline(g);
  const loop = layout.nodeOf.get("Refine/job002")!;
  assert.equal(
    layout.nodeOf.get("CtfRefine/job003"),
    loop,
    "the two jobs of the loop share a node",
  );
  assert.deepEqual(loop.processes.sort(), [
    "CtfRefine/job003",
    "Refine/job002",
  ]);
});

test("a long loop does not explode the column count", () => {
  const processes = ["A/job001"];
  const edges: [string, string][] = [];
  for (let i = 0; i < 8; i += 1) {
    processes.push(`R/job${String(i + 2).padStart(3, "0")}`);
  }
  for (let i = 0; i < processes.length - 1; i += 1) {
    edges.push([processes[i], processes[i + 1]]);
  }
  // Close a three-job loop at the end: R6 -> R7 -> R8 -> R6. Those three
  // condense into one node, so 9 jobs must yield 7 columns, not more.
  edges.push([processes[8], processes[6]]);
  const layout = layoutPipeline(graph(processes, edges));
  const columns = new Set(layout.nodes.map((node) => node.x));
  assert.equal(
    columns.size,
    7,
    `expected 7 columns for 9 jobs with a 3-job loop, got ${columns.size}`,
  );
});

test("edges never enter their own node", () => {
  const g = graph(
    ["A/job001", "R/job002", "C/job003"],
    [
      ["A/job001", "R/job002"],
      ["R/job002", "C/job003"],
      ["C/job003", "R/job002"],
    ],
  );
  const layout = layoutPipeline(g);
  for (const edge of layout.edges) {
    assert.notEqual(edge.from, edge.to, "no self edge");
  }
});

test("a node with no producer is not an edge", () => {
  const g = graph(["A/job001"], []);
  g.edges.inputs.push({ node: "/external/data.star", process: "A/job001" });
  const layout = layoutPipeline(g);
  assert.equal(layout.edges.length, 0);
});

test("fit never shrinks below the readable floor", () => {
  const scale = fitScale(4000, 900, 1000, 700);
  assert.equal(scale, MIN_READABLE_SCALE);
});

test("fit is capped at 100%", () => {
  assert.equal(fitScale(200, 200, 1200, 800), 1);
});

test("natural fit reports the real fit, not the floor", () => {
  // A ten-job pipeline on a laptop: fits at ~76%, which is above the readable
  // floor, so the canvas should open showing the whole graph.
  assert.ok(Math.abs(naturalFitScale(1500, 400, 1140, 700) - 0.76) < 0.01);
  // The same graph on a phone would be 50% - below the floor, so the canvas
  // opens at 100% on the newest job instead of showing chips.
  assert.ok(naturalFitScale(1500, 400, 750, 700) < MIN_READABLE_SCALE);
});

test("natural fit is capped at 100%, like fit", () => {
  assert.equal(naturalFitScale(200, 200, 1200, 800), 1);
});

test("an empty pipeline returns an empty layout", () => {
  const layout = layoutPipeline(graph([], []));
  assert.equal(layout.nodes.length, 0);
  assert.equal(layout.edges.length, 0);
});

test("duplicate edges between two jobs are drawn once", () => {
  const g = graph(
    ["A/job001", "B/job002"],
    [
      ["A/job001", "B/job002"],
      ["A/job001", "B/job002"],
    ],
  );
  assert.equal(layoutPipeline(g).edges.length, 1);
});

console.log(`\n${passed} layout checks passed`);
