# src/app/domain/user_simulator/

## Responsibility
Synthetic user dialogue engine simulating realistic multi-turn user personas, testing support agent boundaries and edge cases.

## Key Files
- `simulator.py`: Multi-turn dialogue coordinator driving conversations between simulated persona and agent.
- `manifests.py`: YAML manifest loaders for scenarios and environment profiles.
- `preflight.py`: Non-destructive preflight validation verifying database reachability and environment variables.
- `events.py`: Typed event emitter recording JSONL simulation streams.

## Integration
- **Consumed by:** `app.cli.simulate`, `app.cli.textual_simulate`.
- **Depends on:** `app.domain.simulation`, `app.config`.
