"""DNS record model shared by zone files, the Cloudflare API and the planner."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any

SUPPORTED_TYPES = ("A", "AAAA", "CNAME", "MX", "TXT", "SRV", "CAA", "NS")
PROXIABLE_TYPES = frozenset({"A", "AAAA", "CNAME"})
AUTO_TTL = 1

# Which keys of ``values`` each type uses, in display order.
VALUE_KEYS: dict[str, tuple[str, ...]] = {
    "A": ("content",),
    "AAAA": ("content",),
    "CNAME": ("content",),
    "TXT": ("content",),
    "MX": ("content", "priority"),
    "SRV": ("priority", "weight", "port", "target"),
    "CAA": ("flags", "tag", "value"),
    "NS": ("content",),
}

# The subset of ``values`` that identifies a record inside a (name, type) group.
# Everything else (priority, weight, ttl, proxied, comment) can be updated in place.
IDENTITY_KEYS: dict[str, tuple[str, ...]] = {
    "A": ("content",),
    "AAAA": ("content",),
    "CNAME": ("content",),
    "TXT": ("content",),
    "MX": ("content",),
    "SRV": ("target", "port"),
    "CAA": ("tag", "value"),
    "NS": ("content",),
}

CAA_TAGS = frozenset({"issue", "issuewild", "iodef"})
_HOST_RE = re.compile(r"^(\*\.)?([a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?\.)*[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?$")


class RecordError(ValueError):
    """A record value is invalid."""


def normalize_host(value: str) -> str:
    """Lower-case a host name and drop the trailing dot."""
    return value.strip().rstrip(".").lower()


def normalize_txt(value: str) -> str:
    """Cloudflare may return TXT content wrapped in quotes; compare without them."""
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"' and '"' not in value[1:-1].replace('\\"', ""):
        return value[1:-1].replace('\\"', '"')
    return value


def normalize_values(rtype: str, values: dict[str, Any]) -> dict[str, Any]:
    """Return a canonical copy of ``values`` used for comparisons."""
    out = dict(values)
    if rtype in ("A", "AAAA"):
        try:
            out["content"] = str(ipaddress.ip_address(str(values["content"]).strip()))
        except ValueError:
            out["content"] = str(values["content"]).strip()
    elif rtype in ("CNAME", "MX", "NS"):
        out["content"] = normalize_host(str(values["content"]))
    elif rtype == "TXT":
        out["content"] = normalize_txt(str(values["content"]))
    elif rtype == "SRV":
        out["target"] = normalize_host(str(values["target"]))
    elif rtype == "CAA":
        out["tag"] = str(values["tag"]).lower()
    return out


def render_content(rtype: str, values: dict[str, Any]) -> str:
    """Human-readable content, matching how Cloudflare shows the record."""
    if rtype == "SRV":
        return f"{values['priority']} {values['weight']} {values['port']} {values['target']}"
    if rtype == "CAA":
        return f'{values["flags"]} {values["tag"]} "{values["value"]}"'
    if rtype == "MX":
        return f"{values['priority']} {values['content']}"
    return str(values["content"])


@dataclass
class Record:
    """One DNS record.

    ``name`` is the fully qualified, lower-case name without a trailing dot.
    ``comment`` is the user's comment *without* the management tag.
    """

    name: str
    type: str
    values: dict[str, Any]
    ttl: int = AUTO_TTL
    proxied: bool = False
    comment: str = ""
    id: str | None = None
    managed: bool = False
    raw_comment: str | None = field(default=None, repr=False)

    @property
    def proxiable(self) -> bool:
        return self.type in PROXIABLE_TYPES

    @property
    def content(self) -> str:
        return render_content(self.type, self.values)

    def canonical(self) -> dict[str, Any]:
        return normalize_values(self.type, self.values)

    def identity(self) -> tuple[Any, ...]:
        canon = self.canonical()
        return (self.name, self.type, *(canon.get(k) for k in IDENTITY_KEYS[self.type]))

    def sort_key(self) -> tuple[str, str, str]:
        return (self.name, self.type, self.content)

    # -- Cloudflare API --------------------------------------------------

    def to_api(self, tag: str) -> dict[str, Any]:
        """Payload for POST/PUT ``/zones/{id}/dns_records``."""
        payload: dict[str, Any] = {
            "type": self.type,
            "name": self.name,
            "ttl": AUTO_TTL if self.proxied else self.ttl,
            "comment": render_comment(self.comment, tag),
        }
        if self.proxiable:
            payload["proxied"] = self.proxied
        if self.type in ("SRV", "CAA"):
            payload["data"] = {k: self.values[k] for k in VALUE_KEYS[self.type]}
        else:
            payload["content"] = self.values["content"]
        if self.type == "MX":
            payload["priority"] = self.values["priority"]
        return payload

    @classmethod
    def from_api(cls, item: dict[str, Any], tag: str) -> Record:
        """Build a record from one element of the ``dns_records`` list."""
        rtype = str(item.get("type", "")).upper()
        name = normalize_host(str(item.get("name", "")))
        data = item.get("data") or {}
        values: dict[str, Any]
        if rtype == "SRV":
            values = _srv_from_api(item, data)
        elif rtype == "CAA":
            values = _caa_from_api(item, data)
        elif rtype == "MX":
            values = {"content": str(item.get("content", "")), "priority": int(item.get("priority") or 0)}
        else:
            values = {"content": str(item.get("content", ""))}
        raw_comment = item.get("comment") or ""
        tags = item.get("tags") or []
        managed = bool(tag) and (tag in raw_comment or tag in tags)
        return cls(
            name=name,
            type=rtype,
            values=values,
            ttl=int(item.get("ttl") or AUTO_TTL),
            proxied=bool(item.get("proxied", False)),
            comment=strip_tag(raw_comment, tag),
            id=item.get("id"),
            managed=managed,
            raw_comment=raw_comment,
        )


def _srv_from_api(item: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    if data.get("target") is not None:
        priority = data.get("priority", item.get("priority", 0))
        return {
            "priority": int(priority or 0),
            "weight": int(data.get("weight") or 0),
            "port": int(data.get("port") or 0),
            "target": str(data["target"]),
        }
    # Older payloads only carry "weight port target" in content and priority on top.
    parts = str(item.get("content", "")).split()
    if len(parts) == 4:
        prio, weight, port, target = parts
    elif len(parts) == 3:
        prio = item.get("priority") or 0
        weight, port, target = parts
    else:
        return {"priority": 0, "weight": 0, "port": 0, "target": str(item.get("content", ""))}
    return {"priority": int(prio), "weight": int(weight), "port": int(port), "target": target}


def _caa_from_api(item: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    if data.get("tag") is not None:
        return {"flags": int(data.get("flags") or 0), "tag": str(data["tag"]), "value": str(data.get("value", ""))}
    return parse_caa(str(item.get("content", "")))


def parse_srv(text: str) -> dict[str, Any]:
    parts = text.split()
    if len(parts) != 4:
        raise RecordError("SRV content must be 'priority weight port target'")
    try:
        prio, weight, port = (int(p) for p in parts[:3])
    except ValueError as exc:
        raise RecordError("SRV priority, weight and port must be integers") from exc
    return {"priority": prio, "weight": weight, "port": port, "target": parts[3]}


def parse_caa(text: str) -> dict[str, Any]:
    parts = text.split(None, 2)
    if len(parts) != 3:
        raise RecordError("CAA content must be 'flags tag \"value\"'")
    try:
        flags = int(parts[0])
    except ValueError as exc:
        raise RecordError("CAA flags must be an integer") from exc
    return {"flags": flags, "tag": parts[1], "value": parts[2].strip().strip('"')}


def render_comment(comment: str, tag: str) -> str:
    comment = comment.strip()
    if not tag:
        return comment
    return f"{comment} {tag}" if comment else tag


def strip_tag(comment: str, tag: str) -> str:
    if not tag:
        return comment.strip()
    return " ".join(comment.replace(tag, " ").split())


def validate_values(rtype: str, values: dict[str, Any]) -> None:
    """Raise :class:`RecordError` if ``values`` is not valid for ``rtype``."""
    if rtype == "A":
        _ip(values["content"], ipaddress.IPv4Address, "IPv4")
    elif rtype == "AAAA":
        _ip(values["content"], ipaddress.IPv6Address, "IPv6")
    elif rtype in ("CNAME", "MX", "NS"):
        _host(values["content"], rtype)
    elif rtype == "TXT":
        if not str(values["content"]):
            raise RecordError("TXT content must not be empty")
    elif rtype == "SRV":
        for key in ("weight", "port"):
            _uint(values[key], key, 65535)
        _host(values["target"], "SRV target")
    elif rtype == "CAA":
        _uint(values["flags"], "flags", 255)
        if str(values["tag"]).lower() not in CAA_TAGS:
            raise RecordError(f"CAA tag must be one of {', '.join(sorted(CAA_TAGS))}")
    if "priority" in values:
        _uint(values["priority"], "priority", 65535)


def _ip(value: Any, kind: type, label: str) -> None:
    try:
        parsed = ipaddress.ip_address(str(value).strip())
    except ValueError as exc:
        raise RecordError(f"{value!r} is not a valid {label} address") from exc
    if not isinstance(parsed, kind):
        raise RecordError(f"{value!r} is not a valid {label} address")


def _host(value: Any, label: str) -> None:
    host = normalize_host(str(value))
    if not host or not _HOST_RE.match(host):
        raise RecordError(f"{label} target {value!r} is not a valid host name")


def _uint(value: Any, label: str, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise RecordError(f"{label} must be an integer between 0 and {maximum}")
