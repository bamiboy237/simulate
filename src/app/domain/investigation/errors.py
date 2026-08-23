"""Domain errors for investigation workflows."""

from uuid import UUID

from fastapi import status

from app.errors import DomainError


class InvestigationNotFoundError(DomainError):
    """Raised when an investigation cannot be found by ID."""

    def __init__(self, investigation_id: UUID) -> None:
        super().__init__(
            code="investigation_not_found",
            message=f"Investigation '{investigation_id}' was not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )


class InvalidStateTransitionError(DomainError):
    """Raised when an invalid state transition is requested."""

    def __init__(self, current_status: str, target_status: str) -> None:
        super().__init__(
            code="invalid_state_transition",
            message=(
                f"Cannot transition investigation status from '{current_status}' "
                f"to '{target_status}'."
            ),
            status_code=status.HTTP_409_CONFLICT,
        )


class TerminalStateImmutableError(DomainError):
    """Raised when attempting to modify an investigation in a terminal state."""

    def __init__(self, status_val: str) -> None:
        super().__init__(
            code="terminal_state_immutable",
            message=f"Investigation is in terminal state '{status_val}' and cannot be modified.",
            status_code=status.HTTP_409_CONFLICT,
        )


class InvalidBridgeTokenError(DomainError):
    """Raised when an unauthorized or mismatched bridge token is provided."""

    def __init__(self) -> None:
        super().__init__(
            code="invalid_bridge_token",
            message="The provided bridge authentication token is invalid or expired.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
