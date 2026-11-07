"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const ui = require("./app.js");

test("viewer accepts only configured HTTP loopback URLs compatible with supervisor CSP", () => {
  assert.equal(ui.safeViewerUrl("http://127.0.0.1:3007/"), "http://127.0.0.1:3007/");
  assert.equal(ui.safeViewerUrl("http://127.0.0.1:3010/view?agent=one"), "http://127.0.0.1:3010/view?agent=one");
  for (const url of ["https://example.com", "http://192.168.1.1:3007", "javascript:alert(1)", "data:text/html,<script>alert(1)</script>", "//127.0.0.1:3007", "http://127.0.0.1.evil.test", "http://user:secret@127.0.0.1:3007", "http://127.0.0.1@evil.test", "https://127.0.0.1:3007", "", null]) {
    assert.equal(ui.safeViewerUrl(url), null, String(url));
  }
});

test("unknown vital measurements stay unknown while actual zero remains zero", () => {
  for (const missing of [undefined, null, NaN, Infinity, "", "20", false]) assert.equal(ui.vitalValue(missing), "—");
  assert.equal(ui.vitalValue(0), "0");
  assert.equal(ui.vitalValue(17.5), "17.5");
  assert.equal(ui.formatPosition({ x: 0, y: 64, z: -10.25 }), "0.0 / 64.0 / -10.3");
  assert.equal(ui.formatPosition([0, null, "5"]), "0.0 / — / —");
  assert.equal(ui.formatPosition(null), "— / — / —");
});

test("death and disconnection override misleading run state", () => {
  assert.equal(ui.agentStatus({ observation: { connected: false }, run: { active: true } }), "Disconnected");
  assert.equal(ui.agentStatus({ observation: { connected: true, alive: false }, run: { active: true } }), "Dead");
  assert.equal(ui.agentStatus({ observation: { connected: true, alive: true }, run: { active: true }, active_role: "buddhi" }), "Buddhi thinking");
  assert.equal(ui.agentStatus({ observation: { connected: true }, run: { status: "stopped" } }), "Stopped");
});

test("action intent and unknown results never become invented success", () => {
  assert.equal(ui.outcomeRecord({ status: "running", skill: "navigate" }).details, "Outcome pending.");
  const unknown = ui.outcomeRecord({ status: "outcome_unknown", skill: "dig", actor: "agent" });
  assert.equal(unknown.status, "outcome_unknown");
  assert.equal(unknown.details, "Outcome not reported.");
  const failed = ui.outcomeRecord({ status: "failed", parameters: { skill: "navigate" }, result: { reason: "No path." } });
  assert.equal(failed.title, "Navigate");
  assert.equal(failed.details, "No path.");
  assert.equal(ui.outcomeRecord({ result: false }).details, "false");
});

test("configured agent records remain distinct, bounded, and selectable by stable IDs", () => {
  const agents = [{ id: "one", name: "<img src=x onerror=alert(1)>" }, { id: "one", name: "duplicate" }, null, { name: "no ID" }, { id: "two" }];
  assert.deepEqual(ui.agentList(agents), [agents[0], agents[4]]);
  assert.equal(ui.agentList(Array.from({ length: 100 }, (_, id) => ({ id: String(id) }))).length, 32);
  assert.deepEqual(ui.agentList({ one: {} }), []);
});

test("inventory distinguishes absent measurements from observed empty inventory", () => {
  assert.equal(ui.inventorySummary(undefined), "Inventory not reported");
  assert.equal(ui.inventorySummary([]), "Inventory empty");
  assert.equal(ui.inventorySummary([{ name: "oak_log", count: 0 }, { displayName: "Apple", count: 2 }]), "oak_log ×0 · Apple ×2");
});

test("operator, model, and game messages preserve literal untrusted content", () => {
  const attack = '<img src=x onerror="alert(1)">';
  const messages = ui.messageRecords({
    recent_events: [{ id: "u1", kind: "user", content: attack }, { id: "a1", kind: "assistant", content: "I will look." }, { kind: "private_snapshot", content: "not a message" }],
    observation: { chat: [{ username: "<script>", message: attack }] }
  });
  assert.equal(messages.length, 3);
  assert.equal(messages[0].label, "Operator → mind");
  assert.equal(messages[0].content, attack);
  assert.equal(messages[2].label, "Minecraft chat · <script>");
  assert.equal(messages[2].content, attack);
});

test("dashboard has no remote assets, active HTML sinks, or invented demo telemetry", () => {
  const source = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
  const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
  assert.doesNotMatch(source, /\.innerHTML\s*=|insertAdjacentHTML|document\.write\s*\(/);
  assert.doesNotMatch(html, /<(?:script|link)[^>]+(?:src|href)=["']https?:/i);
  assert.match(source, /api\("\/api\/fishbowl"\)/);
  assert.match(html, /No telemetry received yet/);
  assert.match(html, /No viewer connected/);
});
