"""A request to write the calendar must not be acknowledged as reading it.

"add gym + lawns to family car calendar tomorrow from 10:30 am to 3:30 pm"
was answered "checking your agenda...".

The action table matches contiguous phrases, and the phrase for this is
"add to calendar" -- but the things being added sit between the verb and the
target, so nothing matched. The message then fell through to the read topics,
where the bare word "calendar" matched, and Alfred announced it was reading a
calendar the owner had just asked it to write.

Naming the calendar back matters as much as the verb. Six calendars are
configured; "setting that up..." is accurate and still leaves the owner
waiting to find out which one it landed on.
"""

from __future__ import annotations

import pytest

from alfred.telegram import TelegramGateway, _phrase_position


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "add gym + lawns to family car calendar tomorrow from 10:30 am to 3:30 pm",
            "adding that to your family car calendar...",
        ),
        ("add dentist to my work calendar friday at 2", "adding that to your work calendar..."),
        ("put lunch with sam on the family calendar", "adding that to your family calendar..."),
        ("throw a reminder on my personal calendar", "adding that to your personal calendar..."),
        # No name given, so none is invented.
        ("block 3-5pm on my calendar for deep work", "adding that to your calendar..."),
        ("add to my calendar: standup at 9", "adding that to your calendar..."),
    ],
)
def test_a_calendar_write_is_acknowledged_as_a_write(message: str, expected: str) -> None:
    assert TelegramGateway.acknowledgement_for(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        "what's on my calendar tomorrow",
        "do i have anything on my calendar friday",
        "any meetings on the work calendar this week",
        "check my agenda",
        # Matches the write shape word for word and differs only by opening
        # with an interrogative.
        "did i book anything on the family car calendar",
        "when did i put that on the family calendar",
    ],
)
def test_a_question_about_the_calendar_stays_a_read(message: str) -> None:
    assert TelegramGateway.acknowledgement_for(message) == "checking your agenda..."


def test_the_owners_wording_is_echoed_not_normalised() -> None:
    """The reply repeats the calendar as it was named, so the owner can see
    Alfred understood which one rather than trusting that it did."""
    ack = TelegramGateway.acknowledgement_for("add oil change to the family car calendar")

    assert "family car" in ack


def test_a_short_final_token_does_not_extend_into_the_next_word() -> None:
    """The loose end lets a stem reach its inflections. "a" is not a stem:
    "book a" reached into "book *a*nything", so asking what was already
    booked was acknowledged as a request to book something."""
    assert _phrase_position(" did i book anything today ", "book a") is None
    assert _phrase_position(" book a table for two ", "book a") is not None


def test_inflections_still_match() -> None:
    """The tightening must not cost what the loose end was for."""
    assert _phrase_position(" three assignments due ", "assignment") is not None
    assert _phrase_position(" marking that done ", "mark") is not None


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("remind me to call mom at 5", "setting that reminder..."),
        ("schedule a dentist appointment", "setting that up..."),
        ("whats in my inbox", "checking your inbox..."),
    ],
)
def test_neighbouring_intents_are_undisturbed(message: str, expected: str) -> None:
    assert TelegramGateway.acknowledgement_for(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        "email mom and tell her im coming home friday",
        "message sam about tomorrow",
        "email the landlord about the lease",
    ],
)
def test_writing_to_a_person_selects_a_send_tool(message: str) -> None:
    """"email them" and "message them" were spelled out, but "them" is the one
    recipient nobody writes. A plain request to write to somebody matched
    neither the send nor the draft vocabulary, so the work lane ran with an
    empty toolset and Alfred could only talk about doing it."""
    from alfred.hermes_tools import select_hermes_tools

    assert "message_send_propose" in select_hermes_tools(message)


def test_drafting_is_still_not_sending() -> None:
    """The widened verb must not swallow the word that rules sending out."""
    from alfred.hermes_tools import select_hermes_tools

    assert select_hermes_tools("draft a reply to that email") == {"message_draft"}
