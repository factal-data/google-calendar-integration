# ATLAS-31 — Tripleseat auth status

## Ticket ask (Joe, 2026-08-12)

1. Initial Atlas auth setup
2. Short team runbook for first `refresh_token` (see `OAUTH_REFRESH_TOKEN_RUNBOOK.md`)

## Credentials interpretation (from ticket description)

| Client label | Maps to | Used? |
|---|---|---|
| Application's ID `Kd79aSt1bSvOjNPdBRDe3toRvnpwyEL-2XnX2V5sIew` | OAuth2 `client_id` / UID | Yes |
| Secret `Czn2oUbcSE_Hv-5ElSRUqB8m8ks7lFryaRogahiDxAg` | OAuth2 `client_secret` | Yes |
| Public API Key `77eab266…` | n/a | **No** |

Jet can update the ticket description UID field to the Application's ID once confirmed.

## Repo

- Bitbucket: `tripleseat-api-client`
- Atlas baseline: **`main`** (Pinstripes)
- OVG deltas: branch **`ovg`** (do not use for Atlas unless Joe says otherwise)

## Local scaffold

- `config.atlas.example.json` — placeholders only
- `config.atlas.json` — Atlas client_id/secret filled; `refresh_token` empty until Postman (gitignored pattern: keep secrets out of git)

## Blocker

Cannot finish Locations/Events auth without a one-time Authorization Code login by an Atlas Tripleseat user that can approve the app, using redirect:

`https://oauth.pstmn.io/v1/callback`

Ask Jet/client for that login (or a shared screen / call) before claiming “Authentication successful.”

## Related tickets

- [PINSTRIPES-45](https://mirusdev.atlassian.net/browse/PINSTRIPES-45) — established Postman callback pattern
- [OVG-9](https://mirusdev.atlassian.net/browse/OVG-9) — same OAuth2 pattern; ETL procs from PinstripesETL
