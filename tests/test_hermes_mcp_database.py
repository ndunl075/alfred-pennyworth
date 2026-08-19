"""The registration must name the database, and preflight must check it.

Hermes was registered without `--db`. `alfred-mcp` fell back to its relative
default, resolved it against Hermes's working directory rather than the
project's, and SQLite created the file on demand -- so `migrate()` handed the
agent a complete, empty schema.

Every tool call then succeeded against a database containing nothing. Asked
to list what it could do, Alfred answered accurately from the database it had:
Gmail was not connected. No exception, no failed call, no stale sync row to
notice -- the one failure mode none of the three earlier checks could see,
because nothing was broken, it was just pointed somewhere else.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alfred.db import Database
from alfred.hermes_mcp import (
    register,
    registered_args,
    registered_database,
    server_args,
)
from alfred.preflight import preflight

PROFILE = """\
# A profile config is mostly commentary explaining decisions, which is why
# registration is a text insertion rather than a YAML round-trip.
model: something

# mcp_servers:
#   example:
#     command: example-mcp
"""


def _config(tmp_path: Path, body: str = PROFILE) -> Path:
    path = tmp_path / "profiles" / "alfred" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    return path


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "real" / "alfred.db")
    database.migrate()
    return database


def test_registration_names_the_database_absolutely(tmp_path: Path) -> None:
    """A relative path means a different file to each process that reads it."""
    database = _database(tmp_path)
    config = _config(tmp_path)

    register(database.path, config_path=config)

    registered = registered_database(config.read_text(encoding="utf-8"))
    assert registered is not None
    assert registered.is_absolute()
    assert registered == database.path.resolve()


def test_a_registration_without_a_database_is_repaired(tmp_path: Path) -> None:
    """The exact broken state, and the re-run that previously did nothing."""
    database = _database(tmp_path)
    config = _config(
        tmp_path,
        PROFILE + "\nmcp_servers:\n  alfred:\n    command: alfred-mcp\n"
        "    args: [--client-id, hermes]\n",
    )

    result = register(database.path, config_path=config)

    assert result.changed
    assert registered_database(config.read_text(encoding="utf-8")) == database.path.resolve()


def test_repointing_keeps_every_other_line(tmp_path: Path) -> None:
    """Repair must not cost the comments, same as insertion."""
    database = _database(tmp_path)
    body = (
        PROFILE + "\nmcp_servers:\n  alfred:\n    command: alfred-mcp\n"
        "    args: [--client-id, hermes]\n\n# a trailing note worth keeping\n"
    )
    config = _config(tmp_path, body)

    register(database.path, config_path=config)
    after = config.read_text(encoding="utf-8")

    assert "# a trailing note worth keeping" in after
    assert "# mcp_servers:" in after
    assert after.count("mcp_servers:") == body.count("mcp_servers:")


def test_a_correct_registration_is_left_alone(tmp_path: Path) -> None:
    database = _database(tmp_path)
    config = _config(tmp_path)
    register(database.path, config_path=config)
    before = config.read_text(encoding="utf-8")

    result = register(database.path, config_path=config)

    assert not result.changed
    assert config.read_text(encoding="utf-8") == before


def test_a_path_with_spaces_survives_the_round_trip(tmp_path: Path) -> None:
    """Unquoted, "C:/Program Files/..." would split into two arguments."""
    database = Database(tmp_path / "Program Files" / "alfred.db")
    database.migrate()
    config = _config(tmp_path)

    register(database.path, config_path=config)

    assert registered_database(config.read_text(encoding="utf-8")) == database.path.resolve()


def test_the_client_id_is_not_lost_when_repointing(tmp_path: Path) -> None:
    """The scoped identity is what keeps Hermes off local-mcp's grants."""
    database = _database(tmp_path)
    config = _config(tmp_path)

    register(database.path, config_path=config)

    args = registered_args(config.read_text(encoding="utf-8"))
    assert args is not None
    assert args[: 2] == ("--client-id", "hermes")
    assert args == server_args(database.path)


@pytest.mark.parametrize(
    "args",
    ["[--client-id, hermes]", "[--client-id, hermes, --db, /somewhere/else.db]"],
)
def test_preflight_fails_when_the_agent_would_read_another_database(
    tmp_path: Path, args: str
) -> None:
    """Both halves of the bug: no --db at all, and a --db pointing elsewhere.

    Tool reachability cannot catch either -- the tools are reachable. It is
    the answers that are wrong.
    """
    database = _database(tmp_path)
    _config(
        tmp_path,
        PROFILE + f"\nmcp_servers:\n  alfred:\n    command: alfred-mcp\n    args: {args}\n",
    )

    report = preflight(database, profile="alfred", profile_root=tmp_path / "profiles")
    check = next(item for item in report.checks if item.name == "mcp_database_matches")

    assert not check.ok
    assert not report.ok
    assert check.fix == "alfred hermes-mcp-register"


def test_preflight_passes_once_registration_is_repaired(tmp_path: Path) -> None:
    database = _database(tmp_path)
    config = _config(tmp_path)
    register(database.path, config_path=config)

    report = preflight(database, profile="alfred", profile_root=tmp_path / "profiles")
    check = next(item for item in report.checks if item.name == "mcp_database_matches")

    assert check.ok


def test_an_unregistered_profile_reports_one_failure_not_two(tmp_path: Path) -> None:
    """Two lines for one cause trains the reader to skim past both."""
    database = _database(tmp_path)
    _config(tmp_path)

    report = preflight(database, profile="alfred", profile_root=tmp_path / "profiles")
    failed = [check.name for check in report.checks if not check.ok]

    assert "hermes_mcp_registered" in failed
    assert "mcp_database_matches" not in failed
