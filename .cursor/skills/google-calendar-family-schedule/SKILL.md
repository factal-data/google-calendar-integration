---
name: google-calendar-family-schedule
description: >-
  Edit and sync Moses family Google Calendar schedules (Huy/Vy/Hoa badminton,
  kumon, piano) via YAML + CLI. Use when the user mentions calendar sync,
  schedules.yaml, Huy/Vy/Hoa/Elliott/Elliana, badminton times, Angela sharing,
  dry-run, or Google Calendar family events.
---

# Google Calendar family schedule

## Source of truth

Read and edit `config/schedules.yaml` only. Current people/calendars/series live there. Do not hardcode times in chat from memory after edits.

Owner: `moses.hoavo@gmail.com`. Share: `angelaphuoc@gmail.com` role `reader`.

## Standard change workflow

1. Edit `config/schedules.yaml`
2. `source .venv/Scripts/activate && python -m src.cli dry-run`
3. If user wants it live (or said sync/apply/update the calendar): `python -m src.cli sync`
4. Confirm Angela shares show `CREATE` or `UNCHANGED` on Huy/Vy/Hoa (not missing)
5. If user questions what’s on the calendar: `python -m src.cli list` and/or query Huy calendar for `badminton` instances via API

## Add vs replace (common mistake)

- Changing `start`/`end`/`summary` on an existing series **replaces** that series (same `id` / `managedId`)
- To have **two** Saturday slots, add a **second series** with a new `id` (e.g. `huy-badminton-saturday-afternoon`)
- Until-done reminders (garage lube, furnace filter, anything with a checkbox) are not YAML series. They are Google Tasks in the **House** list, managed by `apps-script/house-assistant/` (see below). Do not re-add `hoa-garage-door-lube`.
- After sync, verify with `list` or an API query if the user says they don’t see an “extra” event

## Series `id` / rename rules

- `managedId` = series `id`. Keep ids stable when only renaming titles/people so sync **updates** instead of orphaning
- Person keys (`huy`/`vy`/`hoa`) and `calendar_name` can change; list old calendar names under `legacy_combined_calendar_names` so migrate deletes old masters then removes empty calendars
- Prior names: Elliott→Huy, Elliana→Vy, Moses→Hoa (account email still moses.hoavo@gmail.com)
- Legacy list currently includes old combined + `Elliott` / `Elliana` / `Moses`

## Architecture (don’t re-discover)

- Per-person calendars for Angela’s mobile colors
- `prepare_workspace`: ensure calendars → color → ACL shares → migrate managed off other owned calendars → delete empty legacy names
- Sync upserts by `managedId` on the person calendar
- Conflict cleanup: owned unmanaged overlaps + `retire_title_contains` (skips managed events)
- Dry-run with new calendar names uses `pending:` ids; migration of old calendars only fully runs on real sync after create

## Auth

- Access tokens (~1h) refresh from `token.json` automatically. No login per sync.
- OAuth **Testing** apps: Google kills `refresh_token` after ~7 days (`invalid_grant`). `dry-run`/`sync` then print a Moses-profile URL instead of crashing.
- Interactive login default is paste-URL (`python -m src.cli auth --force`), not auto-browser (Work profile).
- Status: `python -m src.cli auth --status`
- Lasting fix: publish the Cloud OAuth consent app to **Production**. Personal Gmail cannot use a service account for this calendar.

## Angela

- New calendars need ACL share (sync does this from `shares:`)
- She must accept invite and enable Huy/Vy/Hoa; set calendar colors in her app if wrong

## House assistant (Apps Script, not this CLI)

`apps-script/house-assistant/` (deploy: `cd apps-script/house-assistant && clasp push`, logged in as moses.hoavo@gmail.com). Hourly trigger runs `tick()`.

- **House** Tasks list: each open task becomes a single event at its due time, then a daily nag series from the due date until checked off. Done rolls to a new task at completion + `repeat`, snapped to `snap:` weekday. Deleting the task removes its events.
- Fields go one per line in task notes (or `[a: b; c: d]` in the title): `repeat: 6 months|none`, `nag: daily|off`, `calendar: Hoa`, `time: 09:00`, `snap: saturday`.
- Events are tagged `managedId:until-done:<taskId>` in the description. Event/series ids live in script property `STATE`, so never scan long date ranges (CalendarApp rate limit).
- **House log inbox** list: each task is appended to Drive `House/House Log` (Doc) and `House/house-log.md`, then deleted. Folder name override: script property `HOUSE_FOLDER`.
- Gemini can only write to the default Tasks list (My List). `tick()` first moves open default-list tasks titled `House note: X` to the inbox (adding `date:`) and `House: X` / `House reminder: X` to House, prefix stripped. `Google Task: X` / `Task nhà: X` / `House task: X` goes to House if it has a due date, else the inbox. Gemini only connects Tasks to a chat when the user's message names the app, so the user starts with "Google Task" ("Task nhà" was not recognized).
- Voice front end: Gemini **House** Gem, instructions in `apps-script/house-assistant/gemini-gem.md`.
- **Photos:** Pixel Share > Drive > `House/Photos/Inbox`. `tick()` → Gemini API
  (`GEMINI_API_KEY`) extracts brand/model/size/serial/price, files under
  `Photos/<Category>`, appends **House Items** + a House Log bullet with the
  photo link. After 3 failures: `Photos/Needs review`. No new Gem.

Full design, task format, deploy and recreate steps: `apps-script/house-assistant/README.md`. Read it before adding features.

To add or change a reminder, edit the Task (phone or tasks.google.com), not YAML.

## Out of scope

This project is not Mirus ETL/Jira/Bitbucket. Ignore ATLAS/Tripleseat unless user explicitly switches context.
