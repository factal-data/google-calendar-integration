# Google Calendar Family Schedule CLI

Sync declarative family schedules from YAML to your Google Calendar. First schedule: Elliott's weekly badminton (blue, 30-minute reminder) on `moses.hoavo@gmail.com`.

## Features

- **Declarative schedules** in `config/schedules.yaml` (add a daughter's classes later without code changes)
- **Color per person** on the primary calendar (Elliott = Blueberry / blue)
- **Weekly RRULE series** with popup reminders
- **Idempotent sync** via private `managedId` so re-runs update instead of duplicating
- **Conflict cleanup** deletes *your* overlapping events on the primary calendar only (not other calendars that appear in the UI)
- **dry-run** before mutating

## One-time Google Cloud setup

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create (or pick) a project.
2. Enable **Google Calendar API** (APIs & Services → Library).
3. Configure the **OAuth consent screen**:
   - User type: **External**
   - Add app name / support email
   - Add yourself (`moses.hoavo@gmail.com`) under **Test users** while the app is in Testing
4. Create credentials: **APIs & Services → Credentials → Create credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Download the JSON and save it as `credentials.json` in this project root
5. Install dependencies and authenticate (see below)

## Install

```bash
cd GoogleCalendarIntegration
python -m venv .venv
# Windows Git Bash / macOS / Linux:
source .venv/Scripts/activate   # or: source .venv/bin/activate
pip install -r requirements.txt
```

## Auth token persistence

- `python -m src.cli auth` writes **`token.json`** (paste the URL in the Moses Chrome profile; default browser is usually Work).
- Later commands reuse and refresh the access token (~1 hour). You do not log in every sync.
- If refresh fails (`invalid_grant`), `dry-run` / `sync` start the paste-URL login instead of dying.
- **Testing** OAuth apps: Google expires refresh tokens after ~7 days. Publish the consent screen to **Production** (Cloud Console → APIs & Services → OAuth consent screen → Publish app) so the token lasts until you revoke it. Verification is not required for a personal desktop app used only by test/your account.
- Status: `python -m src.cli auth --status`

## Commands

```bash
# Paste-URL login in Moses Chrome (default); writes token.json
python -m src.cli auth
python -m src.cli auth --status

# Preview what would be deleted / created / updated
python -m src.cli dry-run

# Remove owned conflicts in schedule slots, then upsert series
python -m src.cli sync

# Show managed instances in the look-ahead window
python -m src.cli list
```

Optional: `python -m src.cli --config path/to/schedules.yaml sync`

## Sharing with Angela

Family events are on **three calendars** (Elliott / Elliana / Moses), each shared with Angela. Google Calendar mobile apps color by calendar, not per-event, so this is how she sees blue / pink / green separately.

```yaml
shares:
  - email: angelaphuoc@gmail.com
    role: reader
```

Angela should enable all three calendars. If colors look wrong on her phone, set each calendar’s color in her Google Calendar app.

## Current schedule (`config/schedules.yaml`)

| Series | Days | Time (America/Chicago) | Color | Reminder |
|--------|------|------------------------|-------|----------|
| Elliott Badminton (weekday) | Tue, Thu | 6:00–8:00 PM | blue (`9`) | 30 min popup |
| Elliott Badminton (Saturday) | Sat | 10:30 AM–12:30 PM | blue (`9`) | 30 min popup |

## Adding another person later

```yaml
people:
  elliott:
    color_id: "9"
    reminder_minutes: 30
  daughter:
    color_id: "5"   # Banana (example)
    reminder_minutes: 30

series:
  # ... existing elliott series ...
  - id: daughter-swim-monday
    person: daughter
    summary: Daughter Swim
    days: [MO]
    start: "16:00"
    end: "17:00"
```

Then run `python -m src.cli dry-run` and `python -m src.cli sync`.

## Classes in another timezone (Zoom with a teacher in Vietnam)

Set `timezone` on the series and write `days`/`start`/`end` in **that** zone. Google repeats the event in that zone and shows it in local time, so US DST changes move it automatically (Vietnam has no DST: Chicago is VN − 12h in CDT, VN − 13h in CST).

```yaml
  - id: hoa-piano
    person: hoa
    summary: Hoa Piano
    timezone: Asia/Ho_Chi_Minh
    days: [WE]      # = Chicago Tue night
    start: "09:00"  # 9:00 PM CDT / 8:00 PM CST
    end: "10:00"
```

`dry-run` prints the Chicago slot and the dates it shifts.

## Conflict cleanup rules

On `sync` / `dry-run`, the tool looks ahead (`conflict_lookahead_weeks`, default 8) at slots defined in YAML and lists events on:

- your **primary** calendar, and
- any **other calendars you own** (e.g. a secondary calendar named `Calendar` or `Family`) when `conflict_scan_owned_calendars: true`

It flags overlaps where the event was **created by you** (`owner_email`) and is **not** already tagged with our `managedId`. Holidays and subscribed calendars you do not own are never touched.

Additionally, `retire_title_contains` deletes owned unmanaged series whose titles match (e.g. old `Elliott Badminton` / `Kumon Elliott` leftovers that do not sit in the new time slots).

Those matches are deleted (recurring master when instances belong to an old series). Set `conflict_scan_owned_calendars: false` to limit cleanup to `calendar_id` only.

## Project layout

```
config/schedules.yaml   # source of truth
src/auth.py             # OAuth + token
src/calendar_client.py  # Calendar API helpers
src/conflicts.py        # owned overlap detection/delete
src/sync.py             # managed series upsert
src/cli.py              # commands
credentials.json        # you provide (gitignored)
token.json              # created by `auth` (gitignored)
```
