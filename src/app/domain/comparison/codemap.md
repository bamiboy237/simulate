# `src/app/domain/comparison/`

## Responsibility

Measures completed simulation runs and decides whether a candidate configuration or hosted
model improves behavior without safety, state, or evidence regressions.

## Design

- Evaluators are deterministic functions over normalized `RunMetrics`; there is no model judge
  for criteria that code can express.
- `ConfigurationSet` enforces a one-variable experiment and requires the baseline to equal the
  configuration recorded in the bundle.
- `RunComparison` retains both runs, evidence links, per-criterion deltas, regressions, and
  blockers. Run identity is checked against bundle ID, bundle hash, and scenario ID.
- Model Lab is a bounded paired-cohort experiment. Task success requires the expected outcome and
  all required safety/state evaluator results.

## Flow

1. `simulation.runner.run_bundle()` builds `RunMetrics`; `run_evaluators()` checks
   authorization, confirmation, allowed tools, required/permitted transitions, policy grounding,
   output, escalation, latency, tokens, and cost.
2. `compare_runs()` verifies run identity and records outcome, evaluator, state, tool-path,
   retry, latency, token, and cost deltas.
3. Missing measurements yield `insufficient_evidence`. Regressions yield
   `candidate_regresses`; only outcome/evaluator/retry improvement can yield
   `candidate_passes`; other differences yield `no_material_difference`.
4. `run_model_lab()` executes each bundle with baseline and candidate `ModelConfig` values,
   aggregates cohort evidence, and returns `recommend_candidate`, `keep_baseline`, or
   `inconclusive`.

## Key Files

- `evaluators.py`: Normalized metrics and deterministic evaluators.
- `compare.py`: Paired-run identity checks, deltas, and verdict.
- `experiment.py`: One-variable configuration and baseline validation.
- `model_lab.py`: Bounded paired-model cohort and recommendation gates.

## Integration

- **Inputs:** `SimulationBundle`, `SimulationRun`, mutations, agent outcomes, and model configs.
- `simulation.runner` runs evaluators; `suite.runner` runs case/cohort comparisons;
  `execution.service` supplies evaluator and configuration choices.
- Run APIs and CLI commands start comparisons; `app.cli.report` formats results.
