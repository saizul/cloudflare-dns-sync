"""A small in-memory fake of the Cloudflare API v4 DNS endpoints, served through httpx.MockTransport."""

from __future__ import annotations

import io
import itertools
import json
import re
from typing import Any

import httpx
import pytest

from cloudflare_dns_sync.cli import App, main

TOKEN = "test-token"
API = "https://api.cloudflare.test/client/v4"


class FakeCloudflare:
    def __init__(self, page_size: int = 100):
        self.zones: dict[str, dict[str, Any]] = {}
        self.records: dict[str, list[dict[str, Any]]] = {}
        self.requests: list[httpx.Request] = []
        self.fail: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
        self.flaky: list[int] = []  # status codes to return before succeeding
        self.page_size = page_size
        self._ids = itertools.count(1)

    # -- setup helpers ---------------------------------------------------

    def add_zone(self, name: str, zone_id: str | None = None) -> str:
        zone_id = zone_id or f"zone{len(self.zones) + 1:04d}"
        self.zones[zone_id] = {"id": zone_id, "name": name, "status": "active"}
        self.records[zone_id] = []
        return zone_id

    def add_record(self, zone_id: str, **fields: Any) -> dict[str, Any]:
        record = self._normalize({"id": f"rec{next(self._ids):04d}", **fields})
        self.records[zone_id].append(record)
        return record

    def _normalize(self, rec: dict[str, Any]) -> dict[str, Any]:
        rec = dict(rec)
        rec.setdefault("ttl", 1)
        rec.setdefault("comment", None)
        rec.setdefault("tags", [])
        rec["proxiable"] = rec["type"] in ("A", "AAAA", "CNAME")
        rec.setdefault("proxied", False)
        data = rec.get("data")
        if rec["type"] == "SRV" and data:
            rec["content"] = f"{data['weight']} {data['port']} {data['target']}"
            rec["priority"] = data["priority"]
        if rec["type"] == "CAA" and data:
            rec["content"] = f'{data["flags"]} {data["tag"]} "{data["value"]}"'
        return rec

    def methods(self) -> list[str]:
        return [f"{r.method} {r.url.path.removeprefix('/client/v4')}" for r in self.requests]

    def writes(self) -> list[str]:
        return [m for m in self.methods() if not m.startswith("GET")]

    # -- transport -------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @staticmethod
    def _ok(result: Any, **extra: Any) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "errors": [], "messages": [], "result": result, **extra})

    @staticmethod
    def _error(status: int, code: int, message: str) -> httpx.Response:
        return httpx.Response(status, json={"success": False, "errors": [{"code": code, "message": message}]})

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return self._error(403, 10000, "Authentication error")
        if self.flaky:
            return httpx.Response(self.flaky.pop(0), headers={"Retry-After": "1"}, json={"success": False})
        path = request.url.path.removeprefix("/client/v4")
        key = (request.method, path)
        if key in self.fail:
            status, body = self.fail[key]
            return httpx.Response(status, json=body)

        if request.method == "GET" and path == "/zones":
            name = request.url.params.get("name")
            zones = [z for z in self.zones.values() if name is None or z["name"] == name]
            return self._ok(zones)

        m = re.fullmatch(r"/zones/([^/]+)/dns_records(?:/([^/]+))?", path)
        if not m or m.group(1) not in self.records:
            return self._error(404, 7003, "Could not route to " + path)
        zone_id, record_id = m.group(1), m.group(2)
        records = self.records[zone_id]
        zone_name = self.zones[zone_id]["name"]

        if request.method == "GET" and record_id is None:
            page = int(request.url.params.get("page", 1))
            per_page = min(int(request.url.params.get("per_page", 100)), self.page_size)
            start = (page - 1) * per_page
            total_pages = max(1, -(-len(records) // per_page))
            return self._ok(
                records[start : start + per_page],
                result_info={"page": page, "per_page": per_page, "total_pages": total_pages, "count": len(records)},
            )
        if request.method == "POST" and record_id is None:
            body = json.loads(request.content)
            if body["name"] != zone_name and not body["name"].endswith("." + zone_name):
                return self._error(400, 9005, "Record name is outside the zone")
            if body["type"] == "CNAME" and any(r["name"] == body["name"] for r in records):
                return self._error(400, 81053, "An A, AAAA, or CNAME record with that host already exists.")
            rec = self._normalize({"id": f"rec{next(self._ids):04d}", **body})
            records.append(rec)
            return self._ok(rec)
        index = next((i for i, r in enumerate(records) if r["id"] == record_id), None)
        if index is None:
            return self._error(404, 81044, "Record does not exist.")
        if request.method == "PUT":
            body = json.loads(request.content)
            records[index] = self._normalize({"id": record_id, **body})
            return self._ok(records[index])
        if request.method == "DELETE":
            records.pop(index)
            return self._ok({"id": record_id})
        return self._error(405, 10405, "Method not allowed")


@pytest.fixture
def cf() -> FakeCloudflare:
    return FakeCloudflare()


class Run:
    def __init__(self, cf: FakeCloudflare, tmp_path):
        self.cf = cf
        self.tmp_path = tmp_path
        self.env = {"CLOUDFLARE_API_TOKEN": TOKEN}

    def write_zone(self, text: str, name: str = "example.com.yaml"):
        path = self.tmp_path / name
        path.write_text(text, encoding="utf-8")
        return path

    def __call__(self, *argv: str, stdin: str = "", tty: bool = False, env: dict[str, str] | None = None):
        out, err = io.StringIO(), io.StringIO()
        inp = io.StringIO(stdin)
        if tty:
            inp.isatty = lambda: True  # type: ignore[method-assign]
        app = App(
            stdout=out,
            stderr=err,
            stdin=inp,
            environ={**self.env, **(env or {})},
            transport=self.cf.transport(),
            sleep=lambda _s: None,
        )
        args = list(argv)
        if args and args[0] in ("plan", "apply", "export"):
            args[1:1] = ["--api-url", API]
        code = main(args, app)
        return code, out.getvalue(), err.getvalue()


@pytest.fixture
def run(cf, tmp_path) -> Run:
    return Run(cf, tmp_path)
