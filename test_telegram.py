"""One-shot test: prove .env loads and Telegram delivers a message."""

import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

if not token or not chat_id:
    print("MISSING values in .env:")
    print(f"  TELEGRAM_BOT_TOKEN = {'SET' if token else 'MISSING'}")
    print(f"  TELEGRAM_CHAT_ID   = {'SET' if chat_id else 'MISSING'}")
    sys.exit(1)

print(f"TOKEN: {token[:15]}...")
print(f"CHAT : {chat_id}")
print("sending test message...")

url = f"https://api.telegram.org/bot{token}/sendMessage"
r = requests.post(
    url,
    json={"chat_id": chat_id, "text": "test from monitor OK"},
    timeout=15,
)
print(f"HTTP {r.status_code}")
print(r.text[:300])

if r.status_code == 200:
    print("\nSUCCESS — check your Telegram chat.")
else:
    print("\nFAILED — see body above. Most common causes:")
    print("  401 -> token is wrong")
    print("  400 chat not found -> chat id wrong, or you never messaged the bot")
