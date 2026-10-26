"""Idempotent upsert of managed recurring series onto per-person calendars."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import calendar_client as cal
from .config import ScheduleConfig, Series
from .schedule_times import (
    first_occurrence_on_or_after,
    series_end_datetime,
    to_rfc3339,
)


def _series_slot(series: Series) -> str:
    if series.freq == "MONTHLY":
        start = series.starts_on.isoformat() if series.starts_on else "?"
        return f"every {series.interval} months from {start} {series.start}-{series.end}"
    return f"{','.join(series.days)} {series.start}-{series.end}"


@dataclass
class SyncAction:
    series_id: str
    action: str  # create | update | unchanged
    event_id: str | None
    summary: str
    detail: str
    calendar_id: str | None = None
    calendar_name: str | None = None


def build_event_body(
    series: Series,
    config: ScheduleConfig,
    *,
    from_date: date | None = None,
) -> dict[str, Any]:
    tz = ZoneInfo(config.timezone)
    person = config.person_for(series)
    today = from_date or datetime.now(tz).date()
    start_dt = first_occurrence_on_or_after(series, today, tz)
    end_dt = series_end_datetime(series, start_dt)
    if series.freq == "MONTHLY":
        rrule = f"RRULE:FREQ=MONTHLY;INTERVAL={series.interval}"
    else:
        rrule = f"RRULE:FREQ=WEEKLY;BYDAY={','.join(series.days)}"

    body: dict[str, Any] = {
        "summary": series.summary,
        "start": {
            "dateTime": start_dt.isoformat(),
            "timeZone": config.timezone,
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": config.timezone,
        },
        "recurrence": [rrule],
        "colorId": person.color_id,
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": person.reminder_minutes},
            ],
        },
        "extendedProperties": {
            "private": {
                cal.MANAGED_PROP_KEY: series.id,
            }
        },
    }
    if series.location:
        body["location"] = series.location
    if series.description:
        body["description"] = series.description
    else:
        body["description"] = (
            f"Managed by GoogleCalendarIntegration ({series.id}). "
            f"Person: {series.person}."
        )
    return body


def _same_managed_payload(existing: dict[str, Any], desired: dict[str, Any]) -> bool:
    keys = ("summary", "colorId", "location", "description", "recurrence")
    for key in keys:
        if existing.get(key) != desired.get(key):
            return False

    for edge in ("start", "end"):
        ex = existing.get(edge) or {}
        de = desired.get(edge) or {}
        if ex.get("timeZone") != de.get("timeZone"):
            return False
        ex_dt = ex.get("dateTime")
        de_dt = de.get("dateTime")
        if not ex_dt or not de_dt:
            return False
        ex_parsed = datetime.fromisoformat(ex_dt.replace("Z", "+00:00"))
        de_parsed = datetime.fromisoformat(de_dt.replace("Z", "+00:00"))
        if (ex_parsed.hour, ex_parsed.minute) != (de_parsed.hour, de_parsed.minute):
            return False

    ex_rem = existing.get("reminders") or {}
    de_rem = desired.get("reminders") or {}
    if bool(ex_rem.get("useDefault")) != bool(de_rem.get("useDefault")):
        return False
    ex_over = sorted(
        [(o.get("method"), o.get("minutes")) for o in (ex_rem.get("overrides") or [])]
    )
    de_over = sorted(
        [(o.get("method"), o.get("minutes")) for o in (de_rem.get("overrides") or [])]
    )
    if ex_over != de_over:
        return False

    ex_props = ((existing.get("extendedProperties") or {}).get("private") or {}).get(
        cal.MANAGED_PROP_KEY
    )
    de_props = ((desired.get("extendedProperties") or {}).get("private") or {}).get(
        cal.MANAGED_PROP_KEY
    )
    return ex_props == de_props


def plan_series_actions(
    service,
    config: ScheduleConfig,
    person_calendars: dict[str, str],
    *,
    from_date: date | None = None,
) -> list[SyncAction]:
    actions: list[SyncAction] = []
    for series in config.series:
        person = config.person_for(series)
        calendar_id = person_calendars[series.person]
        desired = build_event_body(series, config, from_date=from_date)

        if str(calendar_id).startswith("pending:"):
            actions.append(
                SyncAction(
                    series_id=series.id,
                    action="create",
                    event_id=None,
                    summary=series.summary,
                    calendar_id=calendar_id,
                    calendar_name=person.calendar_name,
                    detail=(
                        f"{person.calendar_name}: {_series_slot(series)} "
                        f"{config.timezone} (after calendar create)"
                    ),
                )
            )
            continue

        existing = cal.find_managed_series_master(service, calendar_id, series.id)
        if not existing:
            actions.append(
                SyncAction(
                    series_id=series.id,
                    action="create",
                    event_id=None,
                    summary=series.summary,
                    calendar_id=calendar_id,
                    calendar_name=person.calendar_name,
                    detail=(
                        f"{person.calendar_name}: {_series_slot(series)} "
                        f"{config.timezone}"
                    ),
                )
            )
            continue

        if _same_managed_payload(existing, desired):
            actions.append(
                SyncAction(
                    series_id=series.id,
                    action="unchanged",
                    event_id=existing.get("id"),
                    summary=series.summary,
                    calendar_id=calendar_id,
                    calendar_name=person.calendar_name,
                    detail=f"{person.calendar_name} eventId={existing.get('id')}",
                )
            )
        else:
            actions.append(
                SyncAction(
                    series_id=series.id,
                    action="update",
                    event_id=existing.get("id"),
                    summary=series.summary,
                    calendar_id=calendar_id,
                    calendar_name=person.calendar_name,
                    detail=f"{person.calendar_name} eventId={existing.get('id')}",
                )
            )
    return actions


def apply_series_actions(
    service,
    config: ScheduleConfig,
    person_calendars: dict[str, str],
    actions: list[SyncAction],
    *,
    dry_run: bool = True,
    from_date: date | None = None,
) -> list[SyncAction]:
    applied: list[SyncAction] = []
    for action in actions:
        series = next(s for s in config.series if s.id == action.series_id)
        person = config.person_for(series)
        calendar_id = person_calendars[series.person]
        desired = build_event_body(series, config, from_date=from_date)

        if action.action == "unchanged":
            applied.append(action)
            continue

        if dry_run:
            applied.append(action)
            continue

        if action.action == "create":
            created = cal.insert_event(service, calendar_id, desired)
            applied.append(
                SyncAction(
                    series_id=action.series_id,
                    action="create",
                    event_id=created.get("id"),
                    summary=action.summary,
                    calendar_id=calendar_id,
                    calendar_name=person.calendar_name,
                    detail=(
                        f"created on {person.calendar_name} "
                        f"eventId={created.get('id')}"
                    ),
                )
            )
        elif action.action == "update":
            existing = cal.get_event(service, calendar_id, action.event_id)
            body = dict(desired)
            if existing.get("start") and existing["start"].get("dateTime"):
                tz = ZoneInfo(config.timezone)
                old_start = datetime.fromisoformat(
                    existing["start"]["dateTime"].replace("Z", "+00:00")
                ).astimezone(tz)
                new_start = datetime.fromisoformat(
                    desired["start"]["dateTime"].replace("Z", "+00:00")
                ).astimezone(tz)
                anchored = old_start.replace(
                    hour=new_start.hour,
                    minute=new_start.minute,
                    second=0,
                    microsecond=0,
                )
                body["start"] = {
                    "dateTime": anchored.isoformat(),
                    "timeZone": config.timezone,
                }
                body["end"] = {
                    "dateTime": series_end_datetime(series, anchored).isoformat(),
                    "timeZone": config.timezone,
                }
            updated = cal.update_event(service, calendar_id, action.event_id, body)
            applied.append(
                SyncAction(
                    series_id=action.series_id,
                    action="update",
                    event_id=updated.get("id"),
                    summary=action.summary,
                    calendar_id=calendar_id,
                    calendar_name=person.calendar_name,
                    detail=(
                        f"updated on {person.calendar_name} "
                        f"eventId={updated.get('id')}"
                    ),
                )
            )
        else:
            applied.append(action)
    return applied


def list_managed(
    service,
    config: ScheduleConfig,
    person_calendars: dict[str, str],
    *,
    weeks: int | None = None,
) -> list[dict[str, Any]]:
    tz = ZoneInfo(config.timezone)
    now = datetime.now(tz)
    weeks = weeks or config.conflict_lookahead_weeks
    time_min = to_rfc3339(now)
    time_max = to_rfc3339(now + timedelta(weeks=weeks))
    events: list[dict[str, Any]] = []
    for person_key, calendar_id in person_calendars.items():
        if str(calendar_id).startswith("pending:"):
            continue
        for event in cal.iter_managed_events(
            service,
            calendar_id,
            time_min=time_min,
            time_max=time_max,
        ):
            event = dict(event)
            event["_person"] = person_key
            event["_calendar_name"] = config.people[person_key].calendar_name
            events.append(event)
    events.sort(
        key=lambda e: (e.get("start") or {}).get("dateTime")
        or (e.get("start") or {}).get("date")
        or ""
    )
    return events
