# src/app/domain/suite/

## Responsibility
Organizes test cases into executable test suites and manages batch execution.

## Key Files
- `service.py`: Executes test suites in parallel or sequence.
- `models.py`: Test suite definitions, metadata, and run manifests.

## Integration
- **Consumed by:** `app.api`, CLI batch runners.
