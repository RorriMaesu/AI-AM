# AI-AM

**Four functions. One history. One voice.**

AI-AM is a local cognitive agent inspired by the fourfold antahkarana:
Manas, Chitta, Ahamkara, and Buddhi. It combines local language models with
durable, source-linked memory and a shared working state.

Version 2 makes the four functions explicit. Each produces a validated
structured result; Chitta is now an LLM-assisted memory function; Buddhi
can request another retrieval or reconsideration before responding.
The dashboard shows what each function contributed and which remembered
events supported a decision.

This is an implementation of integrated cognition as a simulation.
First-person language and an animated dashboard are not evidence of
subjective experience.

## What works

- **Manas:** observations, candidate interpretations, uncertainty, and attention.
- **Chitta:** association and selection of retrieved memories with real record IDs.
- **Ahamkara:** ownership of actions, capability limits, commitments, and continuity.
- **Buddhi:** evidence-based response or a bounded request for more processing.
- SQLite events, memories, correction links, and state that survive restarts.
- Local retrieval without downloading an embedding model.
- Separate records for statements, observations, inferences, reflections, and commitments.
- Per-function model selection through a local Ollama server.
- An offline dashboard for conversation, faculty traces, memories, and sources.
- Explicit reflection, memory consolidation, pause/resume, and optional background work.
- A bounded arithmetic calculator; displaying code never executes it.
- A clearly labeled scripted demo using a separate database.
- Tests that use disposable state rather than deleting the installation's memory.

## Start on a drive with room

Requirements: Python 3.10 or newer; Ollama for live inference. Node 22 is
used only for dashboard tests. No Docker, PyTorch, PEFT, or embedding
package is required for the v2 runtime.

Choose the installation and data directories before installing packages.
The following PowerShell example keeps the project, environment, and caches
on T:. Replace the paths with your preferred drive.

```powershell
git clone https://github.com/RorriMaesu/AI-AM.git T:\AI-AM
Set-Location T:\AI-AM

$env:AIAM_DATA_DIR = 'T:\AIAMData'
New-Item -ItemType Directory -Force -Path "$env:AIAM_DATA_DIR\cache\temp" | Out-Null
$env:TEMP = "$env:AIAM_DATA_DIR\cache\temp"
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = "$env:AIAM_DATA_DIR\cache\pip"
$env:HF_HOME = "$env:AIAM_DATA_DIR\cache\huggingface"
$env:PYTHONDONTWRITEBYTECODE = '1'

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

The repository retains historical adapters for traceability. They are not
loaded by this runtime. A sparse clone can omit the adapters directory if
you want to avoid downloading those historical artifacts.

For a quick interface demonstration:

```powershell
.\start_mind.ps1 -Demo -DataDir T:\AIAMData
```

Open [http://localhost:8002](http://localhost:8002).
The demo provider is deterministic sample behavior, not an LLM. Its banner
remains visible, its database is separate, and it never imports live legacy memories.

For live use, start Ollama, install a model that fits your hardware, and set
its exact installed tag in the configuration. AI-AM never pulls models
automatically. Ollama manages its model storage separately: before downloading
anything, set its `OLLAMA_MODELS` environment variable to a roomy drive,
such as `T:\OllamaModels`, and restart Ollama so it uses that location.
AI-AM's `-DataDir` does not relocate an already running Ollama server.
See [Ollama's environment and storage instructions](https://docs.ollama.com/faq).

Then run:

```powershell
.\start_mind.ps1 -DataDir T:\AIAMData
```

The Python equivalent is:

```powershell
python -B core/orchestrator.py --data-dir T:\AIAMData --port 8002
```

Stop through the dashboard or:

```powershell
.\stop_mind.ps1 -Port 8002
```

The stop script requests shutdown of the identified AI-AM service. It does
not kill unrelated Python processes or unload another application's Ollama model.

## Configuration

The repository's `config/engine_config.json` contains configuration, not
the evolving conversation. Live state is stored transactionally in the data
directory. Set it with `--data-dir`, `AIAM_DATA_DIR`, or the launcher's
`-DataDir` argument. The default is the repository's `data` directory.

Use the actual model tags reported by your local Ollama installation.
Each role may select its own model, although sharing a single capable
model is the economical starting point on one GPU. Calls run sequentially
to avoid competing for the same GPU. Model choices made in the dashboard
persist in the database and take precedence over configuration-file model
defaults after a restart.

The important options are:

| Option | Purpose |
|---|---|
| `api_url` | Local Ollama native chat endpoint, normally `http://127.0.0.1:11434/api/chat`. |
| `roles` | Model and temperature settings for each of the four functions. |
| `num_ctx` | Context budget; defaults to 8192. |
| `max_output_tokens` | Bound for each role's generated output; defaults to 600. |
| `max_rounds` | Bound on recurrence before the cycle must finish. |
| `request_timeout` / `cycle_timeout` | Limits for inference and a complete cycle. |
| `background_enabled` | Optional background reflection; disabled initially. |
| `background_interval` | Minimum interval between background work opportunities. |

Only loopback inference hosts and local model tags are accepted. Each
generation checks Ollama's inventory, including cloud metadata behind aliases.
The JSON schema is supplied in both the generation format and the prompt;
the complete prompt, schema, repair context, and reserved output share one
estimated context budget. The app
does not browse the web autonomously. The dashboard's scripts, styles,
and fonts do not depend on a CDN.

A large context and several resident models increase memory demand.
Measure latency and VRAM before increasing parallelism or model count.
Thinking-capable models use their Ollama default thinking mode; AI-AM consumes
their structured final content. Models that spend the output budget thinking
may need a larger `max_output_tokens` and cycle timeout. This remains a live
model compatibility check, not something the scripted demo can establish.

## Memory and correction

The durable store records where a memory came from, when it was created,
its kind, and its confidence. A user statement is evidence that the user
said something; it is not automatically an independently verified fact.

Corrections preserve history. A new memory can supersede an older memory
by its ID, with references to the events supporting the correction.
Default retrieval excludes superseded memories. Unsupported source IDs
and invalid correction targets are rejected. Corrections require evidence
from the current user turn. Raw statement and observation labels require
the complete cited source text; generated paraphrases remain inferences.
An inferred memory or commitment cannot replace recorded raw evidence.

Existing legacy memory tables are preserved and imported as explicitly
unverified legacy records when present in the database being opened.
They are not silently treated as verified observations. Back up an older
installation's database before migration. See
[the migration notes](docs/architecture.md#migration-and-recovery) for the
distinction between an old database and the new default data directory.

## Reflection and sleep

Reflection can revisit an unresolved topic and submit evidence-linked
proposals. It has a processing budget and can yield to user input.

Consolidation currently performs deterministic memory housekeeping. The
historical command name remains usable:

```powershell
python -B training/sleep_tune.py --data-dir T:\AIAMData
```

Its JSON output explicitly reports `weights_trained: false` and
`adapters_activated: false`. It does not manufacture weight files or
declare a training success. Existing adapter artifacts are historical;
there is no automatic weight-training or adapter-activation pipeline in v2.

## Verification

Install the development dependencies, then run:

```powershell
python -B tests/run_tests.py --temp-dir T:\AIAMTestTemp --verbose
node --test tests/ui.test.cjs
```

The runner creates a fresh child directory under the supplied temporary
parent. It redirects runtime state and caches there, disables bytecode
and pytest cache output, and removes only that disposable child afterward.
It does not delete production configuration, databases, or logs.

GitHub Actions runs Python tests on Linux and Windows with Python 3.10
and 3.12, plus the dashboard's Node tests and a real Chromium browser
against the demo server. The browser job checks conversation, evidence
links, pause/resume, persistence, mobile layout, and offline assets, and
publishes screenshots and a trace as workflow artifacts. These checks use mocked or
scripted model responses; they do not measure live model intelligence,
GPU performance, or consciousness.

For behavioral evaluation, compare against a normal assistant with
retrieval at a similar token/time budget. Test correction after restart,
source fidelity, agency attribution, uncertainty, failed action recovery,
and each role's contribution when it is disabled. The
[architecture and evaluation notes](docs/architecture.md) explain the design.

## API

The server binds to loopback. The dashboard uses a WebSocket at `/ws`
and these HTTP routes:

| Route | Use |
|---|---|
| `GET /api/health` | Identify the local service. |
| `GET /api/state` | Current shared state and runtime status. |
| `GET /api/events` | Recent durable events. |
| `GET /api/events/{event_id}` | Read a source event. |
| `POST /api/input` | Queue a message: `{"text":"Hello"}`. |
| `POST /api/retry` | Retry a failed event by ID. |
| `GET /api/memories?query=...` | Retrieve relevant memories. |
| `GET /api/memories/{memory_id}` | Inspect a memory and its provenance. |
| `GET /api/models` | Installed local model tags and role assignments. |
| `POST /api/model/select` | Select a model for a role or all roles. |
| `POST /api/reflect` | Queue reflection on a topic. |
| `POST /api/consolidate` | Consolidate stored memories. |
| `POST /api/control` | Pause or resume. |
| `POST /api/background` | Enable or disable background work. |
| `POST /api/stop` | Request graceful shutdown. |

## License

[Apache License 2.0](LICENSE).
