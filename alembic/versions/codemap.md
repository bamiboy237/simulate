# alembic/versions/

## Responsibility
Stores ordered, immutable schema revisions. Each file defines `upgrade()` and `downgrade()`.

## Revision Lineage

| Order | Revision | Change |
|:---|:---|:---|
| 1 | `0001_baseline` | Empty baseline. |
| 2 | `7ecfe203df28` | Support customers, orders, and tickets. |
| 3 | `4f51cd9c287d` | Policy documents. |
| 4 | `a3d9c2b1e4f5` | Imported trace evidence. |
| 5 | `b7e1f4c8d2a6` | Trace source-identity uniqueness. |
| 6 | `466f31a0c5ac` | Versioned regression cases. |
| 7 | `4bb75b530756` | Versioned regression suites. |
| 8 | `c9a6f3b1d2e4` | Failure feedback, proposals, and reviews. |
| 9 | `9f7c2a1d5e4b` | Retrieval chunks and pgvector index. |
| 10 | `d8f4e2a1b9c7` | Investigations, events, messages, and summaries. |

The chain is linear. Investigation child rows use cascading foreign keys; event sequence and
summary constraints enforce replay and one-summary invariants in PostgreSQL.

## Flow
`uv run alembic upgrade head` loads `alembic/env.py`, reads the current database revision,
and applies each pending `upgrade()` in order. Downgrades reverse one or more revisions.

## Integration
- Managed by [`alembic/env.py`](../env.py).
- Schemas correspond to models under
  `src/app/domain/{support,evidence,regression,suite,failures,retrieval,investigation}/`.
