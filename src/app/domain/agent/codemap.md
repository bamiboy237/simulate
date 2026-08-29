# src/app/domain/agent/

## Responsibility
Encapsulates support-agent routing, tool guards, policy evidence, refund confirmation, and escalation.

## Key Files
- `service.py`: `SupportAgentService` runs ownership-checked tools and keeps the latest order,
  policy, and escalation context for response assembly.
- `errors.py`: Domain exception hierarchy for agent failures.

## Integration
- **Consumed by:** `app.domain.execution`, `app.domain.simulation`.
- **Depends on:** `app.domain.support`, `app.domain.retrieval`, `app.adapters`.
