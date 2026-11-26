import json
from pathlib import Path

import pytest
import yaml

from cloudflare_dns_sync import __version__
from cloudflare_dns_sync.cli import in_ci, main

ZONE = """\
zone: example.com
records:
  - name: "@"
    type: A
    content: 192.0.2.1
    proxied: true
    comment: web
  - name: www
    type: CNAME
    content: "@"
    proxied: true
  - name: "@"
    type: MX
    content: mx.example.net
    priority: 10
  - name: "@"
    type: TXT
    content: "v=spf1 mx -all"
"""

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "zones" / "example.com.yaml"


@pytest.fixture
def zone_id(cf):
    return cf.add_zone("example.com")


def test_version(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_plan_on_empty_zone(run, zone_id):
    path = run.write_zone(ZONE)
    code, out, err = run("plan", str(path), "--color", "never")
    assert code == 0, err
    assert "example.com (zone zone0001)" in out
    assert "+ create A" in out
    assert "Plan: 4 to create, 0 to update, 0 to delete." in out
    assert run.cf.writes() == []  # plan never writes


def test_plan_detailed_exitcode_and_color(run, zone_id):
    path = run.write_zone(ZONE)
    code, out, _ = run("plan", str(path), "--detailed-exitcode", "--color", "always")
    assert code == 2
    assert "\033[32m" in out


def test_no_color_env_wins(run, zone_id):
    path = run.write_zone(ZONE)
    _, out, _ = run("plan", str(path), env={"NO_COLOR": "1"})
    assert "\033[" not in out


def test_apply_with_yes_then_plan_is_clean(run, zone_id):
    path = run.write_zone(ZONE)
    code, out, err = run("apply", str(path), "--yes")
    assert code == 0, err
    assert "example.com: applied 4 of 4 changes" in out
    stored = run.cf.records[zone_id]
    assert {r["type"] for r in stored} == {"A", "CNAME", "MX", "TXT"}
    assert all(r["comment"].endswith("[dns-sync]") for r in stored)
    assert next(r for r in stored if r["type"] == "A")["comment"] == "web [dns-sync]"
    code, out, _ = run("plan", str(path), "--detailed-exitcode")
    assert code == 0
    assert "No changes" in out


def test_apply_in_ci_without_yes_is_a_dry_run(run, zone_id):
    path = run.write_zone(ZONE)
    code, out, _ = run("apply", str(path), env={"CI": "true"}, tty=True)
    assert code == 0
    assert "Dry run (non-interactive or CI)" in out
    assert run.cf.writes() == []


def test_apply_non_interactive_without_yes_is_a_dry_run(run, zone_id):
    path = run.write_zone(ZONE)
    code, out, _ = run("apply", str(path))
    assert code == 0 and "re-run with --yes" in out and run.cf.writes() == []


def test_apply_dry_run_flag(run, zone_id):
    path = run.write_zone(ZONE)
    code, out, _ = run("apply", str(path), "--dry-run", "--yes")
    assert code == 0 and "Dry run: nothing applied." in out and run.cf.writes() == []


def test_apply_interactive_confirmation(run, zone_id):
    path = run.write_zone(ZONE)
    code, out, _ = run("apply", str(path), stdin="no\n", tty=True)
    assert code == 0 and "Cancelled" in out and run.cf.writes() == []
    code, out, _ = run("apply", str(path), stdin="yes\n", tty=True)
    assert code == 0 and "applied 4 of 4" in out
    assert len(run.cf.writes()) == 4


def test_apply_protects_unmanaged_records(run, cf, zone_id):
    cf.add_record(zone_id, name="legacy.example.com", type="A", content="198.51.100.7", comment="added by hand")
    cf.add_record(zone_id, name="old.example.com", type="A", content="198.51.100.8", comment="[dns-sync]")
    path = run.write_zone(ZONE)
    code, out, _ = run("apply", str(path), "--yes", "--show-unmanaged")
    assert code == 0
    names = {r["name"] for r in cf.records[zone_id]}
    assert "legacy.example.com" in names  # untouched
    assert "old.example.com" not in names  # managed and removed from the file
    assert "1 unmanaged records left untouched:" in out
    assert cf.writes()[0].startswith("DELETE")  # deletes run first


def test_max_deletes_guard(run, cf, zone_id):
    for i in range(3):
        cf.add_record(zone_id, name=f"old{i}.example.com", type="A", content=f"198.51.100.{i}", comment="[dns-sync]")
    path = run.write_zone(ZONE)
    code, _, err = run("apply", str(path), "--yes", "--max-deletes", "2")
    assert code == 1 and "more than --max-deletes 2" in err
    assert cf.writes() == []


def test_apply_stops_at_first_error(run, cf, zone_id):
    cf.add_record(zone_id, name="www.example.com", type="A", content="198.51.100.1")  # unmanaged, blocks the CNAME
    path = run.write_zone(ZONE)
    code, out, err = run("apply", str(path), "--yes")
    assert code == 1
    assert "conflicts with an unmanaged record (A)" in out
    assert "create CNAME www.example.com failed" in err and "81053" in err


def test_plan_json_output(run, cf, zone_id):
    cf.add_record(zone_id, name="example.com", type="A", content="192.0.2.99", comment="[dns-sync]", proxied=True)
    cf.add_record(zone_id, name="keep.example.com", type="TXT", content="hello")
    path = run.write_zone(ZONE)
    code, out, _ = run("plan", str(path), "--format", "json")
    assert code == 0
    data = json.loads(out)
    assert data["format_version"] == 1 and data["has_changes"] is True
    zone = data["zones"][0]
    assert zone["summary"] == {"create": 3, "update": 1, "delete": 0, "in_sync": 0, "unmanaged": 1, "unsupported": 0}
    update = next(c for c in zone["changes"] if c["action"] == "update")
    assert update["fields"] == ["content", "comment"]
    assert update["before"]["content"] == "192.0.2.99" and update["after"]["content"] == "192.0.2.1"
    assert zone["unmanaged"][0]["name"] == "keep.example.com"


def test_plan_markdown_and_out_file(run, zone_id, tmp_path):
    path = run.write_zone(ZONE)
    md = tmp_path / "plan.md"
    js = tmp_path / "plan.json"
    code, out, _ = run("plan", str(path), "--format", "markdown", "--out", str(md))
    assert code == 0
    assert out.startswith("<!-- cloudflare-dns-sync-plan -->")
    assert "| `+` create | A | `example.com` |" in out
    assert md.read_text(encoding="utf-8") == out
    run("plan", str(path), "--out", str(js))
    assert json.loads(js.read_text(encoding="utf-8"))["zones"][0]["zone"] == "example.com"


def test_plan_text_shows_field_changes(run, cf, zone_id):
    cf.add_record(zone_id, name="example.com", type="MX", content="mx.example.net", priority=20, comment="[dns-sync]")
    path = run.write_zone(ZONE)
    _, out, _ = run("plan", str(path), "--color", "never")
    assert "~ update MX" in out
    assert "priority: 20 -> 10" in out


def test_missing_token(run, zone_id):
    path = run.write_zone(ZONE)
    code, _, err = run("plan", str(path), env={"CLOUDFLARE_API_TOKEN": ""})
    assert code == 1 and "set CLOUDFLARE_API_TOKEN" in err


def test_custom_token_env(run, zone_id):
    path = run.write_zone(ZONE)
    code, _, err = run("plan", str(path), "--token-env", "CF_TOKEN", env={"CF_TOKEN": "test-token"})
    assert code == 0, err


def test_zone_id_in_file_skips_lookup(run, cf, zone_id):
    path = run.write_zone(ZONE.replace("zone: example.com", f"zone: example.com\nzone_id: {zone_id}"))
    run("plan", str(path))
    assert "GET /zones" not in cf.methods()


def test_unknown_zone_error(run):
    path = run.write_zone(ZONE)
    code, _, err = run("plan", str(path))
    assert code == 1 and "zone example.com not found" in err


def test_invalid_zone_file_lists_errors(run):
    path = run.write_zone("zone: example.com\nrecords:\n  - {name: a, type: A, content: nope}\n")
    code, _, err = run("validate", str(path))
    assert code == 1
    assert "records[0] (a A): 'nope' is not a valid IPv4 address" in err


def test_validate_ok(run):
    path = run.write_zone(ZONE)
    code, out, _ = run("validate", str(path))
    assert code == 0 and "example.com OK (4 records)" in out


def test_export_and_round_trip(run, cf, zone_id, tmp_path):
    cf.add_record(zone_id, name="example.com", type="A", content="192.0.2.1", proxied=True, comment="web")
    cf.add_record(zone_id, name="www.example.com", type="CNAME", content="example.com", proxied=True)
    cf.add_record(zone_id, name="example.com", type="MX", content="mx.example.net", priority=10, ttl=3600)
    cf.add_record(zone_id, name="example.com", type="TXT", content='"v=spf1 mx -all"', ttl=3600)
    cf.add_record(
        zone_id,
        name="_sip._tcp.example.com",
        type="SRV",
        data={"priority": 1, "weight": 2, "port": 5060, "target": "example.com"},
        ttl=3600,
    )
    cf.add_record(
        zone_id, name="example.com", type="CAA", data={"flags": 0, "tag": "issue", "value": "letsencrypt.org"}, ttl=300
    )
    cf.add_record(zone_id, name="example.com", type="NS", content="ns1.example.net")
    out_file = tmp_path / "zones" / "example.com.yaml"
    out_file.parent.mkdir()
    code, _, err = run("export", "example.com", "-o", str(out_file))
    assert code == 0, err
    text = out_file.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    assert doc["defaults"]["ttl"] == 3600
    assert {"name": "www", "type": "CNAME", "content": "@", "proxied": True} in doc["records"]
    assert {"name": "@", "type": "CAA", "flags": 0, "tag": "issue", "value": "letsencrypt.org", "ttl": 300} in doc[
        "records"
    ]
    assert "#   NS example.com ns1.example.net" in text
    # Adopting the export: every record only needs the managed tag added, nothing else.
    code, out, _ = run("plan", str(out_file.parent), "--format", "json")
    changes = json.loads(out)["zones"][0]["changes"]
    assert len(changes) == 6
    assert all(c["action"] == "update" and c["fields"] == ["comment"] for c in changes)
    run("apply", str(out_file.parent), "--yes")
    code, out, _ = run("plan", str(out_file.parent), "--detailed-exitcode")
    assert code == 0, out


def test_export_to_stdout_managed_only(run, cf, zone_id):
    cf.add_record(zone_id, name="a.example.com", type="A", content="192.0.2.1", comment="[dns-sync]")
    cf.add_record(zone_id, name="b.example.com", type="A", content="192.0.2.2")
    code, out, _ = run("export", "example.com", "--managed-only")
    assert code == 0
    names = [r["name"] for r in yaml.safe_load(out)["records"]]
    assert names == ["a"]


def test_example_zone_plans_against_empty_zone(run, cf):
    cf.add_zone("example.com")
    code, out, err = run("plan", str(EXAMPLE), "--format", "json")
    assert code == 0, err
    assert json.loads(out)["zones"][0]["summary"]["create"] > 5


def test_in_ci():
    assert in_ci({"CI": "true"}) and in_ci({"GITHUB_ACTIONS": "true"}) and in_ci({"CI": "1"})
    assert not in_ci({}) and not in_ci({"CI": "false"})


def test_validate_warnings_and_strict(run):
    path = run.write_zone(ZONE + '  - name: "@"\n    type: TXT\n    content: "v=spf1 include:_spf.example.net -all"\n')
    code, out, err = run("validate", str(path))
    assert code == 0
    assert "example.com OK (5 records, 1 warning(s))" in out
    assert "warning:" in err and "2 SPF records" in err
    code, _, err = run("validate", str(path), "--strict")
    assert code == 1 and "2 SPF records" in err
    code, out, _ = run("validate", str(run.write_zone(ZONE)), "--strict")
    assert code == 0 and "warning(s)" not in out


def test_plan_shows_zone_file_warnings(run, zone_id):
    path = run.write_zone(ZONE + '  - name: "@"\n    type: TXT\n    content: "v=spf1 a -all"\n')
    code, out, _ = run("plan", str(path), "--color", "never")
    assert code == 0
    assert "! example.com: 2 SPF records" in out
