"""Register Alfred's MCP server in a Hermes profile, without a human at a prompt.

Alfred's whole agent plane depends on Hermes reaching Alfred over stdio MCP:
architecture section 7 is thirty-three tools with per-turn selection, policy
scoping, and annotations. On the live install none of it had ever run. Hermes
answered `hermes -p alfred mcp list` with "No MCP servers configured", every
learning table sat at zero rows, and not one approval had ever been created
by `mcp:hermes` -- every answer Alfred had given came from the prompt context
pack alone.

The cause was a documented manual step. The profile's own `config.yaml` says
registration must be done by running `hermes mcp add ...` once by hand,
because that command "answers an interactive 'Enable all 17 tools?' prompt it
can't be scripted past". A setup step that cannot be scripted is a setup step
that eventually does not happen, and nothing downstream noticed for weeks --
Alfred kept replying, just from context alone, which is exactly why it went
unseen.

So this writes the same `mcp_servers:` key that command produces.

**Text insertion, not a YAML round-trip.** The profile config is mostly
comments -- why Nous Portal ended up primary, why the approvals list is
deliberately broad, which upstream behaviours were verified against a real
install. `yaml.safe_load` followed by `yaml.dump` would silently discard all
of it, turning a documented file into an anonymous one. Rewriting only the
lines this owns keeps every comment and every unrelated key byte-for-byte.

Idempotent, backed up, and verifiable: re-running changes nothing, the
previous file is kept beside the original the same way Hermes's own backups
are, and the authority on whether it worked is Hermes itself rather than this
module's opinion of the file it just wrote.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

#: Where a Hermes profile lives on Windows. The profile name is appended.
DEFAULT_PROFILE_ROOT = Path.home() / "AppData" / "Local" / "hermes" / "profiles"

#: The server name inside Hermes's config, and the command it runs.
#: `--client-id` gives the profile its own scoped identity rather than
#: sharing `local-mcp` with Claude and Cursor, matching section 7.
SERVER_NAME = "alfred"
SERVER_COMMAND = "alfred-mcp"
BASE_ARGS = ("--client-id", "hermes")


def server_args(database_path: Path) -> tuple[str, ...]:
    """The arguments Hermes must pass, including an *absolute* database path.

    `--db` looks redundant -- `alfred-mcp` has a default -- and passing it is
    the whole point. That default is the **relative** `.alfred/alfred.db`,
    resolved against the working directory of whoever spawned the process.
    The runner is started from the project directory and gets the real
    database; Hermes spawns this server from its own directory and got
    `~/.alfred/alfred.db`, which SQLite created on demand and `migrate()`
    dutifully populated with a full, empty schema.

    So every tool call succeeded against a database containing nothing. No
    Gmail events, no `sync_state` rows, no error -- and Alfred, asked what it
    could do, correctly reported from that database that Gmail was not
    connected. A wrong answer with no failure anywhere to notice.

    An absolute path is the fix: it means the same thing from any directory.
    """
    return (*BASE_ARGS, "--db", Path(database_path).expanduser().resolve().as_posix())


#: An *active* mcp_servers key: at column zero and not commented out. The live
#: config already contains a commented example, so a naive substring check
#: reports success against a file that configures nothing -- which is very
#: close to the original failure.
_ACTIVE_KEY = re.compile(r"^mcp_servers:\s*$", re.MULTILINE)

#: Characters that would end a YAML flow scalar early. Windows paths under
#: "Program Files" contain spaces, and an unquoted one would silently split
#: into two arguments.
_NEEDS_QUOTING = set(" :#,[]{}'\"")


def _scalar(value: str) -> str:
    """Render one flow-sequence entry, quoting only when it must be."""
    if not any(character in _NEEDS_QUOTING for character in value):
        return value
    # Single-quoted YAML takes backslashes literally, which matters on
    # Windows; only the quote itself needs escaping.
    return "'" + value.replace("'", "''") + "'"


def _render_args(args: Sequence[str]) -> str:
    return "[" + ", ".join(_scalar(argument) for argument in args) + "]"


def _block(args: Sequence[str]) -> str:
    return f"""
# Added by `alfred hermes-mcp-register`. This key -- not this distribution's
# mcp.json -- is what Hermes actually reads, and without it Alfred's entire
# MCP tool surface is invisible to the agent.
mcp_servers:
  {SERVER_NAME}:
    command: {SERVER_COMMAND}
    args: {_render_args(args)}
"""


class RegistrationResult(BaseModel):
    config_path: str
    #: False when the key was already present *and correct*, so a re-run is
    #: a no-op. A registration pointing at the wrong database is a change.
    changed: bool
    #: Where the previous file was copied, when one was written.
    backup_path: str | None = None
    detail: str


def profile_config_path(profile: str, *, profile_root: Path | None = None) -> Path:
    return (profile_root or DEFAULT_PROFILE_ROOT) / profile / "config.yaml"


def is_registered(config_text: str) -> bool:
    """Whether an active (uncommented) mcp_servers key is present.

    Presence only. `registered_database` answers whether it points anywhere
    useful, which is the question that actually went unasked.
    """
    return _ACTIVE_KEY.search(config_text) is not None


def _args_line(lines: list[str]) -> int | None:
    """Index of the `args:` line belonging to Alfred's server entry.

    Walked line by line rather than matched with one regex because the
    interesting part is the *nesting* -- which server an `args:` belongs to
    -- and a regex that tracked indentation across three levels would be
    harder to read than the loop it replaced.
    """
    in_servers = False
    in_alfred = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if line.rstrip() == "mcp_servers:":
            in_servers = True
            continue
        if not in_servers or not stripped:
            continue
        if not line[0].isspace():
            return None  # a new top-level key; Alfred's entry is behind us
        if stripped.rstrip(":") == SERVER_NAME and stripped.endswith(":"):
            in_alfred = True
            continue
        if in_alfred and stripped.startswith("args:"):
            return index
        # Another server at the same depth ends Alfred's entry.
        if in_alfred and stripped.endswith(":") and not stripped.startswith("command"):
            in_alfred = False
    return None


def registered_args(config_text: str) -> tuple[str, ...] | None:
    """The arguments Hermes currently passes, or None if unregistered."""
    lines = config_text.splitlines()
    index = _args_line(lines)
    if index is None:
        return None
    inside = lines[index].split("args:", 1)[1].strip().strip("[]")
    return tuple(
        argument.strip().strip("'\"") for argument in inside.split(",") if argument.strip()
    )


def registered_database(config_text: str) -> Path | None:
    """The database the registered server would open, if it names one.

    None covers both "not registered" and "registered without `--db`" -- the
    second being the state that broke the live install, because it does not
    fail, it just resolves somewhere else.
    """
    args = registered_args(config_text)
    if not args or "--db" not in args:
        return None
    position = args.index("--db")
    if position + 1 >= len(args):
        return None
    return Path(args[position + 1])


def register(
    database_path: Path,
    *,
    profile: str = "alfred",
    profile_root: Path | None = None,
    config_path: Path | None = None,
    dry_run: bool = False,
) -> RegistrationResult:
    """Point a Hermes profile at Alfred's MCP server and at the real database.

    Repairs as well as adds. An existing registration naming a different
    database -- or naming none, and so silently falling back to a relative
    path resolved against Hermes's working directory -- is rewritten. The
    earlier version treated any `mcp_servers` key as success and returned
    "nothing to do", which is how a registration pointing at an empty
    database survived a re-run intended to fix exactly that.
    """
    path = config_path or profile_config_path(profile, profile_root=profile_root)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist; install the Hermes profile before registering"
        )
    original = path.read_text(encoding="utf-8")
    wanted = server_args(database_path)
    resolved = wanted[wanted.index("--db") + 1]

    if is_registered(original):
        if registered_args(original) == wanted:
            return RegistrationResult(
                config_path=str(path),
                changed=False,
                detail=f"already registered against {resolved}; nothing to do",
            )
        current = registered_database(original)
        was = current.as_posix() if current else "no --db, so Hermes's own directory"
        action, updated = f"repointed from {was} to {resolved}", _repointed(original, wanted)
    else:
        action, updated = f"registered {SERVER_NAME} against {resolved}", (
            original + ("" if original.endswith("\n") else "\n") + _block(wanted)
        )

    if dry_run:
        return RegistrationResult(
            config_path=str(path), changed=False, detail=f"would have {action} (dry run)"
        )

    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup = path.with_suffix(f".yaml.alfred-bak.{stamp}")
    # Copied rather than moved, so an interrupted write cannot leave the
    # profile without a config at all.
    shutil.copy2(path, backup)
    path.write_text(updated, encoding="utf-8")
    return RegistrationResult(
        config_path=str(path),
        changed=True,
        backup_path=str(backup),
        detail=f"{action}; restart Alfred for the agent to pick it up",
    )


def _repointed(config_text: str, args: Sequence[str]) -> str:
    """Rewrite only Alfred's `args:` line, leaving every other byte alone.

    Same reasoning as the module docstring: the profile config is mostly
    comments explaining decisions, and a YAML round-trip would discard them.
    """
    lines = config_text.splitlines(keepends=True)
    index = _args_line([line.rstrip("\n") for line in lines])
    if index is None:  # pragma: no cover - is_registered() gates this
        raise ValueError("no args line to repoint")
    indent = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
    ending = "\n" if lines[index].endswith("\n") else ""
    lines[index] = f"{indent}args: {_render_args(args)}{ending}"
    return "".join(lines)
