"""Structured static findings and explicit, reasoned line exemptions."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    rule_id: str
    path: str
    line: int
    message: str


def exempt_pattern(exempt_tag: str = "guard-exempt") -> re.Pattern[str]:
    return re.compile(rf"{re.escape(exempt_tag)}\s+([a-z][a-z0-9_.-]+):\s+(.{{8,}})")


EXEMPT_RE = exempt_pattern()


def _line_exempts_rule(
    line: str, rule_id: str, exempt_tag: str = "guard-exempt"
) -> bool:
    match = exempt_pattern(exempt_tag).search(line)
    return bool(match and match.group(1).split(".", 1)[-1] == rule_id.split(".", 1)[-1])
