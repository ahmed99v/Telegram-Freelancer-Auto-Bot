# Freelancer.com US-task Telegram monitor

Polls `freelancer.com` every 60 s for new **active projects posted by US clients**, pushes an immediate Telegram alert for each, and saves the client's freelancer id with their posted/completed task counts to `clients.json`.

## What the alert looks like

```
🇺🇸 New US task
Build a Shopify -> Xero sync app
💰 250-750 USD (fixed)  |  📨 4 bids
🛠 Skills: React.js, Node.js, Shopify

<short description excerpt>

👤 honeviz  (Client #12345678)
   posted: 23  |  completed: 18  |  incomplete: 1
   ⭐ 4.8/5 (16 reviews)
   📊 honeviz (US) — posts: 13, Websites, IT & Software: 10, Design, Media & Architecture: 3
(But the client info doesn't work)
🔗 https://www.freelancer.com/projects/<seo-url>
```

The 📊 line shows **this bot's** running history for the client (not Freelancer's lifetime counter — that's the `posted: 23` line above). The `posts:` number reflects how many tasks from that client this bot has seen, broken down by their top 3 categories.

## Setup (one-time)

```powershell
cd "c:\WORK\Ahmed\GIT OWN REPO\freelancer-monitor"
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
notepad .env
```

In `.env` fill in:

1. **TELEGRAM_BOT_TOKEN** — message `@BotFather` in Telegram, send `/newbot`, copy the token it gives you.
2. **TELEGRAM_CHAT_ID** — start a chat with your new bot, send it any message, then open
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   in a browser and copy the `"chat":{"id": ...}` number.
3. `FREELANCER_OAUTH_TOKEN` is optional — leave blank unless you start hitting rate limits.

## Run

```powershell
.\.venv\Scripts\Activate.ps1
python freelancer_monitor.py
```

The first run **does not alert on existing projects** — it seeds `seen_projects.json` with the current backlog so you only get alerts for truly new posts after that.

Leave the terminal running. To run hands-free at boot, register it as a Windows scheduled task pointing at `.\.venv\Scripts\python.exe freelancer_monitor.py`.

## Files it writes

| File | What |
|------|------|
| `seen_projects.json` | project ids already alerted on (so the script never double-alerts) |
| `clients.json` | client id → `{posted, completed, incomplete, rating, reviews, projects[], last_seen}` — grows over time |
| `category_stats.json` | running counter of how many projects were posted per **category** and per **skill** since the script first ran |

## Category / skill statistics

Every project's skill tags get rolled up into `category_stats.json` so you can see, e.g., how much *Design* vs *React* work showed up this month.

- The per-task Telegram alerts are **unchanged** — stats are not appended to each task message.
- Instead, a **daily summary** is posted to the same Telegram chat once per day (the first tick after local midnight). Disable with `DAILY_SUMMARY=0` in `.env`.
- Run `python freelancer_monitor.py --stats` at any time to print the current report and push it to Telegram on demand.

Sample report:

```
📊 Category stats — 565 projects since 2026-05-25
Design, Media & Architecture: 250
   (Interior Design: 60, SolidWorks: 50, AutoCAD: 40, …)
Websites, IT & Software: 145
   (WordPress: 20, React.js: 10, Android: 8, …)
Engineering & Science: 70
   (Electronic Engineering: 20, C Programming: 18, C# Programming: 12, …)
```

A project tagged with multiple skills counts once per category (not once per skill within it), so the category totals reflect distinct projects.

## Tuning

Edit `.env`:

- `POLL_INTERVAL_SEC=60` — how often to check. 30–60 s is a good range; below 20 s risks rate-limiting.
- `COUNTRY_CODE=us` — change to monitor a different country (`gb`, `au`, `ca`, …).
- `PROJECTS_LIMIT=30` — how many projects per poll. 30 is plenty since you only poll once a minute.

## Inspecting clients later

`clients.json` is plain JSON — open it in any editor, or query with PowerShell:

```powershell
Get-Content clients.json | ConvertFrom-Json | ForEach-Object {
    $_.PSObject.Properties | ForEach-Object {
        "{0,-12} posted={1,-3} completed={2}" -f $_.Name, $_.Value.posted, $_.Value.completed
    }
}
```



The client information didn't been fetched yet.