# DontMissThoseMails: Features

An always-on assistant that reads your college Outlook inbox, decides what matters, condenses it, and
messages you on WhatsApp. It keeps reminding you about deadlines, and about event registrations you
said you were interested in, until you tell it you're done.

---

## 1. Inbox watching

- Checks your Outlook inbox every **5 minutes** (`MAIL_POLL_MINUTES`) via Microsoft Graph (read-only), or any IMAP mailbox.
- On first start it looks back 24 hours (`MAIL_LOOKBACK_HOURS`), so nothing that arrived just before setup is missed.
- Every email is processed **exactly once** (deduplicated by Message-ID), even across restarts.
- HTML emails are converted to clean text, and link targets are kept (registration forms, Moodle links).

## 2. One AI call per email: triage + condensing (Gemini, free tier)

A single Gemini request per email returns everything at once:

| Field | Used for |
|-------|----------|
| `category` | coursework · academic notice · event · opportunity · campus notice · personal · newsletter/promo · other |
| `importance` (0 ignore → 1 low → 2 medium → 3 high → 4 critical) | push now / digest / drop |
| `has_deadline` | create a tracked deadline |
| `is_event`, `needs_registration` | create a tracked event, ask if you're interested, registration reminders |
| `is_noise` | drop promos and automated junk |
| `title`, `summary` | ≤ 8-word title + 2–4 sentences: what, when/where, what you must do |
| `deadlines` | up to 3, with exact local date/time ("this Friday", "EOD", "tomorrow" resolved; 23:59 if only a date) |
| `event` | name, start/end, venue, registration deadline, registration link |
| `primary_link` | the submission / registration / details link |

Fixed rules run **first** and cost nothing:
- `IGNORE_SENDERS` / `IGNORE_SUBJECT_KEYWORDS` → dropped without any AI call.
- `PRIORITY_SENDERS` (default: moodle, lms, dean, registrar, academic, exam, office) → never treated as noise, importance at least *high*.

Thresholds decide the action:
- **Notify now:** importance ≥ `IMPORTANCE_IMMEDIATE_MIN` (1.5), or it has a deadline / is an event.
- **Daily digest only:** relevant but low priority.
- **Drop:** noise, or importance below `IMPORTANCE_DROP_BELOW`.

Every decision is stored with the email for auditing (`/admin/items`, database `emails.decision`).

## 3. Free-tier model and key rotation

- Works with **multiple free Gemini keys** (`GEMINI_API_KEYS`). Use keys from different Google Cloud projects: quota is per project.
- Per key, it discovers the available **lightweight models** automatically: Flash-Lite first, then Flash (newest first),
  then **Gemma** as a high-volume last resort. New or retired models are picked up without code changes.
- Order: all of key 1's models → all of key 2's models → …
- **Per-minute** limit → that model pauses for Google's `retryDelay`, next model is used immediately.
  **Daily** limit → that model is parked until Google's reset (midnight Pacific), remembered across restarts.
- A built-in per-model rate limiter (`GEMINI_RPM`) avoids hitting limits in the first place.
- "Thinking" is turned off/low on every call (not needed for extraction, saves tokens and time).
- If *everything* is exhausted: emails wait in a queue (nothing lost) and retry every minute. After 45 min they
  fall back to keyword rules. Buttons and commands work without AI.
- `/admin/llm` shows the live rotation state.

## 4. WhatsApp notifications, categorised

Each message starts with a category emoji, a priority dot and a short ID you can refer to:

```
📚 Coursework 🟠 high  ·  #12
*OS Assignment 3: Scheduling Simulator*
_from Moodle_

Assignment 3 is released on Moodle. Submit a single zip named <rollno>_A3.zip. 20% late penalty per day.

⏰ Due: Fri 3 Oct, 11:59 PM (in 4 days)
🔗 https://moodle…
[✅ Done] [⏳ Snooze 3h] [🗑️ Ignore]
```

Categories: 📚 Coursework · 🏛️ Academic notice · 🎉 Event · 💼 Opportunity · 🏠 Campus notice · ✉️ Personal · 📩 Other.
Priority: 🔴 critical · 🟠 high · 🟡 medium · ⚪ low.

## 5. Deadline tracking & reminders

- Each deadline found becomes a tracked item (up to 3 per email, e.g. "abstract due" + "final report due").
- Reminders at **72h, 24h, 6h and 1h** before the deadline (`DEADLINE_OFFSETS_HOURS`).
- Reminders in the last 6 hours are marked 🚨 and **ignore quiet hours**.
- Each reminder is sent **once**. If the server was asleep and missed a slot, it sends one catch-up, not a burst.
- If you learn about a deadline late (e.g. 5 hours before), earlier reminder slots are skipped.
- When the deadline passes you get one "⌛ deadline passed" message, then it stops.
- Stops immediately when you tap **✅ Done** or send `done 12`.

## 6. Event flow: interest → registration → attendance

1. A new event arrives → the bot sends a summary (when, where, register-by, link) and asks
   **Are you interested?** `[👍 Interested] [👎 Not interested] [✅ Already registered]`
2. No answer in 24h → it asks **once more**, then leaves it alone.
3. **Interested** → it keeps reminding you to register:
   - at **48h, 24h, 6h, 1h** before registration closes (`REGISTRATION_OFFSETS_HOURS`), and
   - every evening at **18:00** (`EVENT_NAG_HOUR`) until you say you've registered.
   - Each reminder has `[✅ Registered] [⏳ Tomorrow] [👎 Not going]`.
4. **Registered** → reminders **24h and 2h** before the event starts (`EVENT_OFFSETS_HOURS`).
5. Registration closed while you were still "interested" → one message saying so (reply `registered 12` if you did).
6. Past events are archived automatically.

## 7. Daily digest (08:00)

- ⏰ Deadlines in the next 7 days (and a count of later ones)
- 📝 Events waiting for your answer or registration
- 🎉 Events you're registered for
- 📬 Low-priority mail held back from instant notifications, one line each
- Buttons `[👍 Got it] [📋 Full list]`. Tapping one also keeps WhatsApp's 24h window open.

## 8. Talk to it on WhatsApp

| You send | It does |
|----------|---------|
| `help` | command list |
| `list` | all pending deadlines and events |
| `done 12` / `submitted 12` | stop reminders for deadline #12 |
| `registered 12` | mark event registered → pre-event reminders |
| `interested 12` / `no 12` | answer an event invite / drop any item |
| `snooze 12 3h` / `snooze 12 2d` | pause reminders for an item |
| `details 12` | full card with summary and links |
| `add OS quiz prep due Monday 9am` | add your own deadline (LLM parses the date) |
| `digest` | today's digest now |
| `pause` / `resume` | mute everything except urgent reminders (deadline < 6h, event starting) |
| *anything else*, e.g. "I submitted the OS assignment" | **one Gemini call** works out the intent *and* which of your items you mean |

Only messages from `WHATSAPP_RECIPIENT` (you) are accepted; everyone else is ignored.

## 9. Delivery rules

- **Quiet hours** 23:00–07:00 (`QUIET_HOURS_START/END`): non-urgent messages are queued and delivered in the morning.
- **Pause mode:** everything except urgent reminders is held until `resume`.
- **24-hour WhatsApp window:** if Meta refuses a message because you haven't messaged recently, it is queued and a single
  approved template nudges you (at most every 12h). Your next reply or tap delivers the whole queue in order.
- Failed messages are retried up to 5 times.

## 10. Built for free hosting

- Single FastAPI process with an internal 1-minute scheduler. `/cron/tick` lets an external pinger (cron-job.org)
  both keep Render's free tier awake **and** run the jobs, so reminders still fire even if the process slept.
- All state (OAuth tokens, items, sent-reminder log, outbox) lives in Postgres (Neon free tier), so restarts and redeploys lose nothing.
- Every job is idempotent and protected by a lock: safe to trigger as often as you like.
- Webhook signature verification (`WHATSAPP_APP_SECRET`) and an `ADMIN_TOKEN` guard on every admin URL.

## 11. Developer extras

- `python -m scripts.try_email samples/club_event.txt`: see the models each key rotates through, the AI decision and the WhatsApp preview for any email text.
- `pytest`: tests covering the reminder engine, command parsing, Gemini key/model rotation (per-minute vs daily 429s, restarts, bad keys, Gemma), the full email → WhatsApp pipeline and the webhook → button flow.
- `Dockerfile` for Docker-based hosts (Railway, Fly.io, Koyeb, a VPS).
