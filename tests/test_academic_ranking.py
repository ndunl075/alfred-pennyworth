""""What's due tomorrow" answered with 2099.

The academic pack ranked rollups by relevance and then by latest day. With no
term matching, that collapses to newest-first over the whole timeline, which
is exactly backwards for the question people actually ask.

On the live database, on 20 August:

  "what assignments do i have coming up" -> 9, 8, 3 December
  "what's due tomorrow"                  -> 2099, 2098, 2097

The December dates were the end of the semester rather than the next thing
due. The 2099s were a "Happy birthday!" recurrence, expanded annually to the
end of the century, outranking every real assignment on date alone.

Nearness to today is the ordering that question wants, with the future
preferred and the past kept -- "what did i turn in last week" is a real
question too.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alfred.academic_memory import AcademicMemoryService
from alfred.db import Database

TODAY = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "alfred.db")
    database.migrate()
    return database


def _day(database: Database, day: str, title: str, search_text: str | None = None) -> None:
    with database.connect() as connection:
        with database.transaction(connection):
            connection.execute(
                "INSERT INTO academic_daily_rollups (day, group_key, group_label, items_json, "
                "search_text, item_count, generated_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
                (
                    day,
                    "canvas:cse2231",
                    "CSE 2231 AU2026",
                    json.dumps([{"day": day, "title": title, "item_type": "assignment"}]),
                    search_text if search_text is not None else title,
                    TODAY.isoformat(),
                ),
            )


def _live_shape(tmp_path: Path) -> Database:
    """A semester of homework plus the birthday recurrence that beat it."""
    database = _database(tmp_path)
    for offset, number in ((6, 1), (7, 2), (8, 3)):
        stamp = (TODAY + timedelta(days=offset)).date().isoformat()
        _day(database, stamp, f"Homework #{number} [CSE 2231 AU2026 (5471)]")
    _day(database, "2026-12-09", "Project #10 [CSE 2231 AU2026 (5471)]")
    for year in (2097, 2098, 2099):
        _day(database, f"{year}-04-11", "Happy birthday!")
    return database


def _days(database: Database, query: str) -> list[str]:
    return [
        day["day"]
        for day in AcademicMemoryService(database).search(query, limit=3, now=TODAY).days
    ]


def test_coming_up_means_the_next_thing_not_the_last(tmp_path: Path) -> None:
    """Returned 9, 8 and 3 December on 20 August: the end of the semester."""
    days = _days(_live_shape(tmp_path), "what assignments do i have coming up")

    assert days[0] == (TODAY + timedelta(days=6)).date().isoformat()
    assert "2026-12-09" not in days


def test_a_recurrence_to_2099_no_longer_wins(tmp_path: Path) -> None:
    """The literal answer to "what's due tomorrow" was 2099, 2098, 2097."""
    days = _days(_live_shape(tmp_path), "whats due tomorrow")

    assert not any(day.startswith(("2097", "2098", "2099")) for day in days)


def test_the_past_is_ordered_behind_the_future_not_dropped(tmp_path: Path) -> None:
    """"What did i turn in last week" is a real question."""
    database = _database(tmp_path)
    past = (TODAY - timedelta(days=3)).date().isoformat()
    future = (TODAY + timedelta(days=3)).date().isoformat()
    _day(database, past, "Homework #0")
    _day(database, future, "Homework #1")

    days = _days(database, "homework")

    assert days == [future, past]


def test_a_nearer_day_beats_a_further_one(tmp_path: Path) -> None:
    database = _database(tmp_path)
    for offset in (2, 30, 9):
        _day(database, (TODAY + timedelta(days=offset)).date().isoformat(), "Homework")

    days = _days(database, "homework")

    assert days == [
        (TODAY + timedelta(days=offset)).date().isoformat() for offset in (2, 9, 30)
    ]


def test_relevance_still_outranks_nearness(tmp_path: Path) -> None:
    """Proximity is the tie-break, not the primary key: a matching item next
    month must beat an unrelated one tomorrow."""
    database = _database(tmp_path)
    _day(database, (TODAY + timedelta(days=1)).date().isoformat(), "Dentist", "dentist")
    far = (TODAY + timedelta(days=40)).date().isoformat()
    _day(database, far, "Midterm exam", "midterm exam")

    assert _days(database, "when is my midterm")[0] == far


def test_an_unparseable_day_does_not_break_the_pack(tmp_path: Path) -> None:
    """Losing one rollup beats losing the whole context."""
    database = _database(tmp_path)
    _day(database, "not-a-date", "Broken")
    good = (TODAY + timedelta(days=1)).date().isoformat()
    _day(database, good, "Homework")

    assert _days(database, "homework")[0] == good
