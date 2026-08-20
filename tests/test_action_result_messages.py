"""Say what happened, not that something did.

"done — it's on your calendar" was the reply to every calendar write, with
the receipt's html_url and the approval's own preview both sitting unread in
the arguments. Six calendars are configured, so the one fact the owner needed
-- which one, and when -- was the fact left out, and the only way to check was
to go and look.

Nothing here is asserted: every detail comes from what the owner asked for
(the preview) or what the connector returned (the receipt).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from alfred.telegram_actions import TelegramActionWorker, _when

LOCAL = datetime.now().astimezone().tzinfo


def _span(days: int = 1, hour: int = 10, minute: int = 30, length: int = 5):
    start = (datetime.now(LOCAL) + timedelta(days=days)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    end = start + timedelta(hours=length)
    # Stored as UTC, because that is what Google is given.
    return start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()


def _calendar(**overrides):
    start, end = _span()
    preview = {
        "summary": "Gym + lawns",
        "calendar_title": "FAMILY CAR",
        "start": start,
        "end": end,
    }
    preview.update(overrides)
    return preview


def test_a_calendar_write_names_the_calendar_and_the_time() -> None:
    message = TelegramActionWorker._success_message("calendar_event_create", {}, _calendar())

    assert "Gym + lawns" in message
    assert "FAMILY CAR" in message
    assert "tomorrow" in message
    assert "10:30 am" in message


def test_the_time_is_the_owners_not_utc() -> None:
    """The card showed "starts: 2026-08-20T14:30:00+00:00" for an event asked
    for at 10:30 am: correct, unreadable, and four hours off what they typed."""
    message = TelegramActionWorker._success_message("calendar_event_create", {}, _calendar())

    assert "+00:00" not in message
    assert "T14:30" not in message


def test_a_link_is_included_when_the_receipt_has_one() -> None:
    """A claim that something was created is worth more when it can be clicked."""
    message = TelegramActionWorker._success_message(
        "calendar_event_create", {"html_url": "https://calendar.google.com/x"}, _calendar()
    )

    assert "https://calendar.google.com/x" in message


def test_a_replayed_receipt_does_not_claim_a_new_event() -> None:
    """A replay means the event already existed, usually after a timeout.
    Reporting it as new invites a hunt for a duplicate that is not there."""
    message = TelegramActionWorker._success_message(
        "calendar_event_create", {"replayed": True}, _calendar()
    )

    assert "already there" in message


def test_a_sent_email_names_the_recipient_and_subject() -> None:
    message = TelegramActionWorker._success_message(
        "gmail_message_send", {"message_id": "1a01"},
        {"to": "mom@example.com", "subject": "coming home friday"},
    )

    assert "mom@example.com" in message
    assert "coming home friday" in message


def test_a_draft_says_it_is_a_draft() -> None:
    """Otherwise it reads as sent, which is the one mistake that cannot be
    walked back by explaining it afterwards."""
    message = TelegramActionWorker._success_message(
        "gmail_draft_create", {}, {"to": "sam@example.com", "subject": "notes"}
    )

    assert "drafted" in message
    assert "drafts" in message


def test_an_issue_names_its_number_and_repository() -> None:
    message = TelegramActionWorker._success_message(
        "github_issue_create",
        {"issue_number": 29, "html_url": "https://github.com/o/r/issues/29"},
        {"repository": "ndunl075/alfred", "title": "calendar writes silently fail"},
    )

    assert "#29" in message
    assert "ndunl075/alfred" in message
    assert "https://github.com/o/r/issues/29" in message


def test_a_forget_quotes_what_was_forgotten() -> None:
    """The only way to tell which of several similar memories went."""
    message = TelegramActionWorker._success_message(
        "memory_forget", {}, {"statement": "my landlord is Priya"}
    )

    assert "my landlord is Priya" in message


def test_a_missing_preview_still_produces_a_sentence() -> None:
    """Older approvals predate calendar_title, and a receipt can arrive with
    nothing useful in it. Neither should raise."""
    assert TelegramActionWorker._success_message("calendar_event_create", {}, None)
    assert TelegramActionWorker._success_message("gmail_message_send", {}, {})
    assert TelegramActionWorker._success_message("something_new", {}, {}) == "done."


@pytest.mark.parametrize(
    ("action_type", "expected"),
    [
        ("calendar_event_create", "add that to your calendar"),
        ("gmail_message_send", "send that email"),
        ("github_issue_create", "open that issue"),
    ],
)
def test_a_failure_names_what_did_not_happen(action_type: str, expected: str) -> None:
    """"I couldn't finish that action" left the owner unable to tell a failed
    calendar write from a failed send without scrolling back."""
    assert expected in TelegramActionWorker._failure_message(action_type)


def test_a_failure_still_says_nothing_partial_happened() -> None:
    """After a failure the first thing worth knowing."""
    assert "nothing else was attempted" in TelegramActionWorker._failure_message(
        "calendar_event_create"
    )


def test_an_unknown_action_still_fails_legibly() -> None:
    assert TelegramActionWorker._failure_message("brand_new_action").startswith("I couldn't")


@pytest.mark.parametrize(
    ("days", "expected"),
    [(0, "today"), (1, "tomorrow"), (-1, "yesterday")],
)
def test_nearby_days_are_named_not_dated(days: int, expected: str) -> None:
    start, end = _span(days=days)

    assert expected in _when(start, end)


def test_a_day_further_out_gets_a_date() -> None:
    """Past the coming week a weekday alone is ambiguous."""
    start, end = _span(days=30)

    rendered = _when(start, end)
    assert "tomorrow" not in rendered
    assert any(character.isdigit() for character in rendered)


def test_an_unparseable_time_is_omitted_rather_than_guessed() -> None:
    """A sentence missing the time beats one confidently naming the wrong one."""
    assert _when("tomorrow 10:30 am", None) == ""
    assert _when(None, None) == ""
