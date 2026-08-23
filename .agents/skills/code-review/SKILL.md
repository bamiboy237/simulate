---
name: code-review
description: Review code changes for concrete defects, requirement gaps, and operational risk. Use when asked to review a diff, branch, pull request, patch, or proposed implementation and provide prioritized, actionable findings.
---

# Code Review

Review for correctness before style. First understand the intended behavior from the request, specification, tests, surrounding code, or user explanation. Then inspect the changed paths and enough nearby code to judge how the change behaves in context. Do not assume a particular branch model, review platform, or source of requirements.

Look for concrete problems such as:

- requirements that are missing, partially implemented, or contradicted;
- incorrect behavior at boundaries, empty inputs, failure paths, or unusual state transitions;
- unsafe assumptions about types, ordering, retries, time, locale, encoding, or external systems;
- missing validation, authorization, escaping, or secret handling;
- data loss, corruption, incomplete rollback, or incompatible persisted formats;
- race conditions, deadlocks, non-atomic updates, resource leaks, or retry duplication;
- accidental breaks to public interfaces, configuration, callers, or supported environments.

Consider maintainability when the change creates a specific future hazard: duplicated business rules that can diverge, confusing ownership of state, an interface that callers can easily misuse, or complexity disproportionate to the task. Consider performance only when there is a credible costly path, such as unbounded work, repeated I/O, an obvious query waterfall, or a regression in a known hot path. Do not flag subjective preferences as defects when the code follows the project’s conventions and remains understandable.

Distinguish findings from optional improvements. A finding should describe behavior that is wrong, unsafe, incompatible, or likely to fail under realistic conditions. An optional improvement can be mentioned separately and sparingly, but it should not dilute the defect list. Do not manufacture findings to make a review appear thorough, and do not report matters already enforced reliably by existing tooling unless the tooling is failing or bypassed.

Present findings in priority order. For each finding:

1. State the severity and a concise problem title.
2. Cite the relevant file and line or the smallest useful line range.
3. Explain the triggering condition and practical consequence.
4. Suggest a direct correction or the behavior that should be preserved.

Keep each finding focused on one root problem. Avoid long summaries that hide actionable details. If no significant problems are found, say so plainly. Also state remaining verification gaps, such as tests not run, unavailable runtime dependencies, unexamined generated output, or uncertainty about an external contract. A clean review is a valid result.