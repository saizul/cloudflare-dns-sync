"""Load and validate zone files (one YAML file per zone)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from cloudflare_dns_sync.records import (
    AUTO_TTL,
    PROXIABLE_TYPES,
    SUPPORTED_TYPES,
    Record,
    RecordError,
    normalize_host,
    parse_caa,
    parse_srv,
    validate_values,
)

DEFAULT_TAG = "[dns-sync]"
MAX_COMMENT = 100  # Cloudflare's limit on the free plan

TOP_KEYS = {"zone", "zone_id", "managed_tag", "delete_unmanaged", "defaults", "records"}
DEFAULT_KEYS = {"ttl", "proxied"}
RECORD_KEYS = {
    "name",
    "type",
    "content",
    "ttl",
    "proxied",
    "comment",
    "priority",
    "weight",
    "port",
    "target",
    "flags",
    "tag",
    "value",
}


class ZoneFileError(Exception):
    """A zone file could not be loaded. ``errors`` lists every problem found."""

    def __init__(self, source: str, errors: list[str]):
        self.source = source
        self.errors = errors
        super().__init__(f"{source}: " + "; ".join(errors))


@dataclass
class ZoneFile:
    zone: str
    records: list[Record]
    zone_id: str | None = None
    managed_tag: str = DEFAULT_TAG
    delete_unmanaged: bool = False
    source: str = "<memory>"
    warnings: list[str] = field(default_factory=list)


def parse_ttl(value: Any) -> int:
    if value is None or (isinstance(value, str) and value.strip().lower() == "auto"):
        return AUTO_TTL
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecordError("ttl must be 'auto' or an integer number of seconds")
    if value != AUTO_TTL and not 30 <= value <= 86400:
        raise RecordError("ttl must be 'auto' (1) or between 30 and 86400 seconds")
    return value


def qualify(name: str, zone: str) -> str:
    """Turn ``@``, ``www`` or ``www.example.com.`` into a fully qualified name."""
    raw = str(name).strip()
    if raw in ("", "@"):
        return zone
    if raw.endswith("."):
        fqdn = normalize_host(raw)
        if fqdn != zone and not fqdn.endswith("." + zone):
            raise RecordError(f"name {raw!r} is outside the zone {zone}")
        return fqdn
    host = normalize_host(raw)
    if host == zone or host.endswith("." + zone):
        return host
    return f"{host}.{zone}"


def relative(name: str, zone: str) -> str:
    """Inverse of :func:`qualify` used by ``export``."""
    if name == zone:
        return "@"
    suffix = "." + zone
    return name[: -len(suffix)] if name.endswith(suffix) else name + "."


def _target(value: Any, zone: str) -> str:
    text = str(value).strip()
    return zone if text == "@" else text


def _record_values(rtype: str, item: dict[str, Any], zone: str, content: Any) -> dict[str, Any]:
    if rtype == "SRV":
        if content is not None:
            values = parse_srv(str(content))
        else:
            missing = [k for k in ("priority", "weight", "port", "target") if item.get(k) is None]
            if missing:
                raise RecordError(
                    "SRV needs content or priority, weight, port and target (missing: " + ", ".join(missing) + ")"
                )
            values = {k: item[k] for k in ("priority", "weight", "port", "target")}
        values["target"] = _target(values["target"], zone)
        return values
    if rtype == "CAA":
        if content is not None:
            return parse_caa(str(content))
        if item.get("tag") is None or item.get("value") is None:
            raise RecordError("CAA needs content or tag and value")
        return {"flags": item.get("flags", 0), "tag": item["tag"], "value": str(item["value"])}
    if content is None:
        raise RecordError(f"{rtype} needs content")
    if rtype == "MX":
        if item.get("priority") is None:
            raise RecordError("MX needs priority")
        return {"content": _target(content, zone), "priority": item["priority"]}
    if rtype == "CNAME":
        return {"content": _target(content, zone)}
    if rtype == "TXT" and not isinstance(content, str):
        raise RecordError("TXT content must be a string (quote it in YAML)")
    return {"content": str(content)}


def parse_zone(data: Any, source: str = "<memory>") -> ZoneFile:
    """Validate the parsed YAML of one zone file and build a :class:`ZoneFile`."""
    errors: list[str] = []
    if not isinstance(data, dict):
        raise ZoneFileError(source, ["the file must contain a mapping with 'zone' and 'records'"])
    unknown = set(data) - TOP_KEYS
    if unknown:
        errors.append("unknown keys: " + ", ".join(sorted(map(str, unknown))))
    zone_raw = data.get("zone")
    if not isinstance(zone_raw, str) or not zone_raw.strip():
        raise ZoneFileError(source, [*errors, "'zone' (the zone name, e.g. example.com) is required"])
    zone = normalize_host(zone_raw)

    tag = data.get("managed_tag", DEFAULT_TAG)
    if not isinstance(tag, str) or not tag.strip():
        errors.append("managed_tag must be a non-empty string")
        tag = DEFAULT_TAG
    tag = tag.strip()
    delete_unmanaged = data.get("delete_unmanaged", False)
    if not isinstance(delete_unmanaged, bool):
        errors.append("delete_unmanaged must be true or false")
        delete_unmanaged = False
    zone_id = data.get("zone_id")
    if zone_id is not None and not isinstance(zone_id, str):
        errors.append("zone_id must be a string")
        zone_id = None

    defaults = data.get("defaults") or {}
    default_ttl, default_proxied = AUTO_TTL, False
    if not isinstance(defaults, dict):
        errors.append("defaults must be a mapping")
    else:
        if set(defaults) - DEFAULT_KEYS:
            errors.append("defaults: unknown keys: " + ", ".join(sorted(map(str, set(defaults) - DEFAULT_KEYS))))
        try:
            default_ttl = parse_ttl(defaults.get("ttl"))
        except RecordError as exc:
            errors.append(f"defaults: {exc}")
        default_proxied = defaults.get("proxied", False)
        if not isinstance(default_proxied, bool):
            errors.append("defaults: proxied must be true or false")
            default_proxied = False

    raw_records = data.get("records")
    if raw_records is None:
        raw_records = []
    if not isinstance(raw_records, list):
        raise ZoneFileError(source, [*errors, "records must be a list"])

    records: list[Record] = []
    for index, item in enumerate(raw_records):
        where = f"records[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        unknown = set(item) - RECORD_KEYS
        if unknown:
            errors.append(f"{where}: unknown keys: " + ", ".join(sorted(map(str, unknown))))
            continue
        try:
            records.extend(_parse_record(item, zone, tag, default_ttl, default_proxied))
        except RecordError as exc:
            label = item.get("name", "?")
            errors.append(f"{where} ({label} {item.get('type', '?')}): {exc}")

    errors.extend(_check_conflicts(records))
    if errors:
        raise ZoneFileError(source, errors)
    return ZoneFile(
        zone=zone,
        records=records,
        zone_id=zone_id,
        managed_tag=tag,
        delete_unmanaged=delete_unmanaged,
        source=source,
    )


def _parse_record(item: dict[str, Any], zone: str, tag: str, default_ttl: int, default_proxied: bool) -> list[Record]:
    rtype = str(item.get("type", "")).upper()
    if rtype not in SUPPORTED_TYPES:
        raise RecordError(f"type must be one of {', '.join(SUPPORTED_TYPES)}")
    if "name" not in item:
        raise RecordError("name is required (use '@' for the zone apex)")
    name = qualify(str(item["name"]), zone)
    ttl = parse_ttl(item["ttl"]) if "ttl" in item else default_ttl
    proxied = item.get("proxied", default_proxied if rtype in PROXIABLE_TYPES else False)
    if not isinstance(proxied, bool):
        raise RecordError("proxied must be true or false")
    if proxied and rtype not in PROXIABLE_TYPES:
        raise RecordError(f"{rtype} records cannot be proxied")
    comment = item.get("comment", "") or ""
    if not isinstance(comment, str):
        raise RecordError("comment must be a string")
    comment = comment.strip()
    if tag in comment:
        raise RecordError("comment must not contain the managed tag; it is added automatically")
    full = len(comment) + len(tag) + (1 if comment else 0)
    if full > MAX_COMMENT:
        raise RecordError(f"comment plus managed tag is {full} characters; Cloudflare allows {MAX_COMMENT}")

    contents = item.get("content")
    items = contents if isinstance(contents, list) else [contents]
    if isinstance(contents, list) and not contents:
        raise RecordError("content list must not be empty")
    out = []
    for content in items:
        values = _record_values(rtype, item, zone, content)
        validate_values(rtype, values)
        out.append(Record(name=name, type=rtype, values=values, ttl=ttl, proxied=proxied, comment=comment))
    return out


def _check_conflicts(records: list[Record]) -> list[str]:
    errors = []
    seen: set[tuple[Any, ...]] = set()
    by_name: dict[str, set[str]] = defaultdict(set)
    cname_count: dict[str, int] = defaultdict(int)
    for rec in records:
        key = rec.identity()
        if key in seen:
            errors.append(f"duplicate record {rec.name} {rec.type} {rec.content}")
        seen.add(key)
        by_name[rec.name].add(rec.type)
        if rec.type == "CNAME":
            cname_count[rec.name] += 1
    for name, types in sorted(by_name.items()):
        if cname_count[name] > 1:
            errors.append(f"{name}: only one CNAME record is allowed per name")
        if "CNAME" in types and len(types) > 1:
            others = ", ".join(sorted(types - {"CNAME"}))
            errors.append(f"{name}: a CNAME cannot coexist with other records ({others})")
    return errors


def load_zone_file(path: str | Path) -> ZoneFile:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ZoneFileError(str(path), [f"cannot read file: {exc.strerror or exc}"]) from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ZoneFileError(str(path), [f"invalid YAML: {exc}"]) from exc
    return parse_zone(data, str(path))


def discover(paths: list[str]) -> list[Path]:
    """Expand files and directories (``*.yaml``/``*.yml``, not recursive) into a sorted list."""
    found: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            found.extend(sorted(x for x in p.iterdir() if x.suffix in (".yaml", ".yml") and x.is_file()))
        else:
            found.append(p)
    return found


def load_zone_files(paths: list[str]) -> list[ZoneFile]:
    files = discover(paths)
    if not files:
        raise ZoneFileError(", ".join(paths) or ".", ["no zone files found"])
    zones = [load_zone_file(p) for p in files]
    names = [z.zone for z in zones]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ZoneFileError(
            ", ".join(str(f) for f in files), ["zone defined in more than one file: " + ", ".join(dupes)]
        )
    return zones
