#!/usr/bin/env python3
"""Homestead MCP server - the household knowledge base as agent tools.

A thin wrapper over homestead/registry.py, which holds the actual queries and
is tested independently. Register this in Open WebUI as a tool server so the
Archivist and Scout agents can answer from record rather than from guesswork.

    python homestead/server.py

Connection settings come from the environment: HOMESTEAD_DATABASE_URL, or the
standard PG* variables. See homestead/README.md.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Optional

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from homestead import registry  # noqa: E402

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - surfaced at startup only
    raise SystemExit(
        "Could not import FastMCP. This server targets the mcp 1.x API; "
        "mcp 2.x renamed FastMCP to MCPServer. Install with 'pip install "
        f'"mcp>=0.9.0,<2"\'. Original error: {exc}'
    )

HOST = os.getenv("HOMESTEAD_MCP_HOST", "127.0.0.1")
PORT = int(os.getenv("HOMESTEAD_MCP_PORT", "9100"))

mcp = FastMCP("homestead", host=HOST, port=PORT)


def _connect():
    """Open a short-lived connection.

    Per-call connections rather than a pool: this server answers a handful of
    queries a minute at most, and a stale connection after a database restart
    is a far more likely failure than connection overhead being a problem.
    """
    return psycopg.connect(registry.connection_string(), autocommit=True)


@mcp.tool()
def search_equipment(
    query: Optional[str] = None,
    property_name: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
) -> dict[str, Any]:
    """Search household equipment by name, manufacturer, model or serial number.

    Use for questions like "what is the model of the irrigation valve" or
    "which SONOFF devices do we own".
    """
    with _connect() as conn:
        results = registry.search_equipment(
            conn,
            query,
            property_name=property_name,
            category=category,
            status=status,
        )
    return {"count": len(results), "equipment": results}


@mcp.tool()
def get_equipment(equipment_id: str) -> dict[str, Any]:
    """Full detail for one item of equipment, including linked manuals and receipts."""
    with _connect() as conn:
        item = registry.get_equipment(conn, equipment_id)
    if item is None:
        return {"error": f"no equipment with id {equipment_id}"}
    return item


@mcp.tool()
def find_unreachable_equipment(property_name: Optional[str] = None) -> dict[str, Any]:
    """List equipment that has been bought but never confirmed working.

    These are devices with no proven integration path - bought, then stranded.
    Worth reviewing before buying anything else.
    """
    with _connect() as conn:
        results = registry.find_unreachable(conn, property_name)
    return {"count": len(results), "equipment": results}


@mcp.tool()
def check_compatibility(subject: str) -> dict[str, Any]:
    """Check what is known about a device, chip or ecosystem before buying it.

    Always call this before recommending or approving a hardware purchase. A
    verdict of 'unknown' means nothing is on record and the radio and
    integration path must be verified first - it does not mean it will work.
    """
    with _connect() as conn:
        return registry.check_compatibility(conn, subject)


@mcp.tool()
def record_compatibility(
    subject: str,
    verdict: str,
    requires: Optional[str] = None,
    caveat: Optional[str] = None,
    evidence_url: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict[str, Any]:
    """Record a compatibility finding so it is never rediscovered the hard way.

    verdict must be one of: works, works_with_caveats, blocked, unknown.
    Include evidence_url wherever the finding came from a documented source.
    """
    try:
        with _connect() as conn:
            return registry.record_compatibility(
                conn,
                subject,
                verdict,
                requires=requires,
                caveat=caveat,
                evidence_url=evidence_url,
                notes=notes,
            )
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool()
def upcoming(within_days: int = 90) -> dict[str, Any]:
    """Warranties, health follow-ups and dated obligations falling due.

    Negative days_remaining means already overdue.
    """
    with _connect() as conn:
        return registry.upcoming(conn, within_days)


@mcp.tool()
def usage_summary(
    utility: str, property_name: Optional[str] = None, months: int = 12
) -> dict[str, Any]:
    """Water, electricity or gas consumption over recent months.

    utility must be one of: water, electricity, gas. Meters read cumulatively,
    so 'consumed' is the difference between successive readings; the earliest
    reading in the window has none.
    """
    with _connect() as conn:
        return registry.usage_summary(
            conn, utility, property_name=property_name, months=months
        )


@mcp.tool()
def solar_progress(property_name: Optional[str] = None) -> dict[str, Any]:
    """Generation per solar panel, so each panel added shows its contribution."""
    with _connect() as conn:
        return registry.solar_progress(conn, property_name)


@mcp.tool()
def watering_plan(location_name: Optional[str] = None) -> dict[str, Any]:
    """How much water each plant needs and how often.

    litres_per_week is null where the plant's needs have not been recorded -
    that is a gap in the record, not a plant that needs no water.
    """
    with _connect() as conn:
        results = registry.watering_plan(conn, location_name)
    return {"count": len(results), "plants": results}


@mcp.tool()
def find_account(service: str) -> dict[str, Any]:
    """Find where the credentials for a service are kept.

    Returns the vault or safe location, never a password - no secret is stored
    in this database. Use this to tell someone how to get into an account, not
    to hand them the secret itself.
    """
    with _connect() as conn:
        results = registry.find_account(conn, service)
    return {"count": len(results), "accounts": results}


if __name__ == "__main__":
    print(f"Homestead MCP server on http://{HOST}:{PORT}/mcp", file=sys.stderr)
    mcp.run(transport="streamable-http")
