"""Alfred's memory kept every promise and no result.

The owner asked for a calendar event at 2:52. Alfred proposed it and said
"just hit approve on telegram and it'll be on there." The write failed at
2:59 and Alfred said so. At 3:45 the owner asked again, and Alfred answered
"hey, i already sent that over for approval" -- when the approvals table held
nothing at all.

Conversation history was assembled from ``hermes-reply:{external_id}:%``
alone. An action's outcome goes to the same outbox under
``telegram-action-result:`` / ``telegram-action-failed:``, keyed by approval
id: a different prefix and a different key, which the history query could
never match. So the promise was in the model's context and the failure four
minutes later was not, and the only honest conclusion from what it could see
was that the approval was still waiting.

One week of this database holds 167 hermes-reply rows against two action
outcomes. The imbalance is the bug.
"""

from __future__ import annotations

import json
from hashlib import sha256
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alfred.db import Database
from alfred.hermes_bridge import HermesBridge

CHAT = 4242
#: Relative to the clock, not pinned to a date. The lookback window is
#: measured from real "now", so a fixed timestamp silently aged out of it
#: overnight and took six passing tests with it.
NOW = datetime.now(UTC)


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "alfred.db")
    database.migrate()
    return database


def _exchange(database: Database, *, external_id: str, at: datetime, user: str, reply: str) -> None:
    with database.connect() as connection:
        with database.transaction(connection):
            connection.execute(
                "INSERT INTO events (source, external_id, occurred_at, content, content_hash, "
                "metadata_json, ingested_at) VALUES ('telegram', ?, ?, ?, ?, ?, ?)",
                (
                    external_id,
                    at.isoformat(),
                    user,
                    sha256(user.encode()).hexdigest(),
                    json.dumps({"chat_id": CHAT, "agent_deferred": True}),
                    at.isoformat(),
                ),
            )
            connection.execute(
                "INSERT INTO outbox (destination, payload_json, idempotency_key, state, "
                "attempts, created_at) VALUES (?, ?, ?, 'sent', 1, ?)",
                (
                    f"telegram:{CHAT}",
                    json.dumps({"text": reply}),
                    f"hermes-reply:{external_id}:0",
                    at.isoformat(),
                ),
            )


def _outcome(database: Database, *, at: datetime, text: str, failed: bool = True) -> None:
    prefix = "telegram-action-failed" if failed else "telegram-action-result"
    with database.connect() as connection:
        with database.transaction(connection):
            connection.execute(
                "INSERT INTO outbox (destination, payload_json, idempotency_key, state, "
                "attempts, created_at) VALUES (?, ?, ?, 'sent', 1, ?)",
                (
                    f"telegram:{CHAT}",
                    json.dumps({"text": text}),
                    f"{prefix}:{at.timestamp()}",
                    at.isoformat(),
                ),
            )


def _history(database: Database) -> list[dict[str, str]]:
    bridge = HermesBridge.__new__(HermesBridge)
    bridge.database = database
    bridge.max_bubbles = 4
    return HermesBridge._recent_conversation(
        bridge,
        {"external_id": "current", "chat_id": CHAT},
    )


def _live(tmp_path: Path) -> Database:
    """The exact sequence, at the exact spacing it happened."""
    database = _database(tmp_path)
    _exchange(
        database,
        external_id="msg-252",
        at=NOW - timedelta(minutes=53),
        user="add gym + lawns to family car calendar tomorrow from 10:30 am to 3:30 pm",
        reply="just hit approve on telegram and it'll be on there.",
    )
    _outcome(
        database,
        at=NOW - timedelta(minutes=46),
        text="I couldn't add that to your calendar. nothing else was attempted.",
    )
    return database


def test_the_failure_reaches_the_model(tmp_path: Path) -> None:
    """The whole bug in one assertion."""
    history = _history(_live(tmp_path))

    assert any("couldn't add that to your calendar" in item["assistant"] for item in history)


def test_the_promise_is_still_there_too(tmp_path: Path) -> None:
    """The outcome is added, not substituted. Both halves are needed to tell
    a settled promise from an open one."""
    history = _history(_live(tmp_path))

    assert any("hit approve" in item["assistant"] for item in history)


def test_the_outcome_follows_the_promise_it_settles(tmp_path: Path) -> None:
    """Order carries the meaning: the failure has to read as answering that
    request, not as a preamble to the next one."""
    history = _history(_live(tmp_path))
    text = next(item["assistant"] for item in history if "hit approve" in item["assistant"])

    assert text.index("hit approve") < text.index("couldn't add")


def test_an_outcome_attaches_to_the_request_that_caused_it(tmp_path: Path) -> None:
    """With two requests in the window, a failure belongs to the earlier one
    if it landed before the later one was even asked."""
    database = _live(tmp_path)
    _exchange(
        database,
        external_id="msg-330",
        at=NOW - timedelta(minutes=15),
        user="what's on my calendar friday",
        reply="nothing yet.",
    )

    history = _history(database)
    later = next(item for item in history if "friday" in item["user"])

    assert "couldn't add" not in later["assistant"]


def test_a_successful_outcome_is_carried_as_well(tmp_path: Path) -> None:
    """So a repeat request is answered "that's already done" from the receipt
    rather than from having said so."""
    database = _database(tmp_path)
    _exchange(
        database,
        external_id="msg-1",
        at=NOW - timedelta(minutes=30),
        user="add gym to the family car calendar",
        reply="sent it over for approval.",
    )
    _outcome(
        database,
        at=NOW - timedelta(minutes=28),
        text="done — “Gym” on your FAMILY CAR calendar tomorrow, 10:30 am–3:30 pm.",
        failed=False,
    )

    assert any("FAMILY CAR" in item["assistant"] for item in _history(database))


def test_an_outcome_with_no_preceding_exchange_is_dropped(tmp_path: Path) -> None:
    """It has no promise left in the window to settle, and filing it against a
    later unrelated request would be worse than losing it."""
    database = _database(tmp_path)
    _outcome(database, at=NOW - timedelta(hours=5), text="I couldn't send that email.")
    _exchange(
        database,
        external_id="msg-late",
        at=NOW - timedelta(minutes=5),
        user="how much did i sleep",
        reply="seven hours.",
    )

    history = _history(database)

    assert all("couldn't send" not in item["assistant"] for item in history)


def test_history_keeps_its_shape(tmp_path: Path) -> None:
    """Callers read user/assistant; the timestamp used for merging is internal."""
    history = _history(_live(tmp_path))

    assert history
    for item in history:
        assert set(item) == {"user", "assistant"}
