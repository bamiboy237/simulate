# src/app/domain/retrieval/

## Responsibility
Knowledge retrieval subsystem providing policy document chunking, embeddings, and similarity search.

## Key Files
- `service.py`: `RetrievalService` providing semantic search over company policy documents.
- `index.py`: Vector index and chunk store.

## Integration
- **Consumed by:** `app.domain.agent`, `app.domain.support`.
