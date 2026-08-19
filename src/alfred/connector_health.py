"""Shared health classification over every connector's sync_state row.

ARCHITECTURE.md's connector contract lists health() -> status as part of
every connector's interface, but each connector (canvas.py, github.py,
gmail.py, google_calendar.py) is an independent class with no shared base,
and each already writes its outcome to the same sync_state table using the
same success/error convention. Rather than add a redundant health() method
to every class that would just re-query that same table, classification
lives here once and is shared by the CLI and the MCP connector_status tool.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from .db import Database

HealthState = Literal["ok", "stale", "error", "never_synced"]

#: How long each connector may go between syncs before it is genuinely
#: behind. A single threshold reported three different things as one
#: problem: the weekly calendar backfill looked broken five days in when it
#: was not due for seven, which teaches the reader to ignore the column.
#: Only connectors whose cadence differs from the default need an entry.
STALE_AFTER: dict[str, timedelta] = {
    # Full history refresh, deliberately weekly (--calendar-history-interval).
    "google_calendar_history": timedelta(days=8),
    # Canvas publishes on a school's schedule, not Alfred's.
    "canvas_ical": timedelta(days=2),
    "canvas": timedelta(days=2),
}

#: Services whose rows are internal bookkeeping rather than an external
#: connector. The runtime heartbeat is how the watchdog knows Alfred is
#: alive; listing it beside Gmail invites reading a liveness ping as a
#: broken integration.
INTERNAL = frozenset({"runtime"})

#: One display name per external service. `sync_state` is keyed by
#: (connector, account), which is right for the machinery -- each Google
#: calendar syncs independently and fails independently -- and wrong for a
#: dashboard, where six calendar rows plus a catalog row plus six history
#: rows are thirteen lines describing "Google Calendar".
SERVICE_OF: dict[str, str] = {
    "google_calendar": "google calendar",
    "google_calendar_catalog": "google calendar",
    "google_calendar_history": "google calendar",
    "gmail": "gmail",
    "gmail_inbound": "gmail",
    "github": "github",
    "github_pull_requests": "github",
    "google_health": "google health",
    "canvas_ical": "canvas",
    "canvas": "canvas",
    "telegram": "telegram",
    "slack": "slack",
}


class ServiceHealth(BaseModel):
    """One external service, however many sync rows back it."""

    service: str
    state: HealthState
    #: Worst-case freshness across the parts, since a service is only as
    #: current as its least recent piece.
    last_success_at: datetime | None
    last_error: str | None
    #: How many (connector, account) rows this covers, so a reader can tell
    #: a single feed from six calendars without unfolding them.
    sources: int
    #: Named only when something is wrong, so a healthy row stays one line.
    unhealthy: list[str] = []
    #: Free-text context a grouped row would otherwise lose -- the address a
    #: live probe actually reached, for instance, which is the one thing
    #: worth knowing about a connector that has no sync history at all.
    detail: str | None = None


class ConnectorHealth(BaseModel):
    connector: str
    account: str
    state: HealthState
    last_success_at: datetime | None
    last_error: str | None


def connector_health(
    database: Database,
    *,
    now: datetime | None = None,
    stale_after: timedelta = timedelta(hours=24),
) -> list[ConnectorHealth]:
    """Classify every connector/account pair without exposing credentials or synced content."""
    checked_at = (now or datetime.now(UTC)).astimezone(UTC)
    database.migrate()
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT connector, account, last_success_at, last_error FROM sync_state ORDER BY connector, account"
        ).fetchall()
    results: list[ConnectorHealth] = []
    for row in rows:
        last_success = datetime.fromisoformat(row["last_success_at"]) if row["last_success_at"] else None
        state: HealthState
        if row["last_error"]:
            # sync_state clears last_error on every successful sync, so a
            # non-null value here always reflects the most recent attempt.
            state = "error"
        elif last_success is None:
            state = "never_synced"
        elif checked_at - last_success > STALE_AFTER.get(row["connector"], stale_after):
            state = "stale"
        else:
            state = "ok"
        results.append(
            ConnectorHealth(
                connector=row["connector"],
                account=row["account"],
                state=state,
                last_success_at=last_success,
                last_error=row["last_error"],
            )
        )
    return results


#: Worst first, so a service's state is the worst of its parts. An error in
#: one calendar is an error for Google Calendar even if five others are fine.
_SEVERITY = {"error": 3, "never_synced": 2, "stale": 1, "ok": 0}


def service_health(
    database: Database,
    *,
    now: datetime | None = None,
    stale_after: timedelta = timedelta(hours=24),
    include_internal: bool = False,
) -> list[ServiceHealth]:
    """Collapse connector rows into one line per external service.

    `sync_state` is keyed by (connector, account) because that is what
    actually syncs and fails independently. A dashboard wants the opposite:
    thirteen rows describing Google Calendar are thirteen chances to
    misread one slow backfill as an outage.

    A service reports its worst part and its *oldest* success, so grouping
    can only ever make the picture look worse than the rows, never better --
    a summary that hid a broken calendar behind five working ones would be
    worse than no summary.
    """
    grouped: dict[str, list[ConnectorHealth]] = {}
    for entry in connector_health(database, now=now, stale_after=stale_after):
        if not include_internal and entry.connector in INTERNAL:
            continue
        grouped.setdefault(SERVICE_OF.get(entry.connector, entry.connector), []).append(entry)

    services: list[ServiceHealth] = []
    for name, entries in sorted(grouped.items()):
        worst = max(entries, key=lambda item: _SEVERITY[item.state])
        successes = [item.last_success_at for item in entries if item.last_success_at]
        services.append(
            ServiceHealth(
                service=name,
                state=worst.state,
                last_success_at=min(successes) if successes else None,
                last_error=worst.last_error,
                sources=len(entries),
                unhealthy=sorted(
                    {item.connector for item in entries if item.state != "ok"}
                ),
            )
        )
    # Anything needing attention first; a dashboard read top-down should not
    # bury the one broken service under six working ones.
    return sorted(services, key=lambda item: (-_SEVERITY[item.state], item.service))
