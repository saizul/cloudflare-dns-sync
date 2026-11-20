from cloudflare_dns_sync.plan import build_plan
from cloudflare_dns_sync.records import Record
from cloudflare_dns_sync.zonefile import parse_zone

TAG = "[dns-sync]"
_ids = iter(range(1, 10_000))


def live(name, rtype, content=None, *, managed=True, comment="", ttl=1, proxied=False, **extra):
    item = {"id": f"r{next(_ids)}", "name": name, "type": rtype, "ttl": ttl, "proxied": proxied}
    if content is not None:
        item["content"] = content
    item.update(extra)
    item["comment"] = (f"{comment} {TAG}".strip() if managed else comment) or None
    return Record.from_api(item, TAG)


def plan_for(records, live_records, **extra):
    zf = parse_zone({"zone": "example.com", "records": records, **extra})
    return build_plan(zf, live_records, "zone1")


def actions(plan):
    return [(c.action, c.record.type, c.record.name, c.record.content) for c in plan.changes]


A = {"name": "@", "type": "A", "content": "192.0.2.1"}


def test_empty_zone_creates_everything():
    plan = plan_for([A, {"name": "www", "type": "CNAME", "content": "@"}], [])
    assert actions(plan) == [
        ("create", "A", "example.com", "192.0.2.1"),
        ("create", "CNAME", "www.example.com", "example.com"),
    ]
    assert plan.count("create") == 2 and plan.has_changes


def test_in_sync_has_no_changes():
    plan = plan_for([A], [live("example.com", "A", "192.0.2.1")])
    assert plan.changes == [] and plan.in_sync == 1


def test_content_change_updates_managed_record_in_place():
    old = live("example.com", "A", "192.0.2.9")
    plan = plan_for([A], [old])
    assert actions(plan) == [("update", "A", "example.com", "192.0.2.1")]
    change = plan.changes[0]
    assert change.fields == ["content"]
    assert change.after.id == old.id


def test_attribute_changes_are_listed():
    record = {**A, "ttl": 300, "comment": "web"}
    plan = plan_for([record], [live("example.com", "A", "192.0.2.1", ttl=3600)])
    assert plan.changes[0].fields == ["ttl", "comment"]
    plan = plan_for([{**A, "proxied": True}], [live("example.com", "A", "192.0.2.1", ttl=300)])
    assert plan.changes[0].fields == ["proxied"]  # ttl is ignored while proxied


def test_ttl_ignored_for_proxied_records():
    plan = plan_for([{**A, "proxied": True, "ttl": 300}], [live("example.com", "A", "192.0.2.1", proxied=True)])
    assert plan.changes == []


def test_unmanaged_records_are_never_deleted_by_default():
    plan = plan_for(
        [A], [live("example.com", "A", "192.0.2.1"), live("old.example.com", "A", "192.0.2.5", managed=False)]
    )
    assert plan.changes == []
    assert [r.name for r in plan.unmanaged] == ["old.example.com"]


def test_managed_records_missing_from_file_are_deleted():
    plan = plan_for([A], [live("example.com", "A", "192.0.2.1"), live("old.example.com", "A", "192.0.2.5")])
    assert actions(plan) == [("delete", "A", "old.example.com", "192.0.2.5")]


def test_delete_unmanaged_true_deletes_everything_else():
    plan = plan_for([], [live("old.example.com", "TXT", "x", managed=False)], delete_unmanaged=True)
    assert actions(plan) == [("delete", "TXT", "old.example.com", "x")]


def test_exact_match_adopts_unmanaged_record_by_tagging_it():
    plan = plan_for([A], [live("example.com", "A", "192.0.2.1", managed=False, comment="hand made")])
    assert actions(plan) == [("update", "A", "example.com", "192.0.2.1")]
    assert plan.changes[0].fields == ["comment"]


def test_unmanaged_record_with_other_content_is_not_overwritten():
    plan = plan_for([A], [live("example.com", "A", "192.0.2.50", managed=False)])
    assert actions(plan) == [("create", "A", "example.com", "192.0.2.1")]
    assert len(plan.unmanaged) == 1


def test_unmanaged_record_is_overwritten_with_delete_unmanaged():
    plan = plan_for([A], [live("example.com", "A", "192.0.2.50", managed=False)], delete_unmanaged=True)
    assert actions(plan) == [("update", "A", "example.com", "192.0.2.1")]
    assert plan.changes[0].fields == ["content", "comment"]


def test_round_robin_set_changes_minimally():
    records = [{"name": "@", "type": "A", "content": ["192.0.2.1", "192.0.2.2", "192.0.2.3"]}]
    existing = [live("example.com", "A", ip) for ip in ("192.0.2.2", "192.0.2.3", "192.0.2.4", "192.0.2.5")]
    plan = plan_for(records, existing)
    assert actions(plan) == [
        ("delete", "A", "example.com", "192.0.2.5"),
        ("update", "A", "example.com", "192.0.2.1"),
    ]
    assert plan.in_sync == 2


def test_mx_priority_change_is_an_update():
    plan = plan_for(
        [{"name": "@", "type": "MX", "content": "mx.example.net", "priority": 20}],
        [live("example.com", "MX", "MX.example.net.", priority=10)],
    )
    assert actions(plan) == [("update", "MX", "example.com", "20 mx.example.net")]
    assert plan.changes[0].fields == ["priority"]


def test_srv_and_caa_identity_and_updates():
    plan = plan_for(
        [
            {"name": "_sip._tcp", "type": "SRV", "content": "10 5 5060 sip.example.com"},
            {"name": "@", "type": "CAA", "tag": "issue", "value": "letsencrypt.org"},
        ],
        [
            live(
                "_sip._tcp.example.com",
                "SRV",
                data={"priority": 10, "weight": 1, "port": 5060, "target": "sip.example.com"},
            ),
            live("example.com", "CAA", data={"flags": 0, "tag": "issue", "value": "letsencrypt.org"}),
        ],
    )
    assert [(c.action, c.record.type, c.fields) for c in plan.changes] == [("update", "SRV", ["weight"])]


def test_txt_quotes_from_api_are_ignored():
    plan = plan_for(
        [{"name": "@", "type": "TXT", "content": "v=spf1 include:_spf.example.net -all"}],
        [live("example.com", "TXT", '"v=spf1 include:_spf.example.net -all"')],
    )
    assert plan.changes == []


def test_ipv6_is_compared_canonically():
    plan = plan_for(
        [{"name": "@", "type": "AAAA", "content": "2001:0db8:0000::0001"}], [live("example.com", "AAAA", "2001:db8::1")]
    )
    assert plan.changes == []


def test_unsupported_types_are_ignored_even_with_delete_unmanaged():
    plan = plan_for([], [live("example.com", "NS", "ns1.example.net", managed=False)], delete_unmanaged=True)
    assert plan.changes == [] and [r.type for r in plan.unsupported] == ["NS"]


def test_type_change_deletes_before_it_creates():
    plan = plan_for([{"name": "www", "type": "CNAME", "content": "@"}], [live("www.example.com", "A", "192.0.2.1")])
    assert [c.action for c in plan.changes] == ["delete", "create"]


def test_cname_conflict_with_unmanaged_record_is_warned():
    plan = plan_for(
        [{"name": "www", "type": "CNAME", "content": "@"}], [live("www.example.com", "A", "192.0.2.1", managed=False)]
    )
    assert len(plan.warnings) == 1 and "conflicts with an unmanaged record (A)" in plan.warnings[0]


def test_custom_tag_controls_ownership():
    item = {"id": "1", "name": "x.example.com", "type": "A", "content": "192.0.2.7", "comment": f"ours {TAG}"}
    other = Record.from_api(item, "#mine")  # tagged with a different tag: not ours
    assert other.managed is False and other.comment == f"ours {TAG}"
    plan = plan_for([], [other], managed_tag="#mine")
    assert plan.changes == [] and len(plan.unmanaged) == 1
    mine = Record.from_api({**item, "comment": "ours #mine"}, "#mine")
    plan = plan_for([], [mine], managed_tag="#mine")
    assert actions(plan) == [("delete", "A", "x.example.com", "192.0.2.7")]


def test_duplicate_live_records_are_cleaned_up():
    plan = plan_for([A], [live("example.com", "A", "192.0.2.1"), live("example.com", "A", "192.0.2.1")])
    assert actions(plan) == [("delete", "A", "example.com", "192.0.2.1")]
    assert plan.in_sync == 1
