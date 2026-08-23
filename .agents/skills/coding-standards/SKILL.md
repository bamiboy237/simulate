---
name: coding-standards
description: Practical, language- and framework-neutral guidance for implementing features, fixing bugs, and refactoring existing code. Use when making code changes and deciding scope, structure, compatibility, tests, or verification.
---

# Coding Standards

Before editing, understand the behavior being changed and the conventions around it. Read the relevant code, nearby tests, public interfaces, and project guidance. Follow established naming, layout, error handling, and dependency patterns unless they create a concrete problem for this change. Do not impose a preferred architecture on an unfamiliar codebase.

Make the smallest complete change that solves the actual problem. “Smallest” does not mean leaving the behavior half-finished: update affected callers, tests, documentation, configuration, or migrations when they are necessary for a working result. Keep unrelated cleanup out of the change unless it is required to proceed safely.

Prefer code that explains itself:

- Use names that describe domain meaning and intent.
- Keep control flow straightforward; reduce deep nesting and hidden state where practical.
- Handle expected failures explicitly and provide useful error context without exposing secrets.
- Keep modules cohesive and interfaces easy for callers to understand.
- Make dependencies and important side effects visible rather than surprising.

Introduce an abstraction only when it removes real duplication, contains meaningful complexity, or supports variation that already exists. Avoid speculative extension points, premature generalization, pass-through layers, and design-pattern ceremony. Separate concerns when doing so improves clarity, testing, ownership of state, or the ability to change one concern independently—not simply to create more files or layers.

Preserve existing behavior and compatibility by default. If the request requires a breaking change, identify affected consumers and make the impact clear. Treat data formats, persisted state, command-line behavior, configuration, and externally used interfaces as compatibility surfaces even when they are not formally documented.

When behavior changes, add or update tests at the most stable observable interface available. Prefer tests that describe outcomes over tests coupled to private implementation details. Match testing effort to risk: a focused unit test may be enough for isolated logic, while persistence, concurrency, security, migrations, and public interfaces warrant broader checks. Strict test-first development is optional; reliable regression coverage is not.

Before declaring completion, run the narrowest relevant checks available, then broaden them when the change is risky or touches shared behavior. Report what was run and any checks that could not be run. Briefly document decisions that are important and non-obvious—especially compatibility constraints or surprising tradeoffs—but avoid comments and documents that merely restate the code.