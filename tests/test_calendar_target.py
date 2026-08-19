"""Alfred proposed a calendar write it could never execute.

"add gym + lawns to family car calendar" produced an approval whose
calendar_id was the literal string "family car". The owner's calendar is
titled "FAMILY CAR" and its id is
``ba46437c...@group.calendar.google.com``; Google has nothing called "family
car". So the write was proposed, shown, approved -- and then failed at the
API, leaving an approval marked consumed, no event, and no way to retry.

The mapping was in the catalog the whole time. Nothing consulted it, because
``calendar_event_propose`` took ``calendar_id`` as a plain string and the
model had no way to know a name was not an id.

Two failures, both covered here: the name was never resolved, and the
approval was spent *before* the call that failed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from alfred.db import Database
from alfred.google_calendar import (
    CalendarTargetError,
    GoogleCalendarActions,
    known_calendars,
    resolve_calendar_target,
)
from alfred.policy import ApprovalService
from alfred.telegram_actions import action_preview

# The owner's real six, as the catalog stores them.
CATALOG = [
    ("ndunlap075@gmail.com", "ndunlap075@gmail.com", "owner", True),
    ("ba46437c@group.calendar.google.com", "FAMILY CAR", "writer", False),
    ("family12157282285@group.calendar.google.com", "Dunlap Family", "owner", False),
    ("1f629fcbc33@group.calendar.google.com", "Todoist", "owner", False),
    ("4ac9rm5em8@import.calendar.google.com", "Canvas", "reader", False),
    ("en.usa#holiday@group.v.calendar.google.com", "Holidays in United States", "reader", False),
]


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "alfred.db")
    database.migrate()
    with database.connect() as connection:
        with database.transaction(connection):
            for calendar_id, title, access, primary in CATALOG:
                connection.execute(
                    "INSERT INTO connector_records (connector, account, record_type, record_id, "
                    "payload_json, observed_at, active) VALUES (?, ?, ?, ?, ?, ?, '1')",
                    (
                        "google_calendar",
                        calendar_id,
                        "calendar",
                        calendar_id,
                        json.dumps(
                            {"id": calendar_id, "title": title, "access_role": access,
                             "primary": primary}
                        ),
                        datetime.now(UTC).isoformat(),
                    ),
                )
    return database


def test_the_name_the_owner_used_resolves_to_the_id_google_needs(tmp_path: Path) -> None:
    """The actual bug: "family car" reaching Google unchanged."""
    target = resolve_calendar_target(_database(tmp_path), "family car")

    assert target.calendar_id == "ba46437c@group.calendar.google.com"
    assert target.title == "FAMILY CAR"


@pytest.mark.parametrize("spelling", ["family car", "FAMILY CAR", "Family Car", " family  car "])
def test_case_and_spacing_do_not_matter(tmp_path: Path, spelling: str) -> None:
    assert resolve_calendar_target(_database(tmp_path), spelling).title == "FAMILY CAR"


def test_primary_still_works(tmp_path: Path) -> None:
    """The old default must keep working for callers that pass no calendar."""
    assert resolve_calendar_target(_database(tmp_path), "primary").primary is True


def test_a_real_id_passes_through(tmp_path: Path) -> None:
    target = resolve_calendar_target(_database(tmp_path), "ba46437c@group.calendar.google.com")

    assert target.title == "FAMILY CAR"


def test_an_ambiguous_name_is_refused_rather_than_guessed(tmp_path: Path) -> None:
    """"family" matches two. Landing a write on the wrong calendar is worse
    than not landing it, so this refuses and names the options."""
    with pytest.raises(CalendarTargetError) as caught:
        resolve_calendar_target(_database(tmp_path), "family")

    assert "FAMILY CAR" in str(caught.value)
    assert "Dunlap Family" in str(caught.value)


def test_an_unknown_name_lists_what_can_be_written(tmp_path: Path) -> None:
    with pytest.raises(CalendarTargetError) as caught:
        resolve_calendar_target(_database(tmp_path), "the moon")

    assert "no calendar named" in str(caught.value)
    assert "FAMILY CAR" in str(caught.value)


def test_a_read_only_calendar_is_refused(tmp_path: Path) -> None:
    """Canvas is subscribed, not owned. Proposing a write to it would fail at
    Google after the owner had already approved it."""
    with pytest.raises(CalendarTargetError) as caught:
        resolve_calendar_target(_database(tmp_path), "canvas")

    assert "read-only" in str(caught.value)


def test_read_only_calendars_are_never_offered_as_options(tmp_path: Path) -> None:
    with pytest.raises(CalendarTargetError) as caught:
        resolve_calendar_target(_database(tmp_path), "the moon")

    assert "Canvas" not in str(caught.value)
    assert "Holidays" not in str(caught.value)


def test_an_empty_catalog_does_not_block_a_valid_id(tmp_path: Path) -> None:
    """A catalog sync that has not run yet must not make writes impossible."""
    database = Database(tmp_path / "empty.db")
    database.migrate()

    assert resolve_calendar_target(database, "primary").calendar_id == "primary"


def test_the_catalog_reports_which_calendars_can_be_written(tmp_path: Path) -> None:
    writable = {target.title for target in known_calendars(_database(tmp_path)) if target.writable}

    assert writable == {"ndunlap075@gmail.com", "FAMILY CAR", "Dunlap Family", "Todoist"}


def _propose(database: Database, calendar: str = "family car"):
    return GoogleCalendarActions(database, ApprovalService(database)).propose_event(
        actor="mcp:hermes",
        calendar_id=calendar,
        summary="Gym + lawns",
        start=datetime(2026, 8, 20, 14, 30, tzinfo=UTC),
        end=datetime(2026, 8, 20, 19, 30, tzinfo=UTC),
    )


def test_the_proposal_stores_the_resolved_id(tmp_path: Path) -> None:
    approval = _propose(_database(tmp_path))

    assert approval.preview["calendar_id"] == "ba46437c@group.calendar.google.com"


def test_the_approval_card_names_the_calendar(tmp_path: Path) -> None:
    """Six calendars are configured. Approving a write without being shown
    its target asks the owner to confirm something they cannot see."""
    approval = _propose(_database(tmp_path))

    card = action_preview("calendar_event_create", approval.preview)

    assert "calendar: FAMILY CAR" in card


def test_an_unresolvable_calendar_is_refused_before_anyone_approves(tmp_path: Path) -> None:
    """Refusing at proposal time is the point. Refusing at execute() would
    mean the owner had already tapped approve on a write that could not land."""
    database = _database(tmp_path)

    with pytest.raises(CalendarTargetError):
        _propose(database, "the moon")

    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


class _Boom:
    """A calendar API that rejects the write, as Google did for "family car"."""

    def create_event(self, **_: Any) -> dict[str, Any]:
        raise RuntimeError("404 calendar not found")


def test_a_failed_write_does_not_burn_the_approval(tmp_path: Path) -> None:
    """The second failure. consume() ran before the API call, so a rejected
    write left the approval marked consumed: no event, and no way to retry
    without starting over."""
    database = _database(tmp_path)
    approvals = ApprovalService(database)
    approval = _propose(database)
    token = approvals.approve(approval.id, actor="mcp:hermes").token

    actions = GoogleCalendarActions(database, approvals, _Boom())
    with pytest.raises(RuntimeError):
        actions.execute(approval.id, actor="mcp:hermes", token=token)

    after = approvals.get(approval.id)
    assert after is not None
    assert after.state != "consumed"
