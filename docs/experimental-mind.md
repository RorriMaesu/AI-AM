# Experimental mind: persistent attention, action, and prediction

This extension gives AI-AM a persistent working state and a small world with a limited viewpoint. It is a research platform for testing integrated cognition. It does not establish subjective experience.

The existing four-function conversation remains available. The experimental world is isolated from the physical computer: simulated movement and object manipulation cannot operate a real robot, read files, or send messages.

## What changes

Manas's attention estimates now contribute to a bounded shared focus. Relevant items can persist into the next episode, decay, or leave the workspace. Each broadcast is visible to the other functions. Chitta's selected memories influence their ordering and the context offered for subsequent processing.

Ahamkara receives authoritative action records rather than only conversational recollections. Proposed, attempted, completed, failed, cancelled, and uncertain outcomes are distinct. Commitments use explicit updates so omission does not silently remove an earlier commitment.

Buddhi can propose bounded actions in a simulated environment. A prediction is recorded before execution; the resulting observation can revise the agent's beliefs and update learned action consequences.

The working state and simulator state are different things. The simulator owns ground truth. The agent receives only its visible observation, its accumulated beliefs, and observed action outcomes.

## Explore the world

Start the application using the normal launcher and the data drive you selected:

```powershell
.\start_mind.ps1 -DataDir T:\AIAMData
```

The laboratory is available when `experiment_enabled` is true (the default); setting it false disables its mutation controls. No autonomous run starts merely because the laboratory is enabled. The world is initialized lazily when reset or acted upon.

The experimental panel provides manual actions and a separate, explicitly labeled evaluator view. The evaluator can inspect the full world or move an object to construct a test. That hidden state is not included in model prompts.

A useful first experiment is to observe the key, turn away, move it using an evaluator intervention, and look again. During occlusion, the agent should retain its last observation as a belief rather than acquire the key's hidden location. On seeing conflicting evidence, it can revise that belief.

Enter a goal and choose a finite step limit to start a model-controlled run. The run is bounded by `experiment_max_steps` (default 10) and `experiment_budget` (default 30). Conversation, manual actions, Pause, Reset, and evaluator interventions interrupt the current goal run. Restarting does not silently resume it.

The current room layout is fixed. A reset seed is accepted for interface stability; different seed values do not currently generate different rooms. The offline evaluator therefore reports fixed scenarios rather than randomized-world results.

The same observation/action interface is intended to support richer simulated environments later. There is no physical robot connection in this version.

## Predictions and learning

The world model stores structured beliefs, a bounded recent history, and empirical action-transition statistics. Predictions are scored against actual subsequent observations. The learned statistics can adapt to changed controls in the simulator.

This is online learning of a small model of action consequences. It is **not** neural-network training, an activated adapter, or a newly trained recurrent perception model. Historical adapter files remain unused. The transition counts are conditioned on the requested action, not the surrounding scene. Blocked movement and free movement therefore contribute to the same action distribution; this is a small baseline rather than a general model of physical dynamics.

The simulator's energy and action budget constrain behavior. These are operational resource values; they are not measurements of pain, pleasure, fear, or any other feeling.

## Reproducible component evaluation

```powershell
python -B tests/evaluate_mind.py
```

The evaluator runs deterministic scenarios in RAM and prints a JSON report to stdout. It does not start Ollama, download models, create a database file, or write a result file.

Its comparisons use the same observations and action sequences in each condition:

- Retained beliefs versus a model reconstructed from the latest observation.
- Adaptive action predictions versus a model with transition learning disabled.
- Goal-responsive attention versus an otherwise valid attention state without that goal.

These are component experiments. They do not measure the quality of an LLM's interpretation, autonomous planning, or subjective consciousness. Scores describe their named task only; there is no combined consciousness percentage.

Unit, API, and browser tests separately cover persistence, action lifecycle, bounded controls, and the interface. The earlier [local Gemma validation](live-validation-2026-11-07.md) tested v2 behavior; its seven-scenario result must not be treated as validation of every new experimental feature.

## What a stronger evaluation would require

A genuine comparative study should hold the base model, evidence, task distribution, and compute allowance as constant as practical. Compare an ordinary retrieval assistant, the four-stage v2 pipeline, and the experimental workspace. Use unseen environments, repeated trials, and explicit uncertainty bounds.

Disable mechanisms while preserving valid interfaces. A broken parser after removing a function is not evidence of cognitive specialization. Evaluate action ownership, hidden-object belief accuracy, prediction calibration, memory correction, and behavior under distraction.

The most informative failure is one predicted by a mechanism: for example, removing the model of attention should impair the choice of inspection targets while leaving sensory input intact. A more eloquent statement about being aware is not a substitute for that result.

## Research scope

The design is informed by global-workspace, attention-schema, and predictive-model research. These theories disagree about what is required for experience, and their engineering analogues do not settle the philosophical question.

The four Sanskrit function names identify responsibilities within one agent. They are not four separate selves, and the system does not identify a software monitor with the metaphysical witness.

Later experiments may add learned recurrent perceptual representations, role-specific training, richer sensors, and a physical embodiment adapter. Those are research directions, not completed capabilities.

References:

- [AI consciousness indicators](https://doi.org/10.1016/j.tics.2025.10.011)
- [Coordination among neural modules through a shared global workspace](https://arxiv.org/abs/2103.01197)
- [Attention-schema agent and ablation experiments](https://doi.org/10.1073/pnas.2102421118)
- [World models for control](https://doi.org/10.1038/s41586-025-08744-2)
