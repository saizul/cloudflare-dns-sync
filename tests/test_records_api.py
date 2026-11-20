import httpx
import pytest

from cloudflare_dns_sync.api import CloudflareClient, CloudflareError
from cloudflare_dns_sync.records import Record, normalize_txt, render_comment, strip_tag

from .conftest import API, TOKEN, FakeCloudflare

TAG = "[dns-sync]"


def client(cf: FakeCloudflare, token: str = TOKEN, sleeps: list | None = None) -> CloudflareClient:
    sleeps = sleeps if sleeps is not None else []
    return CloudflareClient(token, base_url=API, transport=cf.transport(), sleep=sleeps.append)


def test_comment_tag_helpers():
    assert render_comment("", TAG) == TAG
    assert render_comment(" web ", TAG) == "web [dns-sync]"
    assert strip_tag("web [dns-sync]", TAG) == "web"
    assert strip_tag("[dns-sync]", TAG) == ""
    assert strip_tag("  a  b ", "") == "a  b"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [('"abc"', "abc"), ("abc", "abc"), ('"a" "b"', '"a" "b"'), ('"say \\"hi\\""', 'say "hi"'), ('"', '"')],
)
def test_normalize_txt(raw, expected):
    assert normalize_txt(raw) == expected


def test_to_api_payloads():
    a = Record("www.example.com", "A", {"content": "192.0.2.1"}, ttl=300, proxied=True, comment="web")
    assert a.to_api(TAG) == {
        "type": "A",
        "name": "www.example.com",
        "ttl": 1,
        "comment": "web [dns-sync]",
        "proxied": True,
        "content": "192.0.2.1",
    }
    mx = Record("example.com", "MX", {"content": "mx.example.net", "priority": 10}, ttl=3600)
    assert mx.to_api(TAG)["priority"] == 10 and "proxied" not in mx.to_api(TAG)
    srv = Record("_s._tcp.example.com", "SRV", {"priority": 1, "weight": 2, "port": 3, "target": "t.example.com"})
    assert srv.to_api(TAG)["data"] == {"priority": 1, "weight": 2, "port": 3, "target": "t.example.com"}
    assert "content" not in srv.to_api(TAG)
    caa = Record("example.com", "CAA", {"flags": 0, "tag": "issue", "value": "letsencrypt.org"})
    assert caa.content == '0 issue "letsencrypt.org"'


def test_from_api_handles_old_and_new_shapes():
    srv_old = Record.from_api(
        {"id": "1", "name": "_s._tcp.Example.com", "type": "SRV", "content": "2 3 t.example.com", "priority": 1}, TAG
    )
    assert srv_old.values == {"priority": 1, "weight": 2, "port": 3, "target": "t.example.com"}
    assert srv_old.name == "_s._tcp.example.com"
    caa_old = Record.from_api({"id": "2", "name": "example.com", "type": "CAA", "content": '0 issue "pki.goog"'}, TAG)
    assert caa_old.values == {"flags": 0, "tag": "issue", "value": "pki.goog"}
    tagged = Record.from_api(
        {"id": "3", "name": "a.example.com", "type": "A", "content": "192.0.2.1", "tags": [TAG]}, TAG
    )
    assert tagged.managed is True
    untagged = Record.from_api({"id": "4", "name": "a.example.com", "type": "A", "content": "192.0.2.1"}, TAG)
    assert untagged.managed is False and untagged.comment == ""


def test_zone_lookup_and_pagination():
    cf = FakeCloudflare(page_size=2)
    zid = cf.add_zone("example.com")
    cf.add_zone("other.test")
    for i in range(5):
        cf.add_record(zid, name=f"h{i}.example.com", type="A", content=f"192.0.2.{i}")
    with client(cf) as c:
        assert c.zone_id("example.com") == zid
        records = c.list_records(zid)
    assert [r["name"] for r in records] == [f"h{i}.example.com" for i in range(5)]
    assert cf.methods().count(f"GET /zones/{zid}/dns_records") == 3


def test_unknown_zone_is_a_clear_error():
    cf = FakeCloudflare()
    with client(cf) as c, pytest.raises(CloudflareError, match=r"zone missing\.test not found"):
        c.zone_id("missing.test")


def test_auth_errors_include_cloudflare_message():
    cf = FakeCloudflare()
    with client(cf, token="wrong") as c, pytest.raises(CloudflareError) as info:
        c.zone_id("example.com")
    assert info.value.status == 403
    assert "10000: Authentication error" in str(info.value)


def test_rate_limits_and_server_errors_are_retried():
    cf = FakeCloudflare()
    cf.add_zone("example.com", "z1")
    cf.flaky = [429, 502]
    sleeps: list[float] = []
    with client(cf, sleeps=sleeps) as c:
        assert c.zone_id("example.com") == "z1"
    assert sleeps == [1.0, 1.0]  # Retry-After honoured


def test_retries_give_up_eventually():
    cf = FakeCloudflare()
    cf.flaky = [503] * 10
    with client(cf) as c, pytest.raises(CloudflareError, match="HTTP 503"):
        c.zone_id("example.com")


def test_transport_errors_are_retried_then_reported():
    calls = []

    def boom(request):
        calls.append(request)
        raise httpx.ConnectError("no route", request=request)

    c = CloudflareClient(TOKEN, base_url=API, transport=httpx.MockTransport(boom), sleep=lambda _s: None, max_retries=2)
    with pytest.raises(CloudflareError, match="ConnectError"):
        c.zone_id("example.com")
    assert len(calls) == 3


def test_success_false_and_non_json_are_errors():
    def handler(request):
        if request.url.path.endswith("/zones"):
            return httpx.Response(200, json={"success": False, "errors": [{"code": 1, "message": "nope"}]})
        return httpx.Response(200, text="<html>")

    c = CloudflareClient(TOKEN, base_url=API, transport=httpx.MockTransport(handler))
    with pytest.raises(CloudflareError, match="1: nope"):
        c.zone_id("example.com")
    with pytest.raises(CloudflareError, match="not JSON"):
        c.list_records("z")


def test_crud_round_trip():
    cf = FakeCloudflare()
    zid = cf.add_zone("example.com")
    with client(cf) as c:
        created = c.create_record(zid, {"type": "A", "name": "a.example.com", "content": "192.0.2.1", "ttl": 1})
        c.update_record(zid, created["id"], {"type": "A", "name": "a.example.com", "content": "192.0.2.2", "ttl": 1})
        assert c.list_records(zid)[0]["content"] == "192.0.2.2"
        c.delete_record(zid, created["id"])
        assert c.list_records(zid) == []
        with pytest.raises(CloudflareError, match="81044"):
            c.delete_record(zid, created["id"])
    sent = cf.requests[0]
    assert sent.headers["authorization"] == f"Bearer {TOKEN}"
    assert sent.headers["user-agent"].startswith("cloudflare-dns-sync/")
