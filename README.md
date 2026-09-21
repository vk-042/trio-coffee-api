# Trio Coffee API

A runnable coffee-shop backend built with Python, Flask, and SQLite. This first implementation supports menu creation, transactional inventory reservations, idempotent order submission, and a staff-controlled fulfillment lifecycle.

## What works

- Public menu listing and order creation.
- Staff bearer-key authentication for menu creation, order lookup, and fulfillment updates.
- Server-calculated USD totals stored as integer cents; clients cannot override prices.
- Atomic stock checks and reservations with SQLite transactions, foreign keys, and WAL mode.
- Idempotency keys: a matching retry returns the same order; a changed payload returns HTTP 409.
- Status transitions: `pending → preparing → ready → completed`, or `pending → cancelled`.
- Cancellation restores stock exactly once. Prices and names are snapshotted on order items.
- JSON validation/errors, a 16 KiB request limit, request IDs, health check, and request logging.
- Docker/Gunicorn configuration, persistent volume, and a GitHub Actions test workflow.

## Local setup (Python 3.12)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
export STAFF_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
flask --app 'trio:create_app()' run
```

Run from the repository root. On Windows PowerShell, activate `.venv\Scripts\Activate.ps1` and set `$env:STAFF_API_KEY` to a generated secret. The default database is `instance/trio.sqlite3`; override with `DATABASE`. Startup creates the initial schema without deleting existing data. Keep the same staff key between restarts if you want existing clients to retain access. Never commit it.

For the containerized service:

```bash
# Set STAFF_API_KEY first, as above.
docker compose up --build -d
curl http://localhost:8000/health
```

The container runs as a non-root user. Compose binds only to localhost and persists SQLite in `coffee-data`. Do not use `docker compose down -v` unless you intend to delete that data.

## Try an order

Use port 5000 for Flask development, or 8000 for Docker.

```bash
curl -X POST http://localhost:5000/api/v1/menu \
  -H "Authorization: Bearer $STAFF_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Latte","price_cents":450,"stock":20}'

# Use the menu ID returned above (1 on a fresh database).
curl -X POST http://localhost:5000/api/v1/orders \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: example-order-001' \
  -d '{"items":[{"menu_id":1,"quantity":2}]}'
```

The new order returns HTTP 201 with `id`, `status`, `total_cents: 900`, `currency: USD`, a UTC creation timestamp, and item snapshots. Repeat the same request to get HTTP 200 with the same order and no further stock decrement. Use a new, unpredictable idempotency key for each new order; keys are retained with orders and act as retry credentials. Do not share them across customers.

```bash
curl -X PATCH http://localhost:5000/api/v1/orders/ORDER_ID/status \
  -H "Authorization: Bearer $STAFF_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"status":"preparing"}'
```

## API reference

| Method | Path | Access | Body / behavior |
| --- | --- | --- | --- |
| GET | `/health` | Public | Database connectivity check |
| GET | `/api/v1/menu` | Public | Menu, prices, available stock |
| POST | `/api/v1/menu` | Staff | `name`, `price_cents`, `stock` |
| POST | `/api/v1/orders` | Public | `items`: 1–20 unique entries, each with `menu_id` and `quantity` (1–20); `Idempotency-Key` header required |
| GET | `/api/v1/orders/{id}` | Staff | Retrieve order and item snapshots |
| PATCH | `/api/v1/orders/{id}/status` | Staff | `status`: allowed next state |

Errors use JSON `error`, `message`, and `request_id`. Common responses: 400 invalid fields, 401 missing/invalid staff key, 404 missing resource, 409 insufficient stock/key conflict/invalid transition, 413 oversized request, 415 non-JSON request, 503 database unavailable. Stock refers to sellable drink units, not ingredient-level inventory. Taxes, payments, and refunds are outside this version.

## Tests

```bash
python -m pytest -q
```

23 automated cases cover authentication, validation, money calculation, idempotency, multi-item rollback, persistent storage, cancellation, fulfillment transitions, competing orders for the last unit, and concurrent retries. Tests use isolated temporary databases. The GitHub Actions workflow runs the suite on pushes and pull requests.

## Design and operational limits

This is a tested portfolio backend, not a claim of an operating production coffee business. It has not been load-tested or deployed for real customers. SQLite is appropriate for this single-host prototype; writes serialize, and the database must be on a local persistent disk. Do not horizontally scale replicas with independent database volumes. Future schema changes need explicit versioned migrations; startup schema creation is not a migration system.

Before public deployment, add HTTPS at a reverse proxy, rate limiting and abuse controls for order submission, customer identity and ownership, per-staff accounts and secret rotation, database backups/restore checks, monitoring, and dependency security review. Public order creation currently reserves inventory without payment or reservation expiration, so an unrestricted internet deployment could exhaust stock. Customer tracking, menu edits/restocking, ingredient inventory, payment processing, and a frontend are not implemented. Docker configuration is provided but container execution must be verified in a Docker-enabled environment.
