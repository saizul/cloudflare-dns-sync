"""Compute and apply the difference between a zone file and the live zone."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from cloudflare_dns_sync.api import CloudflareClient, CloudflareError
from cloudflare_dns_sync.records import AUTO_TTL, SUPPORTED_TYPES, VALUE_KEYS, Record
from cloudflare_dns_sync.zonefile import ZoneFile

ACTION_ORDER = {"delete": 0, "update": 1, "create": 2}


@dataclass
class Change:
    action: str  # "create" | "update" | "delete"
    before: Record | None = None
    after: Record | None = None
    fields: list[str] = field(default_factory=list)

    @property
    def record(self) -> Record:
        rec = self.after if self.after is not None else self.before
        assert rec is not None
        return rec

    def sort_key(self) -> tuple[int, str, str, str]:
        return (ACTION_ORDER[self.action], *self.record.sort_key())


@dataclass
class ZonePlan:
    zone: str
    zone_id: str
    tag: str
    changes: list[Change] = field(default_factory=list)
    unmanaged: list[Record] = field(default_factory=list)
    unsupported: list[Record] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    in_sync: int = 0

    def count(self, action: str) -> int:
        return sum(1 for c in self.changes if c.action == action)

    @property
    def has_changes(self) -> bool:
        return bool(self.changes)


def diff_fields(desired: Record, live: Record, tag: str) -> list[str]:
    """Names of the attributes that differ between a desired and a live record."""
    fields = []
    want, have = desired.canonical(), live.canonical()
    for key in VALUE_KEYS[desired.type]:
        if want.get(key) != have.get(key):
            fields.append(key)
    if desired.proxiable and desired.proxied != live.proxied:
        fields.append("proxied")
    want_ttl = AUTO_TTL if desired.proxied else desired.ttl
    if not desired.proxied and want_ttl != live.ttl:
        fields.append("ttl")
    if desired.comment != live.comment or not live.managed:
        fields.append("comment")
    return fields


def build_plan(zonefile: ZoneFile, live: list[Record], zone_id: str = "") -> ZonePlan:
    tag = zonefile.managed_tag
    plan = ZonePlan(zone=zonefile.zone, zone_id=zone_id, tag=tag)

    def handled(rec: Record) -> bool:
        # Apex NS records belong to Cloudflare: never plan changes for them.
        return rec.type in SUPPORTED_TYPES and not (rec.type == "NS" and rec.name == zonefile.zone)

    supported = [r for r in live if handled(r)]
    plan.unsupported = sorted((r for r in live if not handled(r)), key=Record.sort_key)

    desired_groups: dict[tuple[str, str], list[Record]] = defaultdict(list)
    live_groups: dict[tuple[str, str], list[Record]] = defaultdict(list)
    for rec in zonefile.records:
        desired_groups[(rec.name, rec.type)].append(rec)
    for rec in supported:
        live_groups[(rec.name, rec.type)].append(rec)

    def may_touch(rec: Record) -> bool:
        return rec.managed or zonefile.delete_unmanaged

    for key in sorted(set(desired_groups) | set(live_groups)):
        want = sorted(desired_groups.get(key, []), key=Record.sort_key)
        have = sorted(live_groups.get(key, []), key=Record.sort_key)

        # 1. Same identity (e.g. same IP, same MX host): update attributes in place.
        #    This also adopts an untagged record that exactly matches the zone file.
        remaining_want = []
        for rec in want:
            match = next((h for h in have if h.identity() == rec.identity()), None)
            if match is None:
                remaining_want.append(rec)
                continue
            have.remove(match)
            fields = diff_fields(rec, match, tag)
            if fields:
                plan.changes.append(Change("update", before=match, after=_with_id(rec, match), fields=fields))
            else:
                plan.in_sync += 1

        # 2. Pair the rest with live records we are allowed to modify (content changes).
        touchable = [h for h in have if may_touch(h)]
        while remaining_want and touchable:
            rec, match = remaining_want.pop(0), touchable.pop(0)
            have.remove(match)
            plan.changes.append(
                Change("update", before=match, after=_with_id(rec, match), fields=diff_fields(rec, match, tag))
            )

        # 3. Anything left in the zone file is new.
        for rec in remaining_want:
            plan.changes.append(Change("create", after=rec))

        # 4. Anything left live is deleted if managed, otherwise left alone.
        for rec in have:
            if may_touch(rec):
                plan.changes.append(Change("delete", before=rec))
            else:
                plan.unmanaged.append(rec)

    plan.warnings.extend(_cname_conflicts(plan))
    plan.changes.sort(key=Change.sort_key)
    plan.unmanaged.sort(key=Record.sort_key)
    return plan


def _with_id(desired: Record, live: Record) -> Record:
    return Record(
        name=desired.name,
        type=desired.type,
        values=dict(desired.values),
        ttl=desired.ttl,
        proxied=desired.proxied,
        comment=desired.comment,
        id=live.id,
        managed=True,
    )


def _cname_conflicts(plan: ZonePlan) -> list[str]:
    """Creating next to an untouched record of a conflicting type fails at Cloudflare; say so up front."""
    warnings = []
    kept = defaultdict(set)
    for rec in plan.unmanaged:
        kept[rec.name].add(rec.type)
    for change in plan.changes:
        if change.action != "create":
            continue
        rec = change.record
        types = kept.get(rec.name, set())
        if (rec.type == "CNAME" and types) or (rec.type != "CNAME" and "CNAME" in types):
            warnings.append(
                f"{rec.name}: creating {rec.type} conflicts with an unmanaged record "
                f"({', '.join(sorted(types))}); tag it with the managed tag or set delete_unmanaged: true"
            )
    return warnings


@dataclass
class ApplyResult:
    zone: str
    applied: list[Change] = field(default_factory=list)
    failed: Change | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.failed is None


def apply_plan(client: CloudflareClient, plan: ZonePlan) -> ApplyResult:
    """Execute changes in order (deletes, updates, creates); stop at the first error."""
    result = ApplyResult(zone=plan.zone)
    for change in plan.changes:
        try:
            if change.action == "delete":
                assert change.before is not None and change.before.id
                client.delete_record(plan.zone_id, change.before.id)
            elif change.action == "update":
                assert change.after is not None and change.after.id
                client.update_record(plan.zone_id, change.after.id, change.after.to_api(plan.tag))
            else:
                assert change.after is not None
                client.create_record(plan.zone_id, change.after.to_api(plan.tag))
        except CloudflareError as exc:
            result.failed = change
            result.error = str(exc)
            return result
        result.applied.append(change)
    return result


def fetch_live(client: CloudflareClient, zonefile: ZoneFile) -> tuple[str, list[Record]]:
    zone_id = zonefile.zone_id or client.zone_id(zonefile.zone)
    items = client.list_records(zone_id)
    return zone_id, [Record.from_api(item, zonefile.managed_tag) for item in items]


def record_dict(rec: Record, tag: str | None = None) -> dict[str, Any]:
    """JSON-friendly view of a record used by the JSON plan output."""
    out: dict[str, Any] = {"name": rec.name, "type": rec.type, "content": rec.content}
    out.update({k: rec.values[k] for k in VALUE_KEYS[rec.type] if k != "content"})
    out["ttl"] = "auto" if rec.ttl == AUTO_TTL or rec.proxied else rec.ttl
    if rec.proxiable:
        out["proxied"] = rec.proxied
    out["comment"] = rec.comment
    if rec.id:
        out["id"] = rec.id
    if tag is not None:
        out["managed"] = rec.managed
    return out
