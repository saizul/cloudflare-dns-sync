"""Render plans as colored text, Markdown (for pull request comments) or JSON."""

from __future__ import annotations

import json
from typing import Any

from cloudflare_dns_sync._version import __version__
from cloudflare_dns_sync.plan import Change, ZonePlan, record_dict
from cloudflare_dns_sync.records import AUTO_TTL, Record

SYMBOLS = {"create": "+", "update": "~", "delete": "-"}
COLORS = {"create": "32", "update": "33", "delete": "31", "warn": "35", "dim": "2", "bold": "1"}


class Style:
    def __init__(self, color: bool):
        self.color = color

    def __call__(self, text: str, kind: str) -> str:
        if not self.color:
            return text
        return f"\033[{COLORS[kind]}m{text}\033[0m"


def _attrs(rec: Record) -> str:
    parts = ["ttl=auto" if rec.ttl == AUTO_TTL or rec.proxied else f"ttl={rec.ttl}"]
    if rec.proxied:
        parts.append("proxied")
    if rec.comment:
        parts.append(f"comment={rec.comment!r}")
    return " ".join(parts)


def _field_value(rec: Record, name: str, tag: str) -> str:
    if name == "ttl":
        return "auto" if rec.ttl == AUTO_TTL else str(rec.ttl)
    if name == "proxied":
        return str(rec.proxied).lower()
    if name == "comment":
        return repr(rec.raw_comment if rec.raw_comment is not None else _tagged(rec.comment, tag))
    return str(rec.values.get(name))


def _tagged(comment: str, tag: str) -> str:
    return f"{comment} {tag}".strip()


def summary_line(plans: list[ZonePlan]) -> str:
    c = sum(p.count("create") for p in plans)
    u = sum(p.count("update") for p in plans)
    d = sum(p.count("delete") for p in plans)
    if not (c or u or d):
        return "No changes. DNS records are in sync."
    return f"Plan: {c} to create, {u} to update, {d} to delete."


def render_text(plans: list[ZonePlan], color: bool = False, show_unmanaged: bool = False) -> str:
    s = Style(color)
    lines: list[str] = []
    for plan in plans:
        header = f"{plan.zone}" + (f" (zone {plan.zone_id})" if plan.zone_id else "")
        lines.append(s(header, "bold"))
        width_t = max((len(c.record.type) for c in plan.changes), default=4)
        width_n = max((len(c.record.name) for c in plan.changes), default=4)
        for change in plan.changes:
            rec = change.record
            sym = SYMBOLS[change.action]
            head = f"  {sym} {change.action:<6} {rec.type:<{width_t}}  {rec.name:<{width_n}}  {rec.content}"
            if change.action != "update":
                head += f"  {_attrs(rec)}"
            lines.append(s(head, change.action))
            if change.action == "update":
                for name in change.fields:
                    assert change.before is not None and change.after is not None
                    before = _field_value(change.before, name, plan.tag)
                    after = _field_value(change.after, name, plan.tag)
                    lines.append(s(f"        {name}: {before} -> {after}", "dim"))
        if not plan.changes:
            lines.append(s(f"  no changes ({plan.in_sync} records in sync)", "dim"))
        for warning in plan.warnings:
            lines.append(s(f"  ! {warning}", "warn"))
        if plan.unmanaged:
            if show_unmanaged:
                lines.append(s(f"  {len(plan.unmanaged)} unmanaged records left untouched:", "dim"))
                for rec in plan.unmanaged:
                    lines.append(s(f"      {rec.type:<5} {rec.name}  {rec.content}", "dim"))
            else:
                lines.append(
                    s(
                        f"  {len(plan.unmanaged)} unmanaged records left untouched (--show-unmanaged to list)",
                        "dim",
                    )
                )
        if plan.unsupported:
            types = ", ".join(sorted({r.type for r in plan.unsupported}))
            lines.append(s(f"  {len(plan.unsupported)} records of unsupported types ignored ({types})", "dim"))
        lines.append("")
    lines.append(s(summary_line(plans), "bold"))
    return "\n".join(lines) + "\n"


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("`", "'").replace("\n", " ")


def _md_change(change: Change, tag: str) -> str:
    rec = change.record
    if change.action == "update":
        assert change.before is not None and change.after is not None
        detail = "<br>".join(
            f"{name}: `{_md_escape(_field_value(change.before, name, tag))}` → "
            f"`{_md_escape(_field_value(change.after, name, tag))}`"
            for name in change.fields
        )
    else:
        detail = _md_escape(_attrs(rec))
    icon = {"create": "`+` create", "update": "`~` update", "delete": "`-` delete"}[change.action]
    return f"| {icon} | {rec.type} | `{_md_escape(rec.name)}` | `{_md_escape(rec.content)}` | {detail} |"


def render_markdown(plans: list[ZonePlan]) -> str:
    lines = ["<!-- cloudflare-dns-sync-plan -->", "## DNS plan", "", f"**{summary_line(plans)}**", ""]
    for plan in plans:
        counts = f"{plan.count('create')} create, {plan.count('update')} update, {plan.count('delete')} delete"
        lines.append(f"### `{plan.zone}` ({counts})")
        lines.append("")
        if plan.changes:
            lines.append("| Action | Type | Name | Content | Details |")
            lines.append("| --- | --- | --- | --- | --- |")
            lines.extend(_md_change(c, plan.tag) for c in plan.changes)
        else:
            lines.append(f"No changes ({plan.in_sync} records in sync).")
        lines.append("")
        for warning in plan.warnings:
            lines.append(f"> [!WARNING]\n> {_md_escape(warning)}\n")
        if plan.unmanaged:
            lines.append("<details><summary>" + f"{len(plan.unmanaged)} unmanaged records left untouched</summary>")
            lines.append("")
            for rec in plan.unmanaged:
                lines.append(f"- `{rec.type}` `{_md_escape(rec.name)}` `{_md_escape(rec.content)}`")
            lines.append("")
            lines.append("</details>")
            lines.append("")
    lines.append(f"<sub>cloudflare-dns-sync {__version__}</sub>")
    return "\n".join(lines) + "\n"


def plan_to_dict(plans: list[ZonePlan]) -> dict[str, Any]:
    zones = []
    for plan in plans:
        changes = []
        for change in plan.changes:
            item: dict[str, Any] = {"action": change.action}
            if change.before is not None:
                item["before"] = record_dict(change.before)
            if change.after is not None:
                item["after"] = record_dict(change.after)
            if change.action == "update":
                item["fields"] = change.fields
            changes.append(item)
        zones.append(
            {
                "zone": plan.zone,
                "zone_id": plan.zone_id,
                "summary": {
                    "create": plan.count("create"),
                    "update": plan.count("update"),
                    "delete": plan.count("delete"),
                    "in_sync": plan.in_sync,
                    "unmanaged": len(plan.unmanaged),
                    "unsupported": len(plan.unsupported),
                },
                "changes": changes,
                "unmanaged": [record_dict(r) for r in plan.unmanaged],
                "warnings": plan.warnings,
            }
        )
    return {
        "format_version": 1,
        "tool": f"cloudflare-dns-sync {__version__}",
        "has_changes": any(p.has_changes for p in plans),
        "zones": zones,
    }


def render_json(plans: list[ZonePlan]) -> str:
    return json.dumps(plan_to_dict(plans), indent=2, ensure_ascii=False) + "\n"
