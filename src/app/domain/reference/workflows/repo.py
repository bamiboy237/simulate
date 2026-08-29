"""This module provides the shared in-memory reference repository."""

import copy
import dataclasses
from typing import Any, cast

State = object


class InMemoryReferenceRepository:
    """This class stores one disposable workflow state and its mutations.

    The repository is the isolated external-effect boundary: seeding copies
    the approved state, mutations are recorded for audit evidence, and
    destroy discards everything, so no workflow run can leak state.
    """

    def __init__(self) -> None:
        self._state: State | None = None
        self._mutations: list[dict[str, object]] = []

    def seed(self, state: object) -> None:
        """This method loads one approved state as the starting point."""
        self._state = copy.deepcopy(state)
        self._mutations = []

    def snapshot(self) -> object:
        """This method returns the current state as a JSON-safe structure."""
        if self._state is None:
            raise RuntimeError("repository was not seeded")
        if hasattr(self._state, "model_dump"):
            return self._state.model_dump(mode="json")
        if dataclasses.is_dataclass(self._state) and not isinstance(self._state, type):
            return dataclasses.asdict(cast(Any, self._state))
        return copy.deepcopy(self._state)

    def replace(self, state: State) -> None:
        """This method swaps in one new state after a mutation."""
        self._state = state

    def record(
        self,
        *,
        resource: str,
        resource_id: str,
        field: str,
        before: object,
        after: object,
        reason_code: str,
    ) -> None:
        """This method records one accepted mutation for the audit trail."""
        self._mutations.append(
            {
                "resource": resource,
                "resource_id": resource_id,
                "field": field,
                "before": str(before),
                "after": str(after),
                "reason_code": reason_code,
            }
        )

    def mutations(self) -> tuple[dict[str, object], ...]:
        """This method returns the recorded mutation trail."""
        return tuple(self._mutations)

    def destroy(self) -> None:
        """This method discards the disposable state and closes the environment."""
        self._state = None
        self._mutations = []


def update_state(state: object, **updates: object) -> object:
    """This function copies one state with the given field updates.

    It works for pydantic models and dataclasses, the two state shapes the
    approved reference workflows use.
    """
    if hasattr(state, "model_copy"):
        return state.model_copy(update=updates)
    if dataclasses.is_dataclass(state) and not isinstance(state, type):
        return dataclasses.replace(cast(Any, state), **updates)
    raise ValueError("reference state must be a pydantic model or dataclass")
