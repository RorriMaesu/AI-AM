# AI-AM

<p align="center">
  <img src="images/AI_AM_logo.png" alt="AI-AM Logo" width="220" />
</p>

[![Buy Me a Coffee](https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png)](https://www.buymeacoffee.com/rorrimaesu)

AI-AM is a local-first cognitive agent runtime that simulates a fourfold mind loop (Manas, Chitta, Ahamkara, Buddhi) with persistent memory, autonomous curiosity, optional sandboxed code execution, and a real-time web dashboard.

The system runs against a local Ollama-compatible chat-completions endpoint and exposes both a WebSocket UI stream and REST APIs for runtime control, including live model switching.

## Key Features

- Four-layer cognitive cycle:
  - Manas: sensory-reactive impulse generation
  - Chitta: GraphRAG memory retrieval from SQLite
  - Ahamkara: identity-defense interpretation
  - Buddhi: final synthesis and response
- Persistent cognitive state in [config/engine_config.json](config/engine_config.json)
- Local memory graph + imprint tracking in [database/db_manager.py](database/db_manager.py)
- Real-time dashboard and telemetry in [ui/index.html](ui/index.html), [ui/app.js](ui/app.js), [ui/style.css](ui/style.css)
- Runtime model switching from UI and API (Ollama tags)
- Curiosity-triggered web search enrichment
- Optional Docker sandbox execution for emitted Python tools
- Nidra mode sleep-tuning pipeline with adapter era registry in [training/sleep_tune.py](training/sleep_tune.py)

## Architecture Overview

- Runtime entrypoint: [core/orchestrator.py](core/orchestrator.py)
- Web server: FastAPI on `http://localhost:8002`
- Inference endpoint: configured in `llm_parameters.api_url` (default `http://localhost:11434/v1/chat/completions`)
- Memory store: SQLite database at `database/chitta_store.db`
- UI transport: WebSocket stream at `/ws` plus REST endpoints

### Main Runtime Flow

1. Start loop and load state.
2. Receive user input or autonomous reflection stimulus.
3. Run Manas and Chitta in parallel.
4. Run Ahamkara identity layer.
5. Run Buddhi synthesis.
6. Persist nodes/edges/samskaras to Chitta store.
7. Update fatigue/arousal and broadcast state.
8. Trigger Nidra mode when fatigue threshold is exceeded.

## API Endpoints

- `GET /api/state`
  - Returns current runtime state object.
- `POST /api/input`
  - Queues user prompt into the cognitive loop.
- `GET /api/models`
  - Returns installed Ollama tags and active model.
- `POST /api/model/select`
  - Validates and switches active model, persists config, attempts unload of previous model.
- `WebSocket /ws`
  - Streams `state_update`, `cycle_started`, `timeline_update`, `curiosity_search`, `sandbox_log`, and Nidra events.

## Requirements

- Windows environment (PowerShell scripts provided)
- Python 3.10+
- Ollama running on port 11434
- A local Ollama model tag compatible with your configured prompts

Optional:
- Docker (for sandbox tool execution)
- PyTorch + Transformers + PEFT (for real sleep-tuning)
- `sentence-transformers` or ONNX runtime + local ONNX files (for embeddings)
- `duckduckgo_search` package (curiosity web search)

## Quick Start

1. Ensure Ollama is running on `localhost:11434`.
2. Start runtime with [start_mind.ps1](start_mind.ps1).
3. Open dashboard at `http://localhost:8002`.
4. Stop runtime and reclaim VRAM with [stop_mind.ps1](stop_mind.ps1).

## Model Switching

You can switch models at runtime from the UI model selector in the header or through API.

Expected behavior:
- model list is fetched from Ollama `/api/tags`
- selected model is validated
- current model unload is requested
- new model is persisted to state/config and used for subsequent calls

## Configuration

Primary state/config file:
- [config/engine_config.json](config/engine_config.json)

Important section:
- `llm_parameters.model_name`
- `llm_parameters.api_url`
- `llm_parameters.num_ctx`

Note:
This file is runtime-mutated frequently (heartbeat, workspace text, fatigue, etc.). Treat it carefully in commits.

## Testing

Automated integration-style tests live in [tests/run_tests.py](tests/run_tests.py).

The test suite covers:
- Ollama reachability
- sequential cycle execution
- failover and reconnect handling
- Nidra mode and adapter generation checks
- curiosity and dreaming behavior checks

Run:

```bash
python tests/run_tests.py
```

## Training and Adapters

Sleep tuning is implemented in [training/sleep_tune.py](training/sleep_tune.py).

Pipeline summary:
1. extract high-arousal memory nodes
2. build ChatML dataset at `training/chatml_dataset.jsonl`
3. run PEFT LoRA training if dependencies are available
4. fallback to simulated adapter artifact if training is unavailable
5. register new era in `ancestry_ledger`

Adapters are stored under `adapters/era_YYYYMMDD_HHMMSS`.

## UI Branding

AI-AM branding and logo are integrated in the header.

Logo source:
- [images/AI_AM_logo.png](images/AI_AM_logo.png)

## Repository Notes

- Runtime/log/db/sandbox outputs are controlled through [.gitignore](.gitignore).
- Large adapter artifacts can grow repository size quickly; consider curation or Git LFS strategy.

## Troubleshooting

- If app exits unexpectedly:
  - The orchestrator treats `exit` and `shutdown` as control commands when received as prompt input.
- If you see pydantic field attribute warnings:
  - These are non-fatal dependency-level warnings during startup.
- If curiosity search warns about package rename:
  - Migrate usage from `duckduckgo_search` to `ddgs` in a future update.
- If model list is empty:
  - Verify Ollama is running and API base in `llm_parameters.api_url` is correct.
- If dashboard does not load assets:
  - Ensure FastAPI static mounts are active and restart after static path changes.

## License

See [LICENSE](LICENSE).
