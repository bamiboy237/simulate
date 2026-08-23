---
name: performance-optimization
description: Evidence-led guidance for investigating and improving runtime speed, latency, throughput, memory use, I/O, database access, network work, or rendering. Use when a performance symptom, requirement, regression, or measured bottleneck needs attention.
---

# Performance Optimization

Do not assume code is slow because it looks inefficient. Begin with the reported symptom: what operation is slow or resource-heavy, for whom, under what input or load, and what acceptable behavior would be. Separate latency, throughput, memory, startup, rendering, and scalability concerns; improving one may not improve another.

Gather evidence when practical. Use existing benchmarks, traces, profiles, query logs, metrics, timings, production observations, or a small reproducible workload. Make the measurement representative enough to inform the decision, but do not build an elaborate benchmark system for a minor issue. If measurement is unavailable, state that clearly and label the diagnosis as an estimate.

Trace the expensive path before changing it. Prioritize causes that usually dominate total cost:

- poor algorithmic complexity or unbounded work;
- unnecessary disk, network, or database I/O;
- repeated requests, N+1 queries, or serial work that can safely be combined;
- excessive allocations, copying, parsing, or retained memory;
- avoidable recomputation, rendering, or loading of large resources;
- lock contention, blocking work, and badly sized batches or caches.

Optimize the bottleneck rather than everything nearby. Prefer removing work over making the same work slightly faster. Use batching, caching, concurrency, pagination, precomputation, or lower-level techniques only when they fit the observed problem and preserve semantics. Account for invalidation, ordering, consistency, rate limits, memory bounds, and failure behavior; a fast result that is stale, incomplete, or unsafe is not an improvement.

Make one understandable improvement at a time so its effect can be evaluated. Preserve readability unless the measured gain justifies added complexity. Avoid broad rewrites, clever micro-optimizations, pervasive memoization, and new infrastructure when a simpler change addresses the bottleneck. Follow the project’s existing performance patterns and dependencies where they are suitable.

Compare before and after under similar conditions when practical. Record the workload, metric, and result rather than saying only that the code is “faster.” Check correctness as well as performance, including representative edge cases and resource limits. Watch for regressions shifted elsewhere, such as lower latency with higher memory use or fewer queries with oversized responses.

Report conclusions with an evidence level: measured, observed, or estimated. Include relevant numbers and uncertainty. If the change cannot be measured in the available environment, explain what should be measured next and avoid claiming a verified improvement.