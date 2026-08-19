"""Record what the model actually called, especially when it failed.

Alfred logged the tools it *offered* a turn and nothing about what came back.
So a model calling calendar_event_propose with the wrong argument names --
"title" instead of "summary" -- produced no approval, no audit row, and no
error anywhere. The owner saw a reply saying it had not worked and Alfred's
own database held no trace of why. Every diagnosis started from a screenshot.

The per-tool decorator could not see it either: FastMCP validates arguments
against the schema before dispatching, so a call naming the wrong parameter
never reaches the decorated function.

Argument names are recorded and values never are. Names diagnose a malformed
call; values are the owner's mail, calendar and health data.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from alfred.db import Database
from alfred.mcp_server import create_server
from alfred.policy import PolicyStore


def _server(tmp_path: Path):
    path = tmp_path / "alfred.db"
    database = Database(path)
    database.migrate()
    PolicyStore(database).grant(
        client_id="hermes",
        allowed_sensitivities={"public", "personal"},
        allowed_tools={"calendar_event_propose", "system_status", "remember"},
        allow_write=True,
    )
    return create_server(path, client_id="hermes"), path


def _rows(path: Path, tool: str) -> list[sqlite3.Row]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            "SELECT * FROM tool_runs WHERE tool = ? ORDER BY sequence", (tool,)
        ).fetchall()
    finally:
        connection.close()


def _call(server, name: str, arguments: dict):
    async def go():
        return await server.call_tool(name, arguments)

    return asyncio.run(go())


def test_a_call_with_wrong_argument_names_is_recorded(tmp_path: Path) -> None:
    """The exact invisible failure. FastMCP rejects this before the tool runs,
    so nothing downstream of validation could ever have logged it."""
    server, path = _server(tmp_path)

    with pytest.raises(Exception):
        _call(server, "calendar_event_propose",
              {"title": "Gym", "start_time": "x", "end_time": "y"})

    rows = _rows(path, "calendar_event_propose")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "error"


def test_the_recorded_names_are_the_ones_the_model_used(tmp_path: Path) -> None:
    """Which is the whole diagnostic value: it names what it got wrong."""
    server, path = _server(tmp_path)

    with pytest.raises(Exception):
        _call(server, "calendar_event_propose",
              {"title": "Gym", "start_time": "x", "end_time": "y"})

    arguments = json.loads(_rows(path, "calendar_event_propose")[0]["arguments_json"])
    assert arguments["argument_names"] == ["end_time", "start_time", "title"]


def test_the_error_is_kept_so_the_cause_is_readable(tmp_path: Path) -> None:
    server, path = _server(tmp_path)

    with pytest.raises(Exception):
        _call(server, "calendar_event_propose", {"title": "Gym"})

    result = json.loads(_rows(path, "calendar_event_propose")[0]["result_json"])
    assert "summary" in result["error"]
    assert result["error_type"]


def test_argument_values_are_never_stored(tmp_path: Path) -> None:
    """Names diagnose a malformed call. Values are the owner's data, and an
    audit log that accumulates them becomes the thing worth stealing."""
    server, path = _server(tmp_path)

    _call(server, "remember", {"statement": "my landlord is Priya at 555-0100"})

    stored = json.dumps([dict(row) for row in _rows(path, "remember")])
    assert "Priya" not in stored
    assert "555-0100" not in stored
    assert "argument_names" in stored


def test_a_successful_call_is_recorded_too(tmp_path: Path) -> None:
    """Otherwise the log answers "what broke" but never "what worked", and a
    turn that called nothing looks identical to one that called and failed."""
    server, path = _server(tmp_path)

    _call(server, "system_status", {})

    rows = _rows(path, "system_status")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "ok"


def test_timing_is_recorded(tmp_path: Path) -> None:
    server, path = _server(tmp_path)

    _call(server, "system_status", {})

    assert "duration_ms" in json.loads(_rows(path, "system_status")[0]["result_json"])


def test_telemetry_failure_never_breaks_a_working_tool(tmp_path: Path, monkeypatch) -> None:
    """A tool that works must keep working when the recorder cannot write."""
    server, path = _server(tmp_path)

    import alfred.mcp_server as module

    class _Broken:
        def __init__(self, *_: object) -> None:
            raise RuntimeError("audit unavailable")

    monkeypatch.setattr(module, "AuditLog", _Broken)

    assert _call(server, "system_status", {}) is not None


def test_validation_errors_do_not_leak_the_values_they_rejected(tmp_path: Path) -> None:
    """Pydantic quotes the offending input back inside its own message --
    input_value={'statement': '...'} -- so storing the raw error would smuggle
    argument values into the one field that exists to be safe."""
    server, path = _server(tmp_path)

    with pytest.raises(Exception):
        _call(server, "remember", {"wrong_name": "my landlord is Priya at 555-0100"})

    stored = json.dumps([dict(row) for row in _rows(path, "remember")])
    assert "Priya" not in stored
    assert "555-0100" not in stored
    # The diagnosis itself must survive the redaction.
    assert "statement" in stored
