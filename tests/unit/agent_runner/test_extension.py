"""Contract tests for the Prime Agent World Gateway TypeScript extension.

These are focused source-level checks: they pin the extension's alias->canonical
mapping, the official registration style, and the security boundary (token and
raw headers are never returned to the model) without adding a Node dependency.
"""

import re
from pathlib import Path

from app.domain.agent_runner.gateway import SUPPORTED_TOOLS

EXTENSION_PATH = (
    Path(__file__).parents[3]
    / "src"
    / "app"
    / "domain"
    / "agent_runner"
    / "extensions"
    / "world_gateway.ts"
)


def _source() -> str:
    return EXTENSION_PATH.read_text(encoding="utf-8")


def test_extension_file_exists() -> None:
    """Verify the extension is committed at the discovery-path source location."""
    assert EXTENSION_PATH.exists()
    assert "Approved Tools" in _source() or "APPROVED_TOOLS" in _source()


def test_extension_registers_all_nine_approved_tools() -> None:
    """Verify exactly nine identifier-safe aliases map to the nine gateway tools."""
    src = _source()
    names = re.findall(r'^\s*name:\s*"([^"]+)"\s*,\s*$', src, re.MULTILINE)
    canonicals = re.findall(
        r'^\s*canonical:\s*"([^"]+)"\s*,\s*$',
        src,
        re.MULTILINE,
    )

    assert len(names) == 9, f"expected 9 aliases, got {len(names)}"
    assert len(canonicals) == 9, f"expected 9 canonical routes, got {len(canonicals)}"

    aliases = set(names)
    assert len(aliases) == 9
    # Identifiers must be safe for tool registration names.
    assert all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias) for alias in aliases)

    # The alias set maps one-to-one onto the canonical dotted routes.
    mapped = set(canonicals)
    assert len(mapped) == 9
    assert mapped == SUPPORTED_TOOLS

    # Each alias prefix matches its canonical route stem.
    for alias, canonical in zip(names, canonicals, strict=True):
        stem = canonical.split(".")[0]
        assert alias.startswith(stem), f"{alias} does not match {canonical}"


def test_extension_posts_to_canonical_route_with_bearer_token() -> None:
    """Verify tool calls POST to /tools/{canonical_name} with Bearer auth."""
    src = _source()
    assert "${url}/tools/${canonical}" in src
    assert "Authorization: `Bearer ${token}`" in src
    assert "process.env.WORLD_GATEWAY_URL" in src
    assert "process.env.WORLD_GATEWAY_TOKEN" in src
    # The control-plane BRIDGE_TOKEN is never referenced by the extension.
    assert "process.env.BRIDGE_TOKEN" not in src
    assert "JSON.stringify({ arguments: args || {} })" in src


def test_extension_uses_official_registration_and_builtin_fetch() -> None:
    """Verify the extension follows the pinned v0.8.1 extension contract."""
    src = _source()
    assert "export default function registerWorldGatewayTools" in src
    assert "pi.registerTool(" in src
    assert "signal" in src
    # Built-in fetch is used; no Node or agent SDK runtime dependency is declared.
    assert "require(" not in src
    assert 'from "@earendil-works' not in src
    assert "from 'https" not in src


def test_extension_does_not_expose_token_or_raw_headers() -> None:
    """Verify errors are sanitized and never leak secrets or response headers."""
    src = _source()
    # Errors are thrown (official contract) and never return the raw response.
    assert "throw new Error(" in src
    assert "response.headers" not in src
    # The bearer token is confined to the Authorization header construction.
    assert "`Bearer ${token}`" in src
    # No raw response body is returned to the model.
    assert "JSON.stringify(response" not in src
    # Environment access is limited to the two gateway configuration variables.
    assert src.count("process.env.") == 2


def test_extension_imports_only_typebox() -> None:
    """Verify the extension depends only on the runtime-provided typebox package."""
    src = _source()
    imports = re.findall(r'^import .*?from\s+"([^"]+)"\s*;\s*$', src, re.MULTILINE)
    assert imports == ["typebox"]
