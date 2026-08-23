# src/app/domain/regression/

## Responsibility
Manages golden regression datasets, historical performance benchmarks, and automated regression detection.

## Key Files
- `service.py`: Runs regression test suites and computes delta scores against baseline.
- `repository.py`: Persistence for regression cases and test results.

## Integration
- **Consumed by:** `app.api`, CI pipelines.
