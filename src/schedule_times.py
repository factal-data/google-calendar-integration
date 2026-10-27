"""Compute schedule windows and first-occurrence datetimes in a timezone."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import BYDAY_TO_WEEKDAY, Series


def add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


@dataclass(frozen=True)
class TimeWindow:
    """An absolute [start, end) window in the configured timezone."""

    start: datetime
    end: datetime
    series_id: str

    def overlaps(self, other_start: datetime, other_end: datetime) -> bool:
        return self.start < other_end and other_start < self.end


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def first_occurrence_on_or_after(
    series: Series, on_or_after: date, tz: ZoneInfo
) -> datetime:
    """Return the timezone-aware start datetime of the first matching day."""
    start_t = parse_hhmm(series.start)
    if series.freq == "MONTHLY" and series.starts_on:
        cursor = series.starts_on
        while cursor < on_or_after:
            cursor = add_months(cursor, series.interval)
        return datetime.combine(cursor, start_t, tzinfo=tz)
    target_weekdays = {BYDAY_TO_WEEKDAY[d] for d in series.days}
    cursor = on_or_after
    for _ in range(8):
        if cursor.weekday() in target_weekdays:
            return datetime.combine(cursor, start_t, tzinfo=tz)
        cursor += timedelta(days=1)
    raise RuntimeError(f"Could not find first occurrence for series {series.id}")


def series_end_datetime(series: Series, start_dt: datetime) -> datetime:
    start_t = parse_hhmm(series.start)
    end_t = parse_hhmm(series.end)
    end_dt = datetime.combine(start_dt.date(), end_t, tzinfo=start_dt.tzinfo)
    if end_t <= start_t:
        # Spans midnight (not expected for badminton, but keep correct).
        end_dt += timedelta(days=1)
    return end_dt


def expand_series_windows(
    series: Series,
    *,
    tz: ZoneInfo,
    from_date: date,
    weeks: int,
) -> list[TimeWindow]:
    """Expand a weekly series into concrete windows over N weeks."""
    if series.freq != "WEEKLY":
        start_t = parse_hhmm(series.start)
        end_t = parse_hhmm(series.end)
        first = first_occurrence_on_or_after(series, from_date, tz)
        end_exclusive = from_date + timedelta(weeks=weeks)
        windows: list[TimeWindow] = []
        cursor = first.date()
        while cursor < end_exclusive:
            if cursor >= from_date:
                start_dt = datetime.combine(cursor, start_t, tzinfo=tz)
                end_dt = datetime.combine(cursor, end_t, tzinfo=tz)
                if end_t <= start_t:
                    end_dt += timedelta(days=1)
                windows.append(TimeWindow(start=start_dt, end=end_dt, series_id=series.id))
            cursor = add_months(cursor, series.interval)
        return windows
    start_t = parse_hhmm(series.start)
    end_t = parse_hhmm(series.end)
    target_weekdays = {BYDAY_TO_WEEKDAY[d] for d in series.days}
    windows: list[TimeWindow] = []

    end_exclusive = from_date + timedelta(weeks=weeks)
    cursor = from_date
    while cursor < end_exclusive:
        if cursor.weekday() in target_weekdays:
            start_dt = datetime.combine(cursor, start_t, tzinfo=tz)
            end_dt = datetime.combine(cursor, end_t, tzinfo=tz)
            if end_t <= start_t:
                end_dt += timedelta(days=1)
            windows.append(TimeWindow(start=start_dt, end=end_dt, series_id=series.id))
        cursor += timedelta(days=1)
    return windows


def expand_all_windows(
    series_list: tuple[Series, ...] | list[Series],
    *,
    tz: ZoneInfo,
    from_date: date,
    weeks: int,
) -> list[TimeWindow]:
    """Expand every series; a series with its own timezone is expanded in that zone."""
    windows: list[TimeWindow] = []
    for series in series_list:
        series_tz = ZoneInfo(series.timezone) if series.timezone else tz
        windows.extend(
            expand_series_windows(series, tz=series_tz, from_date=from_date, weeks=weeks)
        )
    return windows


def local_slot_segments(
    series: Series,
    *,
    series_tz: ZoneInfo,
    home_tz: ZoneInfo,
    from_date: date,
    weeks: int = 53,
) -> list[tuple[date, str]]:
    """Return (first local date, "Mon,Sat 18:00-19:00 CDT") each time the home-tz slot changes.

    A series in another zone (Vietnam Zoom teacher) shifts in home time when
    either side changes DST; this shows when.
    """
    windows = expand_series_windows(
        series, tz=series_tz, from_date=from_date, weeks=weeks
    )
    segments: list[tuple[date, str, set[int]]] = []
    for window in windows:
        start = window.start.astimezone(home_tz)
        end = window.end.astimezone(home_tz)
        hours = f"{start:%H:%M}-{end:%H:%M} {start.tzname()}"
        if segments and segments[-1][1] == hours:
            segments[-1][2].add(start.weekday())
        else:
            segments.append((start.date(), hours, {start.weekday()}))
    day_names = calendar.day_abbr
    return [
        (first, f"{','.join(day_names[d] for d in sorted(days))} {hours}")
        for first, hours, days in segments
    ]


def to_rfc3339(dt: datetime) -> str:
    """Format aware datetime for Calendar API timeMin/timeMax."""
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.isoformat()
