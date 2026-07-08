"""
Freelancer.com US-project monitor with Telegram alerts.

Polls Freelancer's public projects API for newly-posted projects from the
United States, sends an immediate Telegram alert for each one, and records
the employer's id + reputation (posted / completed task counts) to
clients.json so you build up a history of who you applied to.

Run:  python freelancer_monitor.py
Config: copy .env.example to .env and fill in the three values.

# --- Telegram ---
# 1. Talk to @BotFather on Telegram, /newbot, copy the token here.
TELEGRAM_BOT_TOKEN=8777764312:AAGdyA84cBdZ6s_sz8JhNVVM8NU9vd-pNhw
# 2. Start a chat with your bot, send any message, then visit
#    https://api.telegram.org/bot<TOKEN>/getUpdates  to find your chat_id.
TELEGRAM_CHAT_ID=8232265461

# --- Freelancer (optional, only needed if you hit rate limits) ---
# Get an OAuth token from https://accounts.freelancer.com/settings/develop
FREELANCER_OAUTH_TOKEN=8777764312:AAGdyA84cBdZ6s_sz8JhNVVM8NU9vd-pNhw

# --- Behaviour ---
POLL_INTERVAL_SEC=60
COUNTRY_CODE=us
PROJECTS_LIMIT=30

"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
FREELANCER_OAUTH = os.getenv("FREELANCER_OAUTH_TOKEN", "").strip()

POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "60"))
COUNTRY_CODE = os.getenv("COUNTRY_CODE", "us").lower()
PROJECTS_LIMIT = int(os.getenv("PROJECTS_LIMIT", "30"))

API_BASE = "https://www.freelancer.com/api"
SEEN_FILE = ROOT / "seen_projects.json"
CLIENTS_FILE = ROOT / "clients.json"
STATS_FILE = ROOT / "category_stats.json"

DAILY_SUMMARY = os.getenv("DAILY_SUMMARY", "1").strip() not in ("0", "false", "False", "")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def freelancer_headers() -> dict[str, str]:
    h = {"Accept": "application/json", "User-Agent": "fl-monitor/1.0"}
    if FREELANCER_OAUTH:
        h["freelancer-oauth-v1"] = FREELANCER_OAUTH
    return h


def fetch_active_projects() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Pull recently-posted active projects filtered by country.

    Returns (projects, users_map) where users_map is the `result.users` dict
    Freelancer attaches when `user_details=true`. Keys are user-id strings.
    """
    url = f"{API_BASE}/projects/0.1/projects/active/"
    params = {
        "countries[]": COUNTRY_CODE,
        "limit": PROJECTS_LIMIT,
        "compact": "false",
        "full_description": "true",
        "job_details": "true",
        "user_details": "true",
        "user_employer_reputation": "true",
        "user_country_details": "true",
        "user_status": "true",
    }
    r = requests.get(url, params=params, headers=freelancer_headers(), timeout=20)
    r.raise_for_status()
    payload = r.json()
    if payload.get("status") != "success":
        raise RuntimeError(f"API error: {payload}")
    result = payload.get("result") or {}
    projects = result.get("projects") or []
    users = result.get("users") or {}
    # API sometimes keys users as int, sometimes as str — normalise to str.
    users_map = {str(k): v for k, v in users.items()} if isinstance(users, dict) else {}
    return projects, users_map


def _owner_id(project: dict[str, Any]) -> int | None:
    """Robustly extract a project's owner id. The active-projects endpoint
    usually returns `owner_id` at the top level, but for some project types
    (contests, prototyping, anonymised drafts) the only signal is a nested
    `owner.id`."""
    oid = project.get("owner_id")
    if oid:
        return oid
    owner = project.get("owner") or {}
    return owner.get("id")


def _resolve_user(
    owner_id: int | None,
    project: dict[str, Any],
    users_map: dict[str, Any],
) -> dict[str, Any] | None:
    """Resolve the project owner's user record.

    Preference order:
      1. `result.users` map from the projects call (richest, includes
         employer_reputation when user_employer_reputation=true was sent).
      2. Inline `project.owner` dict (no reputation, but has name/country).
      3. A separate /users/0.1/users/{id} call.
    """
    if owner_id is not None:
        u = users_map.get(str(owner_id))
        if u:
            return u
    inline = project.get("owner")
    if isinstance(inline, dict) and (inline.get("username") or inline.get("public_name")):
        return inline
    if owner_id is not None:
        return fetch_user(owner_id)
    return None


def fetch_user(user_id: int) -> dict[str, Any] | None:
    url = f"{API_BASE}/users/0.1/users/{user_id}"
    params = {
        "reputation": "true",
        "employer_reputation": "true",
        "profile_description": "false",
        "jobs": "false",
        "location_details": "true",
    }
    try:
        r = requests.get(url, params=params, headers=freelancer_headers(), timeout=20)
        r.raise_for_status()
        return r.json().get("result")
    except requests.RequestException as e:
        log(f"  ! user fetch failed for {user_id}: {e}")
        return None


def employer_stats(user: dict[str, Any]) -> dict[str, Any]:
    """Pull post/complete counts from employer_reputation.entire_history."""
    rep = (user or {}).get("employer_reputation") or {}
    hist = rep.get("entire_history") or {}
    return {
        "posted": hist.get("all", 0),
        "completed": hist.get("complete", 0),
        "incomplete": hist.get("incomplete", 0),
        "rating": round(hist.get("overall", 0) or 0, 2),
        "reviews": hist.get("reviews", 0),
        "earnings_score": rep.get("earnings_score", 0),
    }


def send_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("  ! Telegram not configured — skipping send")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=15,
        )
        if r.status_code != 200:
            log(f"  ! Telegram {r.status_code}: {r.text[:200]}")
    except requests.RequestException as e:
        log(f"  ! Telegram send failed: {e}")


def format_alert(
    project: dict[str, Any],
    stats: dict[str, Any],
    client_entry: dict[str, Any] | None = None,
) -> str:
    seo_url = project.get("seo_url") or ""
    link = f"https://www.freelancer.com/projects/{seo_url}" if seo_url else \
           f"https://www.freelancer.com/projects/{project.get('id')}"

    title = (project.get("title") or "Untitled").replace("<", "&lt;").replace(">", "&gt;")
    budget = project.get("budget") or {}
    bmin, bmax = budget.get("minimum"), budget.get("maximum")
    currency = (project.get("currency") or {}).get("code", "")
    project_type = project.get("type", "")
    bid_count = project.get("bid_stats", {}).get("bid_count", 0)
    desc = (project.get("preview_description") or "")[:300]

    jobs = project.get("jobs") or []
    skill_names = [j.get("name") for j in jobs if j.get("name")]
    skills_line = ", ".join(skill_names) if skill_names else "—"

    client_name = (client_entry or {}).get("username") \
        or (client_entry or {}).get("public_name") \
        or f"#{project.get('owner_id')}"
    summary_line = format_client_summary(client_entry)

    return (
        f"🇺🇸 <b>New US task</b>\n"
        f"<b>{title}</b>\n"
        f"💰 {bmin}-{bmax} {currency} ({project_type})  |  📨 {bid_count} bids\n"
        f"🛠 <b>Skills:</b> {skills_line}\n"
        f"\n<i>{desc}</i>\n\n"
        f"👤 <b>{client_name}</b>  (Client #{project.get('owner_id')})\n"
        f"   posted: <b>{stats['posted']}</b>  |  completed: <b>{stats['completed']}</b>  "
        f"|  incomplete: {stats['incomplete']}\n"
        f"   ⭐ {stats['rating']}/5 ({stats['reviews']} reviews)\n"
        + (f"   {summary_line}\n" if summary_line else "")
        + f"\n🔗 {link}"
    )


def empty_stats() -> dict[str, Any]:
    return {
        "since": datetime.now(timezone.utc).isoformat(),
        "total_projects": 0,
        "by_category": {},   # category name -> {"total": int, "skills": {skill: int}}
        "by_skill": {},      # skill name -> int  (flat, across all categories)
        "last_summary_date": "",
    }


def update_stats(stats: dict[str, Any], project: dict[str, Any]) -> None:
    """Increment category + skill counters from a project's `jobs` list.

    A project that lists N skills bumps each of those N skills by 1, and bumps
    each *unique* parent category by 1 (so a project tagged "React.js" +
    "Node.js" — both under "Websites, IT & Software" — adds 1 to that
    category, not 2).
    """
    stats["total_projects"] = stats.get("total_projects", 0) + 1
    stats.setdefault("by_category", {})
    stats.setdefault("by_skill", {})

    jobs = project.get("jobs") or []
    seen_categories: set[str] = set()
    for j in jobs:
        skill = (j.get("name") or "").strip()
        cat = ((j.get("category") or {}).get("name") or "Uncategorized").strip()
        if not skill:
            continue

        cat_entry = stats["by_category"].setdefault(cat, {"total": 0, "skills": {}})
        if cat not in seen_categories:
            cat_entry["total"] += 1
            seen_categories.add(cat)
        cat_entry["skills"][skill] = cat_entry["skills"].get(skill, 0) + 1

        stats["by_skill"][skill] = stats["by_skill"].get(skill, 0) + 1


def format_stats_report(stats: dict[str, Any], top_skills_per_cat: int = 8) -> str:
    total = stats.get("total_projects", 0)
    since = (stats.get("since") or "")[:10]
    cats = stats.get("by_category", {})

    if not total or not cats:
        return f"📊 <b>Category stats</b> (since {since})\nNo projects recorded yet."

    sorted_cats = sorted(cats.items(), key=lambda kv: kv[1].get("total", 0), reverse=True)

    lines = [f"📊 <b>Category stats</b> — {total} projects since {since}", ""]
    for cat, data in sorted_cats:
        cat_total = data.get("total", 0)
        skills = data.get("skills", {})
        top = sorted(skills.items(), key=lambda kv: kv[1], reverse=True)[:top_skills_per_cat]
        skill_str = ", ".join(f"{s}: {n}" for s, n in top)
        if len(skills) > top_skills_per_cat:
            skill_str += f", … (+{len(skills) - top_skills_per_cat})"
        lines.append(f"<b>{cat}</b>: {cat_total}")
        if skill_str:
            lines.append(f"   ({skill_str})")
    return "\n".join(lines)


def maybe_send_daily_summary(stats: dict[str, Any]) -> bool:
    """Send a Telegram report once per local day. Returns True if sent."""
    if not DAILY_SUMMARY:
        return False
    today = datetime.now().strftime("%Y-%m-%d")
    if stats.get("last_summary_date") == today:
        return False
    if stats.get("total_projects", 0) == 0:
        return False
    send_telegram(format_stats_report(stats))
    stats["last_summary_date"] = today
    return True


def _client_country(user: dict[str, Any] | None) -> str:
    """Project country always == COUNTRY_CODE (we filter on it). User account
    country can differ, but for the per-task summary we want the project
    market — which is what the user asked for."""
    return COUNTRY_CODE.upper()


def record_client(
    clients: dict[str, Any],
    project: dict[str, Any],
    stats: dict[str, Any],
    user: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Upsert the client record. Returns the (post-update) entry so the alert
    formatter can show this-bot's history for the client."""
    owner_id = str(project.get("owner_id"))
    if not owner_id or owner_id == "None":
        return None

    entry = clients.get(owner_id, {
        "projects": [],
        "categories": {},   # category name -> count of projects from this client tagged in it
        "skills": {},       # skill name -> count
    })
    entry.setdefault("projects", [])
    entry.setdefault("categories", {})
    entry.setdefault("skills", {})

    entry["last_seen"] = datetime.now(timezone.utc).isoformat()
    entry["posted"] = stats["posted"]
    entry["completed"] = stats["completed"]
    entry["incomplete"] = stats["incomplete"]
    entry["rating"] = stats["rating"]
    entry["reviews"] = stats["reviews"]

    if user:
        username = (user.get("username") or "").strip()
        public_name = (user.get("public_name") or user.get("display_name") or "").strip()
        if username:
            entry["username"] = username
        if public_name:
            entry["public_name"] = public_name
    entry["country"] = _client_country(user)

    pid = project.get("id")
    if pid and pid not in entry["projects"]:
        entry["projects"].append(pid)
        # Only credit categories/skills the first time we see a project.
        jobs = project.get("jobs") or []
        seen_cats: set[str] = set()
        for j in jobs:
            cat = ((j.get("category") or {}).get("name") or "Uncategorized").strip()
            skill = (j.get("name") or "").strip()
            if cat and cat not in seen_cats:
                entry["categories"][cat] = entry["categories"].get(cat, 0) + 1
                seen_cats.add(cat)
            if skill:
                entry["skills"][skill] = entry["skills"].get(skill, 0) + 1

    clients[owner_id] = entry
    return entry


def format_client_summary(entry: dict[str, Any] | None, max_categories: int = 3) -> str:
    """One-line per-client recap, e.g. `honeviz (US) — posts: 13, Design: 6, …`."""
    if not entry:
        return ""
    name = entry.get("username") or entry.get("public_name") or "unknown"
    country = entry.get("country") or COUNTRY_CODE.upper()
    posts = len(entry.get("projects", []))
    cats = entry.get("categories", {})
    top = sorted(cats.items(), key=lambda kv: kv[1], reverse=True)[:max_categories]
    parts = [f"posts: {posts}"] + [f"{c}: {n}" for c, n in top]
    return f"📊 <b>{name}</b> ({country}) — " + ", ".join(parts)


def tick(seen: set[int], clients: dict[str, Any], cat_stats: dict[str, Any]) -> None:
    projects, users_map = fetch_active_projects()
    fresh = [p for p in projects if p.get("id") not in seen]
    log(f"fetched {len(projects)} active US projects, {len(fresh)} new")

    # Newest first so most-recent posts arrive last (top of Telegram).
    fresh.sort(key=lambda p: p.get("submitdate") or 0)

    for p in fresh:
        owner_id = _owner_id(p)
        if owner_id is not None:
            # Normalise so downstream code (record_client key, alert footer)
            # sees a real id even when the API hid it under `owner`.
            p["owner_id"] = owner_id

        user = _resolve_user(owner_id, p, users_map)
        stats = employer_stats(user) if user else {
            "posted": 0, "completed": 0, "incomplete": 0,
            "rating": 0, "reviews": 0, "earnings_score": 0,
        }
        # Record first so the alert can quote this-bot's running counts for
        # the client (including the project being announced).
        client_entry = record_client(clients, p, stats, user)
        update_stats(cat_stats, p)
        send_telegram(format_alert(p, stats, client_entry))
        seen.add(p["id"])
        log(f"  → alerted: {p.get('id')} {p.get('title','')[:60]}")

    summary_sent = maybe_send_daily_summary(cat_stats)
    if summary_sent:
        log("  → sent daily category summary")

    if fresh or summary_sent:
        save_json(SEEN_FILE, sorted(seen))
        save_json(CLIENTS_FILE, clients)
        save_json(STATS_FILE, cat_stats)


def main() -> int:
    # CLI: `--stats` prints the current report and sends it to Telegram, then exits.
    if len(sys.argv) > 1 and sys.argv[1] in ("--stats", "-s"):
        cat_stats = load_json(STATS_FILE, empty_stats())
        report = format_stats_report(cat_stats)
        print(report)
        send_telegram(report)
        return 0

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("WARNING: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in .env — alerts disabled.")

    seen = set(load_json(SEEN_FILE, []))
    clients = load_json(CLIENTS_FILE, {})
    cat_stats = load_json(STATS_FILE, empty_stats())
    log(f"loaded {len(seen)} seen projects, {len(clients)} known clients, "
        f"{cat_stats.get('total_projects', 0)} stat-tracked projects")
    log(f"polling every {POLL_INTERVAL_SEC}s for country={COUNTRY_CODE}")

    # First tick: do NOT alert on the historical backlog — just seed the seen set.
    if not seen:
        try:
            initial = fetch_active_projects()
            for p in initial:
                seen.add(p["id"])
            save_json(SEEN_FILE, sorted(seen))
            log(f"seeded {len(seen)} existing projects (no alerts for these)")
        except requests.RequestException as e:
            log(f"initial fetch failed: {e}")

    while True:
        try:
            tick(seen, clients, cat_stats)
        except requests.RequestException as e:
            log(f"network error: {e}")
        except Exception as e:
            log(f"tick error: {e!r}")
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("stopped.")
