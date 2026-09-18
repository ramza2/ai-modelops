# Alembic migrations

Migrations are generated against the ORM metadata in `app.domain.models` and
run with a synchronous engine derived from `MODELOPS_DATABASE_URL`.

```bash
# from backend/
alembic upgrade head            # apply migrations
alembic revision --autogenerate -m "message"   # create a new migration
alembic downgrade -1            # roll back one revision
```
