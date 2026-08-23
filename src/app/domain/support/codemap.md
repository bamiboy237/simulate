# src/app/domain/support/

## Responsibility
Encapsulates the core e-commerce support domain models (Customers, Orders, Tickets, Policy Documents) and database repositories.

## Key Files
- `models.py`: SQLAlchemy ORM models (`Customer`, `Order`, `Ticket`, `PolicyDocument`).
- `schemas.py`: Pydantic read and write schemas.
- `repository.py`: `SqlAlchemySupportRepository` providing CRUD and search operations.

## Integration
- **Consumed by:** `app.domain.simulation`, `app.domain.agent`, `app.domain.bundle`.
