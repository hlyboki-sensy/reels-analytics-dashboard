#!/usr/bin/env python3
"""Збирач підписників — щодня фіксує загальну кількість підписників (followers_count)
у followers_snapshots.json. На основі цих знімків + метрики нових підписників
дашборд оцінює, скільки людей ВІДПИСАЛОСЬ за день (відписки ≈ нові − чиста зміна).

API не віддає список підписників, тож «хто саме відписався» отримати неможливо —
лише кількість. Збирає лише вперед: запускати за розкладом (1×/день достатньо).
"""
import json, os, requests
from datetime import datetime

DIR = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(DIR, "followers_snapshots.json")
LOG = os.path.join(DIR, "collect_followers.log")
BASE = "https://graph.instagram.com/v25.0"


def _load_env():
    p = os.path.join(DIR, ".env")
    if not os.path.exists(p):
        return
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


_load_env()
TOKEN = os.environ.get("INSTAGRAM_TOKEN", "")


def main():
    now = datetime.utcnow()
    today = now.date().isoformat()
    store = {}
    if os.path.exists(STORE):
        try:
            store = json.load(open(STORE, encoding="utf-8"))
        except Exception:
            store = {}
    try:
        r = requests.get(f"{BASE}/me", params={"fields": "followers_count", "access_token": TOKEN}, timeout=60).json()
        if "error" in r:
            line = f"{now.isoformat()} ПОМИЛКА: {r['error']}"
            open(LOG, "a", encoding="utf-8").write(line + "\n"); print(line); return
        count = r.get("followers_count")
        if count is None:
            line = f"{now.isoformat()} ПОМИЛКА: немає followers_count"
            open(LOG, "a", encoding="utf-8").write(line + "\n"); print(line); return
        store[today] = count  # перезаписуємо останнім значенням за день
        json.dump(store, open(STORE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        line = f"{now.isoformat()} followers_count={count} (днів у базі: {len(store)})"
        open(LOG, "a", encoding="utf-8").write(line + "\n"); print(line)
    except Exception as e:
        line = f"{now.isoformat()} ПОМИЛКА: {e}"
        open(LOG, "a", encoding="utf-8").write(line + "\n"); print(line)


if __name__ == "__main__":
    main()
