"""CLI entrypoint: auth | dry-run | sync | list."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .auth import get_calendar_service, token_info
from .config import DEFAULT_CONFIG_PATH, load_config
from .conflicts import delete_conflicts, find_owned_conflicts
from .sharing import prepare_workspace
from .sync import apply_series_actions, list_managed, plan_series_actions


def _print_conflicts(conflicts) -> None:
    if not conflicts:
        print("Conflicts to remove: none")
        return
    print(f"Conflicts to remove: {len(conflicts)}")
    for c in conflicts:
        start = (c.event.get("start") or {}).get("dateTime") or (
            c.event.get("start") or {}
        ).get("date")
        print(
            f"  - [{c.calendar_name}] {c.summary} @ {start} "
            f"(delete {c.delete_id})"
        )
        print(f"      {c.reason}")


def _print_actions(actions) -> None:
    if not actions:
        print("Series actions: none")
        return
    print(f"Series actions: {len(actions)}")
    for a in actions:
        print(f"  - {a.action.upper():9} {a.series_id}: {a.summary} ({a.detail})")


def _print_shares(share_actions) -> None:
    if not share_actions:
        print("Calendar shares: none configured")
        return
    print(f"Calendar shares: {len(share_actions)}")
    for a in share_actions:
        print(
            f"  - {a.action.upper():9} [{a.calendar_name}] {a.email} "
            f"role={a.role} ({a.detail})"
        )


def _print_notes(notes: list[str]) -> None:
    if not notes:
        return
    print("Calendar setup:")
    for note in notes:
        print(f"  - {note}")


def _print_token_status(info: dict | None, *, account: str) -> None:
    if not info:
        print("No token.json. Run: python -m src.cli auth")
        return
    print(f"  expected account: {account}")
    print(f"  token: {info['path']}")
    print(f"  refresh_token: {'yes' if info['has_refresh_token'] else 'no'}")
    if info.get("authorized_at"):
        age = info.get("age_days")
        age_txt = f"{age:.1f}d ago" if age is not None else "?"
        print(f"  last interactive login: {info['authorized_at']} ({age_txt})")
    else:
        print("  last interactive login: unknown (pre-meta token)")
    if info.get("testing_refresh_likely_expired"):
        print(
            "  warning: Testing-mode refresh tokens die after ~7 days. "
            "Publish the OAuth app (Production) to stop weekly re-login."
        )


def cmd_auth(args: argparse.Namespace) -> int:
    hint = args.account
    if args.status:
        _print_token_status(token_info(), account=hint)
        return 0
    open_browser = bool(args.browser) and not args.no_browser
    get_calendar_service(
        force_consent=args.force,
        login_hint=hint,
        open_browser=open_browser,
    )
    print("Authenticated successfully.")
    _print_token_status(token_info(), account=hint)
    print("Next: python -m src.cli dry-run")
    return 0


def cmd_dry_run(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config) if args.config else None)
    service = get_calendar_service()
    workspace = prepare_workspace(service, config, dry_run=True)
    print(f"Config: {config.path}")
    print(f"Timezone: {config.timezone} | Owner filter: {config.owner_email}")
    print(
        "Person calendars: "
        + ", ".join(
            f"{p.calendar_name}={workspace.person_calendars[k]}"
            for k, p in config.people.items()
        )
    )
    print()
    _print_notes(workspace.notes)
    _print_shares(workspace.share_actions)
    print()
    conflicts = find_owned_conflicts(service, config)
    actions = plan_series_actions(
        service, config, workspace.person_calendars
    )
    _print_conflicts(conflicts)
    print()
    _print_actions(actions)
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config) if args.config else None)
    service = get_calendar_service()
    workspace = prepare_workspace(service, config, dry_run=False)
    print(f"Config: {config.path}")
    print(f"Timezone: {config.timezone}")
    print(
        "Person calendars: "
        + ", ".join(
            f"{p.calendar_name}" for p in config.people.values()
        )
    )
    print()
    _print_notes(workspace.notes)
    _print_shares(workspace.share_actions)
    print()

    conflicts = find_owned_conflicts(service, config)
    _print_conflicts(conflicts)
    deleted = delete_conflicts(
        service, config, conflicts, dry_run=False
    )
    if deleted:
        print(f"Deleted {len(deleted)} conflicting event(s)/series.")
    print()

    actions = plan_series_actions(
        service, config, workspace.person_calendars
    )
    _print_actions(actions)
    applied = apply_series_actions(
        service,
        config,
        workspace.person_calendars,
        actions,
        dry_run=False,
    )
    created = sum(1 for a in applied if a.action == "create")
    updated = sum(1 for a in applied if a.action == "update")
    unchanged = sum(1 for a in applied if a.action == "unchanged")
    print()
    print(
        f"Sync complete: {created} created, {updated} updated, {unchanged} unchanged."
    )
    print(
        "Note for Angela: Google Calendar mobile apps color by *calendar*, not "
        "per-event. She should enable Huy / Vy / Hoa calendars and "
        "set each calendar's color (red / yellow / green) in her app if needed."
    )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config) if args.config else None)
    service = get_calendar_service()
    workspace = prepare_workspace(service, config, dry_run=False)
    _print_notes(workspace.notes)
    _print_shares(workspace.share_actions)
    events = list_managed(
        service, config, workspace.person_calendars, weeks=args.weeks
    )
    if not events:
        print("No managed events found in the look-ahead window.")
        return 0
    print(f"Managed instances ({len(events)}):")
    for event in events:
        props = (event.get("extendedProperties") or {}).get("private") or {}
        mid = props.get("managedId", "?")
        start = (event.get("start") or {}).get("dateTime") or (
            event.get("start") or {}
        ).get("date")
        cal_name = event.get("_calendar_name") or "?"
        print(
            f"  - [{cal_name}/{mid}] {event.get('summary')} @ {start} "
            f"(id={event.get('id')})"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.cli",
        description="Sync family schedules to Google Calendar.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Path to schedules.yaml (default: {DEFAULT_CONFIG_PATH})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    auth_p = sub.add_parser("auth", help="Run OAuth desktop login and save token.json")
    auth_p.add_argument(
        "--force",
        action="store_true",
        help="Force re-consent even if a token already exists",
    )
    auth_p.add_argument(
        "--status",
        action="store_true",
        help="Print token.json status without starting a login",
    )
    auth_p.add_argument(
        "--account",
        default="moses.hoavo@gmail.com",
        help="Google account to pre-select in the login screen (login_hint)",
    )
    auth_p.add_argument(
        "--browser",
        action="store_true",
        help="Auto-open the default browser (usually the Work profile - avoid)",
    )
    auth_p.add_argument(
        "--no-browser",
        action="store_true",
        help="Paste-URL flow (default). Kept for older command lines.",
    )
    auth_p.set_defaults(func=cmd_auth)

    dry_p = sub.add_parser(
        "dry-run",
        help="Preview owned conflicts to delete and series to create/update",
    )
    dry_p.set_defaults(func=cmd_dry_run)

    sync_p = sub.add_parser(
        "sync",
        help="Delete owned conflicts, then upsert managed series",
    )
    sync_p.set_defaults(func=cmd_sync)

    list_p = sub.add_parser("list", help="List managed event instances ahead")
    list_p.add_argument(
        "--weeks",
        type=int,
        default=None,
        help="Look-ahead weeks (default: conflict_lookahead_weeks from config)",
    )
    list_p.set_defaults(func=cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
