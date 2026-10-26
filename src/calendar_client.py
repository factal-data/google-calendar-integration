"""Thin Google Calendar API helpers."""

from __future__ import annotations

from typing import Any, Iterator


MANAGED_PROP_KEY = "managedId"


def list_events(
    service,
    calendar_id: str,
    *,
    time_min: str,
    time_max: str,
    single_events: bool = True,
    private_extended_property: str | None = None,
    show_deleted: bool = False,
) -> list[dict[str, Any]]:
    """List events on a calendar within a time window."""
    events: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "calendarId": calendar_id,
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": single_events,
            "orderBy": "startTime" if single_events else None,
            "showDeleted": show_deleted,
            "maxResults": 2500,
            "pageToken": page_token,
        }
        # googleapiclient rejects None query params
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        if private_extended_property:
            kwargs["privateExtendedProperty"] = private_extended_property

        result = service.events().list(**kwargs).execute()
        events.extend(result.get("items") or [])
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return events


def iter_managed_events(
    service,
    calendar_id: str,
    *,
    time_min: str,
    time_max: str,
) -> Iterator[dict[str, Any]]:
    """Yield expanded instances that carry any managedId private property.

    The Calendar API can filter by exact privateExtendedProperty key=value,
    but not by key presence alone. We list the window and filter client-side.
    """
    for event in list_events(
        service,
        calendar_id,
        time_min=time_min,
        time_max=time_max,
        single_events=True,
    ):
        props = (event.get("extendedProperties") or {}).get("private") or {}
        if MANAGED_PROP_KEY in props:
            yield event


def find_managed_series_master(
    service,
    calendar_id: str,
    managed_id: str,
) -> dict[str, Any] | None:
    """Find the recurring/single event master tagged with managedId."""
    # Search without singleEvents so we get the series master, not instances.
    # time bounds are required by some clients; use a wide window via updatedMin omitted
    # and query by private property.
    page_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "calendarId": calendar_id,
            "privateExtendedProperty": f"{MANAGED_PROP_KEY}={managed_id}",
            "singleEvents": False,
            "maxResults": 50,
            "showDeleted": False,
            "pageToken": page_token,
        }
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        result = service.events().list(**kwargs).execute()
        items = result.get("items") or []
        for item in items:
            # Prefer the recurring master (has recurrence) or a single event.
            # Skip cancelled.
            if item.get("status") == "cancelled":
                continue
            return item
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return None


def insert_event(service, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return service.events().insert(calendarId=calendar_id, body=body).execute()


def update_event(
    service, calendar_id: str, event_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    return (
        service.events()
        .update(calendarId=calendar_id, eventId=event_id, body=body)
        .execute()
    )


def delete_event(service, calendar_id: str, event_id: str) -> None:
    service.events().delete(calendarId=calendar_id, eventId=event_id).execute()


def get_event(service, calendar_id: str, event_id: str) -> dict[str, Any]:
    return service.events().get(calendarId=calendar_id, eventId=event_id).execute()


def list_owned_calendars(service) -> list[dict[str, Any]]:
    """Return calendars the user owns (primary + secondary), excluding holidays/subscribed."""
    calendars: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        result = (
            service.calendarList()
            .list(pageToken=page_token, maxResults=250, minAccessRole="owner")
            .execute()
        )
        for item in result.get("items") or []:
            # Skip holiday / interest calendars even if somehow owner-ranked
            cid = item.get("id") or ""
            if "#holiday@" in cid or "@group.v.calendar.google.com" in cid:
                continue
            calendars.append(item)
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return calendars
