"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const ui = require("../ui/app.js");

class FakeNode {
  constructor(tag, content) { this.tagName = tag; this.children = []; this._text = content || ""; }
  appendChild(child) { this.children.push(child); return child; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
}
const doc = {
  createElement: tag => new FakeNode(tag),
  createTextNode: content => new FakeNode("#text", content)
};
const fence = String.fromCharCode(96).repeat(3);

test("message text, including HTML around code fences, never creates active elements", () => {
  const attack = '<img src=x onerror="alert(1)">\n' + fence + 'python\nprint("<script>")\n' + fence + '\n<svg onload="alert(2)"></svg>';
  const container = new FakeNode("div");
  ui.renderText(container, attack, doc);
  function tags(node) { return [node.tagName, ...node.children.flatMap(tags)]; }
  assert.deepEqual(tags(container), ["div", "#text", "pre", "span", "code", "#text"]);
  assert.ok(container.textContent.includes('<img src=x onerror="alert(1)">'));
  assert.ok(container.textContent.includes('print("<script>")'));
  assert.ok(container.textContent.includes('<svg onload="alert(2)"></svg>'));
});

test("unclosed code fences remain inert, intact text", () => {
  const input = "Example:\n" + fence + "python\n<script>alert(1)</script>";
  assert.deepEqual(ui.tokenizeCode(input), [{ type: "text", content: input }]);
});

test("multiple code blocks preserve surrounding prose", () => {
  const blocks = ui.tokenizeCode("A\n" + fence + "python\nx = 1\n" + fence + "\nB\n" + fence + "json\n{}\n" + fence);
  assert.deepEqual(blocks.map(block => block.type), ["text", "code", "text", "code"]);
  assert.equal(blocks[1].content, "x = 1");
  assert.equal(blocks[3].language, "json");
  assert.equal(blocks[3].content, "{}");
});

test("durable event updates replace queued states without duplicate messages", () => {
  const old = { id: "1", kind: "user", status: "pending", created_at: "2026-01-01T00:00:00Z" };
  const updated = { ...old, status: "complete" };
  const reply = { id: "2", kind: "assistant", created_at: "2026-01-01T00:01:00Z" };
  const merged = ui.mergeEvents([old], [reply, updated, { content: "no identifier" }]);
  assert.equal(merged.length, 2);
  assert.equal(merged[0].status, "complete");
  assert.equal(merged[1].id, "2");
});

test("faculty activity reflects actual runtime role and completion", () => {
  assert.equal(ui.facultyStatus("manas", { status: "thinking", active_role: "manas" }, false), "active");
  assert.equal(ui.facultyStatus("chitta", { status: "thinking", active_role: "manas" }, true), "complete");
  assert.equal(ui.facultyStatus("manas", { status: "paused", active_role: "manas" }, false), "waiting");
});

test("unexpected event kinds cannot become arbitrary CSS classes", () => {
  assert.equal(ui.messageKind('user" onclick="evil()'), "system");
  assert.equal(ui.messageKind("assistant"), "assistant");
});

test("evidence references are unique, valid strings from recorded metadata", () => {
  assert.deepEqual(ui.extractEvidence({ metadata: { evidence_ids: ["a", "a", "", 1, null, "b"] } }), ["a", "b"]);
  assert.deepEqual(ui.extractEvidence({ metadata: { evidence_ids: "not-an-array" } }), []);
  assert.deepEqual(ui.extractEvidence({ metadata: { decision: { evidence_ids: ["m1"] } } }), ["m1"]);
  assert.deepEqual(ui.extractEvidence({}), []);
});

test("missing metrics stay unknown instead of becoming fabricated durations", () => {
  assert.equal(ui.formatDuration(undefined), "—");
  assert.equal(ui.formatDuration("bad"), "—");
  assert.equal(ui.formatDuration(0), "—");
  assert.equal(ui.formatDuration(250), "250 ms");
  assert.equal(ui.formatDuration(1250), "1.3 s");
});

test("model names support both role mappings and direct names", () => {
  assert.equal(ui.modelName({ model: "local:8b" }), "local:8b");
  assert.equal(ui.modelName("local:4b"), "local:4b");
  assert.equal(ui.modelName(null, "fallback"), "fallback");
});


test("reply evidence uses the final Buddhi trace from the real durable event contract", () => {
  const event = { metadata: { memory_ids: ["new-memory"], trace: [
    { role: "manas", result: { observations: ["Something arrived"] } },
    { role: "buddhi", round: 1, result: { evidence_ids: ["earlier"] } },
    { role: "buddhi", round: 2, result: { evidence_ids: ["event-1", "memory-2", "event-1"] } }
  ] } };
  assert.deepEqual(ui.extractEvidence(event), ["event-1", "memory-2"]);
  assert.deepEqual(ui.extractEvidence({ metadata: { ...event.metadata, evidence_ids: [] } }), []);
});

const committedWorkspace = {
  manas: { observations: ["Earlier observation"] },
  chitta: { selected_memory_ids: ["old-memory"] },
  self_assessment: { agency: "Earlier self-assessment" },
  decision: { reply: "Earlier reply", evidence_ids: ["old-event"] }
};

test("a new cognitive cycle cannot present the previous committed faculties as complete", () => {
  const earlier = { cycle: 1, status: "idle", workspace: committedWorkspace };
  const thinking = { ...earlier, cycle: 2, status: "thinking" };
  const live = ui.snapshotFaculties({}, earlier, {});
  assert.deepEqual(ui.snapshotFaculties(earlier, thinking, live), {});
});

test("snapshots during the same live cycle preserve contributions actually received", () => {
  const thinking = { cycle: 2, status: "thinking", workspace: committedWorkspace };
  const live = { manas: { observations: ["Current observation"] } };
  assert.deepEqual(ui.snapshotFaculties(thinking, thinking, live), live);
  assert.deepEqual(ui.snapshotFaculties({}, thinking, {}), {});
});

test("reconnect restores the latest committed faculty results instead of stale browser results", () => {
  const oldState = { cycle: 1, status: "idle", workspace: committedWorkspace };
  const next = { cycle: 2, state_version: 2, status: "idle", workspace: {
    manas: { observations: ["New observation"] },
    chitta: { selected_memory_ids: ["new-memory"] },
    self_assessment: { agency: "New self-assessment" },
    decision: { reply: "New reply" }
  } };
  const result = ui.snapshotFaculties(oldState, next, { buddhi: committedWorkspace.decision });
  assert.equal(result.manas.observations[0], "New observation");
  assert.equal(result.chitta.selected_memory_ids[0], "new-memory");
  assert.equal(result.ahamkara.agency, "New self-assessment");
  assert.equal(result.buddhi.reply, "New reply");
});

test("rollback removes abandoned live results and restores committed evidence", () => {
  const during = { cycle: 2, status: "thinking" };
  const rolledBack = { cycle: 1, status: "paused", workspace: committedWorkspace };
  const partial = { manas: { observations: ["Uncommitted"] }, buddhi: { reply: "Uncommitted" } };
  const restored = ui.snapshotFaculties(during, rolledBack, partial);
  assert.equal(restored.manas.observations[0], "Earlier observation");
  assert.equal(restored.buddhi.reply, "Earlier reply");
  assert.deepEqual(ui.snapshotFaculties(during, { status: "idle", workspace: {} }, partial), {});
});

test("failed interactions retain the durable top-level error after reconnect", () => {
  assert.equal(ui.eventError({ error: "Ollama is unavailable.", metadata: {} }), "Ollama is unavailable.");
  assert.equal(ui.eventError({ metadata: { error: "Older error format." } }), "Older error format.");
  assert.equal(ui.eventError({ error: "", metadata: {} }), "");
});

test("message and reflection limits agree with the server before a request is sent", () => {
  assert.equal(ui.validateInput("  hello  ", false), "hello");
  assert.equal(ui.validateInput("x".repeat(8000), false).length, 8000);
  assert.throws(() => ui.validateInput("x".repeat(8001), false), /8000/);
  assert.throws(() => ui.validateInput("  ", false), /Enter a message/);
  assert.equal(ui.validateInput("x".repeat(2000), true).length, 2000);
  assert.throws(() => ui.validateInput("x".repeat(2001), true), /2000/);
  assert.equal(ui.validateInput("", true), "");
});

test("agent map never reads hidden evaluator objects or walls", () => {
  const observation = { width: 3, height: 2, pose: { position: [0, 0], heading: "east" },
    visible_cells: [{ position: [0, 0], kind: "floor" }, { position: [1, 0], kind: "floor" }],
    objects: [{ id: "key", name: "Key", position: [1, 0] }], inventory: [], energy: 90 };
  const view = { observation, evaluator_only: true, world: { width: 3, height: 2,
    pose: observation.pose, walls: [[2, 1]], objects: { cup: { name: "Secret cup", position: [2, 0] } } } };
  const agent = ui.normalizeWorld(view, false);
  assert.equal(agent.cells.length, 6);
  assert.equal(agent.cells.filter(cell => cell.known).length, 2);
  assert.equal(JSON.stringify(agent).includes("Secret cup"), false);
  assert.equal(agent.cells.find(cell => cell.x === 2 && cell.y === 1).kind, "unknown");
  const observer = ui.normalizeWorld(view, true);
  assert.equal(observer.cells.filter(cell => cell.known).length, 6);
  assert.equal(observer.cells.find(cell => cell.x === 2 && cell.y === 1).kind, "wall");
  assert.equal(observer.cells.find(cell => cell.x === 2 && cell.y === 0).objects[0].name, "Secret cup");
});

test("evaluator display requires the explicit evaluator marker and safely bounds geometry", () => {
  const view = { observation: { width: 1000000, height: -1, pose: { position: [999, 0], heading: "<svg>" },
    visible_cells: [{ position: [0, 0], kind: "script" }, { position: [-1, 0], kind: "wall" }],
    objects: [{ id: "hidden", position: [3, 3] }] },
    world: { objects: { secret: { name: "do not render", position: [0, 0] } } } };
  const result = ui.normalizeWorld(view, true);
  assert.equal(result.width, 9); assert.equal(result.height, 7);
  assert.equal(result.position, null); assert.equal(result.heading, "north");
  assert.equal(result.cells[0].kind, "floor");
  assert.equal(JSON.stringify(result).includes("do not render"), false);
  assert.equal(JSON.stringify(result).includes("hidden"), false);
});

test("world labels remain data and cannot change cell classes", () => {
  const attack = '<img src=x onerror="alert(1)">';
  const result = ui.normalizeWorld({ observation: { width: 1, height: 1,
    visible_cells: [{ position: [0, 0], kind: attack }],
    objects: [{ id: attack, name: attack, position: [0, 0] }] } }, false);
  assert.equal(result.cells[0].kind, "floor");
  assert.equal(result.cells[0].objects[0].name, attack);
  const node = new FakeNode("span");
  node.textContent = result.cells[0].objects[0].name;
  assert.equal(node.children.length, 0);
  assert.equal(node.textContent, attack);
});

test("action ledger reports explicit actors and failed or unknown outcomes", () => {
  assert.deepEqual(ui.actionSummary({ id: "a1", actor: "user", status: "failed",
    tool: "environment", parameters: { action: "move_forward" }, result: { reason: "Wall ahead." } }),
    { id: "a1", actor: "user", status: "failed", title: "Move forward", detail: "Wall ahead.", prediction: "" });
  assert.equal(ui.actionSummary({ tool: "calculator", status: "outcome_unknown" }).status, "outcome_unknown");
  assert.equal(ui.actionSummary({ tool: "calculator" }).actor, "unknown");
});

test("record display bounds preserve stable IDs from keyed belief maps", () => {
  assert.deepEqual(ui.boundedRecords({ key: { position: [2, 3] }, cup: { kind: "remembered" } }, 1),
    [{ id: "cup", kind: "remembered" }]);
  assert.deepEqual(ui.boundedRecords([null, "text", { id: "x" }], 5), [{ id: "x" }]);
});

test("manual targets use only public object knowledge", () => {
  const view = { observation: { objects: [{ id: "key", name: "Key" }], inventory: ["coin"] },
    model: { beliefs: { key: { name: "Key" }, book: { name: "Remembered book" } } },
    evaluator_only: true, world: { objects: { cup: { name: "Secret cup" } } } };
  assert.deepEqual(ui.worldTargets(view), [{ id: "key", name: "Key" }, { id: "book", name: "Remembered book" }, { id: "coin", name: "coin" }]);
  assert.deepEqual(ui.worldTargets({}), []);
});

test("manual movement and sensing actions ignore an object target left selected", () => {
  ["look", "turn_left", "turn_right", "move_forward", "wait"].forEach(action => assert.equal(ui.worldActionTarget(action, "key"), ""));
  ["inspect", "pick_up", "drop"].forEach(action => assert.equal(ui.worldActionTarget(action, "key"), "key"));
  assert.equal(ui.worldActionTarget("inspect", ""), "");
});
