"""One row per service, and a threshold each connector can actually meet.

The connectors page listed 21 rows for what a reader thinks of as six
services: Google Calendar alone had six calendar rows, a catalog row, and
six history rows. Thirteen lines describing one integration are thirteen
chances to misread it, and the page did exactly that -- the weekly history
backfill showed "stale" five days in, when it was not due for seven.

A dashboard that cries wolf teaches the reader to skip the column, which
costs more than the column was ever worth.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from alfred.connector_health import service_health

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def _sync(database, connector: str, account: str, *, ago: timedelta | None = None,
          error: str | None = None) -> None:
    stamp = None if ago is None else (NOW - ago).isoformat()
    with database.connect() as connection:
        with database.transaction(connection):
            connection.execute(
                "INSERT INTO sync_state (connector, account, cursor, last_success_at, "
                "last_error, updated_at) VALUES (?, ?, NULL, ?, ?, ?)",
                (connector, account, stamp, error, NOW.isoformat()),
            )


def _db(tmp_path: Path):
    from alfred.db import Database

    database = Database(tmp_path / "alfred.db")
    database.migrate()
    return database


def _by_name(tmp_path_db, **kwargs):
    return {item.service: item for item in service_health(tmp_path_db, now=NOW, **kwargs)}


def test_thirteen_calendar_rows_become_one_service(tmp_path: Path) -> None:
    database = _db(tmp_path)
    for index in range(6):
        _sync(database, "google_calendar", f"cal{index}", ago=timedelta(minutes=5))
    _sync(database, "google_calendar_catalog", "self", ago=timedelta(minutes=5))
    for index in range(6):
        _sync(database, "google_calendar_history", f"cal{index}", ago=timedelta(days=5))

    services = _by_name(database)

    assert list(services) == ["google calendar"]
    assert services["google calendar"].sources == 13


def test_a_weekly_backfill_is_not_stale_at_five_days(tmp_path: Path) -> None:
    """The bug that made the column untrustworthy: one 24-hour threshold
    applied to a connector that only runs weekly."""
    database = _db(tmp_path)
    _sync(database, "google_calendar", "primary", ago=timedelta(minutes=5))
    _sync(database, "google_calendar_history", "primary", ago=timedelta(days=5))

    assert _by_name(database)["google calendar"].state == "ok"


def test_a_weekly_backfill_is_stale_once_it_really_is(tmp_path: Path) -> None:
    """The threshold is raised, not removed."""
    database = _db(tmp_path)
    _sync(database, "google_calendar", "primary", ago=timedelta(minutes=5))
    _sync(database, "google_calendar_history", "primary", ago=timedelta(days=9))

    assert _by_name(database)["google calendar"].state == "stale"


def test_a_service_reports_its_worst_part(tmp_path: Path) -> None:
    """Grouping must never make the picture look better than the rows. A
    summary that hid one broken calendar behind five working ones would be
    worse than no summary."""
    database = _db(tmp_path)
    for index in range(5):
        _sync(database, "google_calendar", f"cal{index}", ago=timedelta(minutes=5))
    _sync(database, "google_calendar", "broken", ago=timedelta(minutes=5), error="HTTP 403")

    service = _by_name(database)["google calendar"]

    assert service.state == "error"
    assert service.last_error == "HTTP 403"


def test_a_service_reports_its_oldest_success(tmp_path: Path) -> None:
    """Freshness is the least recent piece, for the same reason."""
    database = _db(tmp_path)
    _sync(database, "gmail", "self", ago=timedelta(days=3))
    _sync(database, "gmail_inbound", "self", ago=timedelta(minutes=1))

    assert _by_name(database)["gmail"].last_success_at == NOW - timedelta(days=3)


def test_only_the_unhealthy_parts_are_named(tmp_path: Path) -> None:
    """So a healthy service stays one line and attention goes where needed."""
    database = _db(tmp_path)
    _sync(database, "github", "self", ago=timedelta(minutes=5))
    _sync(database, "gmail", "self", ago=timedelta(days=3))

    services = _by_name(database)

    assert services["github"].unhealthy == []
    assert services["gmail"].unhealthy == ["gmail"]


def test_the_runtime_heartbeat_is_not_an_integration(tmp_path: Path) -> None:
    """It is how the watchdog knows Alfred is alive. Listing it beside Gmail
    invites reading a liveness ping as a broken connector."""
    database = _db(tmp_path)
    _sync(database, "runtime", "runner", ago=timedelta(seconds=4))
    _sync(database, "gmail", "self", ago=timedelta(minutes=5))

    assert "runtime" not in _by_name(database)
    assert "runtime" in _by_name(database, include_internal=True)


def test_trouble_sorts_above_everything_working(tmp_path: Path) -> None:
    """A dashboard read top-down should not bury the one broken service."""
    database = _db(tmp_path)
    _sync(database, "gmail", "self", ago=timedelta(minutes=5))
    _sync(database, "github", "self", ago=timedelta(minutes=5))
    _sync(database, "google_health", "self", ago=timedelta(days=4))

    assert [item.service for item in service_health(database, now=NOW)][0] == "google health"


def test_an_unknown_connector_still_gets_a_row(tmp_path: Path) -> None:
    """A connector added later must appear under its own name rather than
    vanishing because nobody updated the mapping."""
    database = _db(tmp_path)
    _sync(database, "some_new_thing", "self", ago=timedelta(minutes=5))

    assert "some_new_thing" in _by_name(database)
