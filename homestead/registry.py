"""Queries over the homestead registry.

Deliberately free of any MCP dependency so it can be tested against a real
database on its own; `homestead/server.py` is the thin MCP wrapper over these
functions. Every function takes an open psycopg connection as its first
argument and returns plain dicts and lists, ready to serialise.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any, Optional

from psycopg.rows import dict_row


def connection_string() -> str:
    """Build a libpq connection string from the environment."""
    if url := os.getenv("HOMESTEAD_DATABASE_URL"):
        return url
    host = os.getenv("PGHOST", "localhost")
    port = os.getenv("PGPORT", "5432")
    user = os.getenv("PGUSER", "homestead")
    database = os.getenv("PGDATABASE", "homestead")
    parts = [f"host={host}", f"port={port}", f"user={user}", f"dbname={database}"]
    if password := os.getenv("PGPASSWORD"):
        parts.append(f"password={password}")
    return " ".join(parts)


def _rows(conn, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _row(conn, sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
    rows = _rows(conn, sql, params)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Equipment
# ---------------------------------------------------------------------------


def search_equipment(
    conn,
    query: Optional[str] = None,
    *,
    property_name: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Free-text search across name, manufacturer, model and serial number."""
    sql = """
        SELECT e.id, e.name, e.manufacturer, e.model, e.serial_number,
               e.category, e.protocol, e.integration_path, e.status,
               e.purchased_on, e.warranty_expires,
               p.name AS property_name, l.name AS location_name
        FROM equipment e
        JOIN properties p ON p.id = e.property_id
        LEFT JOIN locations l ON l.id = e.location_id
        WHERE (%s::text IS NULL OR (
                  e.name          ILIKE '%%' || %s::text || '%%'
               OR e.manufacturer  ILIKE '%%' || %s::text || '%%'
               OR e.model         ILIKE '%%' || %s::text || '%%'
               OR e.serial_number ILIKE '%%' || %s::text || '%%'))
          AND (%s::text IS NULL OR p.name = %s::text)
          AND (%s::text IS NULL OR e.category = %s::text)
          AND (%s::text IS NULL OR e.status = %s::text)
        ORDER BY e.name
        LIMIT %s
    """
    return _rows(
        conn,
        sql,
        (
            query, query, query, query, query,
            property_name, property_name,
            category, category,
            status, status,
            limit,
        ),
    )


def get_equipment(conn, equipment_id: str) -> Optional[dict[str, Any]]:
    """Full detail for one item, including any linked documents."""
    item = _row(
        conn,
        """
        SELECT e.*, p.name AS property_name, l.name AS location_name
        FROM equipment e
        JOIN properties p ON p.id = e.property_id
        LEFT JOIN locations l ON l.id = e.location_id
        WHERE e.id = %s
        """,
        (equipment_id,),
    )
    if item is None:
        return None

    item["documents"] = _rows(
        conn,
        """
        SELECT d.id, d.title, d.kind, d.paperless_id, d.external_url,
               d.document_date
        FROM documents d
        JOIN equipment_documents ed ON ed.document_id = d.id
        WHERE ed.equipment_id = %s
        ORDER BY d.kind, d.title
        """,
        (equipment_id,),
    )
    return item


def find_unreachable(conn, property_name: Optional[str] = None) -> list[dict[str, Any]]:
    """Equipment bought but never proven to reach the platform.

    A null integration_path is the marker: the device exists on paper but
    nothing has confirmed it actually talks to anything. This is the query
    that surfaces a stranded purchase before another one joins it.
    """
    return _rows(
        conn,
        """
        SELECT e.id, e.name, e.manufacturer, e.model, e.protocol,
               e.purchased_on, e.status, p.name AS property_name
        FROM equipment e
        JOIN properties p ON p.id = e.property_id
        WHERE e.integration_path IS NULL
          AND e.status IN ('active', 'spare')
          AND (%s::text IS NULL OR p.name = %s::text)
        ORDER BY e.purchased_on NULLS LAST, e.name
        """,
        (property_name, property_name),
    )


# ---------------------------------------------------------------------------
# Compatibility - consulted before money is spent
# ---------------------------------------------------------------------------


def check_compatibility(conn, subject: str) -> dict[str, Any]:
    """Look up what is known about a device, chip or ecosystem.

    Matching is deliberately loose in both directions: a stored subject that
    contains the query, or a query that contains the stored subject. Asking
    about "Sonoff MG24 dongle" should still surface the rule filed under
    "SONOFF Dongle Plus MG24 (EFR32MG24)".
    """
    matches = _rows(
        conn,
        """
        SELECT subject, verdict, requires, caveat, evidence_url,
               checked_on, notes
        FROM compatibility_rules
        WHERE lower(subject) LIKE '%%' || lower(%s) || '%%'
           OR lower(%s) LIKE '%%' || lower(subject) || '%%'
        ORDER BY
            CASE verdict
                WHEN 'blocked'            THEN 0
                WHEN 'works_with_caveats' THEN 1
                WHEN 'unknown'            THEN 2
                ELSE 3
            END,
            checked_on DESC
        """,
        (subject, subject),
    )

    if not matches:
        return {
            "subject": subject,
            "verdict": "unknown",
            "matches": [],
            "advice": (
                "Nothing on record. Verify the radio and the integration path "
                "before buying - an unverified assumption is what produces "
                "unusable hardware."
            ),
        }

    # Report the most serious verdict found, not the first alphabetically.
    return {
        "subject": subject,
        "verdict": matches[0]["verdict"],
        "matches": matches,
    }


def record_compatibility(
    conn,
    subject: str,
    verdict: str,
    *,
    requires: Optional[str] = None,
    caveat: Optional[str] = None,
    evidence_url: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict[str, Any]:
    """Record a finding so it is never rediscovered the expensive way."""
    valid = {"works", "works_with_caveats", "blocked", "unknown"}
    if verdict not in valid:
        raise ValueError(f"verdict must be one of {sorted(valid)}, got {verdict!r}")

    row = _row(
        conn,
        """
        INSERT INTO compatibility_rules
            (subject, verdict, requires, caveat, evidence_url, notes)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id, subject, verdict, checked_on
        """,
        (subject, verdict, requires, caveat, evidence_url, notes),
    )
    return row


# ---------------------------------------------------------------------------
# The things that quietly lapse
# ---------------------------------------------------------------------------


def upcoming(conn, within_days: int = 90) -> dict[str, list[dict[str, Any]]]:
    """Warranties, health follow-ups and dated obligations coming due.

    One call rather than three, because the point is a single answer to
    "what have I let slip".
    """
    warranties = _rows(
        conn,
        """
        SELECT e.name, e.manufacturer, e.warranty_expires AS due_on,
               (e.warranty_expires - CURRENT_DATE) AS days_remaining,
               p.name AS property_name
        FROM equipment e
        JOIN properties p ON p.id = e.property_id
        WHERE e.warranty_expires IS NOT NULL
          AND e.status = 'active'
          AND e.warranty_expires <= CURRENT_DATE + make_interval(days => %s::int)
        ORDER BY e.warranty_expires
        """,
        (within_days,),
    )

    health = _rows(
        conn,
        """
        SELECT pe.name AS person, h.kind, h.description,
               h.next_due_on AS due_on,
               (h.next_due_on - CURRENT_DATE) AS days_remaining
        FROM health_records h
        JOIN people pe ON pe.id = h.person_id
        WHERE h.next_due_on IS NOT NULL
          AND h.next_due_on <= CURRENT_DATE + make_interval(days => %s::int)
        ORDER BY h.next_due_on
        """,
        (within_days,),
    )

    dates = _rows(
        conn,
        """
        SELECT title, category, due_on,
               (due_on - CURRENT_DATE) AS days_remaining, notes
        FROM important_dates
        WHERE completed_on IS NULL
          AND due_on <= CURRENT_DATE + make_interval(days => %s::int)
        ORDER BY due_on
        """,
        (within_days,),
    )

    return {
        "within_days": within_days,
        "warranties": warranties,
        "health": health,
        "important_dates": dates,
        "overdue_count": sum(
            1
            for group in (warranties, health, dates)
            for item in group
            if item["days_remaining"] is not None and item["days_remaining"] < 0
        ),
    }


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def usage_summary(
    conn,
    utility: str,
    *,
    property_name: Optional[str] = None,
    months: int = 12,
) -> dict[str, Any]:
    """Month-by-month consumption, derived from consecutive meter readings.

    Meters are cumulative, so consumption is the difference between successive
    readings rather than the reading itself. The first reading in the window
    therefore has no consumption figure - there is nothing before it to
    subtract.
    """
    rows = _rows(
        conn,
        """
        SELECT m.read_on, m.reading, m.unit, m.cost,
               m.reading - LAG(m.reading) OVER (
                   PARTITION BY m.property_id, m.utility ORDER BY m.read_on
               ) AS consumed
        FROM meter_readings m
        JOIN properties p ON p.id = m.property_id
        WHERE m.utility = %s
          AND (%s::text IS NULL OR p.name = %s::text)
          AND m.read_on >= CURRENT_DATE - make_interval(months => %s::int)
        ORDER BY m.read_on
        """,
        (utility, property_name, property_name, months),
    )

    consumed = [r["consumed"] for r in rows if r["consumed"] is not None]
    return {
        "utility": utility,
        "months": months,
        "readings": rows,
        "total_consumed": sum(consumed) if consumed else None,
        "unit": rows[0]["unit"] if rows else None,
    }


def solar_progress(conn, property_name: Optional[str] = None) -> dict[str, Any]:
    """Per-panel generation, so each panel added shows its own contribution."""
    panels = _rows(
        conn,
        """
        SELECT sp.label, sp.watts_peak, sp.installed_on, sp.orientation,
               COALESCE(SUM(sr.kwh_generated), 0) AS kwh_total,
               COUNT(sr.id)                       AS reading_count
        FROM solar_panels sp
        JOIN properties p ON p.id = sp.property_id
        LEFT JOIN solar_readings sr ON sr.panel_id = sp.id
        WHERE (%s::text IS NULL OR p.name = %s::text)
        GROUP BY sp.id, sp.label, sp.watts_peak, sp.installed_on, sp.orientation
        ORDER BY sp.installed_on NULLS LAST, sp.label
        """,
        (property_name, property_name),
    )
    return {
        "panels": panels,
        "total_watts_peak": sum(p["watts_peak"] or 0 for p in panels),
        "total_kwh": sum(p["kwh_total"] for p in panels),
    }


# ---------------------------------------------------------------------------
# Plants
# ---------------------------------------------------------------------------


def watering_plan(conn, location_name: Optional[str] = None) -> list[dict[str, Any]]:
    """What needs watering, how much, and how often."""
    return _rows(
        conn,
        """
        SELECT pl.common_name, pl.species, pl.water_litres,
               pl.water_every_days, pl.sun, pl.notes,
               l.name AS location_name,
               CASE
                   WHEN pl.water_litres IS NOT NULL
                    AND pl.water_every_days IS NOT NULL
                   THEN round(pl.water_litres * 7.0 / pl.water_every_days, 2)
               END AS litres_per_week
        FROM plants pl
        LEFT JOIN locations l ON l.id = pl.location_id
        WHERE (%s::text IS NULL OR l.name = %s::text)
        ORDER BY l.name NULLS LAST, pl.common_name
        """,
        (location_name, location_name),
    )


# ---------------------------------------------------------------------------
# Accounts - locations of secrets, never secrets
# ---------------------------------------------------------------------------


def find_account(conn, service: str) -> list[dict[str, Any]]:
    """Where to find the credentials for a service.

    Returns the vault or safe location, never a secret: no password is stored
    in this database, so none can leak through an agent, a backup or a log.
    """
    return _rows(
        conn,
        """
        SELECT service, category, username, secret_location, recovery_notes,
               renews_on, monthly_cost, currency, notes
        FROM accounts
        WHERE service ILIKE '%%' || %s || '%%'
        ORDER BY service
        """,
        (service,),
    )
