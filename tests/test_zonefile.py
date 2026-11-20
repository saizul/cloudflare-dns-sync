from pathlib import Path

import pytest
import yaml

from cloudflare_dns_sync.records import AUTO_TTL
from cloudflare_dns_sync.zonefile import (
    DEFAULT_TAG,
    ZoneFileError,
    load_zone_file,
    load_zone_files,
    parse_zone,
    qualify,
    relative,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "zones"


def zone(records, **extra):
    return parse_zone({"zone": "example.com", "records": records, **extra})


def errors_of(data) -> list[str]:
    with pytest.raises(ZoneFileError) as info:
        parse_zone(data)
    return info.value.errors


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("@", "example.com"),
        ("", "example.com"),
        ("www", "www.example.com"),
        ("WWW", "www.example.com"),
        ("*.dev", "*.dev.example.com"),
        ("www.example.com", "www.example.com"),
        ("www.example.com.", "www.example.com"),
        ("example.com.", "example.com"),
        ("_sip._tcp", "_sip._tcp.example.com"),
    ],
)
def test_qualify(name, expected):
    assert qualify(name, "example.com") == expected


def test_qualify_rejects_names_outside_the_zone():
    with pytest.raises(ValueError, match="outside the zone"):
        qualify("www.other.org.", "example.com")


def test_relative_is_inverse_of_qualify():
    for name in ("@", "www", "a.b.c", "*.dev"):
        assert relative(qualify(name, "example.com"), "example.com") == name


def test_full_example_parses():
    zf = zone(
        [
            {"name": "@", "type": "A", "content": "203.0.113.10", "proxied": True, "comment": "web"},
            {"name": "@", "type": "AAAA", "content": "2001:db8::10"},
            {"name": "www", "type": "CNAME", "content": "@", "proxied": True},
            {"name": "@", "type": "MX", "content": ["mx1.example.net", "mx2.example.net"], "priority": 10},
            {"name": "@", "type": "TXT", "content": "v=spf1 -all", "ttl": 3600},
            {"name": "_sip._tcp", "type": "SRV", "priority": 10, "weight": 5, "port": 5060, "target": "sip"},
            {"name": "_xmpp._tcp", "type": "SRV", "content": "5 0 5222 xmpp.example.net"},
            {"name": "@", "type": "CAA", "tag": "issue", "value": "letsencrypt.org"},
            {"name": "@", "type": "CAA", "content": '0 iodef "mailto:security@example.com"'},
        ]
    )
    assert zf.zone == "example.com"
    assert zf.managed_tag == DEFAULT_TAG
    assert zf.delete_unmanaged is False
    assert len(zf.records) == 10
    www = next(r for r in zf.records if r.type == "CNAME")
    assert www.values["content"] == "example.com"
    assert www.proxied is True
    txt = next(r for r in zf.records if r.type == "TXT")
    assert txt.ttl == 3600
    srv = [r for r in zf.records if r.type == "SRV"]
    assert srv[0].values == {"priority": 10, "weight": 5, "port": 5060, "target": "sip"}
    assert srv[1].values["target"] == "xmpp.example.net"
    caa = [r for r in zf.records if r.type == "CAA"]
    assert caa[0].values == {"flags": 0, "tag": "issue", "value": "letsencrypt.org"}
    assert caa[1].values["value"] == "mailto:security@example.com"


def test_defaults_apply_to_records():
    zf = zone(
        [
            {"name": "@", "type": "A", "content": "203.0.113.10"},
            {"name": "@", "type": "TXT", "content": "hello"},
            {"name": "x", "type": "A", "content": "203.0.113.11", "proxied": False, "ttl": "auto"},
        ],
        defaults={"ttl": 300, "proxied": True},
    )
    a, txt, x = zf.records
    assert (a.ttl, a.proxied) == (300, True)
    assert (txt.ttl, txt.proxied) == (300, False)  # TXT cannot be proxied, default ignored
    assert (x.ttl, x.proxied) == (AUTO_TTL, False)


def test_custom_tag_and_delete_unmanaged():
    zf = zone([], managed_tag="#iac", delete_unmanaged=True, zone_id="abc")
    assert (zf.managed_tag, zf.delete_unmanaged, zf.zone_id) == ("#iac", True, "abc")


@pytest.mark.parametrize(
    ("record", "message"),
    [
        ({"name": "@", "type": "A", "content": "999.1.1.1"}, "not a valid IPv4"),
        ({"name": "@", "type": "A", "content": "2001:db8::1"}, "not a valid IPv4"),
        ({"name": "@", "type": "AAAA", "content": "192.0.2.1"}, "not a valid IPv6"),
        ({"name": "@", "type": "MX", "content": "mx.example.net"}, "MX needs priority"),
        ({"name": "@", "type": "MX", "content": "mx.example.net", "priority": -1}, "priority must be"),
        ({"name": "@", "type": "TXT", "content": "x", "proxied": True}, "cannot be proxied"),
        ({"name": "@", "type": "TXT", "content": 12}, "must be a string"),
        ({"name": "@", "type": "TXT"}, "needs content"),
        ({"name": "@", "type": "A", "content": "192.0.2.1", "ttl": 5}, "ttl must be"),
        ({"name": "@", "type": "A", "content": "192.0.2.1", "ttl": "fast"}, "ttl must be"),
        ({"name": "@", "type": "NS", "content": "ns1.example.net"}, "type must be one of"),
        ({"type": "A", "content": "192.0.2.1"}, "name is required"),
        ({"name": "@", "type": "A", "content": "192.0.2.1", "contnet": "x"}, "unknown keys"),
        ({"name": "@", "type": "SRV", "priority": 1, "weight": 1, "port": 1}, "missing: target"),
        ({"name": "@", "type": "SRV", "content": "1 2 x host"}, "must be integers"),
        ({"name": "@", "type": "SRV", "content": "1 2 70000 host.example.com"}, "port must be"),
        ({"name": "@", "type": "CAA", "tag": "issues", "value": "x"}, "CAA tag must be"),
        ({"name": "@", "type": "CAA", "tag": "issue"}, "needs content or tag and value"),
        ({"name": "@", "type": "CNAME", "content": "not a host"}, "not a valid host"),
        ({"name": "@", "type": "A", "content": "192.0.2.1", "comment": "x" * 95}, "Cloudflare allows 100"),
        ({"name": "@", "type": "A", "content": "192.0.2.1", "comment": "a [dns-sync]"}, "must not contain"),
        ({"name": "@", "type": "A", "content": []}, "must not be empty"),
    ],
)
def test_record_validation_errors(record, message):
    errors = errors_of({"zone": "example.com", "records": [record]})
    assert any(message in e for e in errors), errors


def test_all_errors_are_reported_at_once():
    errors = errors_of(
        {
            "zone": "example.com",
            "bogus": 1,
            "records": [
                {"name": "a", "type": "A", "content": "x"},
                {"name": "b", "type": "AAAA", "content": "y"},
            ],
        }
    )
    assert len(errors) == 3
    assert errors[1].startswith("records[0] (a A)")


def test_duplicates_and_cname_conflicts():
    errors = errors_of(
        {
            "zone": "example.com",
            "records": [
                {"name": "a", "type": "A", "content": "192.0.2.1"},
                {"name": "A.example.com.", "type": "A", "content": "192.0.2.1"},
                {"name": "www", "type": "CNAME", "content": "a"},
                {"name": "www", "type": "TXT", "content": "x"},
                {"name": "c", "type": "CNAME", "content": "a"},
                {"name": "c", "type": "CNAME", "content": "b"},
            ],
        }
    )
    joined = " | ".join(errors)
    assert "duplicate record a.example.com A" in joined
    assert "www.example.com: a CNAME cannot coexist with other records (TXT)" in joined
    assert "c.example.com: only one CNAME" in joined


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (None, "must contain a mapping"),
        ({"records": []}, "'zone'"),
        ({"zone": "example.com", "records": {}}, "records must be a list"),
        ({"zone": "example.com", "records": ["x"]}, "must be a mapping"),
        ({"zone": "example.com", "managed_tag": ""}, "managed_tag"),
        ({"zone": "example.com", "delete_unmanaged": "yes"}, "delete_unmanaged"),
        ({"zone": "example.com", "defaults": {"ttl": 1, "colour": 1}}, "defaults: unknown keys"),
        ({"zone": "example.com", "defaults": {"proxied": "no"}}, "proxied must be"),
        ({"zone": "example.com", "zone_id": 5}, "zone_id"),
    ],
)
def test_top_level_errors(data, message):
    assert any(message in e for e in errors_of(data))


def test_load_zone_file_errors(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("zone: [unclosed", encoding="utf-8")
    with pytest.raises(ZoneFileError, match="invalid YAML"):
        load_zone_file(bad)
    with pytest.raises(ZoneFileError, match="cannot read file"):
        load_zone_file(tmp_path / "missing.yaml")


def test_load_directory_and_duplicate_zones(tmp_path):
    (tmp_path / "a.yaml").write_text("zone: a.test\nrecords: []\n", encoding="utf-8")
    (tmp_path / "b.yml").write_text("zone: b.test\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    assert [z.zone for z in load_zone_files([str(tmp_path)])] == ["a.test", "b.test"]
    (tmp_path / "c.yaml").write_text("zone: a.test\n", encoding="utf-8")
    with pytest.raises(ZoneFileError, match=r"more than one file: a\.test"):
        load_zone_files([str(tmp_path)])
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ZoneFileError, match="no zone files"):
        load_zone_files([str(empty)])


def test_example_zone_files_are_valid():
    files = sorted(EXAMPLES.glob("*.yaml"))
    assert files
    for path in files:
        zf = load_zone_file(path)
        assert zf.records
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["zone"] == zf.zone
