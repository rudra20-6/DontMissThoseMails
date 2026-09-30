# DontMissThoseMails: Features

A personal assistant that watches your college inbox, decides what matters, condenses it into a few lines, and sends it
to your WhatsApp. It keeps reminding you about deadlines, and about events you said you're interested in, until you tell
it you're done.

Setup: [HOW_TO_RUN.md](HOW_TO_RUN.md).

---

## What you get on WhatsApp

### 📚 Deadlines (assignments, quizzes, submissions, fees, forms)
```
📚 Coursework  ·  🟠 High  ·  D2
OS Assignment 3: Scheduling Simulator
from Moodle

Assignment 3 is released on Moodle. Submit a single zip named <rollno>_A3.zip. 20% late penalty per day.

⏰ Due: Fri 3 Oct, 11:59 PM (in 4 days)
🔗 https://moodle…
[✅ Done] [⏳ Snooze 3h] [🗑️ Ignore]
```
- Reminders **72h, 24h, 6h and 1h** before the deadline (configurable). The last ones are 🚨 and ignore quiet hours.
- Each reminder is sent **once**. If the bot was asleep during a slot, you get one catch-up reminder, not a burst.
- If you learn about a deadline late (say 5h before), earlier slots are skipped.
- When the deadline passes, you get one "⌛ deadline passed" message, then it stops.
- Tap **✅ Done** or send `done os` (or `done D2`) and the reminders stop.

### 🎉 Events (club events, talks, workshops, hackathons, fests)
1. A new event arrives → summary with **when, where, register-by and the link**, plus
   **"Are you interested?"** `[👍 Interested] [👎 Not interested] [✅ Already registered]`
2. No answer in 24h → it asks **once** more.
3. **Interested** → it keeps reminding you to register:
   - **48h, 24h, 6h, 1h** before registration closes;
   - and every evening at **18:00**, until you tap **✅ Registered** (or `registered hackathon`).
4. **Registered** → reminders **24h and 2h** before the event starts.
5. Registration closed while you were still "interested" → one message saying so.

### 🔁 Your own reminders and routines
Just tell it, in your own words:
- *"remind me to put attendance on ISB every hour after 9am until I say I've marked it, every day"*
- *"every weekday at 8:30 remind me to take my ID card"*
- *"remind me at 5pm to call home"* · *"remind me in 20 min to check the oven"*

```
🔁 Routine  ·  R1
Put attendance on ISB

🗓️ Every day · from 9:00 AM, every 1 h until 10:59 PM
⏹️ Stops for the day when you tap ✅ Done
⏭️ Next: Tomorrow, 9:00 AM
```
- Every ping has `[✅ Done for today] [⏳ 15 min] [🛑 Stop]`. **Done** stops it for today, and it starts again the next
  day. **Stop** deletes it.
- Repeats stop for the night when quiet hours start (or at the end time you gave, e.g. *"until 5pm"*).
- These go out at the times you chose, even during quiet hours. `pause` silences them (skipped, not saved up).
- If the bot was asleep for a few slots you get **one** ping, not a burst. An hourly ping that couldn't be delivered
  (WhatsApp's 24h window) is dropped once it's out of date, not delivered hours later.
- A one-off time that has already passed today (*"at 9am"* said at 10am) is set for tomorrow.
- It costs one AI call to set up. After that the reminders use no AI at all.

### 🏛️ Notices and everything else
- Official notices, exam/timetable changes, opportunities, campus notices: summarised in 2–4 sentences with the key link.
- Low-priority mail doesn't ping you. It goes into the **daily digest**.
- Newsletters, promos and automated junk are **dropped**.

Messages are laid out to skim: the title and due time are **bold**, the mail summary is a quoted block, times read
as *Today, 5:00 PM* / *Tomorrow, 9:00 AM*, and commands you can send are shown as `code`. Every message has a category emoji (📚 coursework · 🏛️ academic notice · 🎉 event · 💼 opportunity · 🏠 campus ·
✉️ personal · 📩 other), a priority dot (🔴 critical · 🟠 high · 🟡 medium · ⚪ low), and a short tag (`D2`, `E1`, `R1`) for when names clash.

### ☀️ Daily digest (08:00)
- ⏰ deadlines in the next 7 days
- 📝 events waiting for your answer or registration
- 🎉 events you're registered for
- 📬 one line per low-priority mail that was held back
- `[👍 Got it] [📋 Full list]`. Tapping one also keeps WhatsApp's 24h window open (see below).

### 📥 Catch-up import (one time)
Forward last week's mail to the bot in one go (Outlook attaches them to one email). It unpacks every attached mail
with its original sender, subject and date, analyses them quietly, and sends **one** catch-up message: upcoming deadlines,
events you can still join, important notices. Past items and old low-value notices are skipped. Reminders start for
everything upcoming.

---

## Talking to the bot

| You send | It does |
|---|---|
| `help` | command list |
| `list` | all pending deadlines, events and routines |
| `done dbms` / `submitted os assignment` | stop reminders for that deadline (for a routine: done for today) |
| `done D2` / `done D1 D3` | the same by tag, several at once |
| `registered hackathon` | event registered → pre-event reminders |
| `interested hackathon` / `no hackathon` | answer an event invite / drop any item |
| `snooze os 3h` / `snooze os 2d` / `snooze os till 8pm` | pause reminders for an item |
| `move os to Friday 5pm` / *"the OS deadline got extended to Monday"* | change the date; reminders are re-planned |
| *"remind me …"* | a new one-off reminder or repeating routine (see above) |
| `stop attendance` | delete a routine |
| `undo` | take back your last done / drop / stop / snooze / move |
| `details os` | the full card again, with links (works for notices too) |
| `add DBMS project due Friday 5pm` | add your own deadline or event (AI reads the date) |
| `digest` | today's digest now |
| `pause` / `resume` | mute everything except urgent reminders |
| anything else, e.g. *"I submitted the OS assignment"* | the AI works out what you mean and which item |

### Saying which item
1. **By name** (the usual way): any word or two from the title, e.g. `done dbms`, `snooze hackathon 2h`. Every word you
   type must appear in the title (`os` matches *OS Assignment 3*). If two items match, it asks *"Which one?"* with a button
   for each. If nothing matches, the AI reads your message instead.
2. **By tag**, when names clash: `D1, D2…` deadlines · `E1…` events · `R1…` reminders/routines. Tags are per type and
   stay small: a new item takes the smallest free number. A freed number isn't reused for 24 h, so an old message's tag
   doesn't suddenly point at something new. A bare number (`done 3`) works when only one item has it.
   Notices have no tag, since there's nothing to tick off (`details hostel` still finds them).
3. **Swipe-reply:** reply to any of the bot's messages with `done`, `snooze 2h`, `no`, `details`, `stop`…
   and it applies to that item. Without a swipe-reply, a bare `done` applies to the last thing it messaged you about (it
   says which, and `undo` fixes a wrong guess).

Buttons always point at the exact item, whatever the tags are.

Buttons and exact commands are instant and use no AI. Only messages from your own number (`WHATSAPP_RECIPIENT`) are
accepted; everyone else is ignored.

---

## How it decides (and stays free)

### Mail intake
- Reads a Gmail "mailbot" over IMAP every 5 minutes (college Outlook forwards into it), or Outlook directly via
  Microsoft Graph where IT allows it.
- **Forwarded mail is unwrapped:** `FW:` is stripped and the **original** sender and date are used, so rules like
  "Moodle is always important" still work.
- **Bulk forwards are split** into the original mails (`message/rfc822` attachments).
- Every mail is stored **before** any AI call, so nothing is lost if the AI is busy. It's processed exactly once
  (deduped by Message-ID, and by sender + subject + time for mails that arrive twice).

### One AI call per email
A single Gemini request returns everything:
- **Classification:** category, importance (0–4), has a deadline? is an event? needs registration? is it junk?
- **Summary:** title (≤ 8 words) and a 2–4 sentence summary.
- **Structured data:**
  - up to 3 deadlines, with exact local date and time ("this Friday", "EOD" resolved; 23:59 if only a date is given);
  - event details: start, end, venue, registration deadline, link;
  - the most useful link.

Rules run **before** the AI and cost nothing: `IGNORE_SENDERS` / `IGNORE_SUBJECT_KEYWORDS` drop mail outright;
`PRIORITY_SENDERS` (default `moodle, lms, dean, registrar, academic, exam, office`) are always important.

### Free-tier Gemini, with rotation
- Several free keys (`GEMINI_API_KEYS`), each from its own Google Cloud project (free quota is per project).
- Per key, it discovers the lightweight models automatically: **Flash-Lite → Flash (newest first) → Gemma**. Retired or new
  models are handled with no code change.
- Order: all of key 1's models, then key 2's, and so on.
- **Per-minute** limit → that model pauses for Google's `retryDelay` and the next one is used immediately.
- **Daily** limit → that model is parked until Google's midnight-Pacific reset (remembered across restarts).
- A built-in limiter stays under the free per-minute limits. "Thinking" is off, which saves tokens and time.
- If everything is exhausted, mail waits in the queue and retries every minute. After 45 min it falls back to keyword rules.
- Typical use is a few dozen AI calls a day, far below the free limits of two keys.

---

## Delivery rules
- **Quiet hours** 23:00–07:00: non-urgent messages are queued and delivered in the morning.
- **Pause mode:** only urgent reminders get through until `resume`.
- **WhatsApp 24h window:** WhatsApp only lets a business message you freely within 24h of your last message. If it's closed,
  messages are queued and a single template nudges you (at most every 12h). Your next reply delivers the whole queue in order.
- Failed sends are retried up to 5 times.

## Built for free hosting
- One FastAPI process on Render's free tier, with an internal 1-minute scheduler. `/cron/tick`, hit every 5 minutes by
  cron-job.org, keeps it awake **and** runs the jobs.
- All state lives in Neon Postgres (free), so restarts and redeploys lose nothing. New versions add missing database
  columns automatically.
- Every job is idempotent and locked, so it's safe to trigger as often as you like.

## Diagnostics
- `/admin/whatsapp-check`: checks token and phone ID, whether Meta delivered your last message (and why it was ignored),
  send errors, async delivery failures; `&send=true` sends a test message.
- `/admin/llm`: models per key, cooldowns (per-minute / daily), success and fail counts.
- `/status`, `/admin/items`, `/admin/digest`, `/admin/poll-now`, `/admin/rescan?days=N`.

## Security & privacy
- Admin pages need `ADMIN_TOKEN`; WhatsApp webhooks are signature-checked (`WHATSAPP_APP_SECRET`).
- Mail bodies are deleted from the database once analysed. Only summaries, dates and links are kept.
- Email text is sent to Google Gemini (free tier: Google may use it to improve its products). Keep sensitive senders out
  with `IGNORE_SENDERS`.
- A copy of your college mail sits in the mailbot Gmail. Use a strong password and keep 2-Step Verification on.

## For developers
- `python -m scripts.try_email samples/club_event.txt`: the AI decision + WhatsApp preview for any email text.
- `pytest`: reminder engine, commands, Gemini rotation (per-minute vs daily 429s, restarts, bad keys, Gemma),
  forwarded/bulk-forwarded mail with a fake IMAP server, catch-up import, DB auto-upgrade, webhook flows.
  Runs on SQLite, or on Postgres with `TEST_DATABASE_URL`.
- `Dockerfile` for other hosts (Railway, Fly.io, Koyeb, a VPS).
