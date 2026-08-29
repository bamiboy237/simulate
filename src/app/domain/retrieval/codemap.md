# `src/app/domain/retrieval/`

## Responsibility

Provides versioned policy retrieval: deterministic chunking, embedding and PostgreSQL indexing,
keyword/vector search, rank fusion, safe tracing, strict answer citations, and offline evaluation.

## Design

- `contracts.py` isolates storage and vendors behind `EmbeddingProvider` and `Retriever`
  protocols. Documents, chunks, and hits retain stable source identities.
- `DeterministicChunker` uses explicit whitespace-token windows and overlap. Chunk UUID5 IDs
  include document identity, version, ordinal, and content hash.
- PostgreSQL stores generated `tsvector` text search and 1536-dimension pgvector embeddings.
  Ingestion uses a savepoint and rejects provider count/dimension drift.
- `FusedRetriever` combines keyword and vector ranks with deterministic reciprocal-rank fusion.
- Telemetry stores query hashes and bounded hit metadata, never raw queries.

## Flow

1. `RetrievalIngestionService.ingest()` adapts a policy row, chunks it, obtains and validates
   embeddings, and upserts `RetrievalChunk` rows.
2. `KeywordRetriever` runs parameterized full-text search. `VectorRetriever` embeds the query
   and orders chunks by cosine distance.
3. `FusedRetriever.search()` executes both stages, fuses ranks, and emits safe retrieval spans.
4. Agent code consumes `RetrievalHit` values. `build_cited_policy_answer()` resolves every
   model citation against those hits and fails closed on unknown or missing citations.
5. `evaluate.py` loads versioned JSONL cases, computes recall-at-k and MRR, writes failed-query
   evidence, and enforces the accepted recall floor.

## Key Files

- `contracts.py`: Documents, chunks, hits, and protocols.
- `chunking.py` / `embeddings.py`: Deterministic chunker and fake/OpenAI providers.
- `models.py` / `storage.py`: PostgreSQL model, ingestion, and retrievers.
- `fusion.py`: Rank fusion and instrumented combined retriever.
- `answers.py`: Citation resolution gate.
- `tracing.py`: Query hashing and safe hit telemetry.
- `metrics.py` / `evaluate.py`: Offline quality metrics and artifacts.

## Integration

- `app.api.agent_router` constructs OpenAI, keyword, vector, and fused retrieval adapters.
- `app.domain.agent.service` and `app.adapters.pydantic_ai_agent` retrieve policy evidence;
  agent schemas expose resolved citations.
- Persistence depends on shared SQLAlchemy sessions and PostgreSQL pgvector/full-text support.
