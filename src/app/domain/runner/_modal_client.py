"""Low-level wrapper isolating all Modal SDK interactions.

This module encapsulates every Modal SDK import and call so that the rest of
the codebase and test suites stay completely offline and mock a single file.
"""

import modal

PYTHON_VERSION = "3.12"
MODAL_IMAGE_VERSION = "1.0.0"
PRIME_AGENT_VERSION = "0.2.0"


def build_investigation_image() -> modal.Image:
    """Build the standard container image recipe for investigation sandboxes."""
    return (
        modal.Image.debian_slim(python_version=PYTHON_VERSION)
        .apt_install("git", "curl", "postgresql-client")
        .pip_install(
            "httpx>=0.28.1",
            "pydantic>=2.10.0",
            "fastapi>=0.115.0",
        )
        .run_commands("curl -fsSL https://app.primeintellect.ai/prime-agent/install.sh | sh")
        .add_local_python_source("app")
        .env({"MODAL_IMAGE_VERSION": MODAL_IMAGE_VERSION})
    )


def create_sandbox(
    app_name: str,
    command: list[str],
    *,
    env: dict[str, str | None],
    secrets: dict[str, str | None],
    timeout_s: int = 3600,
    cpus: float = 1.0,
    memory_mib: int = 2048,
    image: modal.Image | None = None,
) -> str:
    """Create a detached sandbox container on Modal and return its sandbox ID."""
    app = modal.App.lookup(app_name, create_if_missing=True)
    container_image = image or build_investigation_image()

    secret_objs = [modal.Secret.from_dict(secrets)] if secrets else []

    sandbox = modal.Sandbox.create(
        *command,
        app=app,
        image=container_image,
        env=env,
        secrets=secret_objs,
        timeout=timeout_s,
        cpu=cpus,
        memory=memory_mib,
    )
    return sandbox.object_id


def poll_sandbox(sandbox_id: str) -> int | None:
    """Poll the sandbox return code. Returns None if still running."""
    try:
        sandbox = modal.Sandbox.from_id(sandbox_id)
        return sandbox.poll()
    except Exception:
        # If sandbox cannot be contacted or has been cleaned up, treat as terminated
        return -1


def terminate_sandbox(sandbox_id: str) -> None:
    """Terminate the sandbox container. Safe to call multiple times."""
    try:
        sandbox = modal.Sandbox.from_id(sandbox_id)
        sandbox.terminate()
    except Exception:
        # Idempotent: ignore if already stopped or not found
        pass
