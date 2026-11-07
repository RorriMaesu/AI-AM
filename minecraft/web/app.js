/* AI-AM Minecraft supervisor UI. Local telemetry only; no model text becomes HTML. */
(function (global) {
  "use strict";
  function text(value) {
    if (value === undefined || value === null) return "";
    if (typeof value === "string") return value;
    try { return JSON.stringify(value, null, 2); } catch (_) { return String(value); }
  }
  function humanize(value) { return String(value || "").replace(/[_-]+/g, " ").replace(/^\w/, function (letter) { return letter.toUpperCase(); }); }
  function finite(value) { return typeof value === "number" && Number.isFinite(value); }
  function safeViewerUrl(value) {
    if (typeof value !== "string" || value.length > 2048) return null;
    try {
      var url = new URL(value);
      if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || url.username || url.password) return null;
      return url.href;
    } catch (_) { return null; }
  }
  function agentList(value) {
    return (Array.isArray(value) ? value : []).filter(function (agent) {
      return agent && typeof agent === "object" && typeof agent.id === "string" && agent.id.length > 0 && agent.id.length <= 128;
    }).slice(0,32).filter(function (agent, index, all) { return all.findIndex(function (item) { return item.id === agent.id; }) === index; });
  }
  function records(value, maximum) {
    var source = Array.isArray(value) ? value : value && typeof value === "object" ? Object.keys(value).map(function (key) { return Object.assign({ id: key }, value[key] && typeof value[key] === "object" ? value[key] : { content: value[key] }); }) : [];
    return source.filter(function (item) { return item && typeof item === "object"; }).slice(-(maximum || 16));
  }
  function formatPosition(value) {
    var coordinates = Array.isArray(value) ? value : value && typeof value === "object" ? [value.x, value.y, value.z] : [];
    return [0,1,2].map(function (index) { return finite(coordinates[index]) ? coordinates[index].toFixed(1) : "—"; }).join(" / ");
  }
  function vitalValue(value) { return finite(value) ? String(Math.round(value * 10) / 10) : "—"; }
  function agentStatus(agent) {
    var observation = agent && agent.observation || {}, run = agent && agent.run || {};
    if (agent && agent.error) return "Error";
    if (observation.connected === false) return "Disconnected";
    if (observation.alive === false) return "Dead";
    if (run.active) return agent.active_role ? humanize(agent.active_role) + " thinking" : "Running";
    return humanize(run.status || agent && agent.status || (observation.connected === true ? "Connected" : "Unknown"));
  }
  function inventorySummary(value) {
    if (!Array.isArray(value)) return "Inventory not reported";
    if (!value.length) return "Inventory empty";
    return value.slice(0,12).map(function (item) {
      if (typeof item === "string") return item;
      return text(item && (item.displayName || item.name || item.type || item.id) || "Unknown item") +
        (item && finite(item.count) ? " ×" + item.count : "");
    }).join(" · ") + (value.length > 12 ? " · +" + (value.length - 12) + " more" : "");
  }
  function outcomeRecord(value) {
    var item = value || {}, result = item.result !== undefined ? item.result : item.outcome !== undefined ? item.outcome : item.reason !== undefined ? item.reason : typeof item.success === "boolean" ? (item.success ? "Reported success." : "Reported failure.") : undefined;
    var status = String(item.status || "unknown");
    var details;
    if (result !== undefined && result !== null) details = typeof result === "object" ? text(result.reason || result.message || result.summary || result) : text(result);
    else details = ["running","pending","queued","planned","requested"].indexOf(status) >= 0 ? "Outcome pending." : "Outcome not reported.";
    return { id: text(item.id), title: humanize(item.skill || item.name || item.parameters && (item.parameters.skill || item.parameters.action) || item.action || item.tool || "Action"),
      status: status, details: details, actor: text(item.actor || item.source || "unknown"), time: text(item.updated_at || item.created_at || item.time || "") };
  }
  function messageRecords(agent) {
    var eventMessages = records(agent && agent.recent_events, 40).filter(function (item) {
      return ["user","assistant","message","chat","minecraft_chat","operator_message","error","death"].indexOf(item.kind || item.type) >= 0 && (item.content !== undefined || item.text !== undefined || item.message !== undefined);
    }).map(function (item) { return { id: text(item.id), role: text(item.kind || item.type), label: text(item.kind === "user" || item.kind === "operator_message" ? "Operator → mind" : item.kind || item.type), content: text(item.content || item.text || item.message), time: text(item.created_at || item.time) }; });
    var gameChat = records(agent && agent.observation && agent.observation.chat, 20).map(function (item) {
      return { id: text(item.id), role: "chat", label: "Minecraft chat · " + text(item.username || item.sender || item.name || "unknown"), content: text(item.message || item.text || item.content), time: text(item.timestamp || item.time) };
    });
    return eventMessages.concat(gameChat).filter(function (item) { return item.content; }).slice(-50);
  }
  var helpers = { safeViewerUrl: safeViewerUrl, agentList: agentList, formatPosition: formatPosition, vitalValue: vitalValue, inventorySummary: inventorySummary, agentStatus: agentStatus, outcomeRecord: outcomeRecord, messageRecords: messageRecords };
  if (typeof module !== "undefined" && module.exports) module.exports = helpers;
  if (typeof document === "undefined") return;
  var $ = function (id) { return document.getElementById(id); };
  function el(tag, className, value) { var node = document.createElement(tag); if (className) node.className = className; if (value !== undefined) node.textContent = text(value); return node; }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  var state = { agents: [] }, selectedId = null, selectedAgent = null, receivedAt = 0, online = false;
  var polling = null, refreshFlight = null, stopped = false, busy = new Set(), viewerKey = null, frameLoaded = false;
  var messageSignature = "", recordSignatures = {}, selectionVersion = 0;
  function announce(message, error) { $("notice").textContent = message; $("notice").className = "notice" + (error ? " error" : ""); $("notice").hidden = false; }
  async function api(path, body) {
    var controller = new AbortController(), timeout = setTimeout(function () { controller.abort(); }, body === undefined ? 10000 : 45000);
    var options = { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" }, signal: controller.signal };
    if (body !== undefined) { options.method = "POST"; options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
    try {
      var response = await fetch(path, options), data = await response.json().catch(function () { return {}; });
      if (!response.ok) throw new Error(text(data.detail || data.error || "The supervisor returned HTTP " + response.status + "."));
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error(body === undefined ? "The supervisor did not respond in time." : "The request timed out; its result is unknown. Refresh before trying again.");
      if (error instanceof TypeError) throw new Error("Cannot reach the local supervisor.");
      throw error;
    } finally { clearTimeout(timeout); }
  }
  function endpoint(id, action) { return "/api/agents/" + encodeURIComponent(id) + "/" + action; }
  function setConnection(connected, message) {
    online = connected;
    $("connection").className = "connection " + (connected ? "connected" : "disconnected");
    $("connection-label").textContent = message || (connected ? "Local supervisor connected" : "Supervisor unavailable");
    $("connection-label").title = $("connection-label").textContent;
    $("unavailable").hidden = connected;
    if (!connected) $("unavailable").textContent = receivedAt ? "The supervisor is unavailable. Displayed telemetry is the last received state; controls are disabled." : "Waiting for the local supervisor. No agent or world telemetry has been received.";
    renderControls();
  }
  function renderAge() {
    if (!receivedAt) { $("observation-age").textContent = "—"; $("observation-time").textContent = "Not yet received"; return; }
    var seconds = Math.max(0, Math.floor((Date.now() - receivedAt) / 1000));
    $("observation-age").textContent = seconds < 2 ? "Just now" : seconds + "s ago";
    $("observation-time").textContent = "Supervisor response · " + new Date(receivedAt).toLocaleTimeString();
  }
  function renderAgents() {
    var list = $("agent-list"), signature = JSON.stringify(state.agents.map(function (agent) { return [agent.id, agent.name, agentStatus(agent), agent.observation && agent.observation.connected, agent.id === selectedId]; }));
    if (recordSignatures.agents === signature) return;
    recordSignatures.agents = signature; clear(list);
    $("agent-count").textContent = String(state.agents.length);
    state.agents.forEach(function (agent, index) {
      var button = el("button", "agent-button" + (agent.observation && agent.observation.connected === true ? " online" : ""));
      button.type = "button"; button.dataset.agentId = agent.id; button.setAttribute("aria-pressed", String(agent.id === selectedId));
      button.appendChild(el("span", "agent-avatar", String(index + 1).padStart(2, "0")));
      var copy = el("span", "agent-copy"); copy.appendChild(el("strong", "", agent.name || agent.id)); copy.appendChild(el("small", "", agentStatus(agent))); button.appendChild(copy);
      var dot = el("span", "status-dot"); dot.setAttribute("aria-hidden", "true"); button.appendChild(dot);
      button.addEventListener("click", function () { selectAgent(agent.id); }); list.appendChild(button);
    });
    if (!state.agents.length) list.appendChild(el("p", "empty", "No configured agents were returned by the supervisor."));
  }
  function selectAgent(id) {
    if (id === selectedId) return;
    selectedId = id; selectionVersion += 1; recordSignatures = {}; messageSignature = "";
    selectedAgent = state.agents.find(function (agent) { return agent.id === id; }) || null;
    $("goal-input").value = ""; $("message-input").value = "";
    render();
  }
  function renderViewer(agent) {
    var observation = agent && agent.observation || {}, url = observation.viewer_error ? null : safeViewerUrl(observation.viewer_url), key = agent && url ? [agent.id, url, observation.connected, observation.alive].join("|") : "";
    if (key !== viewerKey) {
      var oldFrame = $("world-viewer"); if (oldFrame) oldFrame.remove();
      viewerKey = key; frameLoaded = false;
      if (url && agent) {
        var frame = document.createElement("iframe"); frame.id = "world-viewer"; frame.src = url;
        frame.title = text(agent.name || agent.id) + " · local Minecraft viewer";
        frame.setAttribute("sandbox", "allow-scripts allow-same-origin allow-pointer-lock"); frame.setAttribute("allow", "fullscreen"); frame.setAttribute("referrerpolicy", "no-referrer");
        frame.addEventListener("load", function () { if (viewerKey === key) { frameLoaded = true; renderViewer(selectedAgent); } });
        $("viewer-stage").appendChild(frame);
      }
    }
    $("viewer-empty").hidden = !!url;
    $("open-viewer").hidden = !url;
    if (url) $("open-viewer").href = url; else $("open-viewer").removeAttribute("href");
    if (!url) {
      $("viewer-empty-title").textContent = observation.viewer_error ? "Viewer could not start" : observation.viewer_url ? "Viewer URL was not accepted" : "No viewer configured";
      $("viewer-empty-copy").textContent = observation.viewer_error ? text(observation.viewer_error) : observation.viewer_url ? "Only the configured HTTP viewer at 127.0.0.1 can be embedded. Check the local bridge configuration." : "Start the local Minecraft bridge and configure this agent’s viewer URL. No substitute world is displayed.";
      $("viewer-note").textContent = "No local viewer is available.";
    } else {
      $("viewer-note").textContent = observation.connected === false ? "Agent disconnected. The viewer may show its last frame." : (frameLoaded ? "Local viewer frame loaded. Game connectivity is reported by telemetry." : "Opening the configured local viewer…");
    }
  }
  function addRecord(container, title, body, meta, status) {
    var knownStatus = ["failed","error","succeeded","completed"].indexOf(status) >= 0 ? status : "";
    var record = el("article", "record" + (knownStatus ? " " + knownStatus : ""));
    var heading = el("h3", "record-title"); heading.appendChild(el("span", "", title));
    if (status) heading.appendChild(el("span", "record-status", humanize(status)));
    record.appendChild(heading);
    if (body) record.appendChild(el("p", "record-body", body));
    if (meta) record.appendChild(el("p", "record-meta", meta));
    container.appendChild(record);
  }
  function renderRecords(agent) {
    var observation = agent && agent.observation || {}, run = agent && agent.run || {};
    var signature = JSON.stringify([observation.active_action, observation.actions, observation.reflexes, agent && agent.active_role, agent && agent.error, run.goal, agent && agent.focus, agent && agent.ledger, agent && agent.milestones, agent && agent.recent_events]);
    if (signature === recordSignatures.details) return;
    recordSignatures.details = signature;
    var active = $("active-skill"); clear(active);
    var action = observation.active_action;
    if (action) {
      if (typeof action === "object") addRecord(active, humanize(action.skill || action.name || action.action || action.type || "Active action"), text(action.goal || action.parameters || action.params || action.target || ""), [action.id ? "Action " + action.id : "", agent.active_role ? "Faculty: " + humanize(agent.active_role) : "", action.source ? "Source: " + action.source : ""].filter(Boolean).join(" · "), action.status || "running");
      else addRecord(active, humanize(action), "", "", "running");
    } else active.appendChild(el("p", "empty", "No active skill reported."));
    if (run.goal) { var goal = el("div", "run-goal"); goal.appendChild(el("span", "eyebrow", "CURRENT RUN GOAL")); goal.appendChild(el("p", "record-goal", run.goal)); active.appendChild(goal); }
    if (agent && agent.error) addRecord(active, "Agent error", text(agent.error), "Reported by supervisor", "error");
    var focus = $("focus-list"); clear(focus);
    records(agent && agent.focus, 12).forEach(function (item) {
      addRecord(focus, humanize(item.kind || item.type || "Focus"), text(item.content || item.text || item.description || item.subject || item), [item.id ? "ID " + item.id : "", item.reason || "", finite(item.priority) ? "Priority " + item.priority : "", Array.isArray(item.source_ids) && item.source_ids.length ? "Sources " + item.source_ids.join(", ") : ""].filter(Boolean).join(" · "));
    });
    if (!focus.childNodes.length) focus.appendChild(el("p", "empty", "No focus items reported."));
    var outcomes = $("outcomes-list"); clear(outcomes);
    var knownActionIds = new Set();
    records(agent && agent.ledger, 20).concat(records(observation.actions, 20)).filter(function (item) {
      if (!item.id) return true;
      if (knownActionIds.has(item.id)) return false;
      knownActionIds.add(item.id); return true;
    }).slice(-30).reverse().forEach(function (item) {
      var outcome = outcomeRecord(item);
      addRecord(outcomes, outcome.title, outcome.details, ["Actor: " + outcome.actor, outcome.id ? "ID " + outcome.id : "", outcome.time].filter(Boolean).join(" · "), outcome.status);
    });
    records(agent && agent.recent_events, 40).filter(function (item) { return /death|died|unknown|error/.test(String(item.kind || item.type || "")); }).reverse().slice(0,8).forEach(function (item) {
      addRecord(outcomes, humanize(item.kind || item.type), text(item.content || item.message || item.text || item), text(item.created_at || item.time), item.kind === "error" ? "error" : "");
    });
    if (!outcomes.childNodes.length) outcomes.appendChild(el("p", "empty", "No outcomes reported. A requested action is not yet an observed result."));
    if (agent && agent.milestones && typeof agent.milestones === "object") {
      var milestones = el("div", "milestones");
      Object.keys(agent.milestones).slice(0,8).forEach(function (key) { var value = agent.milestones[key]; if (finite(value)) milestones.appendChild(el("span", "", humanize(key) + ": " + value)); });
      if (milestones.childNodes.length) outcomes.appendChild(milestones);
    }
  }
  function renderMessages(agent) {
    var messages = messageRecords(agent), signature = JSON.stringify(messages);
    if (signature === messageSignature) return;
    messageSignature = signature;
    var list = $("messages-list"), nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 60; clear(list);
    messages.forEach(function (message) {
      var article = el("article", "message" + (message.role === "user" || message.role === "operator_message" ? " user" : ""));
      article.appendChild(el("div", "message-label", message.label + (message.time ? " · " + message.time : "")));
      article.appendChild(el("p", "message-content", message.content)); list.appendChild(article);
    });
    if (!messages.length) list.appendChild(el("p", "empty", "No messages reported for this agent."));
    if (nearBottom) list.scrollTop = list.scrollHeight;
  }
  function renderControls() {
    var agent = selectedAgent, observation = agent && agent.observation || {}, run = agent && agent.run || {};
    var blocked = !online || !agent || busy.has(agent.id), canRun = !blocked && state.bridge_online !== false && observation.connected === true && observation.alive !== false && !run.active;
    $("run-button").disabled = !canRun; $("stop-button").disabled = blocked; $("checkpoint-button").disabled = blocked; $("message-button").disabled = blocked;
    $("goal-input").disabled = blocked; $("steps-input").disabled = blocked; $("message-input").disabled = blocked;
    $("run-button").textContent = run.active ? "Run active" : "Run agent ↗";
    $("run-state").textContent = run.active ? "RUNNING · " + text(run.remaining_steps) + " LEFT" : humanize(run.status || "idle").toUpperCase();
    var maxSteps = state.defaults && state.defaults.max_steps;
    $("steps-input").max = String(Number.isInteger(maxSteps) && maxSteps > 0 ? Math.min(30,maxSteps) : 30);
    $("control-note").textContent = !agent ? "Select a configured agent to enable controls." :
      observation.connected !== true ? "The agent is not connected to Minecraft. Run is unavailable; messages, Stop, and mind checkpoints remain available when the supervisor is online." :
      observation.alive === false ? "The agent is dead. A new Run is unavailable until it is alive again. Stop still cancels work." :
      "Stop cancels work and disables body reflexes. Start another explicit Run to continue. Checkpoints save the mind database; the game server saves the world separately.";
  }
  function render() {
    selectedAgent = state.agents.find(function (agent) { return agent.id === selectedId; }) || null;
    renderAgents();
    var agent = selectedAgent, observation = agent && agent.observation || {};
    $("selected-id").textContent = agent ? "AGENT / " + agent.id : "NO AGENT SELECTED";
    $("selected-name").textContent = agent ? agent.name || agent.id : "Waiting for an agent";
    $("agent-status").textContent = agentStatus(agent);
    $("agent-status").className = "status-pill" + (!agent || observation.connected !== true || observation.alive === false || agent.error ? " warning" : "");
    var activeRuns = state.agents.filter(function (item) { return item.run && item.run.active; }).length;
    $("session-status").textContent = state.bridge_online === false ? "Minecraft bridge offline" : activeRuns ? activeRuns + (activeRuns === 1 ? " agent running" : " agents running") : "Awaiting direction";
    $("session-detail").textContent = state.agents.length + (state.agents.length === 1 ? " configured agent" : " configured agents") + " · " + (state.bridge_online === true ? "bridge online" : state.bridge_online === false ? "bridge unavailable" : "bridge status unknown");
    ["health","food"].forEach(function (kind) { var value = observation[kind]; $(kind+"-value").textContent = vitalValue(value) + (finite(value) ? " / 20" : ""); $(kind+"-meter").hidden = !finite(value); if (finite(value)) $(kind+"-meter").value = Math.max(0,Math.min(20,value)); });
    $("position-value").textContent = formatPosition(observation.position);
    $("dimension-value").textContent = observation.dimension ? humanize(observation.dimension) : "Dimension not reported";
    $("world-time").textContent = observation.time === undefined || observation.time === null ? "Minecraft time not reported" : "Minecraft time: " + text(observation.time);
    $("body-status").textContent = observation.enabled === true ? "Body controls enabled" : observation.enabled === false ? "Body controls disabled" : "Body control status not reported";
    $("reflex-summary").textContent = Array.isArray(observation.reflexes) ? (observation.reflexes.length ? "Recent reflexes: " + observation.reflexes.slice(-3).map(function (item) { return text(item.kind || item.skill || item); }).join(", ") : "No recent reflexes reported") : "Recent reflexes not reported";
    $("inventory-summary").textContent = inventorySummary(observation.inventory);
    renderAge(); renderViewer(agent); renderRecords(agent); renderMessages(agent); renderControls();
  }
  async function refresh() {
    if (refreshFlight) return refreshFlight;
    refreshFlight = (async function () {
      try {
        var incoming = await api("/api/fishbowl");
        if (!incoming || !Array.isArray(incoming.agents)) throw new Error("The supervisor returned an invalid state.");
        state = Object.assign({}, incoming, { agents: agentList(incoming.agents) }); receivedAt = Date.now();
        if (!state.agents.some(function (agent) { return agent.id === selectedId; })) {
          selectedId = state.agents.length ? state.agents[0].id : null; selectionVersion += 1; recordSignatures = {}; messageSignature = "";
        }
        setConnection(true); render();
      } catch (error) { setConnection(false); $("connection-label").textContent = error.message; $("connection-label").title = error.message; renderAge(); }
    })().finally(function () { refreshFlight = null; });
    return refreshFlight;
  }
  async function mutate(action, body) {
    var agent = selectedAgent; if (!agent || busy.has(agent.id) || !online) return false;
    var id = agent.id; busy.add(id); renderControls();
    try {
      var result = await api(endpoint(id,action),body);
      if (action === "checkpoint") announce("Mind checkpoint saved for " + (agent.name || id) + (result.path || result.checkpoint_path ? ": " + text(result.path || result.checkpoint_path) : ".") + " Minecraft world saves are managed by the game server.");
      else if (action === "stop") announce("Stop accepted for " + (agent.name || id) + ". Check reported body state and outcomes below.");
      else if (action === "run") announce("Run accepted for " + (agent.name || id) + ".");
      else if (action === "message") announce("Message accepted for " + (agent.name || id) + ".");
      await refresh(); return true;
    } catch (error) { announce(error.message,true); return false; }
    finally { busy.delete(id); renderControls(); }
  }
  $("refresh-button").addEventListener("click", function () { refresh(); });
  $("run-form").addEventListener("submit", async function (event) {
    event.preventDefault(); if ($("run-button").disabled) return;
    var goal = $("goal-input").value.trim(), steps = Number($("steps-input").value), maximum = Number($("steps-input").max);
    if (!goal || goal.length > 2000 || !Number.isInteger(steps) || steps < 1 || steps > maximum) { announce("Enter a goal and a decision limit between 1 and " + maximum + ".",true); return; }
    await mutate("run",{goal:goal,steps:steps});
  });
  $("stop-button").addEventListener("click", function () { mutate("stop",{}); });
  $("checkpoint-button").addEventListener("click", function () { mutate("checkpoint",{}); });
  $("message-form").addEventListener("submit", async function (event) {
    event.preventDefault(); if ($("message-button").disabled) return;
    var value = $("message-input").value.trim(), version = selectionVersion;
    if (!value || value.length > 2000) { announce("Enter a message of 1 to 2000 characters.",true); return; }
    if (await mutate("message",{text:value}) && version === selectionVersion && $("message-input").value.trim() === value) $("message-input").value = "";
  });
  async function poll() { await refresh(); if (!stopped) polling = setTimeout(poll,1000); }
  var ageTimer = setInterval(renderAge,1000);
  window.addEventListener("beforeunload", function () { stopped = true; clearTimeout(polling); clearInterval(ageTimer); });
  poll();
})(typeof globalThis !== "undefined" ? globalThis : this);
