# Contributing to cloudflare-dns-sync

Thank you for helping! New record types, better diffs, documentation and bug reports are
all welcome.

- [Ways to contribute](#ways-to-contribute)
- [Good first issues](#good-first-issues)
- [Development setup](#development-setup)
- [Project layout](#project-layout)
- [Add a record type](#add-a-record-type)
- [Guidelines](#guidelines)
- [Pull request process](#pull-request-process)

## Ways to contribute

| Size | Idea |
| --- | --- |
| 10 minutes | Fix a typo, clarify an option, add a FAQ entry |
| 30 minutes | Improve an error message or add a missing test case |
| 1-2 hours | Add a record type (see below) or one of the good first issues |
| Larger | Saved plans, the batch endpoint, BIND import (open an issue first) |

Comment on an issue before you start so nobody else picks the same one.

## Good first issues

Each of these is self-contained and touches one to three files.

1. **NS records for sub-delegations.** Allow `NS` for names below the apex only (the apex NS
   set belongs to Cloudflare). Identity is the target host. (`records.py`, `zonefile.py`, tests)
2. **PTR records.** For reverse zones (`2.0.192.in-addr.arpa`): content is a host name.
   (`records.py`, tests)
3. **`--zone NAME` filter.** `plan zones/ --zone example.com` only plans the given zones
   (repeatable). (`cli.py`, `tests/test_cli.py`)
4. **Environment interpolation.** Replace `${VAR}` in `content` with the environment
   variable's value, error when unset; `validate --env-file` to check. (`zonefile.py`, tests)
5. **`plan --summary`.** One line per zone (`example.com: +2 ~1 -0`) for chat notifications.
   (`render.py`, `cli.py`, tests)
6. **TLSA records.** `usage`, `selector`, `matching_type`, `certificate` via `data`, like CAA.
   (`records.py`, `zonefile.py`, tests)
7. **Warn on low TTLs for MX.** `validate` prints a warning (not an error) for MX TTL < 300.
   (`zonefile.py`, `cli.py`, tests)
8. **Sort `export` output by type groups** (web, mail, services) with a blank line between
   groups for readability. (`export.py`, tests)

## Development setup

```bash
git clone https://github.com/saizul/cloudflare-dns-sync
cd cloudflare-dns-sync
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e .[dev]
pytest
ruff check . && ruff format --check .
```

Python 3.11+ is needed. **Tests never use the network**: `tests/conftest.py` contains
`FakeCloudflare`, an in-memory implementation of the zone and DNS record endpoints served
through `httpx.MockTransport`, and a `run` fixture that drives the CLI against it:

```python
def test_something(run, cf):
    zone_id = cf.add_zone("example.com")
    cf.add_record(zone_id, name="a.example.com", type="A", content="192.0.2.1", comment="[dns-sync]")
    path = run.write_zone("zone: example.com\nrecords: []\n")
    code, out, err = run("plan", str(path), "--format", "json")
```

Use documentation IP ranges (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`,
`2001:db8::/32`) and `example.com` / `.test` names in tests and docs. Never use real tokens;
the fake accepts `test-token`.

## Project layout

| Path | Responsibility |
| --- | --- |
| `src/cloudflare_dns_sync/records.py` | `Record`, per-type value keys, identity, normalization, API payloads |
| `src/cloudflare_dns_sync/zonefile.py` | YAML loading and validation, name qualification |
| `src/cloudflare_dns_sync/api.py` | `CloudflareClient`: zones, paginated records, CRUD, retries |
| `src/cloudflare_dns_sync/plan.py` | `build_plan` (the diff and ownership rules), `apply_plan` |
| `src/cloudflare_dns_sync/render.py` | Text, Markdown and JSON output |
| `src/cloudflare_dns_sync/export.py` | Live zone to zone file |
| `src/cloudflare_dns_sync/cli.py` | `plan`, `apply`, `export`, `validate` |
| `tests/` | pytest suite with the fake API in `conftest.py` |

## Add a record type

1. Add the type to `SUPPORTED_TYPES` in `records.py` and list its fields in `VALUE_KEYS`
   (display order) and `IDENTITY_KEYS` (what makes two records "the same record", the rest
   is updated in place).
2. If Cloudflare uses a `data` object for it (like SRV and CAA), handle it in
   `Record.to_api`, `Record.from_api` and `render_content`.
3. Validate the values in `validate_values` and parse the YAML form in
   `zonefile._record_values`.
4. Tests: parsing and validation errors (`test_zonefile.py`), plan behaviour
   (`test_plan.py`), and an export round trip (`test_cli.py::test_export_and_round_trip`).
5. Document the keys in the README tables and add a line to `CHANGELOG.md`.

## Guidelines

- Keep runtime dependencies to httpx and PyYAML; discuss new ones first.
- Safety first: never widen what counts as "managed" without an explicit opt-in.
- Never print or log the API token.
- Type hints everywhere; `ruff check` and `ruff format` clean.
- Tests are fast and deterministic: no network, no sleeping (the client takes a `sleep`).

## Pull request process

1. Fork and create a branch (`type/tlsa`, `fix/txt-quotes`).
2. Keep pull requests focused; fill in the checklist in the template.
3. Add an entry to `CHANGELOG.md` under "Unreleased".
4. A maintainer reviews within a few days.

By contributing you agree that your work is released under the MIT License and that you
follow the [Code of Conduct](CODE_OF_CONDUCT.md).
