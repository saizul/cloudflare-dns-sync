## What does this pull request do?

<!-- One or two sentences. Link the issue: "Closes #123". -->

## Type

- [ ] New record type
- [ ] Plan, apply, export or CLI change
- [ ] Bug fix
- [ ] Documentation

## Checklist

- [ ] Tests added or updated (`pytest` passes; tests use the `FakeCloudflare` transport and no network)
- [ ] `ruff check . && ruff format --check .` passes
- [ ] Unmanaged records stay untouched unless `delete_unmanaged: true`
- [ ] No real tokens, zones or IPs in tests (use `test-token`, `example.com`, documentation ranges)
- [ ] README tables and `examples/` updated for new keys or flags
- [ ] CHANGELOG.md updated under "Unreleased"
