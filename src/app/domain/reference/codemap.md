# src/app/domain/reference/

## Responsibility

Runs bounded reference workflows and compares baseline and candidate evidence.

## Design

- `contracts.py` defines workflow, tool, repository, plan, and expectation contracts.
- `runner.py` seeds one disposable repository, executes the plan, derives the result from observed
  state and mutations, and always destroys the repository.
- `compare.py` and `report.py` produce deterministic comparisons and integration reports.

## Flow

`ReferenceWorkflow` -> `run_reference_case()` -> observed state and mutations -> comparison.

## Integration

Uses simulation events and fault scripts. The CLI and user simulator consume the workflow registry.
