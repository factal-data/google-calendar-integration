# Google Calendar Family Schedule CLI

Personal tool for `moses.hoavo@gmail.com` (America/Chicago). Prefer this CLI over ad-hoc Calendar API scripts.

For schedule edits / sync / rename / Angela sharing / Vietnam Zoom class times: use the skill **google-calendar-family-schedule** (`.claude/skills/google-calendar-family-schedule/SKILL.md`).

## Setup

- Venv: `.venv` → `source .venv/Scripts/activate`
- OAuth: `credentials.json` + `token.json` (gitignored). Default login is paste-URL (`python -m src.cli auth --force`) in Moses Chrome, not Work. `dry-run`/`sync` auto-start that flow on `invalid_grant`.
- Testing-mode refresh tokens die ~every 7 days. Lasting fix: Cloud Console → OAuth consent → **Publish app** (Production). Then refresh lasts until revoked.
- Windows needs the `tzdata` package (in `requirements.txt`) for `zoneinfo`.

## Commands

```bash
python -m src.cli dry-run
python -m src.cli sync
python -m src.cli list
```

Always `dry-run` before `sync` unless the user says to apply. After time/title changes the user expects live calendar updates → dry-run then sync.

## People (Angela colors)

One calendar per person (mobile colors by calendar, not event `colorId`). Shared reader: `angelaphuoc@gmail.com`.

| Key | Calendar | Color |
|-----|----------|-------|
| `huy` | Huy | red (Tomato) |
| `vy` | Vy | yellow (Banana) |
| `hoa` | Hoa | green (Pistachio/Basil) |

Source of truth: [config/schedules.yaml](config/schedules.yaml). Do not invent schedule times from memory.

## Vietnam Zoom classes (DST)

Classes with a teacher in Vietnam set `timezone: Asia/Ho_Chi_Minh` on the series; their `days`/`start`/`end` are **Vietnam time** (next morning). Vietnam has no DST, so in Chicago they are 1 hour earlier during CST (Nov–Mar) than during CDT. Google expands the RRULE in the series zone, so no YAML edit is needed at DST changes. Details in the skill.

## Until-done reminders and house log

Not YAML. Repeat-until-checked reminders (garage lube, filters) are Google Tasks in the **House** list; house notes go to **House log inbox**. Photos of parts/receipts go to Drive `House/Photos/Inbox`. All are handled by the Apps Script in `apps-script/house-assistant/` (deploy with `clasp push`). Details in the skill.

## Do not

- Commit secrets (`credentials.json`, `token.json`, `client_secret_*.json`)
- Put all family events on one shared calendar if Angela needs distinct mobile colors
- Confuse this repo with Mirus/Jira/Bitbucket work
