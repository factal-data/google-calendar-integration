"""Load and validate schedule config from YAML."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "schedules.yaml"

# RRULE BYDAY tokens -> Python weekday (Mon=0 ... Sun=6)
BYDAY_TO_WEEKDAY = {
    "MO": 0,
    "TU": 1,
    "WE": 2,
    "TH": 3,
    "FR": 4,
    "SA": 5,
    "SU": 6,
}

# Fallback: event colorId -> calendarList colorId (different palettes).
EVENT_TO_CALENDAR_COLOR = {
    "11": "3",  # Tomato (red)
    "5": "12",  # Banana (yellow)
    "10": "9",  # Basil event -> Pistachio calendar (brighter green)
    "9": "16",  # Blueberry (legacy)
    "4": "2",  # Flamingo (legacy)
    "1": "17",  # Lavender
    "7": "14",  # Peacock
}


@dataclass(frozen=True)
class Person:
    key: str
    calendar_name: str
    color_id: str
    calendar_color_id: str
    reminder_minutes: int


@dataclass(frozen=True)
class Series:
    id: str
    person: str
    summary: str
    days: tuple[str, ...]
    start: str  # HH:MM
    end: str  # HH:MM
    location: str | None = None
    description: str | None = None
    freq: str = "WEEKLY"  # WEEKLY | MONTHLY
    interval: int = 1
    starts_on: date | None = None  # MONTHLY anchor; WEEKLY: no instances before
    until: date | None = None  # last day with an instance (RRULE UNTIL), inclusive
    # IANA zone the days/start/end are written in (e.g. Asia/Ho_Chi_Minh for a
    # Zoom teacher in Vietnam). None = config timezone. Google expands the RRULE
    # in this zone, so the local time shifts by itself across US DST changes.
    timezone: str | None = None


@dataclass(frozen=True)
class ShareRule:
    email: str
    role: str  # reader | writer | owner


@dataclass(frozen=True)
class ScheduleConfig:
    timezone: str
    owner_email: str
    shares: tuple[ShareRule, ...]
    legacy_combined_calendar_names: tuple[str, ...]
    conflict_lookahead_weeks: int
    conflict_scan_owned_calendars: bool
    retire_title_contains: tuple[str, ...]
    people: dict[str, Person]
    series: tuple[Series, ...]
    path: Path

    def person_for(self, series: Series) -> Person:
        try:
            return self.people[series.person]
        except KeyError as exc:
            raise KeyError(
                f"Series {series.id!r} references unknown person {series.person!r}"
            ) from exc

    def timezone_for(self, series: Series) -> str:
        return series.timezone or self.timezone


def _require(mapping: dict[str, Any], key: str, path: Path) -> Any:
    if key not in mapping:
        raise ValueError(f"Missing required key {key!r} in {path}")
    return mapping[key]


def _parse_time(value: str, field: str, series_id: str) -> str:
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"Series {series_id!r}: {field} must be HH:MM, got {value!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Series {series_id!r}: invalid {field} {value!r}")
    return f"{hour:02d}:{minute:02d}"


def load_config(path: Path | None = None) -> ScheduleConfig:
    config_path = path or DEFAULT_CONFIG_PATH
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Config must be a mapping: {config_path}")

    timezone = str(_require(raw, "timezone", config_path))
    owner_email = str(_require(raw, "owner_email", config_path)).strip().lower()
    lookahead = int(raw.get("conflict_lookahead_weeks") or 8)
    scan_owned = bool(raw.get("conflict_scan_owned_calendars", True))
    retire_raw = raw.get("retire_title_contains") or []
    if not isinstance(retire_raw, list):
        raise ValueError(f"retire_title_contains must be a list in {config_path}")
    retire_titles = tuple(str(x).strip().lower() for x in retire_raw if str(x).strip())

    legacy_raw = raw.get("legacy_combined_calendar_names") or []
    if not isinstance(legacy_raw, list):
        raise ValueError(
            f"legacy_combined_calendar_names must be a list in {config_path}"
        )
    legacy_names = tuple(str(x).strip() for x in legacy_raw if str(x).strip())

    shares_raw = raw.get("shares") or []
    if not isinstance(shares_raw, list):
        raise ValueError(f"shares must be a list in {config_path}")
    allowed_roles = {"reader", "writer", "owner"}
    shares: list[ShareRule] = []
    for item in shares_raw:
        if not isinstance(item, dict):
            raise ValueError("Each shares entry must be a mapping")
        email = str(_require(item, "email", config_path)).strip().lower()
        role = str(item.get("role") or "reader").strip().lower()
        if role not in allowed_roles:
            raise ValueError(
                f"shares role must be one of {sorted(allowed_roles)}, got {role!r}"
            )
        shares.append(ShareRule(email=email, role=role))

    people_raw = _require(raw, "people", config_path)
    if not isinstance(people_raw, dict) or not people_raw:
        raise ValueError(f"people must be a non-empty mapping in {config_path}")

    people: dict[str, Person] = {}
    for key, pdata in people_raw.items():
        if not isinstance(pdata, dict):
            raise ValueError(f"people.{key} must be a mapping")
        color_id = str(_require(pdata, "color_id", config_path))
        cal_name = str(
            pdata.get("calendar_name") or str(key).capitalize()
        ).strip()
        cal_color = str(
            pdata.get("calendar_color_id")
            or EVENT_TO_CALENDAR_COLOR.get(color_id)
            or "15"
        )
        people[str(key)] = Person(
            key=str(key),
            calendar_name=cal_name,
            color_id=color_id,
            calendar_color_id=cal_color,
            reminder_minutes=int(pdata.get("reminder_minutes") or 30),
        )

    series_raw = _require(raw, "series", config_path)
    if not isinstance(series_raw, list) or not series_raw:
        raise ValueError(f"series must be a non-empty list in {config_path}")

    series_list: list[Series] = []
    seen_ids: set[str] = set()
    for item in series_raw:
        if not isinstance(item, dict):
            raise ValueError("Each series entry must be a mapping")
        sid = str(_require(item, "id", config_path))
        if sid in seen_ids:
            raise ValueError(f"Duplicate series id {sid!r}")
        seen_ids.add(sid)

        freq = str(item.get("freq") or "WEEKLY").strip().upper()
        if freq not in {"WEEKLY", "MONTHLY"}:
            raise ValueError(f"Series {sid!r}: freq must be WEEKLY or MONTHLY")
        interval = int(item.get("interval") or 1)
        if interval < 1:
            raise ValueError(f"Series {sid!r}: interval must be >= 1")
        starts_on = None
        if item.get("starts_on"):
            starts_on = date.fromisoformat(str(item["starts_on"]))
        until = None
        if item.get("until"):
            until = date.fromisoformat(str(item["until"]))
            if starts_on and until < starts_on:
                raise ValueError(f"Series {sid!r}: until is before starts_on")

        days_raw = item.get("days")
        if days_raw is None:
            days = tuple()
        else:
            days = tuple(str(d).upper() for d in days_raw)
        if freq == "WEEKLY":
            if not days:
                raise ValueError(f"Series {sid!r}: days must not be empty")
            for day in days:
                if day not in BYDAY_TO_WEEKDAY:
                    raise ValueError(
                        f"Series {sid!r}: invalid day {day!r}; "
                        f"expected one of {sorted(BYDAY_TO_WEEKDAY)}"
                    )
        elif starts_on is None:
            raise ValueError(f"Series {sid!r}: MONTHLY freq requires starts_on: YYYY-MM-DD")

        person = str(_require(item, "person", config_path))
        if person not in people:
            raise ValueError(f"Series {sid!r}: unknown person {person!r}")

        series_list.append(
            Series(
                id=sid,
                person=person,
                summary=str(_require(item, "summary", config_path)),
                days=days,
                start=_parse_time(str(_require(item, "start", config_path)), "start", sid),
                end=_parse_time(str(_require(item, "end", config_path)), "end", sid),
                location=(str(item["location"]) if item.get("location") else None),
                description=(
                    str(item["description"]) if item.get("description") else None
                ),
                freq=freq,
                interval=interval,
                starts_on=starts_on,
                until=until,
                timezone=(
                    str(item["timezone"]).strip() if item.get("timezone") else None
                ),
            )
        )

    return ScheduleConfig(
        timezone=timezone,
        owner_email=owner_email,
        shares=tuple(shares),
        legacy_combined_calendar_names=legacy_names,
        conflict_lookahead_weeks=lookahead,
        conflict_scan_owned_calendars=scan_owned,
        retire_title_contains=retire_titles,
        people=people,
        series=tuple(series_list),
        path=config_path,
    )
