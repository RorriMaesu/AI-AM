# AI-AM v2: one persistent agent, four functions

AI-AM uses four function contracts around one durable event history,
one working state, one self-model, and one outward response. The design
is inspired by the fourfold antahkarana. It is a computational
interpretation, not a claim that traditional philosophical categories
map exactly onto software or that the system has subjective experience.

## A cycle

1. Persist the incoming event before it becomes eligible for processing.
2. Claim a pending event and load the current shared state.
3. Manas identifies observations, possible interpretations, uncertainty,
   and a retrieval cue.
4. Retrieve candidate memories from SQLite. Chitta selects and interprets
   those actual records, retaining their IDs.
5. Ahamkara considers ownership, commitments, self-relevance, and actual
   capability boundaries.
6. Buddhi sees the original input and evidence as well as the role outputs.
   It can respond or request a bounded retrieval, reconsideration, or
   arithmetic calculation.
7. Validate proposed evidence references and state changes.
8. Commit the completed event, reply, memories, trace, and state together.
9. Publish the committed result to the dashboard.

All four functions run through structured inference. Their role names
refer to responsibilities, not separate fictional biographies. Shared
weights are a resource choice; each role has separately configurable
model settings.

A deterministic controller owns state updates. LLM outputs are proposals,
not arbitrary commands to overwrite storage or execute code.

## Contracts

| Function | Primary result |
|---|---|
| Manas | Observations, interpretations, uncertainties, retrieval query, salience. |
| Chitta | Selected memory IDs, associations, contradictions, missing context. |
| Ahamkara | Self-relevance, agency, capability limits, commitments. |
| Buddhi | Action, response, evidence IDs, uncertainty, rationale, memory and goal proposals. |

Pydantic validates output shape and limits. Ollama receives the same
schema through its native structured-output mechanism. A malformed
response gets a bounded repair attempt; persistent failure remains a
visible failure rather than a fabricated success.

Schema validity is not truth. The controller must also validate that
cited records exist and that the proposed action is permitted.

## Persistent memory

SQLite stores events, memories, and the committed state. UUIDs identify
records independently of an in-memory heartbeat. Connections are
explicitly closed; multi-record updates use transactions.

Memory kinds distinguish user statements, observations, inferences,
reflections, commitments, and unverified legacy content. Each new memory
carries event references and confidence. Inference and reflection do
not become observations merely because they were recalled repeatedly.

A correction links the new memory to a superseded record. The older
record remains available for inspection. Normal retrieval excludes
superseded records so the agent can stop repeating corrected information
without erasing its history.

Local lexical retrieval provides a reproducible baseline without a
downloaded embedding model. It has important limits: lexical mismatch
can miss paraphrases, and relevance is not a factuality score. Adding
semantic retrieval later should retain source IDs, explicit relevance
thresholds, embedding model/revision metadata, and a tested migration
path.

Consolidation deduplicates compatible records and preserves history.
It does not update neural weights. Automatic training should not be
reintroduced until there is a curated dataset, a compatible served
base model, regression evaluation, a verified activation path, and rollback.

## Recurrence and attention

Buddhi can request another retrieval or reinterpretation within a
bounded cycle. The controller limits requests, generated output, and
elapsed time. A loop iteration is not itself evidence of learning.

Background reflection is optional. It should work on a specific topic
or unresolved goal, remain lower priority than conversation, and stop
when there is no useful change. Quiet waiting is a valid runtime state.

The self-model describes the actual agent, capabilities, and commitments.
Ahamkara can recognize an owned mistake and revise the relevant
commitment. It is not instructed to defend every previous output or to
invent biological sensations.

## Actions and local execution

The v2 action surface includes a bounded arithmetic calculator. Python
code fences are displayed as text. They are not executable directives.
The previous automatic Docker path has been removed from the live cycle.

Inference targets the local native Ollama API. The server binds to
loopback, validates input, checks browser origins, and bounds message
sizes. The frontend renders untrusted content through text nodes and
has no CDN dependencies.

The runtime never automatically pulls models, initiates web searches,
or declares a zero-filled file to be a learned adapter. A live Ollama
connection and installed model are required outside demo mode.

The demo provider is scripted. It is visibly labeled and uses a separate
database, allowing interface inspection without mixing sample behavior
with live memory.

## Migration and recovery

Version 1 used `database/chitta_store.db` and stored changing state in
`config/engine_config.json`. Version 2 defaults to a database in the
chosen data directory. Configuration is no longer a conversation log.

Opening a database containing the legacy tables preserves them and
imports their memories once as unverified records. This conversion does
not establish that old generated narratives are factual.

Before using historical memory:

1. Stop the old runtime gracefully.
2. Make a backup of the old database and its configuration.
3. With the old runtime stopped, copy the old database into the new data
   directory as `chitta.db`, or use the migration path provided by the
   current runtime. Never overwrite an existing v2 database to do this.
4. Start v2 and inspect imported legacy records before relying on them.

An active SQLite database may have a WAL file. Prefer the SQLite backup
API for a live backup; a single-file copy while another process is writing
is not a consistent backup.

Pending inputs are durable. Interrupted processing can return to the
queue on restart; completed replies and memory writes are committed
together. Failed events retain their identity and error state for an
explicit retry. An inference error must not erase the user's message.

## Behavioral evaluation

Unit and API tests establish software properties. They cannot establish
that a chosen local model reasons well, and they do not test consciousness.

A useful experiment compares the same base model and similar compute
budgets across a normal retrieval assistant, the original sequential
role chain, v2, and v2 with individual functions disabled.

| Scenario | Measure |
|---|---|
| Correct a remembered detail, restart, and ask again. | Retention of correction; retrieval of the superseding record. |
| Recall a fictional event introduced as imagination. | Correct separation of imagined and observed events. |
| Ask who promised or performed an action. | Ownership and source fidelity. |
| Present evidence contradicting a prior claim. | Revision, uncertainty, and preservation of the correction trail. |
| Interrupt reflection with conversation. | User-response latency and safe interruption. |
| Cause inference, parsing, or storage failure. | Durable retry, no partial committed cycle, honest error reporting. |
| Disable a function for a task designed to need it. | A measurable, function-specific contribution. |

Report task accuracy, unsupported memory claims, contradiction rate,
correction retention, latency, token use, and peak memory. Use several
trials and held-out tasks. A verbose self-report is not a substitute for
these measurements.

## Further experiments

- Hybrid semantic/lexical retrieval with source-preserving reranking.
- Role-specific adapters trained on curated, independently checked examples.
- Selective heterogeneous models when measured benefits justify memory cost.
- A small persistent simulated environment with partial observations and
  action outcomes.
- Latent communication in a separate backend experiment after a stable
  text/schema baseline.

These remain research directions. They are not represented as completed
features in the dashboard.

## Reading

- [Cognitive Architectures for Language Agents](https://arxiv.org/abs/2309.02427)
- [Generative Agents](https://arxiv.org/abs/2304.03442)
- [Why Do Multi-Agent LLM Systems Fail?](https://arxiv.org/abs/2503.13657)
- [Coordination Through a Shared Global Workspace](https://arxiv.org/abs/2103.01197)
- [AI Consciousness: Indicator Properties](https://arxiv.org/abs/2308.08708)
- [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs)
