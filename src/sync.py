"""Idempotent upsert of managed recurring series onto per-person calendars."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import calendar_client as cal
from .config import BYDAY_TO_WEEKDAY, ScheduleConfig, Series
from .schedule_times import (
    first_occurrence_on_or_after,
    local_slot_segments,
    rrule_until_utc,
    series_end_datetime,
    to_rfc3339,
)


def _series_slot(series: Series, config: ScheduleConfig) -> str:
    series_tz = config.timezone_for(series)
    if series.freq == "MONTHLY":
        start = series.starts_on.isoformat() if series.starts_on else "?"
        slot = f"every {series.interval} months from {start} {series.start}-{series.end}"
    else:
        slot = f"{','.join(series.days)} {series.start}-{series.end}"
    slot = f"{slot} {series_tz}"
    if series.freq == "WEEKLY" and series.starts_on:
        slot += f" from {series.starts_on.isoformat()}"
    if series.until:
        slot += f" until {series.until.isoformat()}"
    if series_tz == config.timezone:
        return slot
    home_tz = ZoneInfo(config.timezone)
    segments = local_slot_segments(
        series,
        series_tz=ZoneInfo(series_tz),
        home_tz=home_tz,
        from_date=datetime.now(home_tz).date(),
    )
    local = "; ".join(
        text if i == 0 else f"from {first:%a %Y-%m-%d} {text}"
        for i, (first, text) in enumerate(segments)
    )
    return f"{slot} = {config.timezone}: {local or 'no more instances'}"


def _has_ended(series: Series, config: ScheduleConfig) -> bool:
    if not series.until:
        return False
    tz = ZoneInfo(config.timezone_for(series))
    return series.until < datetime.now(tz).date()


def _recurrence_key(recurrence: list[str] | None) -> list[tuple[str, ...]]:
    """Order-insensitive RRULE parts, in case Google reorders FREQ/BYDAY/UNTIL."""
    return sorted(
        tuple(sorted(rule.removeprefix("RRULE:").split(";"))) for rule in recurrence or []
    )


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
    tz_name = config.timezone_for(series)
    tz = ZoneInfo(tz_name)
    person = config.person_for(series)
    today = from_date or datetime.now(tz).date()
    start_dt = first_occurrence_on_or_after(series, today, tz)
    end_dt = series_end_datetime(series, start_dt)
    if series.freq == "MONTHLY":
        rrule = f"RRULE:FREQ=MONTHLY;INTERVAL={series.interval}"
    else:
        rrule = f"RRULE:FREQ=WEEKLY;BYDAY={','.join(series.days)}"
    until = rrule_until_utc(series, tz)
    if until:
        rrule += f";UNTIL={until}"
    body: dict[str, Any] = {
        "summary": series.summary,
        "start": {
            "dateTime": start_dt.isoformat(),
            "timeZone": tz_name,
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": tz_name,
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
    keys = ("summary", "colorId", "location", "description")
    for key in keys:
        if existing.get(key) != desired.get(key):
            return False
    if _recurrence_key(existing.get("recurrence")) != _recurrence_key(
        desired.get("recurrence")
    ):
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
        # Google may return dateTime in the calendar's offset, not the event's
        # timeZone; compare wall-clock time in the desired zone.
        edge_tz = ZoneInfo(de["timeZone"])
        ex_parsed = datetime.fromisoformat(ex_dt.replace("Z", "+00:00")).astimezone(edge_tz)
        de_parsed = datetime.fromisoformat(de_dt.replace("Z", "+00:00")).astimezone(edge_tz)
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
                        f"{person.calendar_name}: {_series_slot(series, config)} "
                        f"(after calendar create)"
                    ),
                )
            )
            continue

        existing = cal.find_managed_series_master(service, calendar_id, series.id)
        if not existing and _has_ended(series, config):
            actions.append(
                SyncAction(
                    series_id=series.id,
                    action="unchanged",
                    event_id=None,
                    summary=series.summary,
                    calendar_id=calendar_id,
                    calendar_name=person.calendar_name,
                    detail=f"{person.calendar_name}: ended {series.until}, not created",
                )
            )
            continue
        if not existing:
            actions.append(
                SyncAction(
                    series_id=series.id,
                    action="create",
                    event_id=None,
                    summary=series.summary,
                    calendar_id=calendar_id,
                    calendar_name=person.calendar_name,
                    detail=f"{person.calendar_name}: {_series_slot(series, config)}",
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
                    detail=(
                        f"{person.calendar_name} eventId={existing.get('id')} -> "
                        f"{_series_slot(series, config)}"
                    ),
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
                tz_name = config.timezone_for(series)
                tz = ZoneInfo(tz_name)
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
                # Moving a series to another zone (or new days) can put the old
                # anchor on a day outside BYDAY; start fresh instead.
                weekdays = {BYDAY_TO_WEEKDAY[d] for d in series.days}
                if series.freq == "WEEKLY" and anchored.weekday() not in weekdays:
                    anchored = new_start
                body["start"] = {
                    "dateTime": anchored.isoformat(),
                    "timeZone": tz_name,
                }
                body["end"] = {
                    "dateTime": series_end_datetime(series, anchored).isoformat(),
                    "timeZone": tz_name,
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
