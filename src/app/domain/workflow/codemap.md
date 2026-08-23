# src/app/domain/workflow/

## Responsibility
Workflow orchestration using LangGraph state graphs, managing agent state checkpoints and multi-step decision paths.

## Key Files
- `graph.py`: LangGraph workflow definition and node transitions.
- `checkpoint.py`: State checkpointing and serialization.

## Integration
- **Consumed by:** `app.domain.execution`.
