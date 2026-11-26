# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `NS` records for delegating subdomains. Apex NS records are rejected in zone files, never
  touched by `plan`/`apply` (also with `delete_unmanaged: true`) and listed as a comment by
  `export`; a delegated name cannot carry other records.
- Zone file warnings for records Cloudflare accepts but that do not work as intended: more
  than one SPF (`v=spf1`) record on a name, more than one DMARC record, a DMARC record
  outside `_dmarc.<domain>`, and an explicit TTL on a proxied record. `validate` prints them
  (and `--strict` exits 1), `plan` lists them with the other plan warnings.

## [0.1.0] - 2026-11-20

### Added

- YAML zone files: A, AAAA, CNAME, MX, TXT, SRV and CAA records with `proxied`, TTL
  (`auto` or seconds), comments, per-zone `defaults` and multi-value `content` lists.
- Strict validation that reports every error at once (`validate`).
- `plan` with a colored create/update/delete diff, field-level details, Markdown output for
  pull request comments, JSON output (`format_version: 1`), `--out` and `--detailed-exitcode`.
- `apply` with confirmation, `--yes`, `--dry-run`, `--max-deletes`; dry run by default in CI
  and without a terminal. Deletes run before updates and creates.
- Ownership through a comment tag (`managed_tag`, default `[dns-sync]`); untagged records are
  never modified unless they exactly match the zone file (adoption) or `delete_unmanaged: true`.
- `export` of an existing zone into a zone file.
- Cloudflare API v4 client (httpx) with token auth, pagination and retries on 429/5xx.
- Example GitHub Actions workflow (plan comment on pull requests, apply on main).
- Container image on `ghcr.io/saizul/cloudflare-dns-sync` (non-root).

[Unreleased]: https://github.com/saizul/cloudflare-dns-sync/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/saizul/cloudflare-dns-sync/releases/tag/v0.1.0
