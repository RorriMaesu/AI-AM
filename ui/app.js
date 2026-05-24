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

let knownModels = [];
let activeModel = null;
let shutdownRequested = false;
let shutdownInFlight = false;

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
            
        case 'curiosity_search':
            handleCuriositySearch(data);
            break;
            
        case 'sandbox_log':
            handleSandboxExecution(data);
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

    // Specific logic updates for Buddhi resolutions (print final replies to chat)
    if (layer === 'buddhi') {
        appendChatBubble('agent', content);
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
    sandboxConsole.innerHTML = `
        <span class="console-prompt">BUDDHI Code Directive:</span>
        <pre><code class="language-python">${escapeHtml(code)}</code></pre>
        <span class="console-prompt">Karmendriya Container Output:</span>
        <div class="console-output">${escapeHtml(output)}</div>
    `;
    sandboxConsole.scrollTop = sandboxConsole.scrollHeight;
    appendLedgerLog('novelty', 'Karmendriya Docker sandbox execution completed.');
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

// Boot UI
initNetwork();
loadModelInventory();
connectWebSocket();
