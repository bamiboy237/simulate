"""Vendor-neutral contracts used by the retrieval pipeline."""

from collections.abc import Sequence
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SourceDocument(BaseModel):
    """A versioned source document that can be chunked and indexed.

    ``document_id`` is the stable identity of the logical document.  A changed
    version keeps that identity but receives a new content hash and chunks.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: UUID
    slug: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1)
    source_metadata: dict[str, str] = Field(default_factory=dict)

    @property
    def id(self) -> UUID:
        """Return the stable document identity."""
        return self.document_id


class DocumentChunk(BaseModel):
    """One deterministic, source-addressable chunk."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: UUID
    document_id: UUID
    document_slug: str
    document_version: str
    title: str
    ordinal: int = Field(ge=0)
    text: str = Field(min_length=1)
    content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    source_metadata: dict[str, str] = Field(default_factory=dict)

    @property
    def id(self) -> UUID:
        """Return the stable chunk identity."""
        return self.chunk_id


RetrievalSource = Literal["keyword", "vector", "fused"]


class RetrievalHit(BaseModel):
    """One ranked chunk returned by a retriever."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: UUID
    document_id: UUID
    document_version: str
    text: str = Field(min_length=1)
    score: float
    source: RetrievalSource
    corpus_version: str = Field(min_length=1, max_length=100)
    rank: int = Field(ge=1)
    contributing_ranks: dict[str, int] = Field(default_factory=dict)
    contributing_scores: dict[str, float] = Field(default_factory=dict)


class EmbeddingProvider(Protocol):
    """The one contract that retrieval code uses for embeddings."""

    model: str
    dimension: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed texts in the same order as the input sequence."""

    async def embed_one(self, text: str) -> list[float]:
        """Embed one text."""


class Retriever(Protocol):
    """A retriever returns ranked, source-cited chunks."""

    async def search(self, query: str, limit: int = 5) -> list[RetrievalHit]:
        """Search the indexed corpus without exposing storage details."""

