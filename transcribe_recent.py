#!/usr/bin/env python3
"""Разово розшифровує останні N рілсів (Whisper) і дописує транскрипти в reels_data.json."""
import sys, os, requests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app

N = int(sys.argv[1]) if len(sys.argv) > 1 else 20

# свіжі media_url (підписані лінки протерміновуються, тож тягнемо заново)
r = requests.get(f"{app.BASE}/me/media", params={
    "fields": "id,media_type,timestamp,media_url", "limit": 60, "access_token": app.TOKEN
}, timeout=60).json()
vids = [m for m in r.get("data", []) if m.get("media_type") == "VIDEO"][:N]

data = app.load_data()
byid = {x["id"]: x for x in data}
done = skipped = 0
for i, m in enumerate(vids, 1):
    mid = m["id"]
    rec = byid.get(mid)
    if rec is None:
        continue
    if rec.get("transcript"):
        skipped += 1
        continue
    if not m.get("media_url"):
        continue
    print(f"[{i}/{len(vids)}] розшифровую {mid} …", flush=True)
    txt = app.download_and_transcribe(m["media_url"], mid)
    rec["transcript"] = txt
    done += 1
    print(f"    ✓ {len(txt)} символів", flush=True)

app.save_data(data)
print(f"ГОТОВО: нових розшифровок {done}, уже були {skipped}, опрацьовано рілсів {len(vids)}", flush=True)
