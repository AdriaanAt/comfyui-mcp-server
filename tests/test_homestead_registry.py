"""Tests for homestead/registry.py, run against a real PostgreSQL database.

Skipped unless HOMESTEAD_TEST_DSN points at a database the tests may create and
drop objects in. Real Postgres rather than a fake, because the queries lean on
window functions, interval arithmetic and check constraints that only a real
server enforces.

    createdb homestead_test
    HOMESTEAD_TEST_DSN="dbname=homestead_test" pytest tests/test_homestead_registry.py
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

psycopg = pytest.importorskip("psycopg")

from homestead import registry  # noqa: E402

DSN = os.getenv("HOMESTEAD_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set HOMESTEAD_TEST_DSN to run homestead registry tests"
)

SQL_DIR = REPO_ROOT / "homestead" / "sql"


@pytest.fixture(scope="module")
def conn():
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        # Apply every migration in order, so the fixture exercises the same
        # sequence a real database receives - corrections included.
        for path in sorted(SQL_DIR.glob("*.sql")):
            connection.execute(path.read_text())
        yield connection


@pytest.fixture
def property_id(conn):
    row = conn.execute(
        "INSERT INTO properties (name) VALUES ('Home') "
        "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id"
    ).fetchone()
    return row[0]


def test_search_equipment_matches_serial_number(conn, property_id):
    conn.execute(
        """
        INSERT INTO equipment (property_id, name, manufacturer, serial_number)
        VALUES (%s, 'Water valve', 'SONOFF', 'SNZB-XYZ-991')
        ON CONFLICT DO NOTHING
        """,
        (property_id,),
    )

    by_serial = registry.search_equipment(conn, "XYZ-991")
    assert [r["name"] for r in by_serial] == ["Water valve"]

    by_maker = registry.search_equipment(conn, "sonoff")
    assert any(r["manufacturer"] == "SONOFF" for r in by_maker)


def test_search_equipment_without_query_returns_everything(conn, property_id):
    assert len(registry.search_equipment(conn)) >= 1


def test_find_unreachable_surfaces_equipment_with_no_integration_path(
    conn, property_id
):
    conn.execute(
        """
        INSERT INTO equipment (property_id, name, integration_path)
        VALUES (%s, 'Wired doorbell', 'hardwired, no integration needed')
        """,
        (property_id,),
    )

    stranded = [r["name"] for r in registry.find_unreachable(conn)]
    # The valve was inserted with no integration_path; the doorbell has one.
    assert "Water valve" in stranded
    assert "Wired doorbell" not in stranded


def test_find_unreachable_ignores_retired_equipment(conn, property_id):
    conn.execute(
        """
        INSERT INTO equipment (property_id, name, status)
        VALUES (%s, 'Old hub in a drawer', 'retired')
        """,
        (property_id,),
    )
    assert "Old hub in a drawer" not in [
        r["name"] for r in registry.find_unreachable(conn)
    ]


def test_check_compatibility_finds_seeded_rule_from_a_loose_query(conn):
    """Asking in your own words should still find the filed rule."""
    result = registry.check_compatibility(conn, "MG24")
    assert result["verdict"] == "works_with_caveats"
    assert any("MG24" in m["subject"] for m in result["matches"])
    assert "WSL2" in result["matches"][0]["requires"]


def test_check_compatibility_reports_the_most_serious_verdict_first(conn):
    """A blocked finding must not be buried under a cheerful one."""
    result = registry.check_compatibility(conn, "Zigbee2MQTT on native Windows")
    assert result["verdict"] == "blocked"


def test_check_compatibility_admits_ignorance(conn):
    result = registry.check_compatibility(conn, "some device nobody has tried")
    assert result["verdict"] == "unknown"
    assert result["matches"] == []
    assert "before buying" in result["advice"]


def test_record_compatibility_rejects_an_invented_verdict(conn):
    with pytest.raises(ValueError, match="verdict must be one of"):
        registry.record_compatibility(conn, "Thing", "probably fine")


def test_record_compatibility_round_trips(conn):
    registry.record_compatibility(
        conn,
        "Test Widget 9000",
        "blocked",
        caveat="Cloud-only, no local API.",
        evidence_url="https://example.invalid/widget",
    )
    result = registry.check_compatibility(conn, "Test Widget 9000")
    assert result["verdict"] == "blocked"
    assert result["matches"][0]["caveat"] == "Cloud-only, no local API."


def test_upcoming_flags_overdue_separately_from_merely_due(conn, property_id):
    conn.execute(
        """
        INSERT INTO equipment (property_id, name, warranty_expires)
        VALUES (%s, 'Expired thing', CURRENT_DATE - 10),
               (%s, 'Expiring thing', CURRENT_DATE + 10)
        """,
        (property_id, property_id),
    )

    result = registry.upcoming(conn, within_days=30)
    names = {w["name"]: w["days_remaining"] for w in result["warranties"]}

    assert names["Expired thing"] < 0
    assert names["Expiring thing"] > 0
    assert result["overdue_count"] >= 1


def test_upcoming_respects_its_window(conn, property_id):
    conn.execute(
        """
        INSERT INTO equipment (property_id, name, warranty_expires)
        VALUES (%s, 'Far future thing', CURRENT_DATE + 400)
        """,
        (property_id,),
    )
    result = registry.upcoming(conn, within_days=30)
    assert "Far future thing" not in [w["name"] for w in result["warranties"]]


def test_usage_summary_derives_consumption_from_cumulative_readings(
    conn, property_id
):
    """A meter reads cumulatively; consumption is the difference."""
    today = date.today()
    for months_ago, reading in ((2, 100), (1, 130), (0, 175)):
        conn.execute(
            """
            INSERT INTO meter_readings
                (property_id, utility, read_on, reading, unit)
            VALUES (%s, 'water', %s, %s, 'kL')
            ON CONFLICT DO NOTHING
            """,
            (property_id, today - timedelta(days=30 * months_ago), reading),
        )

    result = registry.usage_summary(conn, "water", months=12)
    consumed = [r["consumed"] for r in result["readings"]]

    # First reading has no predecessor, so no consumption figure.
    assert consumed[0] is None
    assert consumed[1:] == [30, 45]
    assert result["total_consumed"] == 75
    assert result["unit"] == "kL"


def test_usage_summary_handles_a_utility_with_no_readings(conn):
    result = registry.usage_summary(conn, "gas")
    assert result["readings"] == []
    assert result["total_consumed"] is None


def test_solar_progress_reports_each_panel_separately(conn, property_id):
    panel = conn.execute(
        """
        INSERT INTO solar_panels (property_id, label, watts_peak, installed_on)
        VALUES (%s, 'Roof North 1', 450, CURRENT_DATE - 60) RETURNING id
        """,
        (property_id,),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO solar_panels (property_id, label, watts_peak)
        VALUES (%s, 'Roof North 2', 450)
        """,
        (property_id,),
    )
    conn.execute(
        "INSERT INTO solar_readings (panel_id, read_on, kwh_generated) "
        "VALUES (%s, CURRENT_DATE, 2.5)",
        (panel,),
    )

    result = registry.solar_progress(conn)
    by_label = {p["label"]: p for p in result["panels"]}

    assert by_label["Roof North 1"]["kwh_total"] == 2.5
    # A panel with no readings yet reports zero, not null.
    assert by_label["Roof North 2"]["kwh_total"] == 0
    assert result["total_watts_peak"] >= 900


def test_watering_plan_normalises_to_litres_per_week(conn):
    location = conn.execute(
        "INSERT INTO locations (property_id, name, indoor) "
        "SELECT id, 'Front bed', false FROM properties WHERE name='Home' "
        "ON CONFLICT DO NOTHING RETURNING id"
    ).fetchone()
    location_id = location[0] if location else conn.execute(
        "SELECT id FROM locations WHERE name='Front bed'"
    ).fetchone()[0]

    conn.execute(
        """
        INSERT INTO plants
            (location_id, common_name, water_litres, water_every_days, sun)
        VALUES (%s, 'Lavender', 4, 14, 'full')
        """,
        (location_id,),
    )

    plan = {p["common_name"]: p for p in registry.watering_plan(conn)}
    # 4 litres every 14 days is 2 litres a week.
    assert float(plan["Lavender"]["litres_per_week"]) == 2.0


def test_watering_plan_leaves_unknown_needs_null_rather_than_guessing(conn):
    conn.execute(
        "INSERT INTO plants (common_name) VALUES ('Mystery shrub')"
    )
    plan = {p["common_name"]: p for p in registry.watering_plan(conn)}
    assert plan["Mystery shrub"]["litres_per_week"] is None


def test_niview_camera_does_not_inherit_the_sonoff_camera_verdict(conn):
    """Regression: the NiView camera is not a Sonoff product.

    It was once assumed to be one because it sat on a shopping list among
    Sonoff devices, and so wrongly inherited Sonoff's ONVIF/RTSP support.
    Asking about it must never return the Sonoff finding.
    """
    result = registry.check_compatibility(conn, "NiView")

    assert result["matches"], "the NiView finding should exist"
    subjects = " ".join(m["subject"] for m in result["matches"])
    assert "SONOFF" not in subjects.upper()


def test_niview_records_the_open_source_route_and_its_risk(conn):
    """Stock firmware offers no local access; thingino is the only route."""
    result = registry.check_compatibility(conn, "NiView")
    match = result["matches"][0]

    assert "thingino" in match["requires"].lower()
    assert "thingino-firmware" in match["evidence_url"]
    # The risk must travel with the recommendation, not be discovered later.
    assert "bricks" in match["caveat"].lower()


def test_sonoff_camera_rule_is_explicitly_scoped_to_sonoff(conn):
    result = registry.check_compatibility(conn, "SONOFF-branded cameras")
    assert result["matches"], "the Sonoff camera finding should still exist"
    assert "not apply to unbranded" in result["matches"][0]["notes"].lower()


def test_battery_camera_limitation_is_recorded_as_a_general_rule(conn):
    """A battery camera cannot hold a continuous stream, whatever the protocol."""
    result = registry.check_compatibility(conn, "battery-powered PIR cameras")
    assert result["matches"]
    assert "continuous" in result["matches"][0]["caveat"]


def test_the_camera_is_listed_as_not_yet_reachable(conn):
    stranded = [r["name"] for r in registry.find_unreachable(conn)]
    assert "NiView dual-lens security camera" in stranded


def test_the_camera_appears_exactly_once_after_the_rename(conn):
    """Regression: 003 inserts under the old name, 004 renames it.

    Re-applying the migration set must converge on one camera row, not add a
    duplicate on every pass.
    """
    cameras = registry.search_equipment(conn, "NiView")
    assert len(cameras) == 1, [c["name"] for c in cameras]
    assert not registry.search_equipment(conn, "Solar dual-linkage")


def test_find_account_returns_a_location_and_never_a_secret(conn):
    conn.execute(
        """
        INSERT INTO accounts (service, username, secret_location, category)
        VALUES ('Netflix', 'family@example.com',
                'Bitwarden > Home > Netflix', 'streaming')
        ON CONFLICT DO NOTHING
        """
    )

    found = registry.find_account(conn, "netflix")
    assert len(found) == 1
    assert found[0]["secret_location"] == "Bitwarden > Home > Netflix"
    # The schema has nowhere to put one, so no query can return one.
    assert not any(
        key in found[0] for key in ("password", "secret", "credential")
    )
