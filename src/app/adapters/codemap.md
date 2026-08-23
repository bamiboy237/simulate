# src/app/adapters/

## Responsibility
Integration layer connecting domain models to external LLM providers and observability systems.

## Key Files
- `pydantic_ai_agent.py`: Adapter executing Pydantic AI agent models with structured tool definitions.
- `sources/langsmith.py`: Adapter fetching production traces and feedback from LangSmith.

## Integration
- **Consumed by:** `app.domain.agent`, `app.domain.execution`.
- **Depends on:** External APIs (OpenAI, Anthropic, Gemini, LangSmith).
