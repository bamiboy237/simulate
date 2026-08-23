# src/app/domain/bundle/

## Responsibility
Packages complete evaluation test cases into self-contained, reproducible, privacy-safe JSON bundles.

## Key Files
- `compiler.py`: Compiles initial database states, agent fixtures, and ground truth into a `CaseBundle`.
- `allowlist.py`: Strict data allowlist and secret denylist verifying no raw customer PII or API tokens enter bundles.
- `schemas.py`: Pydantic models defining bundle structure and versioning.

## Integration
- **Consumed by:** `app.api.cases`, `app.domain.execution`.
- **Depends on:** `app.domain.support`, `app.domain.simulation`.
