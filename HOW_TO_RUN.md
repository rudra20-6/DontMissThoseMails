# How to set up DontMissThoseMails (tested with IIIT `iiit.ac.in` accounts)

This guide sets up your own copy of the bot. It runs 24/7 on free services and messages **your** WhatsApp about
**your** college mail. Nothing runs on your laptop.

It describes the setup that actually works with IIIT Outlook accounts. IIIT doesn't let students approve apps that
read their mailbox (Microsoft shows **"Need admin approval"**), so instead of reading Outlook directly we:

```
IIIT Outlook ──(Outlook forwarding)──▶ a new Gmail "mailbot" ──(IMAP + app password)──▶ the bot on Render
                                                                                       │
                           Gemini (free) summarises & classifies ◀─────────────────────┤
                                                                                       ▼
                                                  WhatsApp Cloud API (free test number) ──▶ your phone
```

**Time:** about 60–90 minutes the first time. **Cost:** ₹0.

| Step | What | Where |
|---|---|---|
| 1 | Get the code | GitHub |
| 2 | Create a mailbot Gmail + app password | accounts.google.com |
| 3 | Forward your IIIT mail to it | outlook.office.com |
| 4 | Two free Gemini API keys | aistudio.google.com |
| 5 | WhatsApp Cloud API (test number, permanent token) | developers.facebook.com |
| 6 | Free Postgres database | neon.tech |
| 7 | Deploy | render.com |
| 8 | Connect the WhatsApp webhook (+ Live mode, + subscribe) | developers.facebook.com |
| 9 | Keep it awake | cron-job.org |
| 10 | Check everything | your browser |
| 11 | (Optional) Import last week's mail | outlook.office.com |

Keep a notes file open while you go. You'll collect about 10 values that go into Render in step 7.
Every setting is explained in [`.env.example`](.env.example).

---

## 1. Get the code

1. Sign in to GitHub and open this repository. Click **Fork** (top right) → **Create fork**. Render deploys from *your* fork.
2. That's all you need. Running it locally is optional ([Appendix B](#appendix-b-run-locally--tests)).

Later, to get updates: open your fork on GitHub → **Sync fork** → **Update branch**. Render redeploys automatically.

---

## 2. Create a mailbot Gmail + app password

Use a **new** Gmail account that only receives your college mail, so the bot doesn't read your personal mail.

1. Create it: <https://accounts.google.com/signup> (e.g. `yourname.mailbot@gmail.com`).
2. **Do the next steps in an Incognito/private window signed in only to the mailbot account.** If several Google accounts
   are signed in, it's easy to end up changing the wrong one (the URL shows `/u/1/`, `/u/2/`, …).
3. Turn on 2-Step Verification: <https://myaccount.google.com/signinoptions/twosv>
   - Use a **phone number (SMS) or Google Authenticator** as the second step. With only a passkey or security key,
     Google hides app passwords.
   - Continue until it says **"2-Step Verification is on"**.
4. Create an app password: <https://myaccount.google.com/apppasswords>
   - Name: `DontMissThoseMails` → **Create** → copy the **16 letters** (spaces don't matter). Google shows it only once.
   - If it says *"The setting you are looking for is not available for your account"*: 2-Step Verification isn't really on,
     you're on the wrong account, or the account is brand new (wait a few hours and retry).

📝 Note down: `IMAP_USERNAME` = the mailbot address, `IMAP_PASSWORD` = the 16 letters.

---

## 3. Forward your IIIT Outlook mail to the mailbot

1. Open **<https://outlook.office.com/mail/options/mail/forwarding>** (Outlook on the web → Settings → Mail → Forwarding).
2. Tick **Enable forwarding**, enter the mailbot Gmail address, tick **Keep a copy of forwarded messages**, and click **Save**.
3. Check it works: when the next mail arrives in Outlook, it should also appear in the mailbot Gmail within a minute or two.

The bot handles forwarded mail properly: it removes `FW:` and uses the **original** sender (e.g. Moodle), subject and date.

<details>
<summary>If forwarding is blocked (bounce "550 5.7.520 … does not allow external forwarding")</summary>

Use a Power Automate flow instead (included with your college account):
1. <https://make.powerautomate.com>, signed in with your college account → **+ Create → Automated cloud flow**.
2. Name `Forward to mailbot`. For the trigger choose **When a new email arrives (V3)** (Office 365 Outlook) → **Create**. Set Folder = **Inbox**.
3. **+** → **Forward an email (V2)** (Office 365 Outlook): **Message Id** = dynamic content → *Message Id*; **To** = the mailbot address.
4. **Save**. After the next mail, **My flows → Run history** should say *Succeeded*.
</details>

---

## 4. Two free Gemini API keys

All AI work (summaries, dates, "is this important?", understanding your replies) uses Gemini's **free tier**.
The bot rotates across the lightweight models (Flash-Lite → Flash → Gemma) and across your keys when one hits a limit.

**Free quota is per Google Cloud *project*, so each key must be in a different project:**
1. <https://aistudio.google.com/apikey> → **Create API key** → **Create API key in new project** → copy it.
2. **Create API key** again → again **in new project** → copy it.
3. Don't enable billing on these projects (that takes them off the free tier).

📝 Note down: `GEMINI_API_KEYS` = `KEY1,KEY2` (comma, no spaces).

Privacy: on the free tier Google may use prompts to improve its products. Add senders you never want sent to Gemini
to `IGNORE_SENDERS`.

---

## 5. WhatsApp Cloud API

The free **test number** Meta gives you can message up to 5 verified numbers. That's all you need, since the bot only talks to you.

### 5a. Create the app
1. <https://developers.facebook.com/apps/> → **Create app** → use case **Other** → type **Business** → give it a name → create.
   (Create or pick a Meta Business portfolio when asked.)
2. In the app dashboard: **Add product → WhatsApp → Set up**.
3. Left menu **WhatsApp → API Setup** (in newer dashboards: **WhatsApp → Step 1. Try it out**):
   - Copy the **Phone number ID**. It's an ID, *not* the phone number.
   - Copy the **WhatsApp Business Account ID**. You need it in step 8.
   - Under **To** → **Manage phone number list** → add **your own WhatsApp number** and enter the code WhatsApp sends you.
   - Note the test number shown as **From** (e.g. `+1 555 …`). That's the number you'll chat with.

📝 Note down: `WHATSAPP_PHONE_NUMBER_ID`, the WABA ID, and `WHATSAPP_RECIPIENT` = your number with country code, digits only
(e.g. `91XXXXXXXXXX`).

### 5b. Permanent access token
The token on the API Setup page expires after 24 hours. Make one that never expires:
1. <https://business.facebook.com/settings/system-users> → **Add** → name `bot`, role **Admin** → create.
2. Select `bot` → **Assign assets** → **Apps** → your app → **Full control** → assign. (If offered, also assign your WhatsApp account.)
3. **Generate token** → pick your app → expiry **Never** → permissions **`whatsapp_business_messaging`** and
   **`whatsapp_business_management`** → **Generate** → copy it.

📝 Note down: `WHATSAPP_TOKEN`.

### 5c. App secret
App dashboard → **App settings → Basic** → **App secret** → **Show** → copy.
📝 Note down: `WHATSAPP_APP_SECRET`.

---

## 6. Free Postgres database (Neon)

Render's free disk is wiped on every deploy, so the bot keeps its state (deadlines, reminders, queue) in Neon.
1. <https://neon.tech> → sign up → create a project (region: Singapore / closest).
2. **Connect** → copy the connection string (`postgresql://…neon.tech/neondb?sslmode=require`).

📝 Note down: `DATABASE_URL`.

---

## 7. Deploy on Render

1. Generate an admin password for the bot's admin pages. It must be URL-safe. In PowerShell (or any terminal with Python):
   ```powershell
   py -c "import secrets; print(secrets.token_urlsafe(24))"
   ```
   📝 Note down: `ADMIN_TOKEN`.
2. <https://dashboard.render.com> → **New → Blueprint** → connect GitHub → pick **your fork**.
   Render reads [`render.yaml`](render.yaml) and asks for these values:

   | Key | Value |
   |---|---|
   | `ADMIN_TOKEN` | from step 7.1 |
   | `APP_BASE_URL` | leave for now, see step 7.3 |
   | `DATABASE_URL` | Neon connection string |
   | `IMAP_USERNAME` | mailbot Gmail address |
   | `IMAP_PASSWORD` | 16-letter app password |
   | `GEMINI_API_KEYS` | `KEY1,KEY2` |
   | `WHATSAPP_TOKEN` | permanent token |
   | `WHATSAPP_PHONE_NUMBER_ID` | Phone number ID |
   | `WHATSAPP_RECIPIENT` | your number, digits only |
   | `WHATSAPP_APP_SECRET` | App secret |

   `WHATSAPP_VERIFY_TOKEN` is generated for you (see it later under **Environment**), and `MAIL_PROVIDER=imap` is preset.
3. After the first deploy, copy your URL (e.g. `https://dontmissthosemails-abcd.onrender.com`). Go to **Environment**,
   set `APP_BASE_URL` to it (no trailing slash), and **Save changes**.
4. Check: open `https://YOUR-APP.onrender.com/health` → `{"ok":true,…}`.

Keep **one** instance. The scheduler runs inside the web process.

---

## 8. Connect the WhatsApp webhook

This lets the bot *receive* your messages and button taps. All four parts are needed: the dashboard's "Test" button
works without 8c/8d, but real messages from your phone don't.

### 8a. Callback URL
App dashboard → **WhatsApp → Configuration** (or **Configure webhooks**):
- **Callback URL:** `https://YOUR-APP.onrender.com/webhook/whatsapp`
- **Verify token:** the `WHATSAPP_VERIFY_TOKEN` value from Render → Environment
- **Verify and save.** If it fails, open `/health` first (to wake the app), then retry.

### 8b. Subscribe to messages
Same page → **Webhook fields** → row **`messages`** → toggle **Subscribe**.

### 8c. Switch the app to Live
1. **App settings → Basic**: set **Privacy Policy URL** to `https://YOUR-APP.onrender.com/privacy` (the bot serves this page),
   pick a **Category** (e.g. *Utility & productivity*), add an icon if asked → **Save changes**.
2. At the top of the dashboard, flip **App Mode: Development → Live**.

### 8d. Subscribe your WhatsApp account to the app
1. Open <https://developers.facebook.com/tools/explorer/>.
2. On the right: **Meta App** = your app; paste `WHATSAPP_TOKEN` into **Access Token**.
3. Method **POST**, path `YOUR_WABA_ID/subscribed_apps`, using the **real number** from step 5a. Don't leave the words
   `THE_WABA_ID` in; that gives *"Object with ID … does not exist"*. Click **Submit** → `{"success": true}`.
4. (Optional) Method **GET**, same path → your app appears under `data`.

Don't know your WABA ID? In the Explorer: **GET** `debug_token?input_token=<paste the same token>` → look under
`granular_scopes` → `whatsapp_business_management` → `target_ids`.

The ⚠️ next to the token in the Explorer ("User: bot … not you") is fine. It just means the token belongs to the system user.

### 8e. Test
From your phone, send **`hi`** to the test number (the **From** number in step 5a). You should get the help menu back.
If not, see [The bot doesn't reply](#the-bot-doesnt-reply-to-hi).

---

## 9. Keep it awake (cron-job.org)

Render's free tier sleeps after 15 minutes without traffic. A free pinger keeps it awake, and each ping also makes the bot
check mail and send due reminders.

1. First test in your browser: `https://YOUR-APP.onrender.com/cron/tick?token=ADMIN_TOKEN` → `{"queued":true}`.
2. <https://console.cron-job.org/signup> → sign up, confirm your email, log in.
3. **Cronjobs → CREATE CRONJOB**
   - **Title:** `DontMissThoseMails tick`
   - **URL:** `https://YOUR-APP.onrender.com/cron/tick?token=ADMIN_TOKEN`
   - **Execution schedule:** every **5 minutes**
   - **Advanced:** method `GET`, timeout at the maximum (30 s). A timeout while Render wakes up is harmless.
   - **Notifications:** turn off "on failure" (or require several in a row), keep "when the job is disabled".
   - **CREATE**
4. After ~10 minutes, the job's **History** shows **200 OK** runs.

---

## 10. Check everything

Open these (replace `ADMIN_TOKEN`):

| URL | You should see |
|---|---|
| `/status?token=ADMIN_TOKEN` | `"mail_provider": "imap"`, `"gemini_keys": 2`, `"whatsapp_configured": true`; `emails_seen` grows as mail arrives |
| `/admin/whatsapp-check?token=ADMIN_TOKEN&send=true` | `2_token_and_phone_id.ok: true`, and a 🧪 test message on your phone |
| `/admin/llm?token=ADMIN_TOKEN` | the Gemini models each key rotates through, with cooldowns |

When a new college mail arrives, its WhatsApp summary should follow within about 5 minutes. The first **daily digest**
comes the next morning at 08:00.

---

## 11. (Optional) Import last week's mail

Your mailbot only has mail from the moment forwarding started. To give the bot the last week once:

1. Open <https://outlook.office.com/mail/>.
2. Tick the first mail (hover → circle), scroll to a week ago, and **Shift+click** the last one.
3. Click **Forward**. Outlook attaches them all to one email. Send it to the mailbot. Do batches of ~40 (Gmail's 25 MB limit).

The bot unpacks each attached mail, analyses them quietly (about 20 per minute), then sends **one catch-up message**:
upcoming deadlines, events you can still join (reply `interested <id>`), and important notices. Old/past items are skipped,
and reminders start for everything upcoming. Mails that were also forwarded individually are only processed once.

---

## Everyday use

Talk to the bot on WhatsApp. `help` shows all commands. See [FEATURES.md](FEATURES.md) for everything it does.

Admin URLs (all need `?token=ADMIN_TOKEN`):

| URL | What |
|---|---|
| `GET /status` | configuration + counts |
| `GET /admin/items` | last 50 tracked items |
| `GET /admin/digest` | preview today's digest |
| `GET /admin/whatsapp-check` | step-by-step WhatsApp diagnosis (`&send=true` sends a test) |
| `GET /admin/llm` | Gemini models per key, cooldowns, success counts |
| `POST /admin/poll-now` | check mail now |
| `POST /admin/rescan?days=7` | re-read the mailbox from N days ago (quiet import + one catch-up summary) |
| `POST /admin/test-whatsapp` | send a test message |
| `GET /cron/tick` | run one cycle (used by the pinger) |

## Tuning

Change these on Render → Environment (full list in [`.env.example`](.env.example)):

| Setting | Default | Use it to |
|---|---|---|
| `IGNORE_SENDERS` | – | drop noisy senders without any AI call, e.g. `linkedin,newsletter,noreply@quora` |
| `PRIORITY_SENDERS` | `moodle,lms,dean,registrar,academic,exam,office` | always treat these as important |
| `IMPORTANCE_IMMEDIATE_MIN` | `1.5` | raise to `2.5` if you get too many messages (the rest go to the digest) |
| `DEADLINE_OFFSETS_HOURS` | `72,24,6,1` | when deadline reminders fire |
| `REGISTRATION_OFFSETS_HOURS` | `48,24,6,1` | reminders before an event's registration closes |
| `EVENT_NAG_HOUR` | `18` | daily "have you registered?" nudge for events you're interested in |
| `DIGEST_HOUR` | `8` | daily digest time |
| `QUIET_HOURS_START` / `_END` | `23` / `7` | no non-urgent messages at night |

## Troubleshooting

### The bot doesn't reply to "hi"
Open `/admin/whatsapp-check?token=ADMIN_TOKEN&send=true` right after sending `hi` (its log resets when Render restarts):

| What you see | Fix |
|---|---|
| `{"detail":"Not Found"}` | You're on an old deploy, or the URL is mistyped. Check Render finished deploying. |
| `2_token_and_phone_id.ok: false`, code 190 | Token expired → make the permanent token (5b) and update `WHATSAPP_TOKEN`. |
| `2_token_and_phone_id.ok: false`, code 100 | Wrong `WHATSAPP_PHONE_NUMBER_ID` (use the ID, not the phone number). |
| `5_test_send: FAILED … 131030` | Your number isn't in the test number's allowed list (5a). |
| `3_webhook: NONE` | Meta isn't calling the bot. Do **8b, 8c and 8d**, and message the **test number**, not yourself. |
| `3_webhook … accepted: false` | The message came from a different number than `WHATSAPP_RECIPIENT`; the `reason` shows both. |
| `last rejected webhook: bad X-Hub-Signature-256` | Wrong `WHATSAPP_APP_SECRET` → copy it again (5c). |
| test send works, nothing arrives later | `131047` in `last async delivery failure` = 24h window closed → send the bot any message. |

### Other problems

| Symptom | Fix |
|---|---|
| `/status` shows `emails_seen: 0` for a long time | Check the mail reaches the mailbot Gmail (step 3), and `IMAP_USERNAME` / `IMAP_PASSWORD` (the app password, not your normal password). Render logs show `IMAP error: …` if the login fails. |
| `/cron/tick` says `bad or unset ADMIN_TOKEN` | The token has `+ / =` characters → generate a URL-safe one (7.1) and update it. |
| Logs: `AI quota exhausted, N email(s) stay queued` | All Gemini keys/models hit their free limit. Check `/admin/llm`, and make sure the two keys are in **different projects**. After 45 min, queued mail is handled with keyword rules. |
| Messages only arrive after you text the bot | WhatsApp's 24h rule: businesses can only message you freely within 24h of your last message. The bot queues messages and sends one template to wake you. Tapping **👍 Got it** on the morning digest keeps the window open. |
| Webhook "Verify and save" fails | `WHATSAPP_VERIFY_TOKEN` differs, or the app was asleep: open `/health` first and retry. |

---

## Appendix A: Reading Outlook directly (only if IIIT IT approves)

This skips Gmail entirely, but needs an admin to approve the app. Students get **"Need admin approval"** otherwise.

1. <https://entra.microsoft.com> (college account) → **App registrations → New registration**. Redirect URI (Web):
   `https://YOUR-APP.onrender.com/auth/microsoft/callback`.
2. Copy the **Application (client) ID** → `MS_CLIENT_ID`. **Certificates & secrets → New client secret** → the *Value* → `MS_CLIENT_SECRET`.
3. **API permissions → Microsoft Graph → Delegated:** `Mail.Read`, `User.Read`, `offline_access`.
4. If you registered it as **single-tenant**, set `MS_TENANT` to the **Directory (tenant) ID** from the Overview page,
   or you get `AADSTS50194 … not configured as a multi-tenant application`.
5. Ask IT to grant admin consent for your Application ID (read-only `Mail.Read`).
6. Set `MAIL_PROVIDER=graph`, then open `https://YOUR-APP.onrender.com/auth/microsoft/login?token=ADMIN_TOKEN` and sign in.
7. For a one-time import of older mail: `POST /admin/rescan?token=ADMIN_TOKEN&days=7`.

## Appendix B: Run locally / tests

**Windows (PowerShell):**
```powershell
git clone https://github.com/YOUR-USERNAME/DontMissThoseMails.git
cd DontMissThoseMails
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements-dev.txt
copy .env.example .env                # fill it in: notepad .env
python -m pytest -q                   # all tests should pass
python -m scripts.try_email samples\club_event.txt   # see what the AI makes of an email (needs GEMINI_API_KEYS)
uvicorn app.main:app --reload         # http://localhost:8000/health
```
**macOS / Linux:** same, with `python3 -m venv .venv && source .venv/bin/activate` and `cp .env.example .env`.

Set `WHATSAPP_DRY_RUN=true` to print WhatsApp messages in the terminal instead of sending them.
Receiving WhatsApp replies locally needs a public URL (e.g. `ngrok http 8000`).
Set `TEST_DATABASE_URL=postgresql://…` to run the test suite against Postgres instead of SQLite.
