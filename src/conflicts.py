"""Find and delete owned overlapping events on calendars you own."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import calendar_client as cal
from .config import ScheduleConfig
from .schedule_times import TimeWindow, expand_all_windows, to_rfc3339


@dataclass
class ConflictCandidate:
    event: dict[str, Any]
    calendar_id: str
    calendar_name: str
    delete_id: str
    reason: str
    overlapping_windows: list[TimeWindow]

    @property
    def summary(self) -> str:
        return self.event.get("summary") or "(no title)"

    @property
    def html_link(self) -> str | None:
        return self.event.get("htmlLink")


def _parse_event_dt(value: dict[str, Any] | None, tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    if "dateTime" in value:
        raw = value["dateTime"]
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone(tz)
    if "date" in value:
        # All-day: treat as [midnight, next midnight) in local tz
        d = date.fromisoformat(value["date"])
        return datetime.combine(d, datetime.min.time(), tzinfo=tz)
    return None


def _event_bounds(
    event: dict[str, Any], tz: ZoneInfo
) -> tuple[datetime, datetime] | None:
    start = _parse_event_dt(event.get("start"), tz)
    end = _parse_event_dt(event.get("end"), tz)
    if start is None:
        return None
    if end is None:
        # All-day with missing end: assume 1 day
        end = start + timedelta(days=1)
    # All-day end is exclusive date; dateTime end is exclusive too in Calendar API.
    return start, end


def _email_of(node: dict[str, Any] | None) -> str | None:
    if not node:
        return None
    email = node.get("email")
    return email.strip().lower() if isinstance(email, str) else None


def is_owned_by(event: dict[str, Any], owner_email: str) -> bool:
    """True if the event was created by the owner (or organizer.self on owned cal)."""
    owner = owner_email.strip().lower()
    creator = _email_of(event.get("creator"))
    organizer = _email_of(event.get("organizer"))
    organizer_self = bool((event.get("organizer") or {}).get("self"))

    # Secondary calendars you own often have organizer=<calendarId> with self=True
    # and creator=<your gmail>. Prefer creator match; allow organizer.self.
    if creator == owner:
        return True
    if organizer == owner or organizer_self:
        return True
    if creator and creator != owner and not organizer_self:
        return False
    if not creator and not organizer:
        return bool(
            organizer_self or (event.get("creator") or {}).get("self")
        )
    return False


def managed_id_of(event: dict[str, Any]) -> str | None:
    props = (event.get("extendedProperties") or {}).get("private") or {}
    return props.get(cal.MANAGED_PROP_KEY)


def _calendars_to_scan(service, config: ScheduleConfig) -> list[tuple[str, str]]:
    """Return (calendar_id, display_name) pairs to scan for conflicts."""
    result: list[tuple[str, str]] = []
    seen: set[str] = set()

    if config.conflict_scan_owned_calendars:
        for item in cal.list_owned_calendars(service):
            cid = item.get("id")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            name = item.get("summaryOverride") or item.get("summary") or cid
            result.append((cid, str(name)))
    else:
        # Still include person calendars by name if we can resolve them
        for person in config.people.values():
            from .sharing import find_calendar_by_name

            item = find_calendar_by_name(service, person.calendar_name)
            if item and item["id"] not in seen:
                seen.add(item["id"])
                result.append((item["id"], person.calendar_name))

    return result


def _title_matches_retire(summary: str | None, patterns: tuple[str, ...]) -> bool:
    if not summary or not patterns:
        return False
    lowered = summary.lower()
    return any(p in lowered for p in patterns)


def _collect_conflict(
    by_key: dict[tuple[str, str], ConflictCandidate],
    *,
    event: dict[str, Any],
    calendar_id: str,
    calendar_name: str,
    overlapping: list[TimeWindow],
    reason: str,
) -> None:
    recurring_id = event.get("recurringEventId")
    delete_id = recurring_id or event["id"]
    # Prefer deleting the recurring master when we have an instance.
    # If this event IS the master (has recurrence, no recurringEventId), use its id.
    if event.get("recurrence") and not recurring_id:
        delete_id = event["id"]

    key = (calendar_id, delete_id)
    existing = by_key.get(key)
    if existing:
        existing.overlapping_windows.extend(
            w for w in overlapping if w not in existing.overlapping_windows
        )
        return
    by_key[key] = ConflictCandidate(
        event=event,
        calendar_id=calendar_id,
        calendar_name=calendar_name,
        delete_id=delete_id,
        reason=reason,
        overlapping_windows=list(overlapping),
    )


def find_owned_conflicts(
    service,
    config: ScheduleConfig,
    *,
    from_date: date | None = None,
) -> list[ConflictCandidate]:
    """Return owned events to remove: slot overlaps + title retirements."""
    tz = ZoneInfo(config.timezone)
    today = from_date or datetime.now(tz).date()
    windows = expand_all_windows(
        config.series,
        tz=tz,
        from_date=today,
        weeks=config.conflict_lookahead_weeks,
    )

    time_min_dt = (
        min(w.start for w in windows)
        if windows
        else datetime.combine(today, datetime.min.time(), tzinfo=tz)
    )
    time_max_dt = (
        max(w.end for w in windows)
        if windows
        else time_min_dt + timedelta(weeks=config.conflict_lookahead_weeks)
    )
    # Widen slightly for title retirement of series that don't overlap slots
    # (old Sat afternoon / Sunday badminton, etc.).
    time_min = to_rfc3339(time_min_dt)
    time_max = to_rfc3339(
        max(time_max_dt, time_min_dt + timedelta(weeks=config.conflict_lookahead_weeks))
    )

    by_key: dict[tuple[str, str], ConflictCandidate] = {}
    calendars = _calendars_to_scan(service, config)

    # 1) Time-slot overlaps on owned calendars
    for calendar_id, calendar_name in calendars:
        if not windows:
            break
        events = cal.list_events(
            service,
            calendar_id,
            time_min=time_min,
            time_max=time_max,
            single_events=True,
        )
        for event in events:
            if event.get("status") == "cancelled":
                continue
            if managed_id_of(event):
                continue
            if not is_owned_by(event, config.owner_email):
                continue
            bounds = _event_bounds(event, tz)
            if not bounds:
                continue
            overlapping = [w for w in windows if w.overlaps(bounds[0], bounds[1])]
            if not overlapping:
                continue
            slot_ids = ", ".join(sorted({w.series_id for w in overlapping}))
            reason = (
                f"owned recurring series on {calendar_name!r} overlaps "
                f"managed slot(s) ({slot_ids})"
                if event.get("recurringEventId")
                else f"owned event on {calendar_name!r} overlaps "
                f"managed slot(s) ({slot_ids})"
            )
            _collect_conflict(
                by_key,
                event=event,
                calendar_id=calendar_id,
                calendar_name=calendar_name,
                overlapping=overlapping,
                reason=reason,
            )

    # 2) Title-based retirement (last-semester leftovers outside new slots)
    patterns = config.retire_title_contains
    if patterns:
        for calendar_id, calendar_name in calendars:
            seen_event_ids: set[str] = set()
            for pattern in patterns:
                page_token: str | None = None
                while True:
                    kwargs: dict[str, Any] = {
                        "calendarId": calendar_id,
                        "q": pattern,
                        "singleEvents": False,
                        "showDeleted": False,
                        "maxResults": 250,
                        "pageToken": page_token,
                    }
                    kwargs = {k: v for k, v in kwargs.items() if v is not None}
                    result = service.events().list(**kwargs).execute()
                    for event in result.get("items") or []:
                        eid = event.get("id")
                        if not eid or eid in seen_event_ids:
                            continue
                        seen_event_ids.add(eid)
                        if event.get("status") == "cancelled":
                            continue
                        if managed_id_of(event):
                            continue
                        if not is_owned_by(event, config.owner_email):
                            continue
                        if not _title_matches_retire(event.get("summary"), patterns):
                            continue
                        if not event.get("recurrence"):
                            bounds = _event_bounds(event, tz)
                            if bounds and bounds[1] < time_min_dt:
                                continue
                        matched = [
                            p
                            for p in patterns
                            if p in (event.get("summary") or "").lower()
                        ]
                        reason = (
                            f"retire title match {matched} on {calendar_name!r} "
                            f"(unmanaged leftover)"
                        )
                        _collect_conflict(
                            by_key,
                            event=event,
                            calendar_id=calendar_id,
                            calendar_name=calendar_name,
                            overlapping=[],
                            reason=reason,
                        )
                    page_token = result.get("nextPageToken")
                    if not page_token:
                        break

    return sorted(
        by_key.values(),
        key=lambda c: (
            c.calendar_name,
            (
                _parse_event_dt(c.event.get("start"), tz)
                or datetime.min.replace(tzinfo=tz)
            ),
            c.summary,
        ),
    )


def delete_conflicts(
    service,
    config: ScheduleConfig,
    conflicts: list[ConflictCandidate],
    *,
    dry_run: bool = True,
) -> list[str]:
    """Delete conflict candidates. Returns deleted (or would-delete) ids."""
    deleted: list[str] = []
    for conflict in conflicts:
        if dry_run:
            deleted.append(conflict.delete_id)
            continue
        cal.delete_event(service, conflict.calendar_id, conflict.delete_id)
        deleted.append(conflict.delete_id)
    return deleted
