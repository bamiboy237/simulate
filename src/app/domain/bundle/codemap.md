# `src/app/domain/bundle/`

## Responsibility

Compiles reviewed simulation scenarios into deterministic, portable `SimulationBundle`
artifacts. This package is the privacy and provenance boundary between production evidence and
the disposable runner: it keeps source links and hashes, but replaces private identifiers and
free text with approved synthetic values.

## Design

- `SimulationBundle` is a strict, versioned Pydantic aggregate. Its validators reject unknown
  fields, invalid resource records, unsafe fixtures, unapproved reviews, and inconsistent IDs or
  hashes.
- `compiler.py` is deterministic: the same scenario, evidence, review, fixtures, and coverage
  produce the same normalized content, bundle ID, and content hash.
- `allowlist.py` owns the shared privacy policy. Owned resources use exact field allowlists and
  support-domain models; recorded payloads, arguments, metadata, and fault scripts are scanned
  recursively for secrets and source values.
- `extract.py` converts `SimulationState` rows to relationship-preserving synthetic seeds and
  validates recorded fixtures against declared dependency coverage.

## Flow

1. A caller supplies a `SimulationScenario`, optional `TraceEvidence`, human review data,
   fixtures, fault script, adapter versions, and observed `CoverageItem` values.
2. `compile_bundle()` verifies evidence identity, requires approval, sanitizes the request,
   replaces resource IDs with stable UUID5 values, and checks dependency coverage.
3. `extract_resource_seeds()` creates typed customer, order, ticket, and policy seeds.
   `extract_dependency_fixtures()` accepts only declared recorded dependencies and exact tools.
4. The compiler validates the fault boundary and scans every seed and fixture. It records
   redaction decisions and configuration versions.
5. Schema validators derive and verify the content hash and bundle ID. The runner reconstructs
   `SimulationState` from `resources_by_type()`.
6. `compile_confirmed_failure_bundle()` additionally requires a `ConfirmedFailureGroup` whose
   evidence IDs and event IDs resolve in the supplied trace.

## Key Files

- `compiler.py`: Main compiler and confirmed-failure entry point.
- `schemas.py`: Bundle, review, fixture, seed, coverage, and configuration contracts.
- `extract.py`: Synthetic IDs, seed extraction, and fixture validation.
- `allowlist.py`: Resource allowlists and recursive privacy scans.
- `errors.py`: Safe compilation error types.

## Integration

- **Inputs:** Simulation scenarios, canonical evidence, confirmed failure groups, and support
  resource models.
- **Consumers:** `app.domain.simulation.runner`, regression/suite/execution services, case and
  run APIs, and CLI bundle/run commands.
- `app.domain.simulation.recorded` imports the same sensitive-key list, so fixture replay and
  bundle compilation use one privacy policy.
