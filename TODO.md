# AI-AM development

This file distinguishes the implemented v2 foundation from research work.

## V2 foundation

- Four validated function contracts and a shared state.
- A Chitta LLM grounded in retrieved memory records.
- Durable events, provenance, correction links, and transactional completion.
- Bounded recurrence and explicit arithmetic actions.
- Safe local API and an offline dashboard.
- Clearly labeled demo mode with isolated memory.
- Isolated automated tests on Windows and Linux.
- Memory consolidation with honest status; no simulated adapter artifacts.

## Follow-up experiments

- Benchmark live local models on correction, attribution, and uncertainty tasks.
- Compare against a retrieval assistant at matched compute budgets.
- Measure each function with targeted ablations.
- Add source-preserving hybrid retrieval and test embedding migrations.
- Evaluate role adapters only after collecting validated training examples.
- Implement a verified adapter activation/rollback path before enabling training.
- Explore a persistent simulated environment with measurable action outcomes.
- Investigate latent communication as a separate backend.
- Measure long-running storage growth, background work cost, and responsiveness.

Historical adapter files remain in the repository but are not activated by v2.
