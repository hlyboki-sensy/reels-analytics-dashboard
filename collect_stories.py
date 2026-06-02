#!/usr/bin/env python3
"""Збирач сторіз — ловить АКТИВНІ сторіз (живі <24 год) та їхні інсайти й накопичує
історію в stories_media.json. Сторіз згоряє за 24 год і зникає з API назавжди,
тож цей скрипт треба запускати за розкладом (рекомендовано 2×/день).

Кожен запуск:
  • бере /me/stories (тільки активні зараз),
  • для кожної тягне інсайти (reach, replies, shares, перегляди, навігація…),
  • робить upsert у локальний файл за id (оновлює числа, поки сторіз ще жива).

Не потребує сервера й Flask — окремий незалежний скрипт для launchd/cron.
"""
import json, os, requests
from datetime import datetime

DIR = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(DIR, "stories_media.json")
LOG = os.path.join(DIR, "collect_stories.log")
BASE = "https://graph.instagram.com/v25.0"

# Метрики сторіз. Пробуємо повний набір, із відкатом на менший, якщо API щось не дає.
METRIC_SETS = [
    "reach,replies,shares,total_interactions,views,navigation,profile_activity,follows",
    "reach,replies,shares,total_interactions,views,navigation",
    "reach,replies,total_interactions,views",
    "reach,views",
    "reach",
]


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


def _get(url, params):
    try:
        return requests.get(url, params=params, timeout=60).json()
    except Exception as e:
        return {"error": str(e)}


def fetch_story_insights(mid):
    """Повертає (parsed_dict, raw_data_list). raw зберігаємо, щоб нічого не втратити
    (зокрема navigation із breakdown, формат якого уточнимо на живій сторіз)."""
    for ms in METRIC_SETS:
        r = _get(f"{BASE}/{mid}/insights", {"metric": ms, "access_token": TOKEN})
        data = r.get("data")
        if data:
            parsed = {}
            for item in data:
                name = item.get("name")
                if isinstance(item.get("values"), list) and item["values"]:
                    parsed[name] = item["values"][0].get("value")
                elif isinstance(item.get("total_value"), dict):
                    parsed[name] = item["total_value"].get("value")
            return parsed, data
    return {}, []


def main():
    now = datetime.utcnow().isoformat()
    store = {}
    if os.path.exists(STORE):
        try:
            store = {s["id"]: s for s in json.load(open(STORE, encoding="utf-8"))}
        except Exception:
            store = {}

    r = _get(f"{BASE}/me/stories", {
        "fields": "id,media_type,media_url,thumbnail_url,permalink,timestamp,caption",
        "access_token": TOKEN,
    })
    if "error" in r:
        line = f"{now} ПОМИЛКА: {r['error']}"
        open(LOG, "a", encoding="utf-8").write(line + "\n")
        print(line)
        return

    active = r.get("data", [])
    for s in active:
        mid = s["id"]
        parsed, raw = fetch_story_insights(mid)
        rec = store.get(mid, {})
        for k in ("id", "media_type", "permalink", "timestamp", "caption"):
            if s.get(k) is not None:
                rec[k] = s.get(k)
        if s.get("thumbnail_url"):
            rec["thumbnail_url"] = s["thumbnail_url"]
        if s.get("media_url"):
            rec["media_url"] = s["media_url"]
        rec.setdefault("first_seen", now)
        rec["last_seen"] = now
        rec["insights"] = parsed
        rec["insights_raw"] = raw
        store[mid] = rec

    data = sorted(store.values(), key=lambda x: x.get("timestamp", ""), reverse=True)
    json.dump(data, open(STORE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    line = f"{now} активних={len(active)} всього_збережено={len(data)}"
    open(LOG, "a", encoding="utf-8").write(line + "\n")
    print(line)


if __name__ == "__main__":
    main()
