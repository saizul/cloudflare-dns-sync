# cloudflare-dns-sync

[![CI](https://github.com/saizul/cloudflare-dns-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/saizul/cloudflare-dns-sync/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/saizul/cloudflare-dns-sync?sort=semver)](https://github.com/saizul/cloudflare-dns-sync/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Container image](https://img.shields.io/badge/ghcr.io-saizul%2Fcloudflare--dns--sync-2496ED?logo=docker&logoColor=white)](https://github.com/saizul/cloudflare-dns-sync/pkgs/container/cloudflare-dns-sync)
![Python](https://img.shields.io/badge/python-3.11%E2%80%933.13-blue)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

**DNS records as code for Cloudflare.** Keep one small YAML file per zone in git,
review a colored `plan` of what will change on every pull request, and `apply`
it when the pull request is merged. Records you did not put under management are
never touched.

```text
$ cloudflare-dns-sync plan zones/
example.com (zone 023e105f4ecef8ad9ca31a8372d0c353)
  - delete CNAME  old.example.com   example.com  ttl=auto
  ~ update A      example.com       203.0.113.10
        content: 203.0.113.9 -> 203.0.113.10
  ~ update MX     example.com       10 mx1.mail.example.net
        priority: 20 -> 10
  + create TXT    _dmarc.example.com  v=DMARC1; p=quarantine; rua=mailto:dmarc@example.com  ttl=auto
  + create CNAME  www.example.com   example.com  ttl=auto proxied
  1 unmanaged records left untouched (--show-unmanaged to list)

Plan: 2 to create, 2 to update, 1 to delete.
```

## Contents

- [Features](#features)
- [Quick start](#quick-start)
- [Zone file reference](#zone-file-reference)
- [Which records are managed](#which-records-are-managed)
- [Commands](#commands)
- [GitHub Actions](#github-actions)
- [JSON plan format](#json-plan-format)
- [FAQ](#faq)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)

## Features

- **One YAML file per zone** with A, AAAA, CNAME, MX, TXT, SRV and CAA records, `proxied`,
  TTL (`auto` or seconds), comments, per-zone defaults and multi-value shorthand
  (`content: [ip1, ip2]`)
- **`plan`**: a colored create / update / delete diff against the live zone, with the exact
  fields that change; also as **Markdown** (for pull request comments) and **JSON**
- **`apply`**: deletes, then updates, then creates; asks for confirmation, or `--yes`.
  **Dry run by default in CI** and whenever there is no terminal to ask
- **Safe by design**: only records carrying the management tag in their comment are
  updated or deleted. Everything else is reported and left alone unless you opt in with
  `delete_unmanaged: true`. `--max-deletes N` stops runaway deletes
- **`export`**: dump an existing zone to a ready-to-commit zone file; the first `apply`
  simply tags the records, nothing else changes
- **`validate`**: offline checks (IP addresses, host names, TTL ranges, duplicates, CNAME
  conflicts, Cloudflare's 100 character comment limit) that report every error at once
- Cloudflare API v4 with an API token from the environment, pagination, retries on
  `429`/`5xx` honouring `Retry-After`
- Small: httpx + PyYAML. Container image on `ghcr.io`, non-root

## Quick start

1. Create an API token at **Cloudflare dashboard > My Profile > API Tokens** with
   **Zone > Zone > Read** and **Zone > DNS > Edit** for the zones you want to manage.

2. Export the zone you already have and look at the plan:

   ```bash
   export CLOUDFLARE_API_TOKEN=...      # never commit it

   # Docker
   docker run --rm -e CLOUDFLARE_API_TOKEN -v "$PWD:/work" --user "$(id -u):$(id -g)" \
     ghcr.io/saizul/cloudflare-dns-sync:latest export example.com -o zones/example.com.yaml

   # or pip / pipx
   pipx install git+https://github.com/saizul/cloudflare-dns-sync
   mkdir -p zones
   cloudflare-dns-sync export example.com -o zones/example.com.yaml
   cloudflare-dns-sync plan zones/
   ```

   The first plan only adds the management tag to the comment of every exported record.

3. Apply it, then edit the YAML and repeat:

   ```bash
   cloudflare-dns-sync apply zones/          # shows the plan, asks "yes"
   cloudflare-dns-sync apply zones/ --yes    # for scripts and CI
   ```

`cfdns-sync` is installed as a short alias of `cloudflare-dns-sync`.

## Zone file reference

A complete example is in [`examples/zones/example.com.yaml`](examples/zones/example.com.yaml).

```yaml
zone: example.com
# zone_id: 0123456789abcdef0123456789abcdef   # optional, skips the zone lookup
managed_tag: "[dns-sync]"                     # optional, this is the default
delete_unmanaged: false                       # optional, see below
defaults:
  ttl: auto
  proxied: false

records:
  - name: "@"                     # zone apex
    type: A
    content: 203.0.113.10
    proxied: true
    comment: origin web server
  - name: www                     # www.example.com
    type: CNAME
    content: "@"                  # "@" in content means the zone apex
    proxied: true
  - name: api
    type: A
    content: [203.0.113.21, 203.0.113.22]   # two records
    ttl: 300
  - name: "@"
    type: MX
    content: [mx1.mail.example.net, mx2.mail.example.net]
    priority: 10
  - name: "@"
    type: TXT
    content: "v=spf1 include:_spf.mail.example.net -all"
  - name: _sip._tcp
    type: SRV
    priority: 10
    weight: 5
    port: 5060
    target: sip.example.com       # or: content: "10 5 5060 sip.example.com"
  - name: "@"
    type: CAA
    tag: issue                    # issue, issuewild or iodef
    value: letsencrypt.org        # or: content: '0 issue "letsencrypt.org"'
```

### Top-level keys

| Key | Default | Description |
| --- | --- | --- |
| `zone` | required | Zone name as shown in Cloudflare |
| `zone_id` | looked up | Zone id; when set the token does not need Zone:Read |
| `managed_tag` | `[dns-sync]` | Text appended to the comment of every record this tool writes |
| `delete_unmanaged` | `false` | Treat the file as the complete truth: also update and delete untagged records |
| `defaults.ttl` | `auto` | `auto` (Cloudflare's `1`) or 30-86400 seconds |
| `defaults.proxied` | `false` | Default for A, AAAA and CNAME records |
| `records` | `[]` | List of records |

### Record keys

| Key | Types | Description |
| --- | --- | --- |
| `name` | all | `@` for the apex, a relative name (`www`, `_dmarc`, `*.dev`), or a fully qualified name with a trailing dot |
| `type` | all | `A`, `AAAA`, `CNAME`, `MX`, `TXT`, `SRV`, `CAA` |
| `content` | all | Value, or a list of values (one record each). SRV and CAA also accept the separate keys below |
| `ttl` | all | `auto` or seconds; ignored while proxied (Cloudflare always uses auto) |
| `proxied` | A, AAAA, CNAME | Orange cloud |
| `comment` | all | Free text; the management tag is appended automatically |
| `priority` | MX, SRV | 0-65535 |
| `weight`, `port`, `target` | SRV | 0-65535, 0-65535, host name |
| `flags`, `tag`, `value` | CAA | 0-255, `issue`/`issuewild`/`iodef`, text |

Unknown keys are errors, so typos fail in `validate` instead of silently doing nothing.

## Which records are managed

Cloudflare records have a free-text comment. Every record that cloudflare-dns-sync
creates or updates gets the `managed_tag` appended to it (`origin web server [dns-sync]`).
When planning, each live record is one of:

| Live record | In the zone file | Result |
| --- | --- | --- |
| tagged | same name, type and value | updated if TTL, proxied, priority or comment differ |
| tagged | same name and type, other value | updated in place (e.g. an IP change) |
| tagged | not present | **deleted** |
| untagged | same name, type and value | **adopted**: the tag is added, attributes updated |
| untagged | anything else | left alone and listed as unmanaged |
| other types (NS, HTTPS, ...) | - | never touched |

With `delete_unmanaged: true` untagged records are treated like tagged ones. Use it once a
zone is fully described in git. `apply --max-deletes N` is a second safety net.

Values are compared the way DNS sees them: IPv6 addresses canonically, host names without
case or trailing dot, TXT content with or without the surrounding quotes Cloudflare may
return.

## Commands

All commands that talk to Cloudflare read the token from `CLOUDFLARE_API_TOKEN`
(`--token-env NAME` to use another variable). Zone paths may be files or directories
(every `*.yaml`/`*.yml` in it); the default is `./zones`.

| Command | Description |
| --- | --- |
| `plan [PATHS] [--format text\|json\|markdown] [--out FILE] [--show-unmanaged] [--detailed-exitcode]` | Show the changes. `--out plan.md` / `plan.json` also writes a file (format from the extension). `--detailed-exitcode` exits `2` when there are changes |
| `apply [PATHS] [--yes] [--dry-run] [--max-deletes N] [--show-unmanaged]` | Show the plan and apply it. Without `--yes` it asks for `yes`; in CI (`CI=true` or GitHub Actions) or without a terminal it is a dry run |
| `export ZONE [-o FILE] [--managed-only] [--managed-tag TAG] [--zone-id ID]` | Write the live zone as a zone file. The most common TTL becomes `defaults.ttl` |
| `validate [PATHS]` | Check zone files without network access |

Common options: `--color auto|always|never` (`NO_COLOR` is respected), `--api-url`
(or `CLOUDFLARE_API_URL`). Exit codes: `0` success, `1` error, `2` changes
(only with `--detailed-exitcode`).

## GitHub Actions

[`examples/github-actions-dns.yml`](examples/github-actions-dns.yml) runs `plan` on every
pull request that touches `zones/`, posts the Markdown plan as one pull request comment
that is updated on each push, and runs `apply --yes --max-deletes 10` on `main`.

```yaml
      - name: Plan
        env:
          CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}
        run: |
          docker run --rm --user "$(id -u):$(id -g)" -e CLOUDFLARE_API_TOKEN -v "$PWD:/work" -w /work \
            ghcr.io/saizul/cloudflare-dns-sync:latest plan zones --color always --out plan.md
```

Put the apply job behind a GitHub environment with required reviewers if you want a second
pair of eyes before DNS changes go live.

## JSON plan format

`plan --format json` prints a stable document (`format_version: 1`):

```json
{
  "format_version": 1,
  "tool": "cloudflare-dns-sync 0.1.0",
  "has_changes": true,
  "zones": [
    {
      "zone": "example.com",
      "zone_id": "023e105f4ecef8ad9ca31a8372d0c353",
      "summary": {"create": 1, "update": 1, "delete": 0, "in_sync": 9, "unmanaged": 1, "unsupported": 0},
      "changes": [
        {
          "action": "update",
          "before": {"name": "example.com", "type": "A", "content": "203.0.113.9", "ttl": "auto", "proxied": true, "comment": "web", "id": "372e67954025e0ba6aaa6d586b9e0b59"},
          "after": {"name": "example.com", "type": "A", "content": "203.0.113.10", "ttl": "auto", "proxied": true, "comment": "web", "id": "372e67954025e0ba6aaa6d586b9e0b59"},
          "fields": ["content"]
        },
        {"action": "create", "after": {"name": "www.example.com", "type": "CNAME", "content": "example.com", "ttl": "auto", "proxied": true, "comment": ""}}
      ],
      "unmanaged": [{"name": "legacy.example.com", "type": "A", "content": "198.51.100.4", "ttl": "auto", "proxied": false, "comment": "by hand", "id": "..."}],
      "warnings": []
    }
  ]
}
```

MX records add `priority`; SRV records add `priority`, `weight`, `port`, `target`; CAA
records add `flags`, `tag`, `value`.

## FAQ

**Will it delete records I created by hand?**
No. Only records whose comment contains the management tag are deleted, unless you set
`delete_unmanaged: true` for that zone.

**Why a comment tag and not Cloudflare record tags?**
Record tags are only available on paid plans; comments work on every plan. A record is
also treated as managed when its `tags` contain the tag, so both work.

**Can two tools or two repositories manage the same zone?**
Yes, give each its own `managed_tag` and keep `delete_unmanaged: false`.

**What about NS, HTTPS, SVCB, PTR and other types?**
They are listed as unsupported and never modified. Support for more types is a welcome
contribution (see the roadmap).

**Why are deletes applied first?**
A CNAME cannot coexist with other records at the same name, so changing `www` from an
A record to a CNAME needs the delete before the create.

**Does it work with Terraform or octoDNS already managing the zone?**
Do not let two tools own the same records. Export the zone, remove it from the other tool,
and run the first `apply`, which only tags the records.

**Which permissions does the token need?**
`Zone:Read` (to find the zone id; not needed with `zone_id`) and `DNS:Edit`. `plan` and
`export` work with `DNS:Read`.

## Roadmap

Good first issues are marked below; details are in [CONTRIBUTING.md](CONTRIBUTING.md#good-first-issues).

- [ ] More record types: NS (sub-delegations), HTTPS/SVCB, PTR, TLSA, SSHFP *(good first issue per type)*
- [ ] `--zone NAME` to plan/apply only some zones from a directory *(good first issue)*
- [ ] `plan --out plan.json` + `apply --plan plan.json` to apply exactly what was reviewed
- [ ] YAML anchors friendly `templates:` section for repeated record sets *(good first issue)*
- [ ] Environment variable interpolation in content (`${ORIGIN_IP}`) *(good first issue)*
- [ ] Cloudflare batch endpoint for atomic applies
- [ ] `import` from BIND zone files *(good first issue)*

## Contributing

Bug reports, record types, documentation and tests are very welcome. Read
[CONTRIBUTING.md](CONTRIBUTING.md) for the setup (three commands), the project layout and
a list of concrete beginner issues. Please follow the [Code of Conduct](CODE_OF_CONDUCT.md)
and report security problems privately as described in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE) © Saizul Amin
