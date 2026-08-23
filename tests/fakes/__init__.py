"""This package contains test doubles for reuse."""

from tests.fakes.investigation_repository import InMemoryInvestigationRepository
from tests.fakes.runners import FakeRunner

__all__ = ["FakeRunner", "InMemoryInvestigationRepository"]
