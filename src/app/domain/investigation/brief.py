"""Task brief assembly for Prime Agent investigation runs."""

from typing import Any

from app.domain.runner.schemas import EnvironmentSliceRef


def render_task_brief(
    *,
    trace_id: str,
    slice_ref: EnvironmentSliceRef,
    trace_summary: str | None = None,
    evaluator_expectations: list[str] | None = None,
    rules: list[dict[str, Any]] | None = None,
) -> str:
    """Render a structured markdown task brief for Prime Agent.

    The task brief provides full incident context and mandates that the final
    action in the run is a summary.submit tool call with findings and recommendations.
    """
    lines: list[str] = [
        f"# Investigation Task Brief: {trace_id}",
        "",
        "## 1. Incident Overview",
        f"- **Trace ID:** {trace_id}",
        f"- **Environment World:** {slice_ref.world_id} (version {slice_ref.world_version})",
        f"- **Slice Name:** {slice_ref.slice_name}",
        f"- **Provenance:** {slice_ref.provenance}",
        f"- **Fixture Bundle:** {slice_ref.fixture_bundle_ref}",
    ]

    if trace_summary:
        lines.extend([
            "",
            "## 2. Observed Failure Summary",
            trace_summary.strip(),
        ])

    if rules:
        lines.extend([
            "",
            "## 3. Applicable Business Rules & Policies",
        ])
        for rule in rules:
            rule_id = rule.get("id", "rule")
            desc = rule.get("description", "")
            prov = rule.get("provenance", slice_ref.provenance)
            lines.append(f"- **[{prov.upper()}] {rule_id}:** {desc}")

    if evaluator_expectations:
        lines.extend([
            "",
            "## 4. Evaluator Expectations",
        ])
        for exp in evaluator_expectations:
            lines.append(f"- {exp}")

    lines.extend([
        "",
        "## 5. Investigation Protocol & Mandatory Final Step",
        "1. Inspect trace records and reproduction environment state via World Gateway tools.",
        "2. Reproduce and diagnose the root cause of the observed failure.",
        "3. **MANDATORY FINAL ACTION:** Your final tool call MUST be `summary.submit` containing:",
        "   - `findings`: Concrete analysis of root cause and reproduction proof.",
        "   - `next_step`: Specific, actionable mitigation or bug fix recommendation.",
        "   - `evidence_refs`: Array of supporting event/artifact references.",
    ])

    return "\n".join(lines)
