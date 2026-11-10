"""Structured static findings and explicit, reasoned line exemptions."""
from dataclasses import dataclass
import re

@dataclass(frozen=True)
class Finding:
    rule_id: str
    path: str
    line: int
    message: str

EXEMPT_RE = re.compile(r"(?:guard|peq)-exempt\s+([a-z][a-z0-9_.-]+):\s+(.{8,})")
