# Homestead registry

The structured half of the household knowledge base: what you own, where it
came from, when it expires, what it reads this month — and what has already
been learned about which devices actually work.

Three stores, each doing what it is good at:

| Store | Holds |
|---|---|
| **PostgreSQL** (here) | Equipment, serials, warranties, people, health dates, meter readings, solar, plants, compatibility findings |
| **Paperless-ngx** | The scanned paper — manuals, receipts, warranty cards. Referenced here by document id |
| **Open WebUI Knowledge** | The ebook and how-to corpus, for semantic search |

## Setup

```bash
createdb homestead
psql -d homestead -f homestead/sql/001_schema.sql
psql -d homestead -f homestead/sql/002_seed_compatibility.sql
```

The seed loads what has already been established about the Sonoff hardware,
the MG24 dongle, the Windows limitations and the camera situation — so those
findings survive this conversation.

Configure the connection with `HOMESTEAD_DATABASE_URL`, or the standard `PGHOST`
/ `PGPORT` / `PGUSER` / `PGPASSWORD` / `PGDATABASE` variables.

## Running it

```bash
pip install "psycopg[binary]" "mcp>=0.9.0,<2"
python homestead/server.py          # http://127.0.0.1:9100/mcp
```

Register that URL in Open WebUI as a streamable-HTTP MCP tool server, and the
Archivist and Scout agents can answer from record.

## Two deliberate design decisions

**No secrets are stored here.** The `accounts` table records *where* a password
lives — "Bitwarden > Home > Netflix", "sealed envelope in the safe" — and
`secret_location` is `NOT NULL`, because an account nobody can get into is not
recorded, it is lost. This is what lets the custodian agent tell a family member
how to get in without any secret ever sitting in a database an LLM can read, or
in a backup, or in a query log. If a secret genuinely must live here, add a
pgcrypto column and keep the key outside the agent's reach.

**`integration_path` is null until a device is proven working.** That null is
the marker for a stranded purchase, and `find_unreachable_equipment` is the
query that surfaces it. The water valve is exactly this case: owned, on the
network diagram, and reaching nothing.

## Tests

Run against a real PostgreSQL — the queries use window functions, interval
arithmetic and check constraints that only a real server enforces:

```bash
createdb homestead_test
HOMESTEAD_TEST_DSN="dbname=homestead_test" pytest tests/test_homestead_registry.py
```

They skip cleanly when `HOMESTEAD_TEST_DSN` is unset, so CI without a database
stays green.

## Note on where this lives

This sits inside a fork of `comfyui-mcp-server` because that is the repository
the work started in. It is a separate product and should graduate to its own
repository — kept in its own directory so that move is a `git mv` rather than
an untangling.
