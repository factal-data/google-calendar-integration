"""Ensure per-person calendars exist, are colored, and shared with co-parents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import calendar_client as cal
from .config import Person, ScheduleConfig, ShareRule


@dataclass
class ShareAction:
    calendar_name: str
    email: str
    role: str
    action: str  # create | update | unchanged
    detail: str


@dataclass
class PreparedWorkspace:
    config: ScheduleConfig
    person_calendars: dict[str, str]  # person key -> calendar_id
    notes: list[str]
    share_actions: list[ShareAction]

    def calendar_id_for(self, person_key: str) -> str:
        return self.person_calendars[person_key]

    @property
    def any_pending(self) -> bool:
        return any(str(v).startswith("pending:") for v in self.person_calendars.values())


def find_calendar_by_name(service, name: str) -> dict[str, Any] | None:
    target = name.strip().lower()
    for item in cal.list_owned_calendars(service):
        summary = (item.get("summaryOverride") or item.get("summary") or "").strip()
        if summary.lower() == target:
            return item
    return None


def ensure_calendar(
    service,
    *,
    name: str,
    timezone: str,
    description: str,
    dry_run: bool = False,
) -> tuple[str, str]:
    existing = find_calendar_by_name(service, name)
    if existing:
        return existing["id"], f"using existing calendar {name!r}"

    if dry_run:
        return f"pending:{name}", f"would create calendar {name!r}"

    created = (
        service.calendars()
        .insert(
            body={
                "summary": name,
                "timeZone": timezone,
                "description": description,
            }
        )
        .execute()
    )
    return created["id"], f"created calendar {name!r}"


def set_calendar_list_color(
    service,
    calendar_id: str,
    color_id: str,
    *,
    dry_run: bool = False,
) -> str | None:
    if calendar_id.startswith("pending:"):
        return f"would set calendarList colorId={color_id}"
    if dry_run:
        return f"would set calendarList colorId={color_id}"
    try:
        service.calendarList().patch(
            calendarId=calendar_id,
            body={"colorId": color_id, "selected": True},
        ).execute()
    except Exception as exc:  # noqa: BLE001
        return f"calendarList color patch failed: {exc}"
    return f"calendarList colorId={color_id}"


def list_acl(service, calendar_id: str) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        result = (
            service.acl()
            .list(calendarId=calendar_id, pageToken=page_token)
            .execute()
        )
        rules.extend(result.get("items") or [])
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return rules


def ensure_shares(
    service,
    calendar_id: str,
    calendar_name: str,
    shares: tuple[ShareRule, ...],
    *,
    dry_run: bool = False,
) -> list[ShareAction]:
    if not shares:
        return []
    if calendar_id.startswith("pending:"):
        return [
            ShareAction(
                calendar_name=calendar_name,
                email=s.email,
                role=s.role,
                action="create",
                detail="pending calendar create",
            )
            for s in shares
        ]

    existing = list_acl(service, calendar_id)
    by_email: dict[str, dict[str, Any]] = {}
    for rule in existing:
        scope = rule.get("scope") or {}
        if scope.get("type") == "user" and scope.get("value"):
            by_email[str(scope["value"]).lower()] = rule

    actions: list[ShareAction] = []
    for share in shares:
        email = share.email.lower()
        current = by_email.get(email)
        if current and current.get("role") == share.role:
            actions.append(
                ShareAction(
                    calendar_name=calendar_name,
                    email=email,
                    role=share.role,
                    action="unchanged",
                    detail=f"acl id={current.get('id')}",
                )
            )
            continue

        if current and current.get("role") != share.role:
            actions.append(
                ShareAction(
                    calendar_name=calendar_name,
                    email=email,
                    role=share.role,
                    action="update",
                    detail=f"role {current.get('role')} -> {share.role}",
                )
            )
            if not dry_run:
                service.acl().update(
                    calendarId=calendar_id,
                    ruleId=current["id"],
                    body={
                        "role": share.role,
                        "scope": {"type": "user", "value": email},
                    },
                ).execute()
            continue

        actions.append(
            ShareAction(
                calendar_name=calendar_name,
                email=email,
                role=share.role,
                action="create",
                detail="will receive calendar share invite/email",
            )
        )
        if not dry_run:
            service.acl().insert(
                calendarId=calendar_id,
                sendNotifications=True,
                body={
                    "role": share.role,
                    "scope": {"type": "user", "value": email},
                },
            ).execute()
    return actions


def migrate_managed_to_person_calendars(
    service,
    config: ScheduleConfig,
    person_calendars: dict[str, str],
    *,
    dry_run: bool = False,
) -> list[str]:
    """Move managed series onto the correct person calendar; remove duplicates elsewhere."""
    notes: list[str] = []
    owned = cal.list_owned_calendars(service)
    target_ids = {
        cid for cid in person_calendars.values() if not str(cid).startswith("pending:")
    }

    for series in config.series:
        target = person_calendars[series.person]
        if str(target).startswith("pending:"):
            continue
        for item in owned:
            cid = item["id"]
            if cid == target:
                continue
            master = cal.find_managed_series_master(service, cid, series.id)
            if not master:
                continue
            name = item.get("summaryOverride") or item.get("summary") or cid
            person_name = config.people[series.person].calendar_name
            notes.append(
                f"{'would move' if dry_run else 'moved'}: {series.id} "
                f"off {name!r} -> {person_name!r}"
            )
            if not dry_run:
                cal.delete_event(service, cid, master["id"])
    return notes


def delete_legacy_combined_calendars(
    service,
    config: ScheduleConfig,
    person_calendars: dict[str, str],
    *,
    dry_run: bool = False,
) -> list[str]:
    """Delete empty legacy combined calendars after migration."""
    notes: list[str] = []
    protected = set(person_calendars.values())
    for name in config.legacy_combined_calendar_names:
        item = find_calendar_by_name(service, name)
        if not item:
            continue
        cid = item["id"]
        if cid in protected:
            continue
        # Only delete if no managed masters remain
        still_managed = False
        for series in config.series:
            if cal.find_managed_series_master(service, cid, series.id):
                still_managed = True
                break
        if still_managed:
            notes.append(f"kept legacy calendar {name!r} (still has managed events)")
            continue
        notes.append(
            f"{'would delete' if dry_run else 'deleted'} legacy calendar {name!r}"
        )
        if not dry_run:
            service.calendars().delete(calendarId=cid).execute()
    return notes


def prepare_workspace(
    service,
    config: ScheduleConfig,
    *,
    dry_run: bool = False,
) -> PreparedWorkspace:
    """Create/find per-person calendars, color them, share with co-parents, migrate."""
    notes: list[str] = []
    share_actions: list[ShareAction] = []
    person_calendars: dict[str, str] = {}

    for key, person in config.people.items():
        cid, note = ensure_calendar(
            service,
            name=person.calendar_name,
            timezone=config.timezone,
            description=(
                f"{person.calendar_name}'s activities. Managed by "
                "GoogleCalendarIntegration; shared with co-parent(s)."
            ),
            dry_run=dry_run,
        )
        notes.append(note)
        color_note = set_calendar_list_color(
            service, cid, person.calendar_color_id, dry_run=dry_run
        )
        if color_note:
            notes.append(f"{person.calendar_name}: {color_note}")
        person_calendars[key] = cid
        share_actions.extend(
            ensure_shares(
                service,
                cid,
                person.calendar_name,
                config.shares,
                dry_run=dry_run,
            )
        )

    notes.extend(
        migrate_managed_to_person_calendars(
            service, config, person_calendars, dry_run=dry_run
        )
    )
    notes.extend(
        delete_legacy_combined_calendars(
            service, config, person_calendars, dry_run=dry_run
        )
    )

    return PreparedWorkspace(
        config=config,
        person_calendars=person_calendars,
        notes=notes,
        share_actions=share_actions,
    )
