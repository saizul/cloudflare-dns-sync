"""Command line interface: ``plan``, ``apply``, ``export`` and ``validate``."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

import httpx

from cloudflare_dns_sync._version import __version__
from cloudflare_dns_sync.api import DEFAULT_API_URL, CloudflareClient, CloudflareError
from cloudflare_dns_sync.export import export_zone
from cloudflare_dns_sync.plan import ZonePlan, apply_plan, build_plan, fetch_live
from cloudflare_dns_sync.records import Record
from cloudflare_dns_sync.render import render_json, render_markdown, render_text
from cloudflare_dns_sync.zonefile import DEFAULT_TAG, ZoneFileError, load_zone_files

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CHANGES = 2

DEFAULT_PATHS = ["zones"]


def in_ci(environ: os._Environ[str] | dict[str, str]) -> bool:
    return environ.get("CI", "").lower() in ("1", "true", "yes") or environ.get("GITHUB_ACTIONS") == "true"


class App:
    """Holds I/O and the HTTP transport so tests can drive the CLI without a network."""

    def __init__(
        self,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        stdin: TextIO | None = None,
        environ: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self.stdin = stdin or sys.stdin
        self.environ = environ if environ is not None else dict(os.environ)
        self.transport = transport
        self.sleep = sleep

    def err(self, message: str) -> None:
        print(f"error: {message}", file=self.stderr)

    def client(self, args: argparse.Namespace) -> CloudflareClient:
        token = self.environ.get(args.token_env, "").strip()
        if not token:
            raise CloudflareError(
                f"set {args.token_env} to a Cloudflare API token with Zone:Read and DNS:Edit permissions"
            )
        kwargs = {"sleep": self.sleep} if self.sleep else {}
        return CloudflareClient(token, base_url=args.api_url, transport=self.transport, **kwargs)

    def color(self, args: argparse.Namespace) -> bool:
        if args.color == "always":
            return True
        if args.color == "never" or "NO_COLOR" in self.environ:
            return False
        return bool(getattr(self.stdout, "isatty", lambda: False)())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cloudflare-dns-sync",
        description="DNS records as code for Cloudflare: plan and apply YAML zone files.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--token-env",
        default="CLOUDFLARE_API_TOKEN",
        metavar="VAR",
        help="environment variable that holds the API token (default: CLOUDFLARE_API_TOKEN)",
    )
    common.add_argument(
        "--api-url",
        default=os.environ.get("CLOUDFLARE_API_URL", DEFAULT_API_URL),
        help="Cloudflare API base URL (default: %(default)s)",
    )
    common.add_argument(
        "--color", choices=("auto", "always", "never"), default="auto", help="colored output (default: auto)"
    )
    paths = argparse.ArgumentParser(add_help=False)
    paths.add_argument(
        "paths", nargs="*", default=None, help="zone files or directories of *.yaml files (default: ./zones)"
    )

    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p_plan = sub.add_parser("plan", parents=[common, paths], help="show what apply would change")
    p_plan.add_argument("--format", choices=("text", "json", "markdown"), default="text")
    p_plan.add_argument("--out", metavar="FILE", help="also write the plan to FILE (format from extension)")
    p_plan.add_argument("--show-unmanaged", action="store_true", help="list records left untouched")
    p_plan.add_argument(
        "--detailed-exitcode", action="store_true", help="exit 2 when there are changes, 0 when in sync"
    )

    p_apply = sub.add_parser("apply", parents=[common, paths], help="apply the changes to Cloudflare")
    p_apply.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p_apply.add_argument("--dry-run", action="store_true", help="show the plan only (default in CI without --yes)")
    p_apply.add_argument("--show-unmanaged", action="store_true", help="list records left untouched")
    p_apply.add_argument(
        "--max-deletes",
        type=int,
        default=None,
        metavar="N",
        help="refuse to apply when the plan deletes more than N records",
    )

    p_export = sub.add_parser("export", parents=[common], help="write an existing zone as a zone file")
    p_export.add_argument("zone", help="zone name, e.g. example.com")
    p_export.add_argument("-o", "--output", metavar="FILE", help="write to FILE instead of stdout")
    p_export.add_argument("--managed-tag", default=DEFAULT_TAG, help="tag used to mark managed records")
    p_export.add_argument("--managed-only", action="store_true", help="only export records carrying the tag")
    p_export.add_argument("--zone-id", help="zone id (skips the zone lookup)")

    p_validate = sub.add_parser("validate", parents=[paths], help="check zone files without calling the API")
    p_validate.add_argument("--quiet", "-q", action="store_true")
    p_validate.add_argument("--strict", action="store_true", help="exit 1 when there are warnings")
    return parser


def _plans(app: App, args: argparse.Namespace) -> list[ZonePlan]:
    zonefiles = load_zone_files(args.paths or DEFAULT_PATHS)
    plans = []
    with app.client(args) as client:
        for zf in zonefiles:
            zone_id, live = fetch_live(client, zf)
            plans.append(build_plan(zf, live, zone_id))
    return plans


def _format_for(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {".json": "json", ".md": "markdown", ".markdown": "markdown"}.get(suffix, "text")


def _render(plans: list[ZonePlan], fmt: str, color: bool, show_unmanaged: bool) -> str:
    if fmt == "json":
        return render_json(plans)
    if fmt == "markdown":
        return render_markdown(plans)
    return render_text(plans, color=color, show_unmanaged=show_unmanaged)


def cmd_plan(app: App, args: argparse.Namespace) -> int:
    plans = _plans(app, args)
    app.stdout.write(_render(plans, args.format, app.color(args), args.show_unmanaged))
    if args.out:
        Path(args.out).write_text(_render(plans, _format_for(args.out), False, True), encoding="utf-8")
    if args.detailed_exitcode and any(p.has_changes for p in plans):
        return EXIT_CHANGES
    return EXIT_OK


def cmd_apply(app: App, args: argparse.Namespace) -> int:
    plans = _plans(app, args)
    color = app.color(args)
    app.stdout.write(render_text(plans, color=color, show_unmanaged=args.show_unmanaged))
    if not any(p.has_changes for p in plans):
        return EXIT_OK
    deletes = sum(p.count("delete") for p in plans)
    if args.max_deletes is not None and deletes > args.max_deletes:
        app.err(f"the plan deletes {deletes} records, more than --max-deletes {args.max_deletes}; nothing applied")
        return EXIT_ERROR
    if args.dry_run:
        print("Dry run: nothing applied.", file=app.stdout)
        return EXIT_OK
    if not args.yes:
        interactive = bool(getattr(app.stdin, "isatty", lambda: False)())
        if in_ci(app.environ) or not interactive:
            print("Dry run (non-interactive or CI): re-run with --yes to apply.", file=app.stdout)
            return EXIT_OK
        app.stdout.write("Apply these changes? Only 'yes' is accepted: ")
        app.stdout.flush()
        if app.stdin.readline().strip().lower() != "yes":
            print("Cancelled: nothing applied.", file=app.stdout)
            return EXIT_OK

    status = EXIT_OK
    with app.client(args) as client:
        for plan in plans:
            if not plan.has_changes:
                continue
            result = apply_plan(client, plan)
            print(f"{plan.zone}: applied {len(result.applied)} of {len(plan.changes)} changes", file=app.stdout)
            if not result.ok:
                assert result.failed is not None
                rec = result.failed.record
                app.err(f"{plan.zone}: {result.failed.action} {rec.type} {rec.name} failed: {result.error}")
                status = EXIT_ERROR
                break
    return status


def cmd_export(app: App, args: argparse.Namespace) -> int:
    zone = args.zone.strip().rstrip(".").lower()
    with app.client(args) as client:
        zone_id = args.zone_id or client.zone_id(zone)
        items = client.list_records(zone_id)
    records = [Record.from_api(item, args.managed_tag) for item in items]
    text = export_zone(zone, records, tag=args.managed_tag, managed_only=args.managed_only)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output}", file=app.stderr)
    else:
        app.stdout.write(text)
    return EXIT_OK


def cmd_validate(app: App, args: argparse.Namespace) -> int:
    zonefiles = load_zone_files(args.paths or DEFAULT_PATHS)
    warned = False
    for zf in zonefiles:
        if not args.quiet:
            note = f", {len(zf.warnings)} warning(s)" if zf.warnings else ""
            print(f"{zf.source}: {zf.zone} OK ({len(zf.records)} records{note})", file=app.stdout)
        for warning in zf.warnings:
            warned = True
            print(f"warning: {zf.source}: {warning}", file=app.stderr)
    return EXIT_ERROR if warned and args.strict else EXIT_OK


COMMANDS = {"plan": cmd_plan, "apply": cmd_apply, "export": cmd_export, "validate": cmd_validate}


def main(argv: list[str] | None = None, app: App | None = None) -> int:
    app = app or App()
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](app, args)
    except ZoneFileError as exc:
        print(f"error: {exc.source}:", file=app.stderr)
        for line in exc.errors:
            print(f"  - {line}", file=app.stderr)
        return EXIT_ERROR
    except CloudflareError as exc:
        app.err(str(exc))
        return EXIT_ERROR
    except KeyboardInterrupt:
        app.err("interrupted")
        return 130
