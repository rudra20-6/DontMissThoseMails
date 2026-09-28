# DontMissThoseMails 📬 → 💬

**Your college Outlook inbox, condensed and sent to your WhatsApp, with reminders that keep going until you're done.**

- 📚 Moodle assignments, quizzes and official notices → WhatsApp with the exact deadline, then reminders at 72h / 24h / 6h / 1h until you reply **done**.
- 🎉 Club events and hackathons → a 3-line summary plus *"Interested?"*. Say yes and it nags you to register until you say **registered**, then reminds you before it starts.
- 📰 Newsletters and promos → dropped. Low-priority mail → one line in the 08:00 daily digest.
- 💬 Talk to it naturally: *"I submitted the OS assignment"*, *"snooze 12 2d"*, *"add DBMS project due Friday 5pm"*.

**All AI runs on the free Gemini tier.** It rotates across lightweight models (Flash-Lite → Flash → Gemma) and
multiple free API keys, with one call per email. Reminders, buttons and commands are plain code and use no AI.

| Doc | What's inside |
|-----|---------------|
| [HOW_TO_RUN.md](HOW_TO_RUN.md) | Step-by-step: credentials, local run, deploy on Render (free), keep-alive pinger |
| [FEATURES.md](FEATURES.md) | Everything the bot does, and how |
| [.env.example](.env.example) | Every setting, documented |

## How it works

```
 Outlook (Graph / IMAP) ──every 5 min──▶ rules filter ──▶ queue (DB: nothing lost if quota runs out)
                                                               │
                                                               ▼
                         Gemini, ONE call per email: category · importance · deadline? · event? · noise?
                                                    + summary · exact dates · event details · links
                           (key 1: flash-lite → flash → gemma, then key 2: …; per-minute vs daily 429 aware)
                                                               │
                                                               ▼
 WhatsApp  ◀── items + notify (quiet hours, pause, 24h window) ◀── reminder engine + daily digest (every minute)
    │
    └─ your replies/buttons ──▶ exact commands (no AI) or one Gemini call for free text

 Postgres (Neon) stores tokens, emails queue, items, reminder log, outbox, quota cooldowns
```

## Project layout

```
app/
  main.py              FastAPI: WhatsApp webhook, Microsoft login, /cron/tick, admin endpoints
  scheduler.py         1-minute tick: poll mail → reminders → digest → outbox
  config.py            all settings (env vars)
  models.py, db.py     SQLAlchemy models (SQLite locally, Postgres in production)
  clients/             gemini.py (model/key rotation) · whatsapp.py
  mail/                graph.py (Outlook OAuth) · imap.py
  services/
    decisions.py       ALL AI: rules → one Gemini call per email / message
    extraction.py      structured deadline/event data
    pipeline.py        email → decision → items → first notification
    reminders.py       pure reminder planner + executor
    digest.py          daily digest
    commands.py        WhatsApp command / free-text handling
    notifier.py        quiet hours, pause, 24h window, outbox
    messages.py        WhatsApp message templates
scripts/try_email.py   test the brain on a sample email
tests/                 pytest suite
render.yaml            one-click Render blueprint
```

## Quick start (local)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # fill in keys; see HOW_TO_RUN.md
python -m pytest -q
python -m scripts.try_email samples/club_event.txt
uvicorn app.main:app --reload
```

Then follow [HOW_TO_RUN.md](HOW_TO_RUN.md) to deploy.
