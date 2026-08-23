# src/app/domain/audit/

## Responsibility
Maintains immutable audit records and scans event payloads for sensitive data, credentials, and PII.

## Key Files
- `scanner.py`: Recursive scanner verifying telemetry and event payloads against security rules.
- `models.py`: Audit log entries and metadata schemas.

## Integration
- **Consumed by:** `app.domain.bundle`, `app.api`.
