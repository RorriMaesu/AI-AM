// app.js
// Client-side controller for Project Antahkarana Dashboard UI

let socket = null;
let network = null;
let nodesDataset = new vis.DataSet([]);
let edgesDataset = new vis.DataSet([]);

// DOM elements
const statusDot = document.getElementById('connection-status');
const stateBadge = document.getElementById('operational-state-badge');
const arousalBar = document.getElementById('arousal-bar');
const arousalVal = document.getElementById('arousal-val');
const fatigueBar = document.getElementById('fatigue-bar');
const fatigueVal = document.getElementById('fatigue-val');
const curiosityBar = document.getElementById('curiosity-bar');
const curiosityVal = document.getElementById('curiosity-val');
const modelSelect = document.getElementById('model-select');
const modelApplyBtn = document.getElementById('model-apply-btn');
const runtimeStopBtn = document.getElementById('runtime-stop-btn');
const modelStatus = document.getElementById('model-status');

const chatMessages = document.getElementById('chat-messages');
const chatForm = document.getElementById('chat-form');
const chatInput = document.getElementById('chat-input');
const sendBtn = document.getElementById('send-btn');

const manasSlot = document.getElementById('telemetry-manas');
const chittaSlot = document.getElementById('telemetry-chitta');
const ahamkaraSlot = document.getElementById('telemetry-ahamkara');
const buddhiSlot = document.getElementById('telemetry-buddhi');

const sandboxConsole = document.getElementById('sandbox-console');
const ledgerLogs = document.getElementById('ledger-logs');

const browserPreviewFrame = document.getElementById('browser-preview-frame');
const browserFrame = document.getElementById('browser-frame');
const browserFrameEmpty = document.getElementById('browser-frame-empty');
const browserSessionStatus = document.getElementById('browser-session-status');
const browserCurrentUrl = document.getElementById('browser-current-url');
const browserActionLog = document.getElementById('browser-action-log');
const browserGoalInput = document.getElementById('browser-goal-input');
const browserUrlInput = document.getElementById('browser-url-input');
const browserEmbeddedToggle = document.getElementById('browser-embedded-toggle');
const browserStartBtn = document.getElementById('browser-start-btn');
const browserPauseBtn = document.getElementById('browser-pause-btn');
const browserResumeBtn = document.getElementById('browser-resume-btn');
const browserStopBtn = document.getElementById('browser-stop-btn');
const layoutExpandAllBtn = document.getElementById('layout-expand-all-btn');
const layoutCollapseAllBtn = document.getElementById('layout-collapse-all-btn');
const layoutResetBtn = document.getElementById('layout-reset-btn');

const PANEL_STATE_STORAGE_KEY = 'aiam.dashboard.panelState.v1';
const PANEL_STACKED_MEDIA_QUERY = '(max-width: 1200px)';
const DEFAULT_OPEN_BY_COLUMN = {
    left: 'chat',
    right: 'telemetry',
};

let knownModels = [];
let activeModel = null;
let shutdownRequested = false;
let shutdownInFlight = false;
let panelCollapseState = {};
let currentBrowserSession = null;
let latestBrowserFrame = null;

function getAllCollapsiblePanels() {
    return Array.from(document.querySelectorAll('.collapsible-panel'));
}

function isStackedPanelMode() {
    return window.matchMedia(PANEL_STACKED_MEDIA_QUERY).matches;
}

function readPanelCollapseState() {
    try {
        const raw = window.localStorage.getItem(PANEL_STATE_STORAGE_KEY);
        if (!raw) return {};
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed === 'object' ? parsed : {};
    } catch (_err) {
        return {};
    }
}

function writePanelCollapseState() {
    try {
        window.localStorage.setItem(PANEL_STATE_STORAGE_KEY, JSON.stringify(panelCollapseState));
    } catch (_err) {
        // Intentionally ignore storage failures to keep runtime UI responsive.
    }
}

function refreshPanelSurface(panel) {
    const panelKey = panel?.dataset?.panelKey || '';
    if (panelKey === 'graph' && network) {
        setTimeout(() => {
            try {
                network.fit({ animation: { duration: 160 } });
            } catch (_err) {
                // Ignore graph refresh failures if graph is not initialized yet.
            }
        }, 140);
    }
}

function applyPanelCollapsedState(panel, isCollapsed, shouldPersist = true) {
    const key = panel.dataset.panelKey || '';
    const toggleBtn = panel.querySelector('.panel-toggle-btn');
    const title = panel.querySelector('.panel-header h2')?.textContent?.trim() || 'panel';
    const bodyId = toggleBtn?.getAttribute('aria-controls') || '';
    const body = bodyId ? document.getElementById(bodyId) : null;

    panel.classList.toggle('is-collapsed', isCollapsed);

    if (toggleBtn) {
        toggleBtn.textContent = isCollapsed ? 'Expand' : 'Collapse';
        toggleBtn.setAttribute('aria-expanded', String(!isCollapsed));
        toggleBtn.setAttribute('aria-label', `${isCollapsed ? 'Expand' : 'Collapse'} ${title} panel`);
    }
    if (body) {
        body.setAttribute('aria-hidden', String(isCollapsed));
    }

    if (shouldPersist && key) {
        panelCollapseState[key] = isCollapsed;
    }

    if (!isCollapsed) {
        refreshPanelSurface(panel);
    }
}

function ensureAtLeastOnePanelOpenPerColumn(shouldPersist = true) {
    const allPanels = getAllCollapsiblePanels();
    if (isStackedPanelMode()) {
        const openPanels = allPanels.filter((panel) => !panel.classList.contains('is-collapsed'));
        if (openPanels.length === 0) {
            const preferredPanel = allPanels.find((panel) => panel.dataset.panelKey === 'telemetry') || allPanels[0];
            if (preferredPanel) {
                applyPanelCollapsedState(preferredPanel, false, shouldPersist);
            }
        } else if (openPanels.length > 1) {
            const preferredOpen = openPanels.find((panel) => panel.dataset.panelKey === 'telemetry') || openPanels[0];
            allPanels.forEach((panel) => {
                applyPanelCollapsedState(panel, panel !== preferredOpen, shouldPersist);
            });
        }
        return;
    }

    const groupedByColumn = allPanels.reduce((acc, panel) => {
        const column = panel.dataset.column || 'default';
        if (!acc[column]) {
            acc[column] = [];
        }
        acc[column].push(panel);
        return acc;
    }, {});

    Object.entries(groupedByColumn).forEach(([column, panels]) => {
        const hasOpenPanel = panels.some((panel) => !panel.classList.contains('is-collapsed'));
        if (hasOpenPanel) {
            return;
        }
        const preferredKey = DEFAULT_OPEN_BY_COLUMN[column] || '';
        const preferredPanel = panels.find((panel) => panel.dataset.panelKey === preferredKey);
        applyPanelCollapsedState(preferredPanel || panels[0], false, shouldPersist);
    });
}

function recomputePanelLayout() {
    const allPanels = getAllCollapsiblePanels();
    const openByColumn = {};

    allPanels.forEach((panel) => {
        panel.classList.remove('is-solo-open');
        if (panel.classList.contains('is-collapsed')) {
            panel.style.removeProperty('--panel-flex-active');
            return;
        }
        const column = panel.dataset.column || 'default';
        if (!openByColumn[column]) {
            openByColumn[column] = [];
        }
        openByColumn[column].push(panel);
    });

    Object.values(openByColumn).forEach((panels) => {
        if (panels.length === 1) {
            panels[0].classList.add('is-solo-open');
        }
        panels.forEach((panel) => {
            const weight = Number.parseFloat(panel.dataset.weight || '1');
            const safeWeight = Number.isFinite(weight) ? Math.max(0.2, weight) : 1;
            panel.style.setProperty('--panel-flex-active', String(safeWeight));
        });
    });
}

function setPanelCollapsed(panel, isCollapsed, shouldPersist = true) {
    if (!isCollapsed && isStackedPanelMode()) {
        getAllCollapsiblePanels().forEach((otherPanel) => {
            if (otherPanel === panel) return;
            if (otherPanel.classList.contains('is-collapsed')) return;
            applyPanelCollapsedState(otherPanel, true, shouldPersist);
        });
    }

    applyPanelCollapsedState(panel, isCollapsed, shouldPersist);
    ensureAtLeastOnePanelOpenPerColumn(shouldPersist);
    if (shouldPersist) {
        writePanelCollapseState();
    }
    recomputePanelLayout();
}

function setAllPanelsCollapsedState(isCollapsed, forceDefaultRecovery = true) {
    const allPanels = getAllCollapsiblePanels();

    if (!isCollapsed) {
        if (isStackedPanelMode()) {
            const preferredPanel = allPanels.find((panel) => panel.dataset.panelKey === 'telemetry') || allPanels[0];
            allPanels.forEach((panel) => {
                applyPanelCollapsedState(panel, panel !== preferredPanel, true);
            });
        } else {
            allPanels.forEach((panel) => {
                applyPanelCollapsedState(panel, false, true);
            });
        }
    } else {
        allPanels.forEach((panel) => {
            applyPanelCollapsedState(panel, true, true);
        });
        if (forceDefaultRecovery) {
            ensureAtLeastOnePanelOpenPerColumn(true);
        }
    }

    writePanelCollapseState();
    recomputePanelLayout();
}

function resetPanelLayoutDefaults() {
    getAllCollapsiblePanels().forEach((panel) => {
        const column = panel.dataset.column || 'default';
        const panelKey = panel.dataset.panelKey || '';
        const shouldOpen = DEFAULT_OPEN_BY_COLUMN[column] === panelKey;
        applyPanelCollapsedState(panel, !shouldOpen, true);
    });
    ensureAtLeastOnePanelOpenPerColumn(true);
    writePanelCollapseState();
    recomputePanelLayout();
}

function normalizePanelsForViewport() {
    ensureAtLeastOnePanelOpenPerColumn(true);
    writePanelCollapseState();
    recomputePanelLayout();
}

function initCollapsiblePanels() {
    panelCollapseState = readPanelCollapseState();
    const allPanels = getAllCollapsiblePanels();

    allPanels.forEach((panel) => {
        const key = panel.dataset.panelKey || '';
        const toggleBtn = panel.querySelector('.panel-toggle-btn');
        if (!toggleBtn) {
            return;
        }

        const initialCollapsed = Boolean(key && panelCollapseState[key]);
        applyPanelCollapsedState(panel, initialCollapsed, false);

        toggleBtn.addEventListener('click', () => {
            const currentlyCollapsed = panel.classList.contains('is-collapsed');
            setPanelCollapsed(panel, !currentlyCollapsed, true);
        });
    });

    if (layoutExpandAllBtn) {
        layoutExpandAllBtn.addEventListener('click', () => {
            setAllPanelsCollapsedState(false, false);
        });
    }

    if (layoutCollapseAllBtn) {
        layoutCollapseAllBtn.addEventListener('click', () => {
            setAllPanelsCollapsedState(true, true);
        });
    }

    if (layoutResetBtn) {
        layoutResetBtn.addEventListener('click', () => {
            resetPanelLayoutDefaults();
        });
    }

    ensureAtLeastOnePanelOpenPerColumn(false);
    recomputePanelLayout();

    const mediaQueryList = window.matchMedia(PANEL_STACKED_MEDIA_QUERY);
    if (typeof mediaQueryList.addEventListener === 'function') {
        mediaQueryList.addEventListener('change', normalizePanelsForViewport);
    } else if (typeof mediaQueryList.addListener === 'function') {
        mediaQueryList.addListener(normalizePanelsForViewport);
    }
    normalizePanelsForViewport();
}

function setInteractiveControlsEnabled(isEnabled) {
    if (chatInput) {
        chatInput.disabled = !isEnabled;
    }
    if (sendBtn) {
        sendBtn.disabled = !isEnabled;
    }
    if (modelSelect) {
        modelSelect.disabled = !isEnabled;
    }
    if (modelApplyBtn) {
        modelApplyBtn.disabled = !isEnabled;
    }
    if (runtimeStopBtn) {
        runtimeStopBtn.disabled = !isEnabled || shutdownInFlight;
    }
    if (browserStartBtn) {
        browserStartBtn.disabled = !isEnabled;
    }
    if (browserPauseBtn) {
        browserPauseBtn.disabled = !isEnabled;
    }
    if (browserResumeBtn) {
        browserResumeBtn.disabled = !isEnabled;
    }
    if (browserStopBtn) {
        browserStopBtn.disabled = !isEnabled;
    }
    if (browserEmbeddedToggle) {
        browserEmbeddedToggle.disabled = !isEnabled;
    }
}

function applyShutdownState(message) {
    if (shutdownRequested) return;

    shutdownRequested = true;
    setInteractiveControlsEnabled(false);
    stateBadge.textContent = 'Shutting Down';
    stateBadge.className = 'state-badge badge-stopping';
    appendLedgerLog('system', message || 'Graceful shutdown requested. Stopping runtime loops...');

    const shutdownNotice = document.createElement('div');
    shutdownNotice.className = 'system-message';
    shutdownNotice.textContent = 'Shutdown in progress. Runtime controls are disabled.';
    chatMessages.appendChild(shutdownNotice);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

// Initialize Vis.js Graph visualizer
function initNetwork() {
    const container = document.getElementById('memory-graph-container');
    const data = {
        nodes: nodesDataset,
        edges: edgesDataset
    };
    const options = {
        nodes: {
            shape: 'dot',
            size: 16,
            font: {
                color: '#f8fafc',
                size: 12,
                face: 'Outfit'
            },
            borderWidth: 2,
            shadow: true
        },
        edges: {
            width: 2,
            color: { color: '#475569', highlight: '#06b6d4', hover: '#06b6d4' },
            arrows: {
                to: { enabled: true, scaleFactor: 0.5 }
            },
            smooth: {
                enabled: true,
                type: 'dynamic',
                roundness: 0.5
            }
        },
        physics: {
            barnesHut: {
                gravitationalConstant: -2000,
                centralGravity: 0.3,
                springLength: 95,
                springConstant: 0.04,
                damping: 0.09,
                avoidOverlap: 1
            },
            stabilization: { iterations: 150, updateInterval: 25 }
        },
        interaction: {
            hover: true,
            tooltipDelay: 200
        }
    };
    network = new vis.Network(container, data, options);
}

// WebSocket Connection Management
function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws`;
    
    appendLedgerLog('system', 'Attempting WebSocket connection...');
    socket = new WebSocket(wsUrl);

    socket.onopen = () => {
        statusDot.className = 'status-dot dot-online';
        appendLedgerLog('system', 'WebSocket connection established.');
    };

    socket.onclose = () => {
        statusDot.className = 'status-dot dot-offline';
        if (shutdownRequested) {
            appendLedgerLog('system', 'Runtime stopped. WebSocket session closed after shutdown.');
            stateBadge.textContent = 'Stopped';
            stateBadge.className = 'state-badge badge-stopping';
            return;
        }

        appendLedgerLog('failover', 'WebSocket connection closed. Retrying in 3 seconds...');
        setTimeout(connectWebSocket, 3000);
    };

    socket.onerror = (error) => {
        statusDot.className = 'status-dot dot-offline';
        appendLedgerLog('failover', 'WebSocket error encountered: ' + error.message);
    };

    socket.onmessage = (event) => {
        try {
            const payload = JSON.parse(event.data);
            handleServerEvent(payload);
        } catch (err) {
            console.error('Failed to parse WebSocket message:', err);
        }
    };
}

// Parse server events and update UI panels
function handleServerEvent(event) {
    const { type, data, timestamp } = event;
    
    switch (type) {
        case 'state_update':
            updateMetacognition(data);
            syncModelFromState(data);
            break;
            
        case 'cycle_started':
            startCycleUpdate(data);
            break;
            
        case 'timeline_update':
            updateTimelineSlot(data.layer, data.content);
            break;

        case 'chat_message':
            if (data?.sender === 'agent' && data?.content) {
                appendChatBubble('agent', data.content);
                if (data?.mode === 'autonomous') {
                    appendLedgerLog('system', `Autonomous message emitted (${data?.reason || 'policy_ok'}).`);
                }
            }
            break;

        case 'chat_message_suppressed':
            appendLedgerLog('system', `Autonomous message suppressed (${data?.reason || 'policy'}).`);
            break;
            
        case 'curiosity_search':
            handleCuriositySearch(data);
            break;
            
        case 'sandbox_log':
            handleSandboxExecution(data);
            break;

        case 'tool_intent_planned':
            handleToolPolicyTelemetry('planned', data);
            break;

        case 'tool_policy_decision':
            handleToolPolicyTelemetry('policy', data);
            break;

        case 'tool_policy_denied':
            handleToolPolicyTelemetry('denied', data);
            break;

        case 'tool_execution_started':
            handleToolPolicyTelemetry('started', data);
            break;

        case 'tool_execution_completed':
            handleToolPolicyTelemetry('completed', data);
            break;

        case 'tool_execution_failed':
            handleToolPolicyTelemetry('failed', data);
            break;

        case 'mind_contract_update':
            handleMindContractUpdate(data);
            break;

        case 'mind_arbitration':
            handleMindArbitration(data);
            break;

        case 'mind_identity_gate':
            handleMindIdentityGate(data);
            break;

        case 'mind_negotiation':
            handleMindNegotiation(data);
            break;
            
        case 'nidra_triggered':
            handleNidraStatus('sleep', data);
            break;
            
        case 'nidra_completed':
            handleNidraStatus('wake', data);
            break;

        case 'shutdown_initiated':
            applyShutdownState('Graceful shutdown initiated by runtime controller.');
            break;

        case 'browser_state':
            handleBrowserStateSnapshot(data);
            break;

        case 'browser_session_started':
            handleBrowserSessionEvent('started', data);
            break;

        case 'browser_session_paused':
            handleBrowserSessionEvent('paused', data);
            break;

        case 'browser_session_resumed':
            handleBrowserSessionEvent('resumed', data);
            break;

        case 'browser_session_stopped':
            handleBrowserSessionEvent('stopped', data);
            break;

        case 'browser_action_planned':
            appendBrowserLog(`Planned: ${JSON.stringify(data)}`, 'normal');
            break;

        case 'browser_action_executed':
            handleBrowserActionEvent(data);
            break;

        case 'browser_guardrail_blocked':
            appendBrowserLog(`Blocked: ${data.reason || 'Guardrail rule hit'}`, 'failover');
            break;

        case 'browser_frame':
            renderBrowserFrame(data);
            break;

        case 'browser_vision_update':
            appendBrowserLog(`Vision: ${data.content || data.reason || 'no output'}`, data.status === 'ok' ? 'system' : 'failover');
            break;

        case 'browser_mode_changed':
            if (browserEmbeddedToggle) {
                browserEmbeddedToggle.checked = Boolean(data?.prefer_embedded_preview);
            }
            appendBrowserLog(`Mode changed to ${data?.transport || 'unknown'}.`, 'system');
            break;
            
        default:
            console.log('Unhandled event type:', type);
    }
}

function setModelStatus(text, isError = false) {
    if (!modelStatus) return;
    modelStatus.textContent = text;
    modelStatus.style.color = isError ? '#f87171' : '#94a3b8';
}

function renderModelOptions(models, selectedModel) {
    if (!modelSelect) return;
    modelSelect.innerHTML = '';

    if (!models || models.length === 0) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = 'No models found';
        modelSelect.appendChild(option);
        modelSelect.disabled = true;
        if (modelApplyBtn) {
            modelApplyBtn.disabled = true;
        }
        return;
    }

    modelSelect.disabled = false;
    models.forEach((model) => {
        const option = document.createElement('option');
        option.value = model;
        option.textContent = model;
        modelSelect.appendChild(option);
    });

    if (selectedModel && models.includes(selectedModel)) {
        modelSelect.value = selectedModel;
    }

    if (modelApplyBtn) {
        modelApplyBtn.disabled = false;
    }
}

function syncModelFromState(state) {
    const runtimeModel = state?.llm_parameters?.model_name;
    if (!runtimeModel) return;

    activeModel = runtimeModel;
    if (modelSelect && knownModels.includes(runtimeModel)) {
        modelSelect.value = runtimeModel;
    }
    setModelStatus(`Active: ${runtimeModel}`);
}

async function loadModelInventory() {
    if (!modelSelect || !modelApplyBtn) return;

    modelApplyBtn.disabled = true;
    setModelStatus('Loading model tags...');

    try {
        const response = await fetch('/api/models');
        const payload = await response.json();
        if (!response.ok) {
            throw new Error(payload?.message || `HTTP ${response.status}`);
        }

        knownModels = Array.isArray(payload.models) ? payload.models : [];
        activeModel = payload.active_model || null;
        renderModelOptions(knownModels, activeModel);

        if (activeModel) {
            setModelStatus(`Active: ${activeModel}`);
        } else {
            setModelStatus('Active model unknown');
        }
    } catch (error) {
        renderModelOptions([], null);
        setModelStatus(`Model load failed: ${error.message}`, true);
        appendLedgerLog('failover', `Failed to load models from backend: ${error.message}`);
    }
}

async function applySelectedModel() {
    if (!modelSelect || !modelApplyBtn) return;
    if (shutdownRequested) return;
    const requestedModel = modelSelect.value;
    if (!requestedModel) return;

    modelApplyBtn.disabled = true;
    setModelStatus(`Switching to ${requestedModel}...`);

    try {
        const response = await fetch('/api/model/select', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ model: requestedModel })
        });
        const payload = await response.json();
        if (!response.ok || payload.status !== 'ok') {
            throw new Error(payload?.message || `HTTP ${response.status}`);
        }

        activeModel = payload.active_model || requestedModel;
        setModelStatus(`Active: ${activeModel}`);
        appendLedgerLog('system', `Model switched to '${activeModel}'.`);

        if (payload.warning) {
            appendLedgerLog('failover', payload.warning);
        }

        await loadModelInventory();
    } catch (error) {
        setModelStatus(`Switch failed: ${error.message}`, true);
        appendLedgerLog('failover', `Model switch failed: ${error.message}`);
    } finally {
        modelApplyBtn.disabled = false;
    }
}

async function requestRuntimeStop() {
    if (!runtimeStopBtn || shutdownInFlight || shutdownRequested) return;

    shutdownInFlight = true;
    runtimeStopBtn.disabled = true;
    applyShutdownState('Requesting graceful shutdown from dashboard...');

    try {
        const response = await fetch('/api/stop', { method: 'POST' });
        const payload = await response.json();
        if (!response.ok) {
            throw new Error(payload?.message || `HTTP ${response.status}`);
        }

        appendLedgerLog('system', payload.message || 'Graceful shutdown acknowledged by backend.');
    } catch (error) {
        appendLedgerLog('failover', `Shutdown request failed: ${error.message}`);
    } finally {
        shutdownInFlight = false;
    }
}

// Update metacognition gauges and configuration values
function updateMetacognition(state) {
    if (!state || !state.metacognition) return;
    
    const meta = state.metacognition;
    const arousal = meta.arousal_index || 0.0;
    const fatigue = meta.mental_fatigue || 0.0;
    const curiosity = meta.curiosity_index || 0.0;
    const opState = meta.operational_state || 'Idle';

    // Update state badge class
    stateBadge.textContent = opState;
    if (opState.toLowerCase().includes('waking') || opState.toLowerCase().includes('pramana')) {
        stateBadge.className = 'state-badge badge-waking';
    } else if (opState.toLowerCase().includes('dreaming') || opState.toLowerCase().includes('vikalpa')) {
        stateBadge.className = 'state-badge badge-dreaming';
    } else {
        stateBadge.className = 'state-badge badge-sleeping';
    }

    // Update progress bars
    arousalBar.style.width = `${arousal * 100}%`;
    arousalVal.textContent = arousal.toFixed(2);
    
    fatigueBar.style.width = `${fatigue * 100}%`;
    fatigueVal.textContent = fatigue.toFixed(2);
    
    curiosityBar.style.width = `${curiosity * 100}%`;
    curiosityVal.textContent = curiosity.toFixed(2);
}

// Start visual sequence of a cognitive cycle
function startCycleUpdate(data) {
    const { heartbeat_id, stimulus, operational_state, arousal_index, mental_fatigue, curiosity_index } = data;
    
    // Reset slot classes & values
    document.querySelectorAll('.telemetry-slot').forEach(slot => {
        slot.className = slot.className.split(' ').filter(c => !c.startsWith('active-')).join(' ');
    });
    
    manasSlot.textContent = "Sensory processing active...";
    manasSlot.className = "slot-content empty-content";
    
    chittaSlot.textContent = "Memory matrix searching...";
    chittaSlot.className = "slot-content empty-content";
    
    ahamkaraSlot.textContent = "Identity filtering active...";
    ahamkaraSlot.className = "slot-content empty-content";
    
    buddhiSlot.textContent = "Executive resolution active...";
    buddhiSlot.className = "slot-content empty-content";

    // Update metrics bar values immediately
    updateMetacognition({
        metacognition: {
            operational_state,
            arousal_index,
            mental_fatigue,
            curiosity_index
        }
    });

    // Append system message to chat loop
    const systemDiv = document.createElement('div');
    systemDiv.className = 'system-message';
    systemDiv.textContent = `Heartbeat #${heartbeat_id} | State: ${operational_state}`;
    chatMessages.appendChild(systemDiv);
    chatMessages.scrollTop = chatMessages.scrollHeight;

    // Append user message if it is not subconscious reflection
    if (stimulus && !stimulus.startsWith('[Subconscious Stream')) {
        appendChatBubble('user', stimulus);
    }
    
    appendLedgerLog('normal', `Heartbeat #${heartbeat_id} initialized. Stimulus: ${stimulus.substring(0, 45)}...`);
}

// Bind live text streams to specific slot panels
function updateTimelineSlot(layer, content) {
    const slotMap = {
        'manas': { slot: manasSlot, parent: manasSlot.parentElement, activeClass: 'active-manas' },
        'chitta': { slot: chittaSlot, parent: chittaSlot.parentElement, activeClass: 'active-chitta' },
        'ahamkara': { slot: ahamkaraSlot, parent: ahamkaraSlot.parentElement, activeClass: 'active-ahamkara' },
        'buddhi': { slot: buddhiSlot, parent: buddhiSlot.parentElement, activeClass: 'active-buddhi' }
    };
    
    const item = slotMap[layer];
    if (!item) return;

    item.slot.textContent = content;
    item.slot.className = "slot-content";
    item.parent.classList.add(item.activeClass);

    // Scroll slot view
    item.slot.scrollTop = item.slot.scrollHeight;

    // Specific logic updates for Chitta database node connections
    if (layer === 'chitta') {
        parseAndRenderGraph(content);
    }

}

// Parse Chitta GraphRAG context strings and render node networks
function parseAndRenderGraph(chittaContext) {
    const nodes = [];
    const edges = [];
    
    // Regular Expression helpers for node and edge matching
    const nodeRegex = /Concept\s+Node\s+\[([^\]]+)\]:\s+"([^"]+)"\s+\(Similarity:\s+([0-9.]+),\s+Recorded\s+Arousal:\s+([0-9.]+)\)/gi;
    const edgeRegex = /Pathway:\s+\[([^\]]+)\]\s+--\(Weight:\s+([0-9.]+)\)(?:\s+Context:\s+'([^']+)')?-->\s+\[([^\]]+)\]/gi;
    
    let match;
    const seenNodes = new Set();
    
    // Extract memory nodes
    while ((match = nodeRegex.exec(chittaContext)) !== null) {
        const id = match[1];
        const text = match[2];
        const similarity = parseFloat(match[3]);
        const arousal = parseFloat(match[4]);
        seenNodes.add(id);

        // Adjust color dynamically based on similarity score (glowing emerald)
        const greenIntensity = Math.floor(similarity * 155) + 100;
        const colorVal = `rgb(16, ${greenIntensity}, 129)`;
        
        nodes.push({
            id: id,
            label: id,
            title: `Content: ${text.substring(0, 150)}...\nSimilarity: ${similarity}\nArousal: ${arousal}`,
            color: {
                background: '#090d16',
                border: colorVal,
                highlight: { background: '#090d16', border: '#06b6d4' }
            },
            borderWidth: 2,
            size: 10 + (similarity * 15)
        });
    }

    // Extract paths
    while ((match = edgeRegex.exec(chittaContext)) !== null) {
        const source = match[1];
        const weight = parseFloat(match[2]);
        const context = match[3] || 'relation';
        const target = match[4];

        edges.push({
            from: source,
            to: target,
            value: weight,
            title: `Context: ${context}\nWeight: ${weight}`,
            arrows: 'to'
        });
    }

    // Fallback node to keep network initialized
    if (nodes.length === 0) {
        return;
    }

    // Refresh Dataset model
    nodesDataset.clear();
    edgesDataset.clear();
    
    nodesDataset.add(nodes);
    edgesDataset.add(edges);
    
    // Stabilize visual canvas layout
    if (network) {
        network.fit();
    }
}

// Display search operations inside the journal
function handleCuriositySearch(data) {
    const { query, results } = data;
    appendLedgerLog('search', `Curiosity search run: "${query}"`);
    
    const logDiv = document.createElement('div');
    logDiv.className = 'system-message search-alert';
    logDiv.innerHTML = `<span style="color:#10b981;font-weight:600;">Jijnasa Curiosity Search:</span> "${query}"`;
    chatMessages.appendChild(logDiv);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

// Print container logging outputs to console panel
function handleSandboxExecution(data) {
    const { code, output } = data;
    const block = document.createElement('div');
    block.className = 'sandbox-exec-block';
    block.innerHTML = `
        <span class="console-prompt">BUDDHI Code Directive:</span>
        <pre><code class="language-python">${escapeHtml(code)}</code></pre>
        <span class="console-prompt">Karmendriya Container Output:</span>
        <div class="console-output">${escapeHtml(output)}</div>
    `;
    sandboxConsole.appendChild(block);

    while (sandboxConsole.children.length > 40) {
        sandboxConsole.removeChild(sandboxConsole.firstChild);
    }

    sandboxConsole.scrollTop = sandboxConsole.scrollHeight;
    appendLedgerLog('novelty', 'Karmendriya Docker sandbox execution completed.');
}

function appendSandboxTelemetry(text, tone = 'normal') {
    if (!sandboxConsole) return;
    const entry = document.createElement('div');
    entry.className = `console-meta ${tone}`;
    const time = new Date().toLocaleTimeString();
    entry.textContent = `[${time}] ${text}`;
    sandboxConsole.appendChild(entry);

    while (sandboxConsole.children.length > 40) {
        sandboxConsole.removeChild(sandboxConsole.firstChild);
    }
    sandboxConsole.scrollTop = sandboxConsole.scrollHeight;
}

function handleToolPolicyTelemetry(eventKind, payload) {
    const intent = payload?.intent || {};
    const intentId = intent?.intent_id || 'unknown-intent';
    const toolType = intent?.tool_type || 'tool';

    if (eventKind === 'planned') {
        const codeLength = intent?.payload?.code_length || 0;
        appendLedgerLog('system', `Tool intent planned: ${intentId} (${toolType}, ${codeLength} chars).`);
        appendSandboxTelemetry(`Tool intent planned: ${intentId} (${toolType}).`, 'system');
        return;
    }

    if (eventKind === 'policy') {
        const decision = payload?.decision || 'unknown';
        const reasons = Array.isArray(payload?.reasons) ? payload.reasons.join('; ') : '';
        const tone = decision === 'allow' ? 'normal' : 'failover';
        appendLedgerLog(tone, `Tool policy decision for ${intentId}: ${decision}${reasons ? ` | ${reasons}` : ''}`);
        appendSandboxTelemetry(`Policy ${decision.toUpperCase()} for ${intentId}${reasons ? ` -> ${reasons}` : ''}`, tone);
        return;
    }

    if (eventKind === 'denied') {
        const reasons = Array.isArray(payload?.reasons) ? payload.reasons.join('; ') : 'No reason provided';
        appendLedgerLog('failover', `Tool execution denied by policy (${intentId}): ${reasons}`);
        appendSandboxTelemetry(`Execution denied for ${intentId}: ${reasons}`, 'failover');
        return;
    }

    if (eventKind === 'started') {
        appendLedgerLog('system', `Tool execution started: ${intentId}`);
        appendSandboxTelemetry(`Execution started for ${intentId}.`, 'system');
        return;
    }

    if (eventKind === 'completed') {
        appendLedgerLog('novelty', `Tool execution completed: ${intentId}`);
        appendSandboxTelemetry(`Execution completed for ${intentId}.`, 'normal');
        return;
    }

    if (eventKind === 'failed') {
        appendLedgerLog('failover', `Tool execution failed: ${intentId}`);
        appendSandboxTelemetry(`Execution failed for ${intentId}.`, 'failover');
    }
}

function handleMindContractUpdate(payload) {
    const cycleId = payload?.cycle_id || 'cycle_unknown';
    const salience = payload?.salience || {};
    const conflicts = Array.isArray(payload?.conflicts) ? payload.conflicts : [];
    const roles = payload?.roles || {};
    const intentChannels = payload?.intent_channels || {};
    const roleModulation = payload?.role_modulation || {};
    const mm = payload?.multimodal_evidence || {};

    const summary = [
        `Mind contract ${cycleId}`,
        `S=${Number(salience?.composite || 0).toFixed(2)}`,
        `N=${Number(salience?.novelty || 0).toFixed(2)}`,
        `U=${Number(salience?.urgency || 0).toFixed(2)}`,
        `I=${Number(salience?.identity_threat || 0).toFixed(2)}`,
        `conflicts=${conflicts.length}`
    ].join(' | ');

    appendLedgerLog(conflicts.length > 0 ? 'failover' : 'system', summary);

    const roleLine = [
        `manas:${Number(roles?.manas?.confidence || 0).toFixed(2)}${roles?.manas?.schema_valid ? '✓' : '!'}`,
        `chitta:${Number(roles?.chitta?.confidence || 0).toFixed(2)}${roles?.chitta?.schema_valid ? '✓' : '!'}`,
        `ahamkara:${Number(roles?.ahamkara?.confidence || 0).toFixed(2)}${roles?.ahamkara?.schema_valid ? '✓' : '!'}`,
        `buddhi:${Number(roles?.buddhi?.confidence || 0).toFixed(2)}${roles?.buddhi?.schema_valid ? '✓' : '!'}`
    ].join(' | ');
    appendSandboxTelemetry(`Contract confidence -> ${roleLine}`, 'system');

    const missingSchema = [];
    ['manas', 'chitta', 'ahamkara', 'buddhi'].forEach((roleName) => {
        const missing = roles?.[roleName]?.schema_missing || [];
        if (Array.isArray(missing) && missing.length > 0) {
            missingSchema.push(`${roleName}:${missing.join(',')}`);
        }
    });
    if (missingSchema.length > 0) {
        appendLedgerLog('failover', `Schema missing -> ${missingSchema.join(' | ')}`);
    }

    const repairBits = [];
    ['manas', 'ahamkara', 'buddhi'].forEach((roleName) => {
        const repair = roles?.[roleName]?.schema_repair || {};
        if (repair?.attempted) {
            repairBits.push(`${roleName}:${repair?.success ? 'repaired' : 'fallback'}`);
        }
    });
    if (repairBits.length > 0) {
        appendLedgerLog('failover', `Schema repair -> ${repairBits.join(' | ')}`);
    }

    const modLine = [
        `M:t${Number(roleModulation?.manas?.temperature || 0).toFixed(2)}/p${Number(roleModulation?.manas?.top_p || 0).toFixed(2)}`,
        `A:t${Number(roleModulation?.ahamkara?.temperature || 0).toFixed(2)}/p${Number(roleModulation?.ahamkara?.top_p || 0).toFixed(2)}`,
        `B:t${Number(roleModulation?.buddhi?.temperature || 0).toFixed(2)}/p${Number(roleModulation?.buddhi?.top_p || 0).toFixed(2)}`
    ].join(' | ');
    appendSandboxTelemetry(`Role modulation -> ${modLine}`, 'system');

    appendSandboxTelemetry(`Multimodal evidence -> used:${Boolean(mm?.used)} reason:${mm?.reason || 'none'}`, mm?.used ? 'normal' : 'system');

    if (conflicts.length > 0) {
        const topConflict = conflicts[0] || {};
        appendSandboxTelemetry(`Conflict: ${topConflict.type || 'unknown'} (${topConflict.severity || 'n/a'}) ${topConflict.reason || ''}`, 'failover');
    }

    const curiosityIntent = intentChannels?.curiosity_intent;
    if (curiosityIntent?.type && curiosityIntent?.target) {
        appendLedgerLog('search', `Buddhi intent -> ${curiosityIntent.type}: ${curiosityIntent.target}`);
    }
}

function handleMindArbitration(payload) {
    const cycleId = payload?.cycle_id || 'cycle_unknown';
    const conflicts = Array.isArray(payload?.conflicts) ? payload.conflicts : [];
    const note = payload?.note || 'Arbitration note unavailable.';
    const top = conflicts[0] || {};

    appendLedgerLog('failover', `Arbitration ${cycleId}: ${top.type || 'conflict'} (${top.severity || 'n/a'})`);
    appendSandboxTelemetry(`Arbitration engaged -> ${top.reason || 'Reason unavailable'}`, 'failover');
    appendSandboxTelemetry(note, 'system');
}

function handleMindIdentityGate(payload) {
    const level = payload?.level || 'unknown';
    const reason = payload?.reason || 'not_provided';
    const status = payload?.status || 'unknown';
    const allowCuriosity = Boolean(payload?.allow_curiosity);
    const allowTool = Boolean(payload?.allow_tool);
    const tone = level === 'blocked' ? 'failover' : (level === 'caution' ? 'system' : 'normal');
    appendLedgerLog(tone, `Identity gate ${level}/${status}: curiosity=${allowCuriosity} tool=${allowTool} (${reason})`);
}

function handleMindNegotiation(payload) {
    const attempted = Boolean(payload?.attempted);
    const applied = Boolean(payload?.applied);
    const rounds = Number(payload?.rounds_used || 0);
    const before = Number(payload?.conflicts_before || 0);
    const after = Number(payload?.conflicts_after || 0);
    const tone = applied ? 'system' : (attempted ? 'failover' : 'normal');
    appendLedgerLog(tone, `Negotiation pass: attempted=${attempted} applied=${applied} rounds=${rounds} conflicts=${before}->${after}`);
}

function normalizeFramePath(framePath) {
    if (!framePath) return '';
    if (framePath.startsWith('/')) return framePath;
    const idx = framePath.indexOf('workspace/');
    if (idx >= 0) {
        return '/' + framePath.substring(idx);
    }
    return '/' + framePath;
}

function renderBrowserSurface() {
    const hasEmbeddedPreview = currentBrowserSession?.transport === 'embedded-preview' && currentBrowserSession?.current_url;
    const hasDesktopFrame = Boolean(latestBrowserFrame?.path);

    if (browserPreviewFrame) {
        if (hasEmbeddedPreview) {
            browserPreviewFrame.src = `/api/browser/preview?url=${encodeURIComponent(currentBrowserSession.current_url)}&t=${Date.now()}`;
            browserPreviewFrame.style.display = 'block';
        } else {
            browserPreviewFrame.style.display = 'none';
            browserPreviewFrame.removeAttribute('src');
        }
    }

    if (browserFrame) {
        browserFrame.style.display = hasEmbeddedPreview ? 'none' : (hasDesktopFrame ? 'block' : 'none');
    }

    if (browserFrameEmpty) {
        browserFrameEmpty.style.display = (!hasEmbeddedPreview && !hasDesktopFrame) ? 'flex' : 'none';
        browserFrameEmpty.textContent = hasEmbeddedPreview
            ? 'Loading embedded preview...'
            : 'No browser frame yet.';
    }
}

function renderBrowserFrame(frame) {
    if (!browserFrame || !frame) return;
    const normalized = normalizeFramePath(frame.path);
    if (!normalized) return;

    latestBrowserFrame = frame;
    browserFrame.src = `${normalized}?t=${Date.now()}`;
    renderBrowserSurface();
}

function appendBrowserLog(text, category = 'normal') {
    if (!browserActionLog) return;
    const entry = document.createElement('div');
    entry.className = `log-entry ${category}`;
    const time = new Date().toLocaleTimeString();
    entry.textContent = `[${time}] ${text}`;
    browserActionLog.appendChild(entry);

    while (browserActionLog.children.length > 120) {
        browserActionLog.removeChild(browserActionLog.firstChild);
    }
    browserActionLog.scrollTop = browserActionLog.scrollHeight;
}

function updateBrowserSessionUI(session) {
    if (!session) return;
    currentBrowserSession = session;
    if (browserSessionStatus) {
        if (session.active && session.paused) {
            browserSessionStatus.textContent = 'Paused';
        } else if (session.active) {
            browserSessionStatus.textContent = 'Active';
        } else {
            browserSessionStatus.textContent = 'Idle';
        }
    }
    if (browserCurrentUrl) {
        browserCurrentUrl.textContent = session.current_url || 'N/A';
    }
    renderBrowserSurface();
}

function handleBrowserStateSnapshot(payload) {
    if (!payload) return;
    const browser = payload.browser || {};
    const session = browser.session || {};
    const browserConfig = payload.config || {};
    updateBrowserSessionUI(session);

    if (browserEmbeddedToggle && typeof browserConfig.prefer_embedded_preview !== 'undefined') {
        browserEmbeddedToggle.checked = Boolean(browserConfig.prefer_embedded_preview);
    }

    const recentFrames = browser.recent_frames || [];
    if (recentFrames.length > 0) {
        renderBrowserFrame(recentFrames[recentFrames.length - 1]);
    } else {
        latestBrowserFrame = null;
        renderBrowserSurface();
    }

    const recentActions = browser.recent_actions || [];
    if (recentActions.length > 0) {
        appendBrowserLog(`Loaded ${recentActions.length} prior browser actions.`, 'system');
    }
}

function handleBrowserSessionEvent(eventType, payload) {
    const session = payload?.session || payload?.browser?.session || {};
    updateBrowserSessionUI(session);
    appendBrowserLog(`Session ${eventType}.`, 'system');
}

function handleBrowserActionEvent(payload) {
    const status = payload?.status || 'unknown';
    const actionType = payload?.action?.type || 'action';
    appendBrowserLog(`Action ${actionType} -> ${status}`, status === 'error' ? 'failover' : 'normal');

    if (payload?.frame) {
        renderBrowserFrame(payload.frame);
    }
}

async function sendBrowserCommand(command, body = {}) {
    try {
        const response = await fetch('/api/browser/control', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ command, ...body })
        });

        const payload = await response.json();
        if (!response.ok || payload?.status === 'error') {
            throw new Error(payload?.message || `HTTP ${response.status}`);
        }
        return payload;
    } catch (error) {
        appendBrowserLog(`Command '${command}' failed: ${error.message}`, 'failover');
        return null;
    }
}

async function loadBrowserState() {
    try {
        const response = await fetch('/api/browser/state');
        const payload = await response.json();
        if (!response.ok) {
            throw new Error(payload?.message || `HTTP ${response.status}`);
        }
        handleBrowserStateSnapshot(payload);
    } catch (error) {
        appendBrowserLog(`Unable to load browser state: ${error.message}`, 'failover');
    }
}

// Render Nidra transitions
function handleNidraStatus(phase, data) {
    if (phase === 'sleep') {
        appendLedgerLog('novelty', 'Fatigue threshold met. Suspended runtime loops to enter Nidra Sleep.');
        stateBadge.textContent = 'Nidra (Sleeping)';
        stateBadge.className = 'state-badge badge-sleeping';
        
        // Add full sleep screen block overlay
        const sleepMessage = document.createElement('div');
        sleepMessage.className = 'system-message sleep-mode-active';
        sleepMessage.innerHTML = `<span style="color:#fbbf24;font-weight:800;">[NIDRA MODE]</span> Sleep-tuning LoRA adapters active...`;
        chatMessages.appendChild(sleepMessage);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    } else {
        appendLedgerLog('system', 'Waking from Nidra. Dynamic Adapters compiled.');
        if (data && data.state) {
            updateMetacognition(data.state);
        }
    }
}

// Helper: Append styled chat bubbles
function appendChatBubble(sender, text) {
    const bubble = document.createElement('div');
    bubble.className = `chat-bubble ${sender}`;
    
    // Simple parser to extract code blocks inside chat outputs
    if (text.includes('```')) {
        bubble.innerHTML = parseMarkdownCode(text);
    } else {
        bubble.textContent = text;
    }
    
    chatMessages.appendChild(bubble);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

// Helper: Append entries to the Sakshi Ledger panel
function appendLedgerLog(category, text) {
    const entry = document.createElement('div');
    entry.className = `log-entry ${category}`;
    const time = new Date().toLocaleTimeString();
    entry.textContent = `[${time}] ${text}`;
    ledgerLogs.appendChild(entry);
    
    // Tail scroll limit
    while (ledgerLogs.children.length > 80) {
        ledgerLogs.removeChild(ledgerLogs.firstChild);
    }
    ledgerLogs.scrollTop = ledgerLogs.scrollHeight;
}

// Form event submissions
chatForm.addEventListener('submit', (e) => {
    e.preventDefault();
    if (shutdownRequested) {
        appendLedgerLog('system', 'Input is disabled while shutdown is in progress.');
        return;
    }

    const prompt = chatInput.value.trim();
    if (!prompt) return;

    if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({
            type: 'user_input',
            data: prompt
        }));
        chatInput.value = '';
    } else {
        appendLedgerLog('failover', 'Cannot send prompt: WebSocket disconnected.');
    }
});

if (modelApplyBtn) {
    modelApplyBtn.addEventListener('click', () => {
        applySelectedModel();
    });
}

if (runtimeStopBtn) {
    runtimeStopBtn.addEventListener('click', () => {
        requestRuntimeStop();
    });
}

if (browserStartBtn) {
    browserStartBtn.addEventListener('click', async () => {
        const goal = browserGoalInput?.value?.trim() || 'manual browser autonomy session';
        const url = browserUrlInput?.value?.trim() || '';
        const payload = await sendBrowserCommand('start', { goal, url });
        if (payload) {
            handleBrowserSessionEvent('started', payload);
        }
    });
}

if (browserPauseBtn) {
    browserPauseBtn.addEventListener('click', async () => {
        const payload = await sendBrowserCommand('pause');
        if (payload) {
            handleBrowserSessionEvent('paused', payload);
        }
    });
}

if (browserResumeBtn) {
    browserResumeBtn.addEventListener('click', async () => {
        const payload = await sendBrowserCommand('resume');
        if (payload) {
            handleBrowserSessionEvent('resumed', payload);
        }
    });
}

if (browserStopBtn) {
    browserStopBtn.addEventListener('click', async () => {
        const payload = await sendBrowserCommand('stop');
        if (payload) {
            handleBrowserSessionEvent('stopped', payload);
        }
    });
}

if (browserEmbeddedToggle) {
    browserEmbeddedToggle.addEventListener('change', async () => {
        const preferEmbedded = Boolean(browserEmbeddedToggle.checked);
        const payload = await sendBrowserCommand('set_mode', {
            prefer_embedded_preview: preferEmbedded
        });
        if (payload) {
            appendBrowserLog(`Transport set to ${payload.transport || (preferEmbedded ? 'embedded-preview' : 'desktop-automation')}.`, 'system');
        }
    });
}

// HTML escaping helper
function escapeHtml(text) {
    return text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

// Clean markdown parser helper
function parseMarkdownCode(text) {
    const codeRegex = /```(python|javascript|bash|json)?\s*([\s\S]*?)\s*```/g;
    return text.replace(codeRegex, (match, lang, code) => {
        return `<pre><code class="language-${lang || 'txt'}">${escapeHtml(code)}</code></pre>`;
    });
}

// Tab Navigation Management
function initTabNavigation() {
    const tabs = document.querySelectorAll('.nav-tab');
    
    // Read active tab from localStorage, default to 'overview'
    const savedTab = localStorage.getItem('aiam.activeTab') || 'overview';
    
    // Apply active tab
    setActiveTab(savedTab);

    tabs.forEach(tab => {
        tab.addEventListener('click', () => {
            const targetTab = tab.dataset.tab;
            setActiveTab(targetTab);
        });
    });
}

function setActiveTab(tabName) {
    const tabs = document.querySelectorAll('.nav-tab');
    const container = document.querySelector('.dashboard-container');
    if (!container) return;
    
    container.setAttribute('data-active-tab', tabName);
    localStorage.setItem('aiam.activeTab', tabName);

    tabs.forEach(t => {
        t.classList.toggle('active', t.dataset.tab === tabName);
    });

    // Refresh graph visualizer and trigger fit layout when memory tab is active
    if (tabName === 'memory' && network) {
        setTimeout(() => {
            try {
                network.fit({ animation: { duration: 250 } });
            } catch (_err) {
                // Ignore vis fit exceptions on uninitialized canvas
            }
        }, 180);
    }
}

// Boot UI
initTabNavigation();
initCollapsiblePanels();
initNetwork();
loadModelInventory();
loadBrowserState();
connectWebSocket();
