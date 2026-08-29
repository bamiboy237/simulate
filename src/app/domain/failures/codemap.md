# `src/app/domain/failures/`

## Responsibility

Turns canonical traces into explainable failure candidates, groups similar failures with a
deterministic baseline, and controls the human-review boundary that creates confirmed failure
groups for regression-case compilation.

## Design

- `features.py` uses ordered rules and named scalar features. It does not embed raw text or use
  a model classifier; every candidate retains source evidence and event IDs.
- `grouping.py` implements an order-independent DBSCAN-style baseline. Stable UUID5
  group/proposal IDs derive from versioned content hashes.
- `FailureReviewService` owns `proposed -> confirmed|corrected|rejected`. Reviews are immutable,
  actor-attributed, and applied once under a repository row lock.
- Repository protocols have in-memory and SQLAlchemy implementations.
- `ConfirmedFailureGroup` is the only output approved for bundle compilation.

## Flow

1. `dataset.py` loads the versioned manifest, combines canonical fixtures with deterministic
   routing/generation traces, and verifies the dataset hash.
2. `extract_failure_candidate()` ignores accepted successes; otherwise it classifies the trace
   and extracts retry, escalation, retrieval, tool, database, policy, and timing features with
   exact evidence event IDs.
3. `group_candidates()` clusters candidates and reports coverage/outliers.
   `proposals_from_result()` creates provenance-rich review proposals.
4. `FailureReviewService` persists and reviews proposals, then returns `ConfirmedFailureGroup`
   only for confirmed or corrected proposals.
5. Bundle compilation verifies the group's evidence and event references before creating a
   runnable artifact.
6. Separately, `FeedbackImportService` parses LangSmith annotations, validates references, and
   stores idempotent revisions or corrections.

## Key Files

- `features.py`: Explainable classification and feature extraction.
- `grouping.py`: Clustering, metrics, and proposal construction.
- `service.py`: Human review lifecycle and confirmed-output gate.
- `repository.py` / `models.py`: In-memory and PostgreSQL persistence.
- `feedback.py`: Annotation parsing and revision-safe import.
- `dataset.py` / `evaluate.py`: Versioned fixture dataset and offline artifact.
- `schemas.py`: Taxonomy, provenance, proposal, review, and metric contracts.

## Integration

- **Inputs:** Canonical evidence and optional LangSmith feedback through
  `app.adapters.sources.langsmith_feedback`.
- `app.api.failures_router` lists, fetches, and reviews proposals.
- `app.domain.bundle.compiler` consumes only `ConfirmedFailureGroup`, preserving the human
  review gate.
