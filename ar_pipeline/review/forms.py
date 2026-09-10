"""Pure helpers translating the review form to/from canonical dicts.

The form uses flat dotted keys — ``header.payer_name``,
``line_items[0].deductions[1].amount`` — so a plain HTML form with
JS-cloned rows round-trips without any client framework. ``canonical_diff``
turns an edit into one audit row per changed leaf.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

FieldEdit = tuple[str, str | None, str | None]

_INDEX_RE = re.compile(r"^(?P<name>[a-z_]+)\[(?P<idx>\d+)\]$")


def _leaf(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str)


def parse_form_to_canonical(form: Mapping[str, str]) -> dict:
    header: dict = {}
    header_deductions: dict[int, dict] = {}
    lines: dict[int, dict] = {}
    line_deductions: dict[int, dict[int, dict]] = {}

    for raw_key, raw_val in form.items():
        val = raw_val.strip()
        if val == "":
            continue
        parts = raw_key.split(".")
        if parts[0] == "header" and len(parts) == 2:
            header[parts[1]] = val
        elif parts[0] == "header" and len(parts) == 3:
            m = _INDEX_RE.match(parts[1])
            if m and m.group("name") == "deductions":
                header_deductions.setdefault(int(m.group("idx")), {})[parts[2]] = val
        elif parts[0].startswith("line_items[") and len(parts) == 2:
            m = _INDEX_RE.match(parts[0])
            if m:
                lines.setdefault(int(m.group("idx")), {})[parts[1]] = val
        elif parts[0].startswith("line_items[") and len(parts) == 3:
            m0 = _INDEX_RE.match(parts[0])
            m1 = _INDEX_RE.match(parts[1])
            if m0 and m1 and m1.group("name") == "deductions":
                line_deductions.setdefault(int(m0.group("idx")), {}).setdefault(
                    int(m1.group("idx")), {}
                )[parts[2]] = val

    if header_deductions:
        header["deductions"] = [header_deductions[i] for i in sorted(header_deductions)]

    line_items: list[dict] = []
    for idx in sorted(lines):
        item = lines[idx]
        deds = line_deductions.get(idx)
        if deds:
            item["deductions"] = [deds[j] for j in sorted(deds)]
        line_items.append(item)

    return {"header": header, "line_items": line_items}


def canonical_diff(old: dict, new: dict) -> list[FieldEdit]:
    edits: list[FieldEdit] = []
    _diff_node("", old, new, edits)
    return edits


def _diff_node(path: str, old: object, new: object, edits: list[FieldEdit]) -> None:
    if isinstance(old, dict) or isinstance(new, dict):
        old_d = old if isinstance(old, dict) else {}
        new_d = new if isinstance(new, dict) else {}
        for key in list(old_d) + [k for k in new_d if k not in old_d]:
            child = f"{path}.{key}" if path else key
            _diff_node(child, old_d.get(key), new_d.get(key), edits)
        return
    if isinstance(old, list) or isinstance(new, list):
        old_l = old if isinstance(old, list) else []
        new_l = new if isinstance(new, list) else []
        for i in range(max(len(old_l), len(new_l))):
            o = old_l[i] if i < len(old_l) else None
            n = new_l[i] if i < len(new_l) else None
            _diff_node(f"{path}[{i}]", o, n, edits)
        return
    o_leaf, n_leaf = _leaf(old), _leaf(new)
    if o_leaf != n_leaf:
        edits.append((path, o_leaf, n_leaf))
