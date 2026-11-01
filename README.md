# WhatsApp intake bot

FastAPI service that runs a WhatsApp (Meta Cloud API) chat collecting an applicant's name, email, loan amount, tenure and documents, zips the documents and stores everything in its own Postgres. LCNC pulls the result with `GET /session` and `GET /zip`. It calls no Uncia system. Test data only.

## API (all under `/api/whatsapp`)
| Method | Path | Notes |
|---|---|---|
| POST | `/start` | `{"mobile","role"?}` role `A` (default) or `CA`; creates/resets the session in step CONSENT and sends the template (`uncia_loan_offer_intro`, Yes/No buttons) |
| GET | `/webhook` | Meta verification: returns `hub.challenge` as text/plain when `hub.verify_token` matches, else 403 |
| POST | `/webhook` | Meta messages; `X-Hub-Signature-256` checked (401 if bad), answers 200 at once, processed in the background, duplicate message ids ignored |
| GET | `/session?mobile=&role=` | `{ok, status, name, email, loan_amount, tenure_months, zip_url, file_count}`; status `COLLECTING`, `DONE` or `OPTED_OUT`; `{ok:false}` if none |
| GET | `/zip?mobile=&role=` | the zip, 404 until DONE |
| GET | `/health` | liveness, always 200 (Koyeb probe + uptime pinger) |
| GET | `/health/ready` | database, config, Meta API; 200 or 503 with a reason per check |

`/health` and `/health/ready` also answer without the `/api/whatsapp` prefix. CORS allows only `ALLOWED_ORIGIN`.

## Consent step
`/start` puts the session in CONSENT, so the customer never has to say Hi first: the next inbound message from that number is the answer, typed or a quick-reply button (message type `button` or `interactive`, text or payload).
- YES (any case) -> "Great! What is your full name?" and the normal flow (email, loan amount, tenure, documents, DONE).
- NO -> "No problem, you won't hear from us again about this offer. Thank you!", status `OPTED_OUT`. Any later message from that number gets only "You have opted out. Contact us if you change your mind." (RESTART and Hi do not reopen it). Only a new `POST /start` reopens it.
- Anything else -> "Please reply YES to continue or NO to opt out." and stays in CONSENT.
- A Hi from a number with no session starts a session in CONSENT too.

The template's language must match how it is registered in Meta (`START_TEMPLATE_LANGUAGE`, currently `en`), and it must be Active before `/start` can send it.

## Configuration
Environment variables only (see `.env.example`). Never commit values. `GET /api/whatsapp/health/ready` lists which required ones are missing, names only.

## Run locally
```
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest
DATABASE_URL=... (other vars) .venv/Scripts/python -m uvicorn app.main:app --port 8000
```

## Deploy: Koyeb + Neon
1. **Neon**: create a project, copy the connection string (`sslmode=require`) into `DATABASE_URL`. Tables are created on first use.
2. **Koyeb**: new Web Service from this GitHub repo, builder Dockerfile, free instance. Add the variables from `.env.example` as environment variables / secrets. Health check: HTTP GET `/api/whatsapp/health`, expect 200. Set `PUBLIC_BASE_URL` to the Koyeb URL.
3. **Uptime pinger** (UptimeRobot or cron-job.org): GET `<PUBLIC_BASE_URL>/api/whatsapp/health` every 5-10 minutes so the free instance does not scale to zero.
4. **Register the webhook** (returns Meta's exact failure reason):
```
curl -X POST "https://graph.facebook.com/v25.0/<APP_ID>/subscriptions" \
  -d "object=whatsapp_business_account" \
  -d "callback_url=<PUBLIC_BASE_URL>/api/whatsapp/webhook" \
  --data-urlencode "verify_token=<WHATSAPP_VERIFY_TOKEN>" \
  -d "fields=messages" \
  --data-urlencode "access_token=<APP_ID>|<APP_SECRET>"
curl -X POST "https://graph.facebook.com/v25.0/<WABA_ID>/subscribed_apps" -H "Authorization: Bearer <WHATSAPP_ACCESS_TOKEN>"
```

## Notes
- Messages are queued in memory after the 200; if the instance is killed with messages still queued they are lost (Meta does not retry a 200). The chat is one question at a time, so the applicant just resends.
- `/session` and `/zip` are keyed by mobile number only: add a session code before production.
