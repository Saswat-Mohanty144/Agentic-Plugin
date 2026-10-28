"""Structural fingerprint of a host Salary Slip (piece F1, decision F1-6).

Only the fields the PF calculation reads are fingerprinted, so unrelated host changes don't block payroll,
while any change to what we read (rename, removal, type change) does.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

TOP_LEVEL_FIELDS = ("name", "employee", "start_date", "end_date", "currency", "gross_pay", "docstatus", "modified",
                    "earnings")
EARNING_FIELDS = ("salary_component", "abbr", "amount", "statistical_component", "do_not_include_in_total")


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def slip_fingerprint(slip: Dict[str, Any]) -> str:
    pairs: List[str] = []
    for key in TOP_LEVEL_FIELDS:
        pairs.append(f"{key}:{_json_type(slip[key]) if key in slip else 'missing'}")
    rows = slip.get("earnings") if isinstance(slip.get("earnings"), list) else []
    for key in EARNING_FIELDS:
        types = sorted({(_json_type(row[key]) if isinstance(row, dict) and key in row else "missing") for row in rows})
        pairs.append(f"earnings[].{key}:{'|'.join(types) or 'empty'}")
    digest = hashlib.sha256(json.dumps(sorted(pairs)).encode()).hexdigest()
    return f"sha256:{digest}"
