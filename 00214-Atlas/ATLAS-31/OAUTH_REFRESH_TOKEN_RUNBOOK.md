# Tripleseat OAuth 2.0 — Initial `refresh_token` Runbook

Use this for **every** new Tripleseat API client (Pinstripes / OVG / Atlas / next).
The gather script never opens a browser. Someone must complete Authorization Code **once**, put `refresh_token` in deploy `config.json`, then the client refreshes access tokens forever after.

Official docs:

- [API Authentication](https://support.tripleseat.com/hc/en-us/articles/19394408627479-API-Authentication)
- [OAuth 1.0 → 2.0 Migration Guide](https://support.tripleseat.com/hc/en-us/articles/35211389645079-OAuth-1-0-to-OAuth-2-0-Migration-Guide-for-Tripleseat-API)

Repo: `tripleseat-api-client` — Atlas uses **`main`** (Pinstripes baseline). OVG-specific flatten work lives on branch **`ovg`**.

---

## What we do NOT use

| Credential | Use for Mirus gather? |
|---|---|
| **Public API Key** | **No.** Ignore if the client sends one. Not part of OAuth 2.0. |
| OAuth 1.0 consumer key/secret | **No.** Sunset path; we are OAuth 2.0 only. |
| `client_credentials` grant alone | **No** for Locations/Events. Those need user context. You may get a token, then hit `401` / `Token resource owner not found` on data endpoints. |

We **do** use OAuth 2.0 **Authorization Code** once → `refresh_token` → ongoing `grant_type=refresh_token`.

---

## Who provides what

### Client (or Tripleseat admin on their account)

1. Create (or edit) an **OAuth 2.0 Client Application** in Tripleseat:  
   **Settings → Tripleseat API & Webhooks → OAuth 2.0 Client Applications → + New Application**
2. Set **Redirect URL** to Postman’s callback (required; localhost is a pain):  
   `https://oauth.pstmn.io/v1/callback`
3. Send Mirus:
   - **UID** / Application ID / Consumer ID → our `client_id`
   - **Secret** → our `client_secret`
4. Provide a **login that can approve the app** (email/password or SSO user with permission to authorize the integration). Mirus cannot finish the Postman browser step without this.
5. Confirm Locations / Events API access is enabled for that app/account if Tripleseat gates scopes separately.

### Mirus

1. Put `client_id` + `client_secret` into deploy `config.json` (leave `refresh_token` empty until step 2).
2. Run **one-time Postman Authorization Code** flow (below) while logged in as the approving user.
3. Copy `refresh_token` into deploy `config.json` on the ETL server.
4. Smoke-test: Locations gather, then a small Events window.
5. Ongoing: `api_client.py` refreshes access tokens; if Tripleseat rotates `refresh_token`, the script writes the new value back to `config.json`.

---

## End-to-end flow

```text
Client registers OAuth2 app
        │  Redirect URL = https://oauth.pstmn.io/v1/callback
        │  Sends UID + Secret to Mirus
        ▼
Mirus config.json: client_id + client_secret
        ▼
Postman Authorization Code (once)
        │  Browser login as approving user → Allow
        │  Postman receives code → exchanges for tokens
        ▼
Copy refresh_token → deploy config.json on ETL server
        ▼
api_client.py: refresh_token → access_token → Locations/Events
```

### Postman setup (Authorization Code)

1. Create a request (any) against Tripleseat, or use Authorization tab on a blank request.
2. **Type:** OAuth 2.0  
3. **Configure New Token:**
   - **Grant type:** Authorization Code  
   - **Callback URL:** `https://oauth.pstmn.io/v1/callback` (must match the app)  
   - **Auth URL:** `https://login.tripleseat.com/oauth2/authorize`  
   - **Access Token URL:** `https://api.tripleseat.com/oauth2/token`  
   - **Client ID:** UID / Application ID from client  
   - **Client Secret:** Secret from client  
   - **Scope:** `read write` (or whatever Tripleseat shows for the app)  
   - **Client Authentication:** Send client credentials in body (typical)
4. Click **Get New Access Token**.
5. Browser opens Tripleseat login → sign in with the **approving user** → Allow.
6. Postman shows token dialog. Copy **Refresh Token** (and optionally Access Token for a quick GET test).
7. Paste into server `config.json`:

```json
{
  "base_url": "https://api.tripleseat.com",
  "client_id": "<UID / Application ID>",
  "client_secret": "<Secret>",
  "refresh_token": "<from Postman>",
  "endpoints": {
    "Locations": {
      "url_ending": "/api/v1/locations",
      "i_table": "i_TripleseatAPI_Locations"
    },
    "Events": {
      "url_ending": "/api/v1/events",
      "i_table": "i_TripleseatAPI_Events"
    }
  }
}
```

8. Smoke test from the client folder (example):

```bash
python api_client.py --servername wdsc3 --database <Client>ETL --endpoint Locations --loglevel INFO
```

### Why Postman / that callback

Pinstripes hit this first ([PINSTRIPES-45](https://mirusdev.atlassian.net/browse/PINSTRIPES-45)): Tripleseat often rejects or breaks on `http://localhost:8080/`. Their support recommended Postman. Registering `https://oauth.pstmn.io/v1/callback` unblocked Authorization Code. After that, **no callback is needed again** — only `refresh_token`.

---

## Checklist for the next ticket (copy/paste)

- [ ] Client created OAuth 2.0 app (not Public API Key)
- [ ] Redirect URL = `https://oauth.pstmn.io/v1/callback`
- [ ] Mirus has UID (`client_id`) + Secret (`client_secret`)
- [ ] Approving-user login available for the Postman browser step
- [ ] Postman Authorization Code completed; `refresh_token` saved to deploy `config.json`
- [ ] Locations smoke test OK
- [ ] Sample Events pull OK
- [ ] Public API Key filed under “ignore / not used”

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Token resource owner not found` on Locations/Events | Token from `client_credentials` or auth never completed by a real user. Redo Authorization Code; use that `refresh_token`. |
| Refresh exchange 400/401 | Wrong `client_id`/`client_secret`, or refresh revoked. Re-run Postman once. |
| Browser login fails / no permission | Need a Tripleseat user that can authorize the app (admin). |
| Redirect mismatch | App Redirect URL must **exactly** match Postman callback. |
| Client only sent “Public API Key” | Ask again for OAuth 2.0 **UID** + **Secret** from Client Applications. |

---

## Atlas note (`ATLAS-31`)

- Use repo branch **`main`**.
- Client-provided **Application’s ID** maps to `client_id` (UID).
- Client-provided **Secret** maps to `client_secret`.
- Client-provided **Public API Key** is unused.
- Auth is **blocked** until someone completes Postman once with an approving Atlas Tripleseat login and we store `refresh_token`.
