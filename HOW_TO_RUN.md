# How to run & deploy DontMissThoseMails

This guide takes you from nothing to a bot running 24/7 on Render (free) and messaging your WhatsApp.
It takes about 45 minutes the first time. Do the sections in order.

| # | What | Where | Cost |
|---|------|-------|------|
| 1 | Get the code + Python | your laptop | free |
| 2 | Postgres database | Neon (neon.tech) | free |
| 3 | Outlook access (Microsoft Graph app) | entra.microsoft.com | free |
| 4 | WhatsApp Cloud API | developers.facebook.com | free test number |
| 5 | Two Gemini API keys | aistudio.google.com | free tier |
| 6 | Run locally (optional, recommended) | your laptop | – |
| 7 | Deploy to Render | render.com | free |
| 8 | Connect everything + keep-alive pinger | cron-job.org | free |

All settings live in environment variables. The full list with explanations is in
[`.env.example`](.env.example). Locally they go in a `.env` file; on Render they go in the dashboard.

---

## 1. Code + Python

You need **Python 3.11+** and **git**.

**Windows (PowerShell):**

```powershell
git clone https://github.com/rudra20-6/DontMissThoseMails.git
cd DontMissThoseMails
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1      # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements-dev.txt
copy .env.example .env             # then fill it in with: notepad .env
python -m pytest -q                # should print "23 passed"
```

**macOS / Linux:**

```bash
git clone https://github.com/rudra20-6/DontMissThoseMails.git
cd DontMissThoseMails
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
python -m pytest -q
```

Generate an `ADMIN_TOKEN` and put it in `.env`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

---

## 2. Database (Neon, free, permanent)

Render's free disk is wiped on every deploy or restart, and Render's own free Postgres is deleted after 30 days.
Use **Neon** (or Supabase) so your tokens, deadlines and reminders survive.

1. Sign up at <https://neon.tech> and create a project (pick a region close to Singapore/Mumbai).
2. On the dashboard, click **Connect** and copy the connection string. It looks like
   `postgresql://user:pass@ep-xxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require`
3. Put it in `DATABASE_URL`.

Tables are created automatically on first start. For purely local testing you can leave the default
`sqlite:///./dontmiss.db`.

---

## 3. Outlook access (Microsoft Graph)

The app reads your inbox with **read-only** permission (`Mail.Read`). You register a small "app" with Microsoft once.

1. Go to <https://entra.microsoft.com> and sign in with your **college Outlook account**.
   (Most college tenants allow students to register apps. If it's blocked, see *Plan B* below.)
2. **Applications → App registrations → New registration**
   - Name: `DontMissThoseMails`
   - Supported account types: **Accounts in any organizational directory and personal Microsoft accounts**
   - Redirect URI: platform **Web**, URL `https://YOUR-APP.onrender.com/auth/microsoft/callback`
     (add `http://localhost:8000/auth/microsoft/callback` as a second one for local testing: *Authentication → Add URI*)
3. Copy the **Application (client) ID** into `MS_CLIENT_ID`.
4. **Certificates & secrets → New client secret** (24 months). Copy the **Value** (not the ID) into `MS_CLIENT_SECRET`.
   Set a calendar reminder to renew it before it expires.
5. **API permissions → Add a permission → Microsoft Graph → Delegated** → add `Mail.Read`, `User.Read`, `offline_access`.
6. Keep `MS_TENANT=common`.

You'll actually log in in step 8, after deploying.

**If your college shows "Need admin approval":** your college has blocked user consent. Options:

- **Plan B (easiest): forward to Gmail + IMAP.** In Outlook web → Settings → Mail → Forwarding, forward everything
  to a Gmail account (some colleges block external forwarding; then try an Outlook *inbox rule* that forwards).
  In Gmail enable 2-Step Verification, then create an **App Password** (<https://myaccount.google.com/apppasswords>).
  Set `MAIL_PROVIDER=imap`, `IMAP_HOST=imap.gmail.com`, `IMAP_USERNAME=you@gmail.com`, `IMAP_PASSWORD=<16-char app password>`.
- **Plan C:** forward to a personal outlook.com account and use Graph with `MS_TENANT=consumers`.
- **Plan D:** ask your IT department to approve the app (it only reads mail).

---

## 4. WhatsApp Cloud API (free test number)

Your free WhatsApp Business setup gives you a **test phone number** that can message up to 5 verified numbers.
That's all this bot needs, since it only ever messages *you*.

1. Go to <https://developers.facebook.com> → **My Apps → Create app** → use case **Other** → type **Business**.
2. In the app dashboard, **Add product → WhatsApp → Set up** (select or create your Meta Business account).
3. Open **WhatsApp → API Setup**:
   - Copy **Phone number ID** → `WHATSAPP_PHONE_NUMBER_ID` (it's an ID, not the phone number).
   - Under **To**, click **Manage phone number list**, add **your personal number** and verify it with the code.
     Put it (country code, digits only, e.g. `919876543210`) in `WHATSAPP_RECIPIENT`.
   - The **temporary access token** shown there works for 24 hours, which is fine for a first test.
4. **Permanent token** (so the bot doesn't die after 24h):
   1. <https://business.facebook.com> → **Settings → Users → System users → Add** → name `bot`, role **Admin**.
   2. **Assign assets** → Apps → your app → Full control. Also assign your WhatsApp account.
   3. **Generate new token** → pick your app → expiry **Never** → permissions `whatsapp_business_messaging`
      and `whatsapp_business_management` → copy into `WHATSAPP_TOKEN`.
5. **App settings → Basic → App secret → Show**, then copy it into `WHATSAPP_APP_SECRET` (used to verify that webhooks really come from Meta).
6. Choose any random string for `WHATSAPP_VERIFY_TOKEN` (you'll paste the same string into Meta in step 8).

### About the 24-hour window (read this)

WhatsApp only lets a business send free-form messages within **24 hours of your last message to it**.
The bot handles this for you:

- If the window is closed, it queues the messages and sends one **template** message (`hello_world` by default,
  which is pre-approved on every test number). Reply anything, or tap a button, and it delivers everything it queued.
- Every digest and reminder has buttons. Tapping one (e.g. **👍 Got it** on the morning digest) keeps the window open,
  so in practice you'll rarely see the template.
- Optional, nicer template: in **WhatsApp Manager → Message templates**, create a *Utility* template called
  e.g. `dmtm_nudge` with body `📬 You have {{1}} new update(s) from DontMissThoseMails. Reply *show* to see them.`
  Once it's approved, set `WHATSAPP_TEMPLATE_NAME=dmtm_nudge`, `WHATSAPP_TEMPLATE_LANG=en` and `WHATSAPP_TEMPLATE_HAS_PARAM=true`.

Meta may charge for template messages sent outside the window (pricing depends on country and changes over time).
Replies inside the window are free. Keeping the window open with a daily tap avoids most template sends.

---

## 5. Gemini API keys (free, two of them)

Gemini does all the AI work. The app is designed to stay inside the **free tier**:

- **One call per email** does everything: category, importance, "has a deadline?", "is an event?",
  "needs registration?", "is this noise?", *plus* the summary, exact dates, event details and links.
- **One call per free-text WhatsApp message** ("I submitted the OS assignment", "add DBMS project due Friday").
- **Zero calls** for button taps, exact commands (`done 12`, `list`, ...), reminders and the digest.
- Mail from `IGNORE_SENDERS` is dropped without any call.

### Create the keys (important: two different projects)

Google's free quota is **per Google Cloud project, not per key**. Two keys in the same project share one quota.

1. Go to <https://aistudio.google.com/apikey> → **Create API key** → **Create API key in new project**. Copy it.
2. Click **Create API key** again → again choose **new project** (a *different* one). Copy it.
3. Put both in `GEMINI_API_KEYS`, comma-separated, no spaces: `GEMINI_API_KEYS=AIzaAAA...,AIzaBBB...`

Don't enable billing on those projects. If a project has billing enabled, it's no longer on the free tier.

### How the rotation works

With `GEMINI_MODELS=auto` the app asks Google (ListModels) which models **each key** can use, and builds this order:

1. key 1: the lightweight models, **Flash-Lite first, then Flash, newest version first** (up to `GEMINI_MAX_MODELS`), then the best **Gemma** model;
2. key 2: the same list;
3. and so on for more keys.

On every request it walks that list:

- **Per-minute limit hit** (Google says so in the error): that model sits out for the `retryDelay` Google gives (usually seconds) and the next model is used.
- **Daily limit hit:** that model on that key is parked until **midnight Pacific time**, when Google resets daily quotas (about 12:30–1:30 PM in India). This is saved in the database, so restarts don't waste requests on it.
- **Model retired or unavailable:** skipped for 24h. New models appear automatically, since the list is refreshed every 12h.
- **Bad key:** skipped for an hour, and an error goes in the logs.
- The app also keeps itself under `GEMINI_RPM` requests per minute per model, so it rarely hits a limit at all.
- **Everything exhausted** (very unlikely with two projects): new emails wait in a queue and are retried every minute.
  After `LLM_RETRY_MAX_MINUTES` (45) they are processed with keyword rules, so a deadline is never silently lost.
  WhatsApp buttons and exact commands keep working with no AI.

See the live picture at `https://YOUR-APP.onrender.com/admin/llm?token=ADMIN_TOKEN`: the models per key,
their cooldowns, and success/fail counts.

### How much headroom?

Free limits change often and differ per account. Check yours in AI Studio → **Usage / Rate limits**.
Recently the Flash-Lite models have had a few hundred to ~1,000 requests/day each, Flash models fewer
(sometimes only ~20/day), and Gemma much more. A busy college inbox is roughly 30–100 emails a day, so with
2 projects × several models you typically have 10–50× more quota than needed.

**Privacy note:** on the free tier, Google may use prompts to improve its products. Your emails are sent to Gemini
for summarising. If that bothers you, add sensitive senders to `IGNORE_SENDERS`.

---

## 6. Run locally (optional, recommended)

```bash
.\.venv\Scripts\Activate.ps1     # macOS/Linux: source .venv/bin/activate
# 1) try the brain on the sample emails (only needs GEMINI_API_KEYS)
python -m scripts.try_email samples/moodle_assignment.txt
python -m scripts.try_email samples/club_event.txt

# 2) run the whole server
uvicorn app.main:app --reload --port 8000
```

Then open:

- <http://localhost:8000/health> → `{"ok": true}`
- `http://localhost:8000/status?token=YOUR_ADMIN_TOKEN` → shows what is configured
- `http://localhost:8000/auth/microsoft/login?token=YOUR_ADMIN_TOKEN` → connect Outlook (needs the localhost redirect URI from step 3)

Set `WHATSAPP_DRY_RUN=true` in `.env` to print WhatsApp messages in the terminal instead of sending them.
(WhatsApp *replies* need a public URL, so test two-way chat after deploying, or use a tunnel such as `ngrok http 8000`.)

---

## 7. Deploy to Render (free)

1. Push this repo to your GitHub (it's already there if you're reading this on GitHub).
2. Go to <https://dashboard.render.com> → **New → Blueprint** → connect your GitHub → select this repository.
   Render reads [`render.yaml`](render.yaml) and creates a free web service called `dontmissthosemails`.
   *(Alternative: **New → Web Service**, Build command `pip install -r requirements.txt`, Start command
   `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`, Health check path `/health`.)*
3. Fill in the environment variables it asks for (the values from steps 2–5). Then in **Environment** add any extra
   ones you changed from the defaults in `.env.example`.
4. After the first deploy, note your URL, e.g. `https://dontmissthosemails.onrender.com`.
   Set `APP_BASE_URL` to exactly that (no trailing slash) and save; Render redeploys.
5. Check `https://YOUR-APP.onrender.com/status?token=ADMIN_TOKEN`. `ADMIN_TOKEN` was auto-generated by the
   blueprint; you can see it under **Environment**.

> Keep **one** instance and `--workers 1`. The scheduler runs inside the web process; two copies would double-send.

---

## 8. Connect everything

### 8a. WhatsApp webhook (so the bot can read your replies)

Meta dashboard → your app → **WhatsApp → Configuration → Webhook → Edit**:

- Callback URL: `https://YOUR-APP.onrender.com/webhook/whatsapp`
- Verify token: the value of `WHATSAPP_VERIFY_TOKEN`
- Click **Verify and save**, then under **Webhook fields** click **Subscribe** on `messages`.

Now send **hi** from your phone to the test number. The bot replies with the help menu.
(Also try `curl -X POST "https://YOUR-APP.onrender.com/admin/test-whatsapp?token=ADMIN_TOKEN"`.)

### 8b. Connect Outlook

Open in your browser: `https://YOUR-APP.onrender.com/auth/microsoft/login?token=ADMIN_TOKEN`
→ sign in with your college account → accept → you'll see **✅ Outlook connected!**
The refresh token is stored in the database and renewed automatically.

Force a first check: `curl -X POST "https://YOUR-APP.onrender.com/admin/poll-now?token=ADMIN_TOKEN"`

### 8c. Keep it awake (pinger)

Render free services sleep after 15 minutes without traffic. Use a free pinger that **also drives the jobs**:

1. Sign up at <https://cron-job.org> → **Create cronjob**
2. URL: `https://YOUR-APP.onrender.com/cron/tick?token=ADMIN_TOKEN`
3. Schedule: **every 5 minutes** → Save.

That call wakes the app *and* runs a full cycle (check mail, send due reminders, digest, deliver queued messages).
The app also has its own 1-minute internal scheduler while awake. UptimeRobot pinging `/health` every 5 minutes
works too, but `/cron/tick` is more robust.

Render's free tier gives 750 instance-hours a month, enough for one service running 24/7.

---

## Everyday use

Just use WhatsApp. Send **help** at any time. See [FEATURES.md](FEATURES.md) for everything it does.

Useful admin URLs (all need `?token=ADMIN_TOKEN`):

| URL | What |
|-----|------|
| `GET /status` | configuration + counts |
| `GET /admin/items` | last 50 tracked items |
| `GET /admin/llm` | Gemini models per key, cooldowns (per-minute / daily), success counts |
| `GET /admin/digest` | preview today's digest |
| `POST /admin/poll-now` | check mail right now |
| `POST /admin/test-whatsapp` | send a test message |
| `GET /cron/tick` | run one cycle (for the pinger) |

## Tuning

Everything in the *Behaviour tuning* block of `.env.example` can be changed on Render → Environment, e.g.:

- Too many mails? Raise `IMPORTANCE_IMMEDIATE_MIN` to `2.5` (only high/critical get pushed; the rest go to the digest),
  and add noisy senders to `IGNORE_SENDERS`.
- Missing things? Lower it to `1.0` and add senders to `PRIORITY_SENDERS`.
- Different reminder rhythm: `DEADLINE_OFFSETS_HOURS=120,48,24,3`.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `/status` says `outlook_connected: false` | Redo 8b. Check the redirect URI in Entra matches `APP_BASE_URL` + `/auth/microsoft/callback` exactly. |
| `AADSTS65001` / "Need admin approval" | Your college blocks consent → use Plan B (IMAP) in step 3. |
| No WhatsApp messages at all | Check logs on Render. `WhatsApp HTTP 401` → token expired (make the permanent token, step 4.4). `131030` → your number isn't in the test recipient list. |
| Messages arrive only after you text the bot | The 24h window was closed. That's expected; see "About the 24-hour window". |
| Webhook "verify" fails in Meta | `WHATSAPP_VERIFY_TOKEN` differs, or the app was asleep. Open `/health` first, then retry. |
| Everything lost after a redeploy | You are on SQLite. Set `DATABASE_URL` to Neon (step 2). |
| Logs show `AI quota exhausted, N email(s) stay queued` | All keys/models are at their limit. Check `/admin/llm`. Make sure your two keys are in **different projects**. Emails are handled with keyword rules after 45 min. |
| `/admin/llm` shows only fallback model names | ListModels failed: check that the key is valid (AI Studio → API keys). |
