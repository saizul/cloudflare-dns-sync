# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | yes |

## What counts as a vulnerability

- The API token appearing in output, error messages, plan files or the container image
- A record without the management tag being updated or deleted while
  `delete_unmanaged` is `false`
- `apply` changing records when it reported a dry run, or applying something other than
  the plan it printed
- A crafted zone file or API response causing code execution or writes outside the
  requested `--out`/`--output` path

## Reporting

Please **do not** open a public issue. Use
[GitHub private vulnerability reporting](https://github.com/saizul/cloudflare-dns-sync/security/advisories/new)
with the version, the impact and steps to reproduce. Do not include real API tokens.

You will get an answer within 5 working days. Fixes are released as soon as possible and
credited in the changelog unless you prefer otherwise.

## Hardening checklist for users

- Scope the API token to the zones you manage, with only Zone:Read and DNS:Edit.
- Store it as a CI secret; give pull request jobs from forks no access to it.
- Protect the apply job with a GitHub environment that requires a reviewer.
- Use `--max-deletes` in automation.
