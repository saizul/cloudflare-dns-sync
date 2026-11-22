"""Turn a live zone into a zone file."""

from __future__ import annotations

from collections import Counter
from typing import Any

import yaml

from cloudflare_dns_sync._version import __version__
from cloudflare_dns_sync.records import AUTO_TTL, SUPPORTED_TYPES, VALUE_KEYS, Record, normalize_txt
from cloudflare_dns_sync.zonefile import DEFAULT_TAG, relative


class _Dumper(yaml.SafeDumper):
    pass


def _str_presenter(dumper: yaml.SafeDumper, value: str) -> yaml.Node:
    return dumper.represent_scalar("tag:yaml.org,2002:str", value)


_Dumper.add_representer(str, _str_presenter)


def export_zone(
    zone: str,
    records: list[Record],
    *,
    tag: str = DEFAULT_TAG,
    managed_only: bool = False,
    zone_id: str | None = None,
) -> str:
    """Return a zone file (YAML text) describing ``records``.

    The most common TTL becomes ``defaults.ttl`` so the output stays short.
    Records of unsupported types are listed as comments at the end.
    """
    supported = [
        r for r in records if r.type in SUPPORTED_TYPES and not _apex_ns(r, zone) and (r.managed or not managed_only)
    ]
    skipped = [r for r in records if r.type not in SUPPORTED_TYPES or _apex_ns(r, zone)]
    supported.sort(key=lambda r: (r.name != zone, r.name, r.type, r.content))

    ttls = Counter(AUTO_TTL if r.proxied else r.ttl for r in supported)
    default_ttl = ttls.most_common(1)[0][0] if ttls else AUTO_TTL

    doc: dict[str, Any] = {"zone": zone}
    if zone_id:
        doc["zone_id"] = zone_id
    if tag != DEFAULT_TAG:
        doc["managed_tag"] = tag
    doc["delete_unmanaged"] = False
    doc["defaults"] = {"ttl": "auto" if default_ttl == AUTO_TTL else default_ttl, "proxied": False}
    doc["records"] = [_record(r, zone, default_ttl) for r in supported]

    header = (
        f"# Exported by cloudflare-dns-sync {__version__}.\n"
        "# Review it, commit it, then run `cloudflare-dns-sync plan` to adopt these records.\n"
    )
    body = yaml.dump(doc, Dumper=_Dumper, sort_keys=False, allow_unicode=True, default_flow_style=False, width=200)
    footer = ""
    if skipped:
        footer = "# Not exported (unsupported types or apex NS, left untouched by plan/apply):\n" + "".join(
            f"#   {r.type} {r.name} {r.content}\n" for r in sorted(skipped, key=Record.sort_key)
        )
    return header + body + footer


def _apex_ns(rec: Record, zone: str) -> bool:
    """Apex NS records belong to Cloudflare and cannot be set from a zone file."""
    return rec.type == "NS" and rec.name == zone


def _record(rec: Record, zone: str, default_ttl: int) -> dict[str, Any]:
    item: dict[str, Any] = {"name": relative(rec.name, zone), "type": rec.type}
    if rec.type in ("SRV", "CAA"):
        for key in VALUE_KEYS[rec.type]:
            value = rec.values[key]
            item[key] = relative(value, zone) if key == "target" and value == zone else value
    else:
        content = rec.values["content"]
        if rec.type in ("CNAME", "MX", "NS") and content.rstrip(".").lower() == zone:
            content = "@"
        if rec.type == "TXT":
            content = normalize_txt(content)
        item["content"] = content
        if rec.type == "MX":
            item["priority"] = rec.values["priority"]
    if rec.proxied:
        item["proxied"] = True
    else:
        ttl = rec.ttl
        if ttl != default_ttl:
            item["ttl"] = "auto" if ttl == AUTO_TTL else ttl
    if rec.comment:
        item["comment"] = rec.comment
    return item
