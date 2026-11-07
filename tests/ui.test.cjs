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
