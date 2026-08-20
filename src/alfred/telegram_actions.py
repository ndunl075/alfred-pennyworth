"""Durable Telegram approvals for Alfred's existing safe action proposals."""

from __future__ import annotations

import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from .action_executor import ActionExecutor
from .audit import AuditEvent, AuditLog
from .db import Database
from .outbox import Outbox
from .policy import ApprovalService
from .secret_store import SystemKeyringSecretStore


ACTION_LABELS = {
    "calendar_event_create": "calendar event",
    "gmail_draft_create": "email draft",
    "gmail_message_send": "send email",
    "github_issue_create": "GitHub issue",
    "composio_tool_execute": "Composio action",
    "memory_forget": "forget request",
}


#: How much of a body to show before trimming. Long enough for a real email
#: to be read in full on a phone, short enough that the buttons stay on
#: screen -- an approval the owner has to scroll past is one they approve
#: without reading.
PREVIEW_BODY_CHARS = 900


def action_preview(action_type: str, preview: dict[str, Any]) -> str:
    """Render what will actually happen, from the stored approval record.

    The agent already describes its own proposal in prose, but that is the
    model's account of what it did, written before the record existed and
    free to differ from it. "i queued it up with the subject 'hi, it's
    alfred'" told the owner the subject and nothing else, so approving meant
    sending a letter they had never read.

    This reads the record the executor will use, so what is shown and what is
    sent cannot disagree.
    """
    if action_type in {"gmail_message_send", "gmail_draft_create"}:
        lines = [f"to: {preview.get('to', '(no recipient)')}",
                 f"subject: {preview.get('subject') or '(no subject)'}"]
        body = str(preview.get("body") or "").strip()
        if body:
            lines.append("")
            lines.append(_trim(body))
        return "\n".join(lines)
    if action_type == "calendar_event_create":
        return "\n".join(
            [
                f"event: {preview.get('summary') or '(untitled)'}",
                # Named, because six calendars are configured and approving a
                # write without being shown its target asks the owner to
                # confirm something they cannot see.
                f"calendar: {preview.get('calendar_title') or preview.get('calendar_id') or '?'}",
                f"starts: {preview.get('start', '?')}",
                f"ends: {preview.get('end', '?')}",
            ]
        )
    if action_type == "github_issue_create":
        lines = [f"repo: {preview.get('repository', '?')}",
                 f"title: {preview.get('title') or '(untitled)'}"]
        body = str(preview.get("body") or "").strip()
        if body:
            lines.append("")
            lines.append(_trim(body))
        return "\n".join(lines)
    return ""


def _when(start: Any, end: Any, *, now: datetime | None = None) -> str:
    """Render a stored UTC span the way the owner said it.

    The preview keeps UTC because that is what Google is given, and the
    approval card showed it raw: "starts: 2026-08-20T14:30:00+00:00" for an
    event the owner had asked for at 10:30 am. Correct, and unreadable, and
    four hours off what they typed.

    Returns "" rather than guessing when the timestamps are missing or
    unparseable -- a sentence that simply omits the time is better than one
    confidently naming the wrong one.
    """
    if not isinstance(start, str):
        return ""
    try:
        opens = datetime.fromisoformat(start).astimezone()
    except ValueError:
        return ""
    today = (now.astimezone() if now else datetime.now().astimezone()).date()
    days = (opens.date() - today).days
    day = {0: "today", 1: "tomorrow", -1: "yesterday"}.get(days)
    if day is None:
        # Inside the coming week a weekday reads faster than a date; past
        # that, the date is the only thing that disambiguates.
        day = opens.strftime("%A") if 0 < days < 7 else opens.strftime("%a %b %d")
    span = _clock(opens)
    if isinstance(end, str):
        try:
            span = f"{span}–{_clock(datetime.fromisoformat(end).astimezone())}"
        except ValueError:
            pass
    return f" {day}, {span}"


def _clock(moment: datetime) -> str:
    """"10:30 am", not "10:30 AM" or "%-I" (which is not portable to Windows)."""
    return moment.strftime("%I:%M %p").lstrip("0").lower()


def _trim(body: str) -> str:
    if len(body) <= PREVIEW_BODY_CHARS:
        return body
    # Cut on a line break where possible so a trimmed letter still ends on a
    # readable boundary rather than mid-word.
    cut = body.rfind("\n", 0, PREVIEW_BODY_CHARS)
    if cut < PREVIEW_BODY_CHARS // 2:
        cut = PREVIEW_BODY_CHARS
    return body[:cut].rstrip() + "\n[...]"


def action_keyboard(
    approvals: list[tuple[str, str]],
) -> dict[str, list[list[dict[str, str]]]]:
    """The only keyboard Alfred still sends: a write it may not perform alone.

    This used to carry the response-feedback buttons underneath as well.
    Ratings are now inferred from the conversation, so a keyboard appearing at
    all means a decision is actually waiting on the owner.
    """
    rows: list[list[dict[str, str]]] = []
    for approval_id, _action_type in approvals[:3]:
        # Cancel first so approve sits on the right, under the thumb and away
        # from it. The label is bare "approve" because the message above
        # already says what is being approved -- "approve send email" next to
        # "cancel" made the destructive-looking button the wide one and read
        # like a second description rather than a choice.
        rows.append(
            [
                {"text": "cancel", "callback_data": f"aa:{approval_id}:n"},
                {"text": "approve", "callback_data": f"aa:{approval_id}:y"},
            ]
        )
    return {"inline_keyboard": rows}


class TelegramActionWorker:
    """Execute button decisions outside Telegram intake's database transaction."""

    actor = "mcp:hermes"

    def __init__(
        self,
        database: Database,
        *,
        executor: Callable[[str, str, str], dict[str, Any]] | None = None,
        secret_store: SystemKeyringSecretStore | None = None,
        max_attempts: int = 3,
    ) -> None:
        self.database = database
        self.approvals = ApprovalService(database)
        self.secrets = secret_store or SystemKeyringSecretStore()
        self.executor = executor or self._execute
        self.max_attempts = max_attempts

    def run_pending(self, *, limit: int = 5) -> int:
        self.database.migrate()
        handled = 0
        for _ in range(limit):
            intent = self._claim()
            if intent is None:
                break
            try:
                if intent["decision"] == "reject":
                    self.approvals.reject(intent["approval_id"], actor=self.actor)
                    message = "cancelled. nothing changed."
                else:
                    token = self._approval_token(intent["approval_id"])
                    approval = self.approvals.get(intent["approval_id"])
                    if approval is not None and approval.state == "pending":
                        self.approvals.approve_with_token(
                            intent["approval_id"], actor=self.actor, token=token
                        )
                    result = self.executor(intent["approval_id"], self.actor, token)
                    # The preview holds what the owner asked for; the receipt
                    # holds what the connector did. Naming the outcome takes
                    # both -- "Gym + lawns" comes from one, the link from the
                    # other -- and the approval is read before executing, so
                    # consuming it does not cost us the detail.
                    message = self._success_message(
                        intent["action_type"],
                        result,
                        approval.preview if approval is not None else None,
                    )
                self._complete(intent, message)
                if intent["decision"] == "approve":
                    self.secrets.delete(self._secret_name(intent["approval_id"]))
                handled += 1
            except Exception as error:
                self._retry_or_fail(intent, error)
        return handled

    def _execute(self, approval_id: str, actor: str, token: str) -> dict[str, Any]:
        return ActionExecutor(self.database).execute(approval_id, actor=actor, token=token)

    def _approval_token(self, approval_id: str) -> str:
        name = self._secret_name(approval_id)
        existing = self.secrets.get_optional(name)
        if existing:
            return existing
        token = "alf_" + secrets.token_urlsafe(32)
        self.secrets.store(name, token)
        return token

    @staticmethod
    def _secret_name(approval_id: str) -> str:
        return f"telegram-approval-{approval_id}"

    def _claim(self) -> sqlite3.Row | None:
        now = datetime.now(UTC).isoformat()
        with self.database.connect() as connection:
            with self.database.transaction(connection):
                row = connection.execute(
                    """
                    SELECT i.*, l.chat_id, l.action_type
                    FROM telegram_action_intents i
                    JOIN telegram_action_links l USING (approval_id)
                    WHERE i.state IN ('pending', 'running')
                      AND (i.next_attempt_at IS NULL OR i.next_attempt_at <= ?)
                    ORDER BY i.created_at
                    LIMIT 1
                    """,
                    (now,),
                ).fetchone()
                if row is None:
                    return None
                connection.execute(
                    """
                    UPDATE telegram_action_intents
                    SET state = 'running', attempts = attempts + 1, updated_at = ?
                    WHERE approval_id = ?
                    """,
                    (now, row["approval_id"]),
                )
                return row

    def _complete(self, intent: sqlite3.Row, message: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self.database.connect() as connection:
            with self.database.transaction(connection):
                connection.execute(
                    """
                    UPDATE telegram_action_intents
                    SET state = 'completed', updated_at = ?, completed_at = ?, last_error = NULL
                    WHERE approval_id = ?
                    """,
                    (now, now, intent["approval_id"]),
                )
                Outbox.enqueue(
                    connection,
                    destination=f"telegram:{intent['chat_id']}",
                    payload={"text": message},
                    idempotency_key=f"telegram-action-result:{intent['approval_id']}",
                )
                AuditLog.append_in_transaction(
                    connection,
                    AuditEvent(
                        actor="owner:telegram",
                        client="telegram",
                        tool="telegram_action",
                        outcome="completed",
                        result={
                            "approval_id": intent["approval_id"],
                            "decision": intent["decision"],
                            "action_type": intent["action_type"],
                        },
                        correlation_id=intent["approval_id"],
                    ),
                )

    def _retry_or_fail(self, intent: sqlite3.Row, error: Exception) -> None:
        attempts = int(intent["attempts"]) + 1
        final = attempts >= self.max_attempts
        now = datetime.now(UTC)
        retry_at = None if final else (now + timedelta(seconds=30 * attempts)).isoformat()
        with self.database.connect() as connection:
            with self.database.transaction(connection):
                connection.execute(
                    """
                    UPDATE telegram_action_intents
                    SET state = ?, next_attempt_at = ?, last_error = ?, updated_at = ?
                    WHERE approval_id = ?
                    """,
                    (
                        "failed" if final else "pending",
                        retry_at,
                        error.__class__.__name__,
                        now.isoformat(),
                        intent["approval_id"],
                    ),
                )
                if final:
                    Outbox.enqueue(
                        connection,
                        destination=f"telegram:{intent['chat_id']}",
                        payload={"text": self._failure_message(intent["action_type"])},
                        idempotency_key=f"telegram-action-failed:{intent['approval_id']}",
                    )

    @staticmethod
    def _failure_message(action_type: str) -> str:
        """Name the thing that did not happen.

        "I couldn't finish that action. nothing else was attempted." was what
        the owner got when a calendar write failed -- accurate, and it left
        them unable to tell a failed calendar write from a failed send without
        scrolling back to find which approval this replied to.

        The reason is deliberately not quoted. An exception from a connector
        can carry the message body or the recipient it choked on, and this
        goes to a chat; the audit log is where the detail belongs. "nothing
        else was attempted" stays, because after a failure the first thing
        worth knowing is that nothing partial happened.
        """
        attempted = {
            "calendar_event_create": "add that to your calendar",
            "gmail_message_send": "send that email",
            "gmail_draft_create": "save that draft",
            "github_issue_create": "open that issue",
            "memory_forget": "forget that",
            "composio_tool_execute": "run that action",
        }.get(action_type)
        opening = f"I couldn't {attempted}." if attempted else "I couldn't finish that action."
        return f"{opening} nothing else was attempted, and it's in the log if you want the reason."

    @staticmethod
    def _success_message(
        action_type: str, result: dict[str, Any], preview: dict[str, Any] | None = None
    ) -> str:
        """Say what actually happened, not that something did.

        "done — it's on your calendar" was the reply to every calendar write,
        with the receipt's html_url and the approval's own preview both sitting
        unread in the arguments. Six calendars are configured, so the one fact
        the owner needed -- which one, and when -- was the fact left out, and
        the only way to check was to go and look.

        Everything named here came from the owner or from the connector's
        receipt, so this repeats their own words back rather than asserting
        anything new. Where a link exists it is included: a claim that an issue
        was opened is worth more when it can be clicked.
        """
        preview = preview or {}
        if action_type == "calendar_event_create":
            summary = preview.get("summary") or "the event"
            calendar = preview.get("calendar_title") or preview.get("calendar_id")
            where = f" on your {calendar} calendar" if calendar else " on your calendar"
            when = _when(preview.get("start"), preview.get("end"))
            # A replayed receipt means the event already existed -- usually a
            # retry after a timeout. Reporting it as new would invite the owner
            # to go looking for a duplicate that is not there.
            lead = "that was already there —" if result.get("replayed") else "done —"
            message = f"{lead} “{summary}”{where}{when}."
            link = result.get("html_url")
            return f"{message}\n{link}" if link else message

        if action_type in {"gmail_message_send", "gmail_draft_create"}:
            to = preview.get("to")
            subject = preview.get("subject")
            what = "sent" if action_type == "gmail_message_send" else "drafted"
            parts = [f"{what} to {to}" if to else what]
            if subject:
                parts.append(f"“{subject}”")
            tail = " — it’s in your Gmail drafts." if action_type == "gmail_draft_create" else "."
            return f"{' — '.join(parts)}{tail}"

        if action_type == "github_issue_create":
            number = result.get("issue_number")
            repository = preview.get("repository")
            title = preview.get("title")
            opened = f"opened #{number}" if number else "opened the issue"
            where = f" in {repository}" if repository else ""
            named = f" — “{title}”" if title else ""
            link = result.get("html_url")
            message = f"{opened}{where}{named}."
            return f"{message}\n{link}" if link else message

        if action_type == "memory_forget":
            # The statement is the owner's own, and quoting it is the only way
            # they can tell which of several similar memories went.
            statement = preview.get("statement") or preview.get("text")
            return f"forgotten — “{_trim(str(statement))}”." if statement else "done — I forgot it."

        if action_type == "composio_tool_execute":
            tool = preview.get("tool") or preview.get("tool_slug")
            return f"done — {tool} finished." if tool else "done — the Composio action finished."

        return "done."
