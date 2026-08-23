# src/app/domain/agent/

## Responsibility
Encapsulates the AI support agent business logic, prompt templates, tool binding, and policy adherence checks.

## Key Files
- `service.py`: `SupportAgentService` orchestrating agent prompt preparation, tool execution, and response synthesis.
- `errors.py`: Domain exception hierarchy for agent failures.

## Integration
- **Consumed by:** `app.domain.execution`, `app.domain.simulation`.
- **Depends on:** `app.domain.support`, `app.domain.retrieval`, `app.adapters`.
