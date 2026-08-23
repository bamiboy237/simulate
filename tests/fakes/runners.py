"""Re-export the production FakeRunner for test convenience."""

from app.domain.runner.fake import FakeRunner

__all__ = ["FakeRunner"]
