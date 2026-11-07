/* AI-AM's observatory. No remote assets and no model text is interpreted as HTML. */
(function (global) {
  "use strict";
  var ROLES = [
    { id: "manas", name: "Manas", subtitle: "Attention & interpretation", symbol: "◌" },
    { id: "chitta", name: "Chitta", subtitle: "Memory & association", symbol: "⟡" },
    { id: "ahamkara", name: "Ahamkara", subtitle: "Identity & ownership", symbol: "◎" },
    { id: "buddhi", name: "Buddhi", subtitle: "Judgment & direction", symbol: "◇" }
  ];
  function text(value) {
    if (value === undefined || value === null) return "";
    return typeof value === "string" ? value : JSON.stringify(value, null, 2);
  }
  function formatDuration(ms) {
    if (!Number.isFinite(Number(ms)) || Number(ms) <= 0) return "—";
    return Number(ms) < 1000 ? Math.round(Number(ms)) + " ms" : (Number(ms) / 1000).toFixed(1) + " s";
  }
  function shortId(value) { return String(value || "").slice(0, 8); }
  function humanize(value) { return String(value || "").replace(/[_-]/g, " ").replace(/^\w/, function (letter) { return letter.toUpperCase(); }); }
  function messageKind(value) {
    return ["user", "assistant", "reflection", "error"].indexOf(value) >= 0 ? value : "system";
  }
  function tokenizeCode(value) {
    var source = text(value), fence = String.fromCharCode(96).repeat(3), pattern = new RegExp(fence + "([\\w+-]*)\\s*\\n?([\\s\\S]*?)" + fence, "g");
    var result = [], last = 0, match;
    while ((match = pattern.exec(source)) !== null) {
      if (match.index > last) result.push({ type: "text", content: source.slice(last, match.index) });
      result.push({ type: "code", language: match[1] || "", content: match[2].replace(/\n$/, "") });
      last = pattern.lastIndex;
    }
    if (last < source.length || !result.length) result.push({ type: "text", content: source.slice(last) });
    return result;
  }
  function renderText(container, value, doc) {
    doc = doc || document;
    tokenizeCode(value).forEach(function (part) {
      if (part.type === "text") { container.appendChild(doc.createTextNode(part.content)); return; }
      var pre = doc.createElement("pre");
      if (part.language) {
        var label = doc.createElement("span"); label.className = "code-label"; label.textContent = part.language; pre.appendChild(label);
      }
      var code = doc.createElement("code"); code.textContent = part.content; pre.appendChild(code); container.appendChild(pre);
    });
  }
  function mergeEvents(existing, incoming) {
    var byId = new Map();
    existing.concat(incoming).forEach(function (event) { if (event && typeof event.id === "string") byId.set(event.id, event); });
    return Array.from(byId.values()).sort(function (a, b) { return String(a.created_at || "").localeCompare(String(b.created_at || "")); });
  }
  function facultyStatus(role, state, completed) {
    if (state && state.active_role === role && state.status === "thinking") return "active";
    return completed ? "complete" : "waiting";
  }
  function modelName(role, fallback) {
    return typeof role === "string" ? role : ((role && role.model) || fallback || "");
  }
  function extractEvidence(event) {
    var meta = (event && event.metadata) || {};
    var trace = Array.isArray(meta.trace) ? meta.trace : [];
    var finalBuddhi = trace.filter(function (entry) { return entry && entry.role === "buddhi"; }).pop();
    var ids = meta.evidence_ids || (meta.decision && meta.decision.evidence_ids) ||
      (finalBuddhi && finalBuddhi.result && finalBuddhi.result.evidence_ids) || meta.source_event_ids || [];
    return Array.isArray(ids) ? Array.from(new Set(ids.filter(function (id) { return typeof id === "string" && id.length > 0; }))) : [];
  }
  function snapshotFaculties(previous, next, live) {
    previous = previous || {}; next = next || {};
    if (next.status === "thinking") {
      // The engine's workspace is the last committed cycle, not the in-flight cycle.
      return previous.status === "thinking" && previous.cycle === next.cycle ? Object.assign({}, live) : {};
    }
    var workspace = next.workspace || {}, result = {};
    var restored = {
      manas: workspace.manas || ((workspace.observations || []).length ? { observations: workspace.observations, interpretations: workspace.interpretations || [] } : null),
      chitta: workspace.chitta || ((workspace.memories || []).length ? { memories: workspace.memories } : null),
      ahamkara: workspace.ahamkara || workspace.self_assessment,
      buddhi: workspace.buddhi || workspace.decision
    };
    ROLES.forEach(function (role) {
      var value = restored[role.id];
      if (value && typeof value === "object" && Object.keys(value).length) result[role.id] = value;
    });
    return result;
  }
  function eventError(event) { return text(event && (event.error || (event.metadata || {}).error)); }
  function validateInput(value, reflection) {
    var input = String(value || "").trim(), limit = reflection ? 2000 : 8000;
    if (!reflection && !input) throw new Error("Enter a message first.");
    if (input.length > limit) throw new Error((reflection ? "Reflection topics" : "Messages") + " are limited to " + limit + " characters.");
    return input;
  }

  function boundedRecords(value, maximum) {
    var rows = Array.isArray(value) ? value : (value && typeof value === "object" ? Object.keys(value).map(function (key) {
      return typeof value[key] === "object" && value[key] !== null ? Object.assign({ id: key }, value[key]) : { id: key, content: value[key] };
    }) : []);
    return rows.filter(function (row) { return row && typeof row === "object"; }).slice(-(maximum || 12));
  }
  function normalizeWorld(view, evaluator) {
    view = view || {};
    // Never fall back to private world data in the ordinary agent view.
    var observation = view.observation || {};
    var source = evaluator && view.evaluator_only === true && view.world ? view.world : observation;
    var width = Number.isInteger(source.width) && source.width > 0 && source.width <= 32 ? source.width : 9;
    var height = Number.isInteger(source.height) && source.height > 0 && source.height <= 32 ? source.height : 7;
    function coordinate(position) {
      return Array.isArray(position) && position.length === 2 && position.every(Number.isInteger) &&
        position[0] >= 0 && position[0] < width && position[1] >= 0 && position[1] < height ? position : null;
    }
    var visible = new Map(), objects = new Map();
    if (evaluator && source === view.world) {
      for (var y = 0; y < height; y++) for (var x = 0; x < width; x++) visible.set(x + "," + y, "floor");
      (Array.isArray(source.walls) ? source.walls : []).forEach(function (position) { if (coordinate(position)) visible.set(position.join(","), "wall"); });
    } else {
      (Array.isArray(observation.visible_cells) ? observation.visible_cells : []).forEach(function (cell) {
        if (cell && coordinate(cell.position)) visible.set(cell.position.join(","), cell.kind === "wall" ? "wall" : "floor");
      });
    }
    boundedRecords(source.objects, 100).forEach(function (item) {
      var position = coordinate(item.position);
      if (!position || !visible.has(position.join(","))) return;
      var key = position.join(","), at = objects.get(key) || [];
      at.push({ id: text(item.id).slice(0,128), name: text(item.name || item.id).slice(0,500) }); objects.set(key, at);
    });
    var pose = source.pose || {}, agent = coordinate(pose.position);
    var heading = ["north","east","south","west"].indexOf(pose.heading) >= 0 ? pose.heading : "north";
    var cells = [];
    for (var row = 0; row < height; row++) for (var column = 0; column < width; column++) {
      var key = column + "," + row;
      cells.push({ x: column, y: row, known: visible.has(key), kind: visible.get(key) || "unknown",
        objects: objects.get(key) || [], agent: !!agent && agent[0] === column && agent[1] === row });
    }
    return { width: width, height: height, cells: cells, position: agent, heading: heading,
      energy: Number.isFinite(source.energy) ? source.energy : null,
      inventory: Array.isArray(source.inventory) ? source.inventory.map(text).slice(0,10) : [],
      tick: source.tick, evaluator: evaluator && source === view.world };
  }
  function actionSummary(action) {
    action = action || {};
    var parameters = action.parameters || {}, result = action.result || {};
    return { title: humanize(parameters.action || action.action || action.tool || "Action"),
      actor: text(action.actor || "unknown"), status: text(action.status || "unknown"),
      detail: text(result.reason || result.content || action.error || parameters.expression || parameters.target || ""),
      prediction: text(action.prediction || ""), id: text(action.id || action.action_id || "") };
  }

  function worldTargets(view) {
    var observation = view && view.observation || {}, model = view && view.model || {}, targets = new Map();
    boundedRecords(observation.objects, 20).concat(boundedRecords(view && view.beliefs || model.beliefs, 20)).forEach(function (item) {
      var id = item.id || item.object_id;
      if (typeof id === "string" && id.length > 0 && id.length <= 32 && !targets.has(id)) targets.set(id, { id: id, name: text(item.name || id) });
    });
    (Array.isArray(observation.inventory) ? observation.inventory : []).slice(0,20).forEach(function (id) {
      if (typeof id === "string" && id.length > 0 && id.length <= 32 && !targets.has(id)) targets.set(id, { id: id, name: id });
    });
    return Array.from(targets.values()).slice(0,20);
  }
  function worldActionTarget(action, selected) {
    return ["inspect", "pick_up", "drop"].indexOf(action) >= 0 ? String(selected || "") : "";
  }
  var helpers = { worldTargets: worldTargets, worldActionTarget: worldActionTarget, normalizeWorld: normalizeWorld, boundedRecords: boundedRecords, actionSummary: actionSummary,  tokenizeCode: tokenizeCode, renderText: renderText, formatDuration: formatDuration, messageKind: messageKind, mergeEvents: mergeEvents, facultyStatus: facultyStatus, modelName: modelName, extractEvidence: extractEvidence, snapshotFaculties: snapshotFaculties, eventError: eventError, validateInput: validateInput };
  if (typeof module !== "undefined" && module.exports) module.exports = helpers;
  if (typeof document === "undefined") return;

  var $ = function (id) { return document.getElementById(id); };
  var state = {}, events = [], eventNodes = new Map(), memories = new Map(), roleResults = {}, models = [], socket = null;
  var reconnectTimer = null, reconnectAttempts = 0, memoryTimer = null, toastTimer = null, searchSequence = 0, stopped = false;
  var refreshFlight = null, isSending = false;
  var experimentView = null, evaluatorView = null, experimentFlight = null, experimentBusy = false;
  function el(tag, className, content) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = text(content);
    return node;
  }
  function clear(node) { node.replaceChildren(); }
  function announce(message, error) {
    if (error) { $("alert-message").textContent = text(message); $("alert-banner").hidden = false; return; }
    $("toast").textContent = text(message); $("toast").hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { $("toast").hidden = true; }, 5500);
  }
  async function api(path, body) {
    var options = { credentials: "same-origin", headers: { Accept: "application/json" } };
    if (body !== undefined) { options.method = "POST"; options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
    var response;
    try { response = await fetch(path, options); }
    catch (error) { throw new Error("The local runtime is unavailable. Start AI-AM, then reconnect."); }
    var data = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new Error(text(data.detail || data.error || ("Request failed (" + response.status + ").")));
    return data;
  }
  function setConnection(connected) {
    $("connection-status").className = "connection " + (connected ? "connected" : "disconnected");
    $("connection-label").textContent = connected ? "Connected locally" : "Reconnecting";
    $("connection-status").setAttribute("aria-label", connected ? "Connected to local runtime" : "Disconnected, trying to reconnect");
  }
  function timeLabel(value) {
    var date = new Date(value); return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  function createFaculties() {
    ROLES.forEach(function (role) {
      var card = el("details", "faculty-card"); card.dataset.role = role.id; card.id = "faculty-" + role.id;
      var summary = el("summary");
      summary.appendChild(el("span", "faculty-symbol", role.symbol));
      var labels = el("span"); labels.appendChild(el("span", "faculty-name", role.name)); labels.appendChild(el("span", "faculty-subtitle", role.subtitle));
      summary.appendChild(labels);
      var indicator = el("span", "faculty-indicator"); indicator.setAttribute("aria-label", "Waiting"); summary.appendChild(indicator);
      card.appendChild(summary);
      var result = el("div", "faculty-result", "A contribution will appear here when this faculty is used."); result.id = "result-" + role.id;
      card.appendChild(result); $("faculty-list").appendChild(card);
    });
  }
  function renderResult(role, value, elapsed) {
    var target = $("result-" + role); if (!target) return;
    clear(target);
    if (value && typeof value === "object" && !Array.isArray(value)) {
      Object.entries(value).forEach(function (entry) {
        if (entry[1] === undefined || entry[1] === null || (Array.isArray(entry[1]) && !entry[1].length)) return;
        target.appendChild(el("div", "result-label", humanize(entry[0])));
        target.appendChild(el("p", "", text(entry[1])));
      });
    } else target.appendChild(el("p", "", text(value)));
    if (!target.childNodes.length) target.appendChild(el("p", "", "No additional information was needed."));
    if (elapsed !== undefined) target.appendChild(el("div", "faculty-timing", "Completed in " + formatDuration(elapsed)));
  }
  function setRoleAppearance() {
    ROLES.forEach(function (role) {
      var status = facultyStatus(role.id, state, Object.prototype.hasOwnProperty.call(roleResults, role.id));
      var card = $("faculty-" + role.id);
      card.className = "faculty-card " + status;
      card.querySelector(".faculty-indicator").setAttribute("aria-label", humanize(status));
      var orbit = document.querySelector('[data-orbit-role="' + role.id + '"]');
      orbit.classList.toggle("active", status === "active");
    });
  }
  function listSection(container, title, items) {
    if (!Array.isArray(items) || !items.length) return;
    container.appendChild(el("div", "small-label", title));
    var list = el("ul");
    items.forEach(function (item) { list.appendChild(el("li", "", typeof item === "object" ? (item.content || item.description || item.text || text(item)) : item)); });
    container.appendChild(list);
  }
  function renderState(next) {
    var previousResults = roleResults;
    roleResults = snapshotFaculties(state, next, roleResults);
    state = next || {};
    var self = state.self_model || {}, meta = state.metrics || {}, workspace = state.workspace || {};
    $("identity-name").textContent = self.name || "AI-AM";
    $("identity-description").textContent = self.description || "Four cooperating faculties, one shared history.";
    $("mind-status").textContent = humanize(state.status || "idle");
    $("cycle-count").textContent = "#" + String(state.cycle || 0).padStart(3, "0");
    $("state-version").textContent = "STATE " + (state.state_version || 0);
    $("memory-count").textContent = String((state.memory_stats || {}).active_memories || 0);
    $("metric-tokens").textContent = Number(meta.tokens || 0).toLocaleString();
    $("metric-latency").textContent = formatDuration(meta.latency_ms);
    $("metric-errors").textContent = String(meta.errors || 0);
    $("demo-banner").hidden = state.backend !== "demo";
    $("backend-label").textContent = state.backend === "demo" ? "Scripted demo · durable memory" : "Local inference · durable memory";
    var paused = state.status === "paused" || state.paused === true;
    $("pause-button").textContent = paused ? "Resume" : "Pause";
    $("pause-button").disabled = state.status === "stopped";
    $("background-toggle").checked = !!state.background_enabled;
    $("thinking-strip").hidden = state.status !== "thinking";
    $("thinking-label").textContent = state.active_role ? humanize(state.active_role) + " is contributing" : "Integrating the shared perspective";
    $("session-indicator").textContent = paused ? "Processing paused" : (state.status === "thinking" ? "A response is taking shape" : "Continuity preserved");
    $("composer-hint").textContent = paused ? "Messages will wait until you resume" : "Ctrl / ⌘ + Enter to send";
    $("send-button").disabled = stopped || state.status === "stopped" || isSending;
    clear($("self-details"));
    listSection($("self-details"), "CAPABILITIES", self.capabilities);
    listSection($("self-details"), "COMMITMENTS", self.commitments);
    listSection($("self-details"), "ACTIVE GOALS", state.goals);
    if (!$("self-details").childNodes.length) $("self-details").appendChild(el("p", "muted", "The self-model will develop through recorded interactions."));
    var current = state.current_event;
    $("attention-text").textContent = typeof current === "object" && current ? (current.content || current.text || "Integrating the current observation.") : (current ? String(current) : "Waiting for an observation.");
    clear($("uncertainty-list"));
    (Array.isArray(workspace.uncertainties) ? workspace.uncertainties : []).slice(0, 4).forEach(function (item) { $("uncertainty-list").appendChild(el("span", "uncertainty-tag", typeof item === "string" ? item : text(item))); });
    ROLES.forEach(function (role) {
      var value = roleResults[role.id];
      if (JSON.stringify(value) !== JSON.stringify(previousResults[role.id])) {
        renderResult(role.id, value || "Waiting for this faculty.");
      }
    });
    setRoleAppearance();
    if (Array.isArray(state.recent_events)) renderEvents(state.recent_events);
    if (state.experiment) { experimentView = state.experiment; renderExperiment(); }
    else renderExperimentFocus();
  }

  function appendExperimentRecord(container, title, detail, meta, status) {
    var allowed = ["succeeded", "failed", "outcome_unknown", "attempted", "proposed"];
    var card = el("div", "experiment-record" + (allowed.indexOf(status) >= 0 ? " " + status : ""));
    card.appendChild(el("p", "record-title", title));
    if (detail) card.appendChild(el("p", "record-detail", detail));
    if (meta) card.appendChild(el("div", "record-meta", meta));
    container.appendChild(card);
    return card;
  }
  function renderExperimentFocus() {
    var container = $("experiment-focus"); if (!container) return;
    clear(container);
    var workspace = state.workspace || {}, attention = workspace.attention || {};
    var selected = attention.selected_ids || [];
    var focus = workspace.focus || attention.focus || boundedRecords(attention.items, 30).filter(function (item) {
      return selected.indexOf(item.id) >= 0;
    });
    boundedRecords(focus, 8).forEach(function (item) {
      appendExperimentRecord(container, item.content || item.text || item.label || item.id,
        Array.isArray(item.reasons) ? item.reasons.join(" · ") : (item.reason || ""),
        [item.kind ? humanize(item.kind) : "", item.score !== undefined ? "Priority " + Number(item.score).toFixed(2) : "",
          Array.isArray(item.source_event_ids) ? item.source_event_ids.length + " source(s)" : ""].filter(Boolean).join(" · "));
    });
    if (!container.childNodes.length) container.appendChild(el("p", "empty-note", "Focus will appear when the agent processes an event."));
    var missed = boundedRecords(attention.missed || attention.excluded, 3);
    if (missed.length) container.appendChild(el("p", "experiment-note",
      "Outside current focus: " + missed.map(function (item) { return item.content || item.text || item.id; }).join("; ")));
  }
  function renderExperimentControls() {
    var run = experimentView && experimentView.run || {}, active = !!run.active;
    document.querySelectorAll("[data-world-action], #intervention-form button, #reverse-controls, #restore-controls, #experiment-reset").forEach(function (button) {
      button.disabled = experimentBusy || active || stopped || state.status === "stopped";
    });
    var globallyPaused = state.paused === true || state.status === "paused";
    $("experiment-run").disabled = experimentBusy || active || globallyPaused || stopped || state.status === "stopped";
    $("experiment-pause").disabled = experimentBusy || !active || stopped || state.status === "stopped";
  }
  function renderExperiment() {
    if (!$("world-grid")) return;
    renderExperimentFocus();
    var view = experimentView || {}, observation = view.observation || {}, model = view.model || {};
    var wantsObserver = $("observer-toggle").checked;
    var currentEvaluator = evaluatorView && evaluatorView.revision === view.revision ? evaluatorView : null;
    var world = normalizeWorld(currentEvaluator || view, wantsObserver && !!currentEvaluator);
    var grid = $("world-grid"); clear(grid);
    grid.style.gridTemplateColumns = "repeat(" + world.width + ", minmax(0, 1fr))";
    grid.classList.toggle("evaluator", world.evaluator);
    grid.setAttribute("aria-label", (world.evaluator ? "Observer map with hidden world revealed" : "Agent observation; striped cells are unobserved") + ", " + world.width + " by " + world.height);
    var directions = { north: "↑", east: "→", south: "↓", west: "←" };
    world.cells.forEach(function (cell) {
      var names = cell.objects.map(function (item) { return item.name; }).join(", ");
      var label = "(" + cell.x + ", " + cell.y + "): " + (cell.known ? cell.kind : "unobserved");
      if (names) label += " · " + names;
      if (cell.agent) label += " · agent facing " + world.heading;
      var node = el("span", "world-cell " + (cell.known ? "known " + cell.kind : "unknown") +
        (cell.objects.length ? " object" : "") + (cell.agent ? " agent" : ""));
      node.dataset.x = String(cell.x); node.dataset.y = String(cell.y); node.dataset.known = String(cell.known);
      node.title = label; node.setAttribute("aria-label", label);
      if (cell.agent) node.textContent = directions[world.heading];
      else if (cell.objects.length) node.textContent = cell.objects[0].id === "key" ? "⚿" : cell.objects[0].id === "cup" ? "◡" : "●";
      else node.textContent = cell.known ? (cell.kind === "wall" ? "" : "·") : "·";
      if (cell.agent && cell.objects.length) node.appendChild(el("span", "cell-object", "●"));
      grid.appendChild(node);
    });
    $("world-view-title").textContent = world.evaluator ? "Observer view: complete world" : "What AI-AM can observe";
    $("observer-notice").hidden = !wantsObserver;
    $("observer-notice").textContent = world.evaluator ? "Supervisor view. Hidden information is not given to the agent." : "Refreshing supervisor view. The agent observation is shown until it arrives.";
    $("experiment-revision").textContent = "WORLD " + (view.revision || 0);
    var pose = observation.pose || {}, position = pose.position;
    $("world-status").textContent = position ? "At (" + position.join(", ") + "), facing " + text(pose.heading) +
      " · observation tick " + text(observation.tick) : "Start or reset the simulated world to observe it.";
    clear($("world-observation"));
    if (Number.isFinite(observation.energy)) $("world-observation").appendChild(el("span", "", "Energy " + observation.energy));
    $("world-observation").appendChild(el("span", "", "Carrying: " + ((observation.inventory || []).join(", ") || "nothing")));
    if (Number.isFinite(observation.steps_remaining)) $("world-observation").appendChild(el("span", "", observation.steps_remaining + " environment steps remaining"));
    var names = boundedRecords(observation.objects, 20).map(function (item) { return item.name || item.id; });
    $("world-observation").appendChild(el("span", "", "Visible: " + (names.join(", ") || "no objects")));

    var targetSelect = $("world-target"), selectedTarget = targetSelect.value; clear(targetSelect);
    var automatic = el("option", "", "Automatic"); automatic.value = ""; targetSelect.appendChild(automatic);
    worldTargets(view).forEach(function (item) {
      var option = el("option", "", item.name); option.value = item.id; targetSelect.appendChild(option);
    });
    targetSelect.value = Array.from(targetSelect.options).some(function (option) { return option.value === selectedTarget; }) ? selectedTarget : "";

    var run = view.run || {};
    $("experiment-run-status").textContent = run.active ? "RUNNING · " + text(run.remaining_steps) + " LEFT" : (state.paused === true || state.status === "paused" ? "RESUME THE MIND TO RUN" : humanize(run.status || "ready").toUpperCase());

    var beliefs = $("experiment-beliefs"); clear(beliefs);
    boundedRecords(view.beliefs || model.beliefs, 15).forEach(function (item) {
      var where = item.position || item.last_observed_position || item.last_position || item.location;
      var description = item.content || item.description || (where ? "Last location: " + text(where) : "Location unknown");
      appendExperimentRecord(beliefs, item.name || item.object_id || item.id || "Belief", description,
        [item.location_status || item.status || item.kind || "", item.confidence !== undefined ? "Confidence " + Number(item.confidence).toFixed(2) : "",
          item.last_seen_tick !== undefined ? "Observed at tick " + item.last_seen_tick : ""].filter(Boolean).join(" · "));
    });
    if (!beliefs.childNodes.length) beliefs.appendChild(el("p", "empty-note", "No object beliefs have been formed yet. Unseen objects are not supplied as facts."));

    var predictions = $("experiment-predictions"); clear(predictions);
    boundedRecords(view.predictions || model.predictions || model.history, 8).reverse().forEach(function (item) {
      var expected = item.expected || item.prediction || item.expected_observation || item.predicted_pose || item.predicted_effect || item.description || "";
      var actual = item.actual || item.outcome || item.observed || item.effect || "";
      var details = (expected ? "Expected: " + text(expected) : "") + (actual ? "\nObserved: " + text(actual) : "") +
        (item.statement ? "\nStated expectation (unscored): " + text(item.statement) : "");
      appendExperimentRecord(predictions, humanize(item.action || item.action_id || "Prediction"), details,
        [item.status || "", item.error !== undefined ? "Error " + text(item.error) : item.prediction_error !== undefined && item.prediction_error !== null ? "Brier error " + text(item.prediction_error) : "",
          item.confidence !== undefined ? "Pose forecast probability " + Number(item.confidence).toFixed(2) : ""].filter(Boolean).join(" · "));
    });
    boundedRecords(model.discrepancies || view.discrepancies, 4).forEach(function (item) {
      appendExperimentRecord(predictions, "Expectation revised", item.content || item.description || item.reason || text(item), "Observed discrepancy");
    });
    if (!predictions.childNodes.length) predictions.appendChild(el("p", "empty-note", "Predictions and observed outcomes will appear after actions."));
    clear($("experiment-metrics"));
    var metrics = Object.assign({}, model.calibration || {}, model.metrics || {}, view.metrics || {});
    Object.keys(metrics).slice(0,8).forEach(function (key) {
      var value = metrics[key];
      if (typeof value === "number" && Number.isFinite(value)) $("experiment-metrics").appendChild(el("span", "", humanize(key) + ": " + (Number.isInteger(value) ? value : value.toFixed(3))));
    });

    var actions = $("experiment-actions"); clear(actions);
    boundedRecords(view.actions || state.action_ledger, 12).reverse().forEach(function (item) {
      var summary = actionSummary(item);
      appendExperimentRecord(actions, summary.title, summary.detail,
        humanize(summary.actor) + " · " + humanize(summary.status), summary.status);
    });
    if (!actions.childNodes.length) actions.appendChild(el("p", "empty-note", "No actions have been recorded."));
    renderExperimentControls();
    if (wantsObserver && !currentEvaluator && !experimentFlight) refreshExperiment().catch(function (error) { announce(error.message, true); });
  }
  async function refreshExperiment() {
    if (experimentFlight) return experimentFlight;
    experimentFlight = (async function () {
      experimentView = await api("/api/experiment");
      // Read the toggle after the await: it may change during the public request.
      if ($("observer-toggle").checked) {
        var receivedEvaluator = await api("/api/experiment/evaluator");
        evaluatorView = $("observer-toggle").checked ? receivedEvaluator : null;
      } else { evaluatorView = null; }
      renderExperiment();
    })().finally(function () { experimentFlight = null; });
    return experimentFlight;
  }
  async function experimentMutation(endpoint, body, message) {
    if (experimentBusy) return;
    experimentBusy = true; renderExperimentControls();
    try {
      await api("/api/experiment/" + endpoint, body);
      await Promise.all([refresh(), refreshExperiment()]);
      if (message) announce(message);
    } catch (error) { announce(error.message, true); }
    finally { experimentBusy = false; renderExperimentControls(); }
  }
  function bindExperimentControls() {
    $("observer-toggle").addEventListener("change", function () {
      if (!$("observer-toggle").checked) evaluatorView = null;
      renderExperiment();
      refreshExperiment().catch(function (error) { announce(error.message, true); });
    });
    document.querySelectorAll("[data-world-action]").forEach(function (button) {
      button.addEventListener("click", function () {
        var confidence = Number($("world-confidence").value);
        if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1) { announce("Stated confidence must be between 0 and 1.", true); return; }
        experimentMutation("step", { action: button.dataset.worldAction, target: worldActionTarget(button.dataset.worldAction, $("world-target").value),
          prediction: $("world-prediction").value.trim(), confidence: confidence });
      });
    });
    $("experiment-run-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var goal = $("experiment-goal").value.trim(), steps = Number($("experiment-steps").value);
      if (!goal || goal.length > 2000 || !Number.isInteger(steps) || steps < 1 || steps > 10) {
        announce("Enter a goal and a step limit between 1 and 10.", true); return;
      }
      experimentMutation("run", { goal: goal, steps: steps }, "The bounded experiment is queued.");
    });
    $("experiment-pause").addEventListener("click", function () { experimentMutation("pause", {}, "The experiment run is stopping."); });
    $("experiment-reset").addEventListener("click", function () { experimentMutation("reset", { seed: 0 }, "The simulated world has been reset."); });
    $("experiment-refresh").addEventListener("click", function () { refreshExperiment().catch(function (error) { announce(error.message, true); }); });
    $("intervention-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var position = [Number($("intervention-x").value), Number($("intervention-y").value)];
      if (!position.every(Number.isInteger)) { announce("Use whole-number grid coordinates.", true); return; }
      experimentMutation("intervene", { kind: "move_object", object_id: $("intervention-object").value, position: position }, "The environment changed. The agent must observe it to update its belief.");
    });
    $("reverse-controls").addEventListener("click", function () { experimentMutation("intervene", { kind: "alter_controls", mapping: { turn_left: "turn_right", turn_right: "turn_left" } }, "Left and right are now reversed in the simulator."); });
    $("restore-controls").addEventListener("click", function () { experimentMutation("intervene", { kind: "alter_controls", mapping: { turn_left: "turn_left", turn_right: "turn_right" } }, "Normal turn controls restored."); });
  }

  function sourceButton(id, label, type) {
    var button = el("button", "evidence-link", label || shortId(id)); button.type = "button";
    button.title = id;
    button.addEventListener("click", function () { showSource(id, type); });
    return button;
  }
  function createMessage(event) {
    var kind = messageKind(event.kind), article = el("article", "message " + kind);
    article.dataset.eventId = event.id;
    var header = el("div", "message-header");
    header.appendChild(el("span", "message-avatar", kind === "assistant" ? "◉" : (kind === "reflection" ? "◇" : (kind === "error" ? "!" : "↗"))));
    header.appendChild(el("span", "", kind === "assistant" ? "AI-AM" : kind === "user" ? "You" : humanize(kind)));
    var time = el("time", "", timeLabel(event.created_at)); if (event.created_at) time.dateTime = event.created_at; header.appendChild(time);
    article.appendChild(header);
    var content = el("div", "message-content"); renderText(content, event.content); article.appendChild(content);
    if (["pending", "queued", "processing", "failed"].indexOf(event.status) >= 0) {
      var status = el("div", "message-status " + (event.status === "failed" ? "failed" : ""));
      status.appendChild(el("span", "", event.status === "failed" ? "This interaction needs another attempt." : (event.status === "processing" ? "Being considered" : "Waiting to be considered")));
      if (event.status === "failed") {
        var retry = el("button", "retry-button", "Retry"); retry.type = "button";
        retry.addEventListener("click", async function () {
          retry.disabled = true;
          try { await api("/api/retry", { event_id: event.id }); await refresh(); announce("The interaction is queued again."); }
          catch (error) { announce(error.message, true); retry.disabled = false; }
        });
        status.appendChild(retry);
        var error = eventError(event);
        if (error) status.appendChild(el("span", "message-error", error));
      }
      article.appendChild(status);
    }
    var evidence = extractEvidence(event);
    if (evidence.length) {
      var links = el("div", "evidence-links");
      evidence.forEach(function (id, index) { links.appendChild(sourceButton(id, "Source " + (index + 1), "auto")); });
      article.appendChild(links);
    }
    return article;
  }
  function renderEvents(next) {
    events = mergeEvents(events, Array.isArray(next) ? next : []);
    var conversation = $("conversation"), nearBottom = conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 110;
    var visible = events.filter(function (event) { return messageKind(event.kind) !== "system"; });
    $("welcome").hidden = visible.length > 0;
    var changed = false, preceding = $("welcome");
    visible.forEach(function (event) {
      var signature = JSON.stringify(event), previous = eventNodes.get(event.id);
      var node = previous && previous.signature === signature ? previous.node : createMessage(event);
      if (!previous || node !== previous.node) {
        if (previous) previous.node.replaceWith(node);
        eventNodes.set(event.id, { node: node, signature: signature }); changed = true;
      }
      // A reconnect may supply older history after the latest snapshot.
      if (preceding.nextSibling !== node) { conversation.insertBefore(node, preceding.nextSibling); changed = true; }
      preceding = node;
    });
    if (changed) {
      if (nearBottom) conversation.scrollTop = conversation.scrollHeight; else $("new-messages").hidden = false;
    }
  }
  async function refresh() {
    if (refreshFlight) return refreshFlight;
    refreshFlight = Promise.all([api("/api/state"), api("/api/events?limit=100")]).then(function (values) {
      renderState(values[0]); renderEvents(values[1].events || []);
    }).finally(function () { refreshFlight = null; });
    return refreshFlight;
  }
  async function loadMemories() {
    var sequence = ++searchSequence, query = $("memory-search").value.trim();
    try {
      var data = await api("/api/memories?query=" + encodeURIComponent(query));
      if (sequence !== searchSequence) return;
      clear($("memory-list"));
      var list = data.memories || [];
      list.forEach(function (memory) {
        memories.set(memory.id, memory);
        var card = el("button", "memory-card"); card.type = "button";
        var meta = el("div", "memory-meta"); meta.appendChild(el("span", "", humanize(memory.kind))); meta.appendChild(el("span", "", shortId(memory.id)));
        card.appendChild(meta); card.appendChild(el("p", "memory-preview", memory.content));
        card.addEventListener("click", function () { showRecord(memory, "memory"); }); $("memory-list").appendChild(card);
      });
      if (!list.length) $("memory-list").appendChild(el("p", "empty-note", query ? "No relevant memory was found." : "Experiences will appear here as the mind learns from your conversation."));
    } catch (error) {
      if (sequence === searchSequence) { clear($("memory-list")); $("memory-list").appendChild(el("p", "empty-note", "Memory is temporarily unavailable.")); }
    }
  }
  function openDialog(dialog) { if (!dialog.open) dialog.showModal(); }
  function showRecord(record, type) {
    $("detail-title").textContent = type === "memory" ? "A recorded experience" : "The original event";
    clear($("detail-body"));
    var dl = el("dl");
    [["Kind", humanize(record.kind)], ["Source", record.source], ["Recorded", record.created_at ? new Date(record.created_at).toLocaleString() : ""], ["Record ID", record.id]].forEach(function (item) {
      if (!item[1]) return; dl.appendChild(el("dt", "", item[0])); dl.appendChild(el("dd", "", item[1]));
    });
    if (record.confidence !== undefined) { dl.appendChild(el("dt", "", "Confidence")); dl.appendChild(el("dd", "", Math.round(Number(record.confidence) * 100) + "% · recorded estimate")); }
    if (record.supersedes) { dl.appendChild(el("dt", "", "Corrects")); var dd = el("dd"); dd.appendChild(sourceButton(record.supersedes, shortId(record.supersedes), "memory")); dl.appendChild(dd); }
    $("detail-body").appendChild(dl);
    var content = el("div", "source-content"); renderText(content, record.content); $("detail-body").appendChild(content);
    if (Array.isArray(record.source_event_ids) && record.source_event_ids.length) {
      $("detail-body").appendChild(el("p", "small-label", "ORIGINAL SOURCES"));
      var links = el("div", "evidence-links");
      record.source_event_ids.forEach(function (id, index) { links.appendChild(sourceButton(id, "Event " + (index + 1) + " · " + shortId(id), "event")); });
      $("detail-body").appendChild(links);
    }
    openDialog($("detail-dialog"));
  }
  async function showSource(id, type) {
    try {
      if (type !== "event" && memories.has(id)) { showRecord(memories.get(id), "memory"); return; }
      var event = events.find(function (item) { return item.id === id; });
      if (type !== "memory" && event) { showRecord(event, "event"); return; }
      var record;
      if (type === "memory") { record = await api("/api/memories/" + encodeURIComponent(id)); showRecord(record.memory || record, "memory"); }
      else if (type === "event") { record = await api("/api/events/" + encodeURIComponent(id)); showRecord(record.event || record, "event"); }
      else {
        try { record = await api("/api/memories/" + encodeURIComponent(id)); showRecord(record.memory || record, "memory"); }
        catch (_) { record = await api("/api/events/" + encodeURIComponent(id)); showRecord(record.event || record, "event"); }
      }
    } catch (error) { announce("The source record could not be opened: " + error.message, true); }
  }
  function connect() {
    if (stopped) return;
    var protocol = location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(protocol + "//" + location.host + "/ws");
    socket.addEventListener("open", function () {
      reconnectAttempts = 0; setConnection(true);
      refresh().catch(function (error) { announce(error.message, true); }); loadMemories();
    });
    socket.addEventListener("message", function (message) {
      var event; try { event = JSON.parse(message.data); } catch (_) { return; }
      var data = event.data || {};
      if (event.type === "state" || event.type === "state_update") renderState(data);
      if (event.type === "role_started") {
        state.status = "thinking"; state.active_role = data.role;
        $("thinking-strip").hidden = false;
        $("thinking-label").textContent = humanize(data.role) + " is contributing";
        $("thinking-round").textContent = data.round ? "ROUND " + data.round : "";
        setRoleAppearance();
      }
      if (event.type === "role_completed") {
        roleResults[data.role] = data.result || {}; renderResult(data.role, data.result, data.elapsed_ms);
        state.active_role = null; setRoleAppearance();
      }
      if (event.type === "cycle_completed") {
        if (data.state) renderState(data.state);
        refresh().catch(function (error) { announce(error.message, true); }); loadMemories();
      }
      if (event.type === "experiment_updated" || event.type === "action_updated") refreshExperiment().catch(function () {});
      if (event.type === "memory_updated") { loadMemories(); refresh().catch(function () {}); }
      if (event.type === "error") {
        announce(data.message || data.error || "The runtime could not complete this interaction. You can retry it.", true);
        refresh().catch(function () {});
      }
    });
    socket.addEventListener("close", function () {
      setConnection(false);
      if (!stopped) {
        var delay = Math.min(15000, 800 * Math.pow(1.6, reconnectAttempts++));
        clearTimeout(reconnectTimer); reconnectTimer = setTimeout(connect, delay);
      }
    });
    socket.addEventListener("error", function () { setConnection(false); });
  }
  async function loadModels() {
    $("models-status").textContent = "Loading installed models…"; $("save-models").disabled = true;
    try {
      var data = await api("/api/models"); models = data.models || [];
      clear($("model-fields"));
      ROLES.forEach(function (role) {
        var field = el("div", "model-field"), label = el("label", "", role.name); label.htmlFor = "model-" + role.id;
        var select = el("select"); select.id = "model-" + role.id; select.name = role.id;
        var selected = modelName((data.roles || state.roles || {})[role.id], state.model);
        var names = models.slice(); if (selected && names.indexOf(selected) < 0) names.unshift(selected);
        if (!names.length) { var option = el("option", "", "No model available"); option.value = ""; select.appendChild(option); }
        names.forEach(function (name) { var option = el("option", "", name + (models.indexOf(name) < 0 ? " (not installed)" : "")); option.value = name; select.appendChild(option); });
        select.value = selected || names[0] || ""; field.appendChild(label); field.appendChild(select); $("model-fields").appendChild(field);
      });
      $("models-status").textContent = data.error ? text(data.error) : models.length ? models.length + " installed model" + (models.length === 1 ? "" : "s") + ". Changes apply between cognitive cycles." : "No local models found. Start Ollama and install a model, then refresh.";
      $("save-models").disabled = !models.length;
    } catch (error) { $("models-status").textContent = error.message; }
  }
  function resizeComposer() { $("message-input").rows = Math.min(6, Math.max(1, $("message-input").value.split("\n").length)); }
  function bindControls() {
    $("composer").addEventListener("submit", async function (event) {
      event.preventDefault(); var value = $("message-input").value.trim(); if (!value || isSending || stopped) return;
      isSending = true; $("send-button").disabled = true;
      try {
        value = validateInput(value, false);
        var queued = await api("/api/input", { text: value }); queued = queued.event || queued;
        if (queued.id) renderEvents([queued]);
        $("message-input").value = ""; resizeComposer(); await refresh();
        $("conversation").scrollTop = $("conversation").scrollHeight;
      } catch (error) { announce(error.message, true); }
      finally { isSending = false; $("send-button").disabled = state.status === "stopped"; $("message-input").focus(); }
    });
    $("message-input").addEventListener("input", resizeComposer);
    $("message-input").addEventListener("keydown", function (event) { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); $("composer").requestSubmit(); } });
    document.querySelectorAll("[data-prompt]").forEach(function (button) {
      button.addEventListener("click", function () { $("message-input").value = button.dataset.prompt; resizeComposer(); $("message-input").focus(); });
    });
    $("pause-button").addEventListener("click", async function () {
      var button = $("pause-button"); button.disabled = true;
      try { await api("/api/control", { action: state.status === "paused" || state.paused ? "resume" : "pause" }); await refresh(); }
      catch (error) { announce(error.message, true); }
      finally { button.disabled = state.status === "stopped"; }
    });
    $("background-toggle").addEventListener("change", async function () {
      var desired = $("background-toggle").checked; $("background-toggle").disabled = true;
      try { await api("/api/background", { enabled: desired }); await refresh(); announce(desired ? "Background reflection enabled." : "Background reflection paused."); }
      catch (error) { $("background-toggle").checked = !desired; announce(error.message, true); }
      finally { $("background-toggle").disabled = false; }
    });
    $("reflect-button").addEventListener("click", function () { openDialog($("reflect-dialog")); $("reflect-topic").focus(); });
    $("reflect-form").addEventListener("submit", async function (event) {
      event.preventDefault(); var button = event.submitter; if (button) button.disabled = true;
      try { await api("/api/reflect", { topic: validateInput($("reflect-topic").value, true) }); $("reflect-dialog").close(); $("reflect-topic").value = ""; await refresh(); announce("Reflection queued."); }
      catch (error) { announce(error.message, true); }
      finally { if (button) button.disabled = false; }
    });
    $("consolidate-button").addEventListener("click", async function () {
      var button = $("consolidate-button"); button.disabled = true;
      try { var result = await api("/api/consolidate", {}); await Promise.all([refresh(), loadMemories()]); announce(result.message || "Memory consolidation completed. Original experiences are preserved."); }
      catch (error) { announce(error.message, true); }
      finally { button.disabled = false; }
    });
    $("memory-search").addEventListener("input", function () { clearTimeout(memoryTimer); memoryTimer = setTimeout(loadMemories, 250); });
    $("models-button").addEventListener("click", function () { openDialog($("models-dialog")); loadModels(); });
    $("refresh-models").addEventListener("click", loadModels);
    $("models-form").addEventListener("submit", async function (event) {
      event.preventDefault(); $("save-models").disabled = true;
      try {
        for (var role of ROLES) {
          var selected = $("model-" + role.id).value;
          if (!selected || models.indexOf(selected) < 0) throw new Error("Select an installed model for " + role.name + ".");
        }
        for (var item of ROLES) {
          var choice = $("model-" + item.id).value;
          if (choice !== modelName((state.roles || {})[item.id])) await api("/api/model/select", { role: item.id, model: choice });
        }
        await refresh(); $("models-dialog").close(); announce("Faculty models updated.");
      } catch (error) { $("models-status").textContent = error.message; await refresh().catch(function () {}); }
      finally { $("save-models").disabled = !models.length; }
    });
    document.querySelectorAll(".modal-close").forEach(function (button) { button.addEventListener("click", function () { button.closest("dialog").close(); }); });
    document.querySelectorAll("dialog").forEach(function (dialog) { dialog.addEventListener("click", function (event) { if (event.target === dialog) { var bounds = dialog.getBoundingClientRect(); if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close(); } }); });
    document.querySelectorAll("[data-panel]").forEach(function (button) {
      if (button.tagName !== "BUTTON") return;
      button.addEventListener("click", function () {
        document.querySelector(".observatory").dataset.panel = button.dataset.panel;
        if (button.dataset.panel === "experiment") refreshExperiment().catch(function (error) { announce(error.message, true); });
        document.querySelectorAll(".mobile-tabs button").forEach(function (tab) { tab.setAttribute("aria-pressed", String(tab.dataset.panel === button.dataset.panel)); });
      });
    });
    $("dismiss-alert").addEventListener("click", function () { $("alert-banner").hidden = true; });
    $("new-messages").addEventListener("click", function () { $("conversation").scrollTop = $("conversation").scrollHeight; $("new-messages").hidden = true; });
    $("conversation").addEventListener("scroll", function () { var conversation = $("conversation"); if (conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 110) $("new-messages").hidden = true; });
    document.addEventListener("visibilitychange", function () { if (!document.hidden) { refresh().catch(function () {}); loadMemories(); } });
    window.addEventListener("beforeunload", function () { stopped = true; clearTimeout(reconnectTimer); if (socket) socket.close(); });
  }
  createFaculties(); bindControls(); bindExperimentControls();
  refresh().catch(function (error) { announce(error.message, true); });
  loadMemories(); connect();
  refreshExperiment().catch(function () { $("world-status").textContent = "The experiment is unavailable. Refresh after the runtime is ready."; });
})(typeof globalThis !== "undefined" ? globalThis : this);
