"""Versioned, approved business-world contracts.

These models describe only the reviewed boundary needed to define an
experiment. They intentionally contain no executable code, credentials, or
runtime state.
"""

from collections.abc import Iterable
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.world.errors import ActorToolBoundaryError

IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_.-]*$"
VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._+-]*$"
CONTENT_HASH_PATTERN = r"^[0-9a-f]{64}$"


class ProvenanceKind(StrEnum):
    """The reviewed origin of a field in a business-world model."""

    OBSERVED = "observed"
    GENERATED = "generated"
    IMPORTED = "imported"
    VALIDATED = "validated"
    HUMAN_APPROVED = "human_approved"


class ActorKind(StrEnum):
    """The role an actor plays in a business world."""

    USER_AGENT = "user_agent"
    SCENARIO_AGENT = "scenario_agent"
    ENVIRONMENT = "environment"
    EXTERNAL_EVENT = "external_event"


class DependencyBindingMode(StrEnum):
    """The approved execution mode for a world dependency."""

    MOCK = "mock"
    GENERATED = "generated"
    RECORDED = "recorded"
    SANDBOX = "sandbox"
    STAGING = "staging"
    APPROVED_LIVE = "approved_live"


class WorldIdentity(BaseModel):
    """An approved, immutable version of a business world."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    world_id: str = Field(pattern=IDENTIFIER_PATTERN, max_length=100)
    version: str = Field(pattern=VERSION_PATTERN, max_length=100)
    content_hash: str = Field(pattern=CONTENT_HASH_PATTERN)
    approval_status: Literal["approved"] = "approved"
    approval_ref: str = Field(min_length=1, max_length=200)


class FieldProvenance(BaseModel):
    """The reviewed origin and source reference for one world-model field."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_path: str = Field(min_length=1, max_length=300)
    kind: ProvenanceKind
    source_ref: str = Field(min_length=1, max_length=500)


class WorldActor(BaseModel):
    """One actor allowed to interact with a business world."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: str = Field(pattern=IDENTIFIER_PATTERN, max_length=100)
    kind: ActorKind


class WorldTool(BaseModel):
    """One declared tool and the actor that exclusively owns it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: str = Field(pattern=IDENTIFIER_PATTERN, max_length=200)
    owner_actor_id: str = Field(pattern=IDENTIFIER_PATTERN, max_length=100)


class DependencyBinding(BaseModel):
    """An approved, versioned technical binding for one world dependency."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dependency_id: str = Field(pattern=IDENTIFIER_PATTERN, max_length=200)
    mode: DependencyBindingMode
    version: str = Field(pattern=VERSION_PATTERN, max_length=100)
    content_hash: str = Field(pattern=CONTENT_HASH_PATTERN)


class SafeStateProjection(BaseModel):
    """A versioned state view that may be shown to one declared actor.

    The projection describes field paths only. It never carries raw state or
    evaluator-only data in the world contract.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    projection_id: str = Field(pattern=IDENTIFIER_PATTERN, max_length=200)
    actor_id: str = Field(pattern=IDENTIFIER_PATTERN, max_length=100)
    version: str = Field(pattern=VERSION_PATTERN, max_length=100)
    content_hash: str = Field(pattern=CONTENT_HASH_PATTERN)
    field_paths: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_field_paths(self) -> "SafeStateProjection":
        """Reject repeated fields so every projection has one unambiguous view."""
        if len(set(self.field_paths)) != len(self.field_paths):
            raise ValueError("field_paths must not repeat")
        return self


class WorldContract(BaseModel):
    """The reviewed world boundary shared by future scenario and runner work."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+\.\d+$")
    identity: WorldIdentity
    field_provenance: tuple[FieldProvenance, ...] = ()
    actors: tuple[WorldActor, ...] = Field(min_length=1)
    tools: tuple[WorldTool, ...] = ()
    dependency_bindings: tuple[DependencyBinding, ...] = ()
    safe_state_projections: tuple[SafeStateProjection, ...] = ()

    @model_validator(mode="after")
    def validate_boundaries(self) -> "WorldContract":
        """Reject ambiguous ownership and references to undeclared actors."""
        self._require_unique(
            (actor.actor_id for actor in self.actors),
            "actors must not repeat an actor_id",
        )
        self._require_unique(
            (tool.tool_id for tool in self.tools),
            "tools must not repeat a tool_id",
        )
        self._require_unique(
            (binding.dependency_id for binding in self.dependency_bindings),
            "dependency_bindings must not repeat a dependency_id",
        )
        self._require_unique(
            (projection.projection_id for projection in self.safe_state_projections),
            "safe_state_projections must not repeat a projection_id",
        )
        self._require_unique(
            (provenance.field_path for provenance in self.field_provenance),
            "field_provenance must not repeat a field_path",
        )

        actor_ids = {actor.actor_id for actor in self.actors}
        for tool in self.tools:
            if tool.owner_actor_id not in actor_ids:
                raise ActorToolBoundaryError(
                    f"tool {tool.tool_id!r} has undeclared owner {tool.owner_actor_id!r}"
                )
        for projection in self.safe_state_projections:
            if projection.actor_id not in actor_ids:
                raise ActorToolBoundaryError(
                    f"projection {projection.projection_id!r} has undeclared actor "
                    f"{projection.actor_id!r}"
                )
        return self

    @staticmethod
    def _require_unique(values: Iterable[str], message: str) -> None:
        items = tuple(values)
        if len(set(items)) != len(items):
            raise ValueError(message)

    def tool_ids_for(self, actor_id: str) -> frozenset[str]:
        """Return the complete tool set owned by one declared actor."""
        self._require_actor(actor_id)
        return frozenset(tool.tool_id for tool in self.tools if tool.owner_actor_id == actor_id)

    def assert_tool_owned_by(self, actor_id: str, tool_id: str) -> None:
        """Raise when an actor attempts to use a tool owned by another actor."""
        if tool_id not in self.tool_ids_for(actor_id):
            raise ActorToolBoundaryError(f"actor {actor_id!r} does not own tool {tool_id!r}")

    def projections_for(self, actor_id: str) -> tuple[SafeStateProjection, ...]:
        """Return only the safe state projections assigned to one actor."""
        self._require_actor(actor_id)
        return tuple(
            projection
            for projection in self.safe_state_projections
            if projection.actor_id == actor_id
        )

    def _require_actor(self, actor_id: str) -> None:
        if actor_id not in {actor.actor_id for actor in self.actors}:
            raise ActorToolBoundaryError(f"actor {actor_id!r} is not declared by the world")
