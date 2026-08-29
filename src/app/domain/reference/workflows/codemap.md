# src/app/domain/reference/workflows/

## Responsibility

Builds the seven reference workflows used by the deterministic reference harness.

## Design

- `repo.py` provides disposable in-memory state. `seed()` deep-copies approved state, and
  `destroy()` discards state and mutations.
- `six_reference.py` builds incident response, CI triage, claims denial, returns resolution,
  onboarding, and disputes. `flight_booking.py` builds flight booking.

## Flow

Fixture state -> workflow builder -> `ALL_WORKFLOWS` -> reference runner.

## Integration

Uses fixture data from `app.domain.reference_workflows` and contracts from `app.domain.reference`.
