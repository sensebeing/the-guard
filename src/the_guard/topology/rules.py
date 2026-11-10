"""Which files the topology guard covers, and the vocabulary it judges them by."""

from __future__ import annotations

from pathlib import Path
import re

from ..finding import EXEMPT_RE

FOR_UPDATE_SQL_RE = re.compile(
    r"\b(?:FOR\s+(?:UPDATE|NO\s+KEY\s+UPDATE)|pg_(?:try_)?advisory_xact_lock|LOCK\s+TABLE)\b",
    re.IGNORECASE,
)

READ_ACTION_SUFFIXES = (
    ":history",
    ":batch-get",
    ":detail",
    ":context",
    ":list",
    ":search",
)

READ_FUNCTION_PREFIXES = (
    "get_",
    "read_",
    "list_",
    "fetch_",
    "retrieve_",
    "query_",
    "load_",
    "lookup_",
)
READ_FUNCTION_EXACT = frozenset({"get", "read", "list", "fetch", "retrieve"})

LOCK_OR_WRITE_FUNCTION_MARKERS = (
    "for_update",
    "claim",
    "lock",
    "acquire",
    "consume",
    "lease",
    "dequeue",
    "pop",
    "mutate",
    "upsert",
    "ensure",
    "create",
    "update",
    "delete",
    "insert",
    "write",
    "save",
    "commit",
    "publish",
    "register",
    "revoke",
    "reset",
    "cancel",
    "withdraw",
    "complete",
    "reopen",
    "mark_",
    "set_",
    "record_",
)

READ_CALL_PREFIXES = (
    "get_",
    "read_",
    "fetch_",
    "retrieve_",
    "load_",
    "lookup_",
    "find_",
    "ask_history",
)
READ_CALL_EXACT = frozenset({"get", "read", "fetch", "retrieve", "ask_history"})
NON_RESOURCE_READ_CALLS = frozenset(
    {
        "get_nowait",
        "get_connection",
        "get_session",
        "get_lock",
        "get_for_update",
        "get_event_loop",
        "get_running_loop",
        "get_logger",
        "get_settings",
        "get_clock",
        "read_line",
        "readline",
        "read_bytes",
        "readexactly",
        "read_chunk",
    }
)

WRITE_CALL_MARKERS = (
    "create",
    "update",
    "delete",
    "insert",
    "upsert",
    "save",
    "write",
    "commit",
    "execute",
    "apply",
    "mutate",
    "reset",
    "cancel",
    "withdraw",
    "complete",
    "reopen",
    "claim",
    "release",
    "publish",
    "sync",
    "post",
    "put",
    "patch",
    "remove",
    "clear",
    "flush",
    "refresh",
    "ensure",
    "register",
    "revoke",
    "record_",
    "mark_",
    "set_",
    "add_",
    "append_",
    "transition",
    "send",
    "wait",
)

BATCH_OR_INFRA_CALL_MARKERS = (
    "batch",
    "bulk",
    "many",
    "page",
    "chunk",
    "stream",
    "next",
    "cursor",
    "all",
    "list",
    "connection",
    "session",
    "token",
    "lease",
    "lock",
    "client",
    "channel",
    "queue",
    "nowait",
)

RETRY_LOOP_VAR_NAMES = frozenset(
    {"_", "attempt", "attempts", "retry", "retries", "try_index", "idx", "i"}
)

SWIFT_FUNC_DECL_RE = re.compile(
    r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:public|internal|package|private|fileprivate|open|final|override|mutating|nonisolated|static|class)\s+)*func\s+(?P<name>[A-Za-z_]\w*)\s*\("
)
SWIFT_AWAIT_CALL_RE = re.compile(
    r"\bawait\s+(?P<receiver>[A-Za-z_][\w.?]*)\.(?P<method>[A-Za-z_]\w*)\s*\((?P<args>[^;\n]*)\)"
)
SWIFT_READ_METHOD_PREFIXES = (
    "fetch",
    "get",
    "load",
    "read",
    "askHistory",
    "context",
    "detail",
)
SWIFT_WRITE_METHOD_PREFIXES = (
    "send",
    "create",
    "update",
    "delete",
    "reset",
    "submit",
    "confirm",
    "cancel",
    "resolve",
    "patch",
    "post",
    "put",
    "save",
    "mutate",
    "mark",
)



def _line_exempts_rule(line: str, rule_id: str, exempt_tag: str | None = None) -> bool:
    pattern = re.compile(re.escape(exempt_tag) + r"\s+([a-z][a-z0-9_.-]+):\s+(.{8,})") if exempt_tag else EXEMPT_RE
    match = pattern.search(line)
    return bool(match and match.group(1).split(".", 1)[-1] == rule_id.split(".", 1)[-1])
