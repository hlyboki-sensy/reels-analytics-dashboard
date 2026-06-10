from flask import Flask, jsonify, render_template_string, request
import requests, json, os, subprocess, threading, time, base64
from datetime import datetime
import anthropic

app = Flask(__name__)

# Підвантажуємо .env (простий лоадер, без зовнішніх залежностей)
def _load_env():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

_load_env()

TOKEN = os.environ.get("INSTAGRAM_TOKEN", "YOUR_INSTAGRAM_TOKEN_HERE")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_KEY_HERE")
BASE = "https://graph.instagram.com/v25.0"
DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = f"{DIR}/reels_data.json"
POSTS_FILE = f"{DIR}/posts_data.json"
STORIES_FILE = f"{DIR}/stories_data.json"
AUDIO_DIR = f"{DIR}/audio"
os.makedirs(AUDIO_DIR, exist_ok=True)

status = {"running": False, "message": "Готово", "progress": 0, "total": 0}

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _api_get(url, params=None, tries=4, base_sleep=1.5):
    """GET до Graph API з ретраями — стійко до обривів зʼєднання й тимчасових збоїв.
    Важливо для великих акаунтів, де довга серія запитів інколи рветься."""
    last = None
    for i in range(tries):
        try:
            return requests.get(url, params=(params or {}), timeout=60).json()
        except Exception as e:
            last = e
            time.sleep(base_sleep * (i + 1))
    raise last

def fetch_insights(media_id):
    r = _api_get(f"{BASE}/{media_id}/insights", {
        "metric": "reach,saved,shares,total_interactions,views",
        "access_token": TOKEN
    })
    result = {}
    for item in r.get("data", []):
        result[item["name"]] = item["values"][0]["value"]
    return result

def _insights_breakdown(metrics, since, until):
    """Акаунт-аналітика total_value з розбивкою по типу контенту (STORY/REEL/POST...).
    Повертає {metric: {'total': N, 'by': {'STORY': N, 'REEL': N, ...}}}."""
    r = requests.get(f"{BASE}/me/insights", params={
        "metric": ",".join(metrics),
        "period": "day",
        "metric_type": "total_value",
        "breakdown": "media_product_type",
        "since": since,
        "until": until,
        "access_token": TOKEN,
    }).json()
    out = {}
    for item in r.get("data", []):
        tv = item.get("total_value", {}) or {}
        by = {}
        bds = tv.get("breakdowns", []) or []
        if bds:
            for res in bds[0].get("results", []):
                dv = res.get("dimension_values", [])
                if dv:
                    by[dv[0]] = res.get("value", 0)
        out[item["name"]] = {"total": tv.get("value", 0), "by": by}
    return out

def fetch_stories(since, until):
    """Збирає аналітику сторіз за довільний діапазон дат [since, until]: агрегат + тренд.
    since/until — об'єкти date. Дані з акаунт-аналітики (media_product_type=STORY) —
    працює і для минулих періодів (на відміну від /me/stories, що дає лише активні)."""
    from datetime import timedelta
    span = max(1, (until - since).days)
    metrics = ["reach", "total_interactions", "shares"]
    summ = _insights_breakdown(metrics, since.isoformat(), until.isoformat())

    def sv(m):
        return summ.get(m, {}).get("by", {}).get("STORY", 0)

    total_reach = summ.get("reach", {}).get("total", 0)
    story_reach = sv("reach")
    summary = {
        "reach": story_reach,
        "interactions": sv("total_interactions"),
        "shares": sv("shares"),
        "share_of_reach": round(story_reach / total_reach * 100, 1) if total_reach else 0,
    }

    # Тренд по «кошиках». Розмір кошика підбираємо так, щоб було ~10-13 точок.
    bucket_days = max(1, span // 12)
    trend = []
    d = since
    while d < until:
        b_end = min(d + timedelta(days=bucket_days), until)
        wk = _insights_breakdown(["reach", "total_interactions"], d.isoformat(), b_end.isoformat())
        trend.append({
            "week": d.isoformat(),
            "reach": wk.get("reach", {}).get("by", {}).get("STORY", 0),
            "interactions": wk.get("total_interactions", {}).get("by", {}).get("STORY", 0),
        })
        d = b_end

    return {
        "summary": summary,
        "trend": trend,
        "days": span,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "bucket_days": bucket_days,
        "updated": datetime.utcnow().isoformat(),
    }

_whisper_model = None

def _get_whisper_model():
    """Лінива ініціалізація моделі faster-whisper (вантажиться один раз)."""
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        size = os.environ.get("WHISPER_MODEL", "small")
        _whisper_model = WhisperModel(size, device="cpu", compute_type="int8")
    return _whisper_model

def download_and_transcribe(media_url, media_id):
    """Завантажує відео й розшифровує його через faster-whisper (без ffmpeg)."""
    txt_path = f"{AUDIO_DIR}/{media_id}.txt"
    if os.path.exists(txt_path):
        return open(txt_path, encoding="utf-8").read().strip()
    video_path = f"{AUDIO_DIR}/{media_id}.mp4"
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                 "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
        last_err = None
        for attempt in range(3):
            try:
                resp = requests.get(media_url, headers=headers, timeout=180)
                resp.raise_for_status()
                with open(video_path, "wb") as f:
                    f.write(resp.content)
                break
            except Exception as de:
                last_err = de
                time.sleep(2)
        else:
            raise last_err
        model = _get_whisper_model()
        lang = os.environ.get("WHISPER_LANGUAGE", "uk")
        segments, _info = model.transcribe(
            video_path,
            language=lang,
            beam_size=5,
            vad_filter=True,                      # вирізає музику/тишу → менше «галюцинацій»
            vad_parameters=dict(min_silence_duration_ms=500),
            condition_on_previous_text=False,     # не тягне попередній текст → менше зациклень
            no_speech_threshold=0.6,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(text)
        return text
    except Exception as e:
        print(f"Whisper error {media_id}: {e}")
        return ""
    finally:
        if os.path.exists(video_path):
            try:
                os.remove(video_path)
            except OSError:
                pass

def load_posts():
    if os.path.exists(POSTS_FILE):
        with open(POSTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []

def fetch_post_insights(media_id):
    """Інсайти допису (фото/каруселі стрічки)."""
    r = _api_get(f"{BASE}/{media_id}/insights", {
        "metric": "reach,saved,shares,total_interactions,views,profile_visits,follows",
        "access_token": TOKEN,
    })
    result = {}
    for item in r.get("data", []):
        try:
            result[item["name"]] = item["values"][0]["value"]
        except Exception:
            pass
    return result

def sync_posts():
    """Синхронізує дописи стрічки (IMAGE + CAROUSEL_ALBUM) з інсайтами."""
    global status
    status["running"] = True
    status["message"] = "Завантажую список дописів..."
    try:
        url = f"{BASE}/me/media"
        params = {"fields": "id,caption,media_type,timestamp,like_count,comments_count,media_url,permalink,thumbnail_url", "limit": 100, "access_token": TOKEN}
        all_media = []
        while url:
            r = _api_get(url, params if not all_media else {})
            all_media.extend(r.get("data", []))
            url = r.get("paging", {}).get("next")
            params = {}

        posts = [m for m in all_media if m.get("media_type") in ("IMAGE", "CAROUSEL_ALBUM")]
        status["total"] = len(posts)
        existing = {p["id"]: p for p in load_posts()}
        enriched = []
        for i, post in enumerate(posts):
            mid = post["id"]
            status["progress"] = i + 1
            status["message"] = f"[{i+1}/{len(posts)}] Допис {post.get('timestamp','')[:10]}"
            if mid in existing and existing[mid].get("insights"):
                # оновлюємо лайки/коменти (вони змінюються), решту лишаємо
                existing[mid]["like_count"] = post.get("like_count", existing[mid].get("like_count", 0))
                existing[mid]["comments_count"] = post.get("comments_count", existing[mid].get("comments_count", 0))
                enriched.append(existing[mid])
                continue
            post["insights"] = fetch_post_insights(mid)
            enriched.append(post)
            if len(enriched) % 50 == 0:  # проміжне збереження (стійкість до обривів)
                with open(POSTS_FILE, "w", encoding="utf-8") as f:
                    json.dump(enriched, f, ensure_ascii=False, indent=2)
            time.sleep(0.3)

        with open(POSTS_FILE, "w", encoding="utf-8") as f:
            json.dump(enriched, f, ensure_ascii=False, indent=2)
        status["message"] = f"✅ Готово! Оновлено дописів: {len(enriched)}"
    except Exception as e:
        try:  # зберігаємо частковий прогрес — наступний запуск продовжить
            with open(POSTS_FILE, "w", encoding="utf-8") as f:
                json.dump(enriched, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        status["message"] = f"❌ Помилка (збережено {len(enriched)}): {e}"
    finally:
        status["running"] = False

def sync_reels(with_whisper=False):
    global status
    status["running"] = True
    status["message"] = "Завантажую список рілсів..."

    try:
        # Отримуємо всі медіа
        url = f"{BASE}/me/media"
        params = {"fields": "id,caption,media_type,timestamp,like_count,comments_count,media_url,permalink,thumbnail_url", "limit": 100, "access_token": TOKEN}
        all_media = []
        while url:
            r = _api_get(url, params if not all_media else {})
            all_media.extend(r.get("data", []))
            url = r.get("paging", {}).get("next")
            params = {}
        
        reels = [m for m in all_media if m["media_type"] == "VIDEO"]
        status["total"] = len(reels)

        # Завантажуємо наявні
        existing = {r["id"]: r for r in load_data()}
        enriched = []

        for i, reel in enumerate(reels):
            mid = reel["id"]
            status["progress"] = i + 1
            status["message"] = f"[{i+1}/{len(reels)}] Обробляю {reel.get('timestamp','')[:10]}"

            if mid in existing and existing[mid].get("insights") and (not with_whisper or existing[mid].get("transcript") is not None):
                if not with_whisper or existing[mid].get("transcript"):
                    enriched.append(existing[mid])
                    continue

            reel["insights"] = fetch_insights(mid)
            reel["transcript"] = existing.get(mid, {}).get("transcript", "")

            if with_whisper and not reel["transcript"] and reel.get("media_url"):
                status["message"] = f"[{i+1}/{len(reels)}] 🎤 Whisper розшифровує..."
                reel["transcript"] = download_and_transcribe(reel["media_url"], mid)

            enriched.append(reel)
            time.sleep(0.3)

        save_data(enriched)
        status["message"] = f"✅ Готово! Оновлено рілсів: {len(enriched)}"
    except Exception as e:
        status["message"] = f"❌ Помилка: {e}"
    finally:
        status["running"] = False

@app.route("/")
def index():
    return render_template_string(open(f"{DIR}/index.html").read())

@app.route("/api/reels")
def api_reels():
    return jsonify(load_data())

@app.route("/api/sync")
def api_sync():
    if status["running"]:
        return jsonify({"error": "Вже запущено"})
    whisper = request.args.get("whisper") == "1"
    threading.Thread(target=sync_reels, args=(whisper,), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/meta")
def api_meta():
    """Підпис автора у футері + кількість підписників (для ER за підписниками)."""
    handle = os.environ.get("CREDIT_HANDLE", "hlyboki_sensy").lstrip("@").strip()
    fc = 0
    try:
        r = _api_get(f"{BASE}/me", {"fields": "followers_count", "access_token": TOKEN})
        fc = r.get("followers_count", 0) or 0
    except Exception:
        pass
    return jsonify({
        "handle": handle,
        "url": f"https://instagram.com/{handle}" if handle else "",
        "followers_count": fc,
    })

@app.route("/api/posts")
def api_posts():
    return jsonify(load_posts())

@app.route("/api/demographics")
def api_demographics():
    """Демографія підписників: вік, стать, місто, країна (за 90 днів)."""
    try:
        out = {}
        for bd in ("age", "gender", "city", "country"):
            r = requests.get(f"{BASE}/me/insights", params={
                "metric": "follower_demographics", "period": "lifetime",
                "metric_type": "total_value", "breakdown": bd,
                "timeframe": "last_90_days", "access_token": TOKEN,
            }).json()
            rows = []
            try:
                for res in r["data"][0]["total_value"]["breakdowns"][0]["results"]:
                    rows.append({"k": res["dimension_values"][0], "v": res.get("value", 0)})
            except Exception:
                pass
            rows.sort(key=lambda x: -x["v"])
            out[bd] = rows
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/sync_posts")
def api_sync_posts():
    if status["running"]:
        return jsonify({"error": "Вже запущено"})
    threading.Thread(target=sync_posts, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/status")
def api_status():
    return jsonify(status)

@app.route("/api/analysis")
def api_analysis():
    afile = f"{DIR}/analysis.json"
    if os.path.exists(afile):
        with open(afile, encoding="utf-8") as f:
            return jsonify(json.load(f))
    return jsonify({"error": "no data"})

@app.route("/api/followers")
def api_followers():
    """Приріст підписників по днях за обраний діапазон (?since=&until= або ?days=).
    follower_count в API обмежений ~30 днями на запит, тож ділимо на вікна."""
    from datetime import timedelta, date as _date
    today = datetime.utcnow().date()
    since_s = request.args.get("since")
    until_s = request.args.get("until")
    try:
        if since_s and until_s:
            since = _date.fromisoformat(since_s)
            until = _date.fromisoformat(until_s)
        else:
            try:
                days = int(request.args.get("days", "90"))
            except ValueError:
                days = 90
            until = today
            since = today - timedelta(days=days)
        if until > today:
            until = today
        if since >= until:
            since = until - timedelta(days=1)
    except ValueError:
        until = today
        since = today - timedelta(days=90)
    try:
        url = f"{BASE}/me/insights"
        result = {}
        d = since
        while d < until:
            chunk_end = min(d + timedelta(days=30), until)
            r = requests.get(url, params={
                "metric": "follower_count", "period": "day",
                "since": d.isoformat(), "until": chunk_end.isoformat(),
                "access_token": TOKEN,
            }).json()
            for item in r.get("data", []):
                if item["name"] == "follower_count":
                    for v in item["values"]:
                        result[v["end_time"][:10]] = v["value"]
            d = chunk_end
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)})

def _fetch_new_followers(since, until):
    """{date: к-сть нових підписників} за діапазон (chunked по 30 днів)."""
    from datetime import timedelta
    url = f"{BASE}/me/insights"
    result = {}
    d = since
    while d < until:
        ce = min(d + timedelta(days=30), until)
        r = requests.get(url, params={
            "metric": "follower_count", "period": "day",
            "since": d.isoformat(), "until": ce.isoformat(), "access_token": TOKEN,
        }).json()
        for item in r.get("data", []):
            if item["name"] == "follower_count":
                for v in item["values"]:
                    result[v["end_time"][:10]] = v["value"]
        d = ce
    return result

@app.route("/api/unfollows")
def api_unfollows():
    """Оцінка відписок по днях: відписки ≈ нові підписники − чиста зміна загальної к-сті.
    Спирається на щоденні знімки followers_snapshots.json (збирач collect_followers.py).
    Поіменно «хто» — API не дає; лише кількість, і лише вперед від старту збору."""
    from datetime import timedelta, date as _date
    today = datetime.utcnow().date()
    since_s = request.args.get("since")
    until_s = request.args.get("until")
    try:
        if since_s and until_s:
            since = _date.fromisoformat(since_s); until = _date.fromisoformat(until_s)
        else:
            days = int(request.args.get("days", "90"))
            until = today; since = today - timedelta(days=days)
        if until > today: until = today
        if since >= until: since = until - timedelta(days=1)
    except Exception:
        until = today; since = today - timedelta(days=90)

    path = os.path.join(DIR, "followers_snapshots.json")
    snaps = {}
    if os.path.exists(path):
        try:
            snaps = json.load(open(path, encoding="utf-8"))
        except Exception:
            snaps = {}
    if len([d for d in snaps]) < 2:
        return jsonify({"ready": False, "snapshots": len(snaps), "rows": []})

    try:
        gross = _fetch_new_followers(since, until)
        dates = sorted(snaps.keys())
        rows = []
        for i in range(1, len(dates)):
            d_prev, d_cur = dates[i - 1], dates[i]
            if not (since.isoformat() <= d_cur <= until.isoformat()):
                continue
            net = snaps[d_cur] - snaps[d_prev]
            # сумуємо нових за всі дні проміжку (на випадок пропущених днів)
            pd = _date.fromisoformat(d_prev); cd = _date.fromisoformat(d_cur)
            gained = 0; x = pd + timedelta(days=1)
            while x <= cd:
                gained += gross.get(x.isoformat(), 0); x += timedelta(days=1)
            lost = max(0, gained - net)
            rows.append({"date": d_cur, "gained": gained, "lost": lost, "net": net})
        return jsonify({"ready": True, "snapshots": len(snaps), "rows": rows})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/stories")
def api_stories():
    """Аналітика сторіз за діапазон дат (?since=YYYY-MM-DD&until=YYYY-MM-DD)
    або за пресет (?days=7|30|60|90, типово 90). Кеш у stories_data.json
    окремо для кожного діапазону (TTL 6 год), ?refresh=1 — оновити."""
    from datetime import timedelta, date as _date
    force = request.args.get("refresh") == "1"
    today = datetime.utcnow().date()
    since_s = request.args.get("since")
    until_s = request.args.get("until")
    try:
        if since_s and until_s:
            since = _date.fromisoformat(since_s)
            until = _date.fromisoformat(until_s)
        else:
            try:
                days = int(request.args.get("days", "90"))
            except ValueError:
                days = 90
            until = today
            since = today - timedelta(days=days)
        # Захист: коректний порядок і не в майбутньому
        if until > today:
            until = today
        if since >= until:
            since = until - timedelta(days=1)
    except ValueError:
        until = today
        since = today - timedelta(days=90)
    key = f"{since.isoformat()}_{until.isoformat()}"
    try:
        cache = {}
        if os.path.exists(STORIES_FILE):
            with open(STORIES_FILE, encoding="utf-8") as f:
                cache = json.load(f)
            # Сумісність зі старим форматом (плоский об'єкт без розбивки по днях)
            if not isinstance(cache, dict) or "summary" in cache:
                cache = {}
        entry = cache.get(key) if isinstance(cache, dict) else None
        if not force and entry:
            try:
                age = (datetime.utcnow() - datetime.fromisoformat(entry.get("updated", ""))).total_seconds()
            except Exception:
                age = 1e9
            if age < 6 * 3600:
                return jsonify(entry)
        data = fetch_stories(since, until)
        cache[key] = data
        with open(STORIES_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/stories_collected")
def api_stories_collected():
    """Окремі сторіз, зібрані збирачем collect_stories.py (накопичується вперед)."""
    path = os.path.join(DIR, "stories_media.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return jsonify(json.load(f))
        except Exception as e:
            return jsonify({"error": str(e)})
    return jsonify([])

@app.route("/api/reanalyze")
def api_reanalyze():
    def run():
        import subprocess
        subprocess.run(["python3", f"{DIR}/analyze_patterns.py"], cwd=DIR)
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})

def _build_script_prompt(topic, extra):
    """Будує (system_prompt, user_prompt) зі стилю/даних акаунта."""
    # Топ-10 рілсів — різноманітна вибірка (за переглядами, збереженнями, ER)
    data = load_data()
    with_text = [r for r in data if r.get("transcript") and r.get("insights", {}).get("views", 0) > 0]

    def score(r):
        ins = r.get("insights", {})
        v = ins.get("views", 0)
        s = ins.get("saved", 0)
        sh = ins.get("shares", 0)
        ti = ins.get("total_interactions", 0)
        er = ti / v if v > 0 else 0
        # зважений скор: перегляди + бонус за високий ER і збереження
        return v * (1 + er * 5 + s / max(v, 1) * 20)

    with_text.sort(key=score, reverse=True)
    top = with_text[:10]

    examples = "\n\n---\n\n".join([
        f"[{r.get('timestamp','')[:10]} · {r['insights']['views']:,} views · saves {r['insights'].get('saved',0)} · shares {r['insights'].get('shares',0)}]\n{r['transcript']}"
        for r in top
    ])

    # Завантажуємо дані аналізу для точного профілю стилю
    analysis = {}
    afile = f"{DIR}/analysis.json"
    if os.path.exists(afile):
        with open(afile, encoding="utf-8") as f:
            analysis = json.load(f)

    # Найкраща довжина з даних
    la = analysis.get("length_analysis", {})
    best_len_bucket = max(la.items(), key=lambda x: x[1].get("avg_views", 0))[0] if la else ""
    avg_wc = analysis.get("overall", {}).get("avg_word_count", 0) or \
             round(sum(len(r["transcript"].split()) for r in with_text) / max(len(with_text), 1))
    best_len = f"{best_len_bucket} — це твій золотий розмір" if best_len_bucket else f"~{avg_wc} слів"

    # Слова-магніти з хуків
    hook_words = [w["word"] for w in analysis.get("hook_winners", [])[:8]]
    # Слова топ-рілсів
    win_words = [w["word"] for w in analysis.get("word_winners", [])[:10]]
    # Слова для збережень
    save_words = [w["word"] for w in analysis.get("save_words", [])[:6]]
    # Питання vs твердження
    qs = analysis.get("questions_vs_statements", {})
    q_better = (qs.get("with_question", {}).get("avg_views", 0) >
                qs.get("without_question", {}).get("avg_views", 0))
    # Розмовність
    cs = analysis.get("casualness", {})
    casual_better = (cs.get("high_casual", {}).get("avg_views", 0) >
                     cs.get("low_casual", {}).get("avg_views", 0))

    style_profile = f"""ПРОФІЛЬ СТИЛЮ (на основі даних із {len(with_text)} рілсів цього акаунта):
• Найкраща довжина: {best_len}
• Хук: {"починати з питання (+залучення)" if q_better else "починати з провокаційного твердження"}
• Стиль мовлення: {"розмовний зі сленгом — дає більше переглядів" if casual_better else "нейтральний діловий — працює краще в цього автора"}
• Слова-магніти для хука (трапляються в топ-рілсах у 2-16x частіше): {', '.join(hook_words) if hook_words else 'немає даних'}
• Слова тем, які заходять: {', '.join(win_words) if win_words else 'немає даних'}
• Слова, які спонукають зберігати: {', '.join(save_words) if save_words else 'немає даних'}
• Середній ER найкращих рілсів: {analysis.get('overall', {}).get('avg_er', 0)}%"""

    system_prompt = f"""Ти — персональний скрипт-райтер автора цього Instagram-акаунта.
Ти глибоко вивчив його дані й точно знаєш, що працює в його аудиторії.

{style_profile}

Твоє завдання — писати скрипти, які звучать на 100% як сам автор, а не як ШІ. Пиши українською."""

    user_prompt = f"""Ось мої топ-10 рілсів — вивчи мій стиль, ритм, структуру фраз, переходи:

{examples}

---

Напиши скрипт на тему: **{topic}**
{f'Контекст/акцент: {extra}' if extra else ''}

Правила:
1. Жвавий розмовний стиль — говорити без зупинки, наче тема тебе захоплює
2. Довжина: {best_len}. Якщо в полі «додатково» вказана інша довжина — слухай її.
3. Хук (перші 2-3 фрази) має зупинити скрол ({"питання" if q_better else "провокація/контроверсія"})
4. {'Використовуй розмовну мову — у цього автора це працює' if casual_better else 'Розмовно, але стримано'}
5. {'Встав слова-магніти в хук, якщо доречно: ' + ', '.join(hook_words[:4]) if hook_words else ''}
6. Копіюй ритм і переходи з прикладів вище — там є характерні для автора звороти

Формат відповіді:

🎣 ХУК:
[лише перші 2-3 фрази]

📝 ПОВНИЙ СКРИПТ:
[весь скрипт без розмітки, готовий до запису]

🔍 РОЗБІР:
- Тип хука: ...
- Ключові тригери: ...
- Слова з твоїх патернів: ..."""
    return system_prompt, user_prompt


@app.route("/api/generate_script", methods=["POST"])
def api_generate_script():
    try:
        api_key = ANTHROPIC_KEY or request.json.get("api_key", "")
        if not api_key:
            return jsonify({"error": "Немає API-ключа Anthropic. Додай його в .env або передай у запиті."})

        topic = request.json.get("topic", "").strip()
        extra = request.json.get("extra", "").strip()
        if not topic:
            return jsonify({"error": "Вкажи тему"})

        system_prompt, user_prompt = _build_script_prompt(topic, extra)

        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        )
        result = msg.content[0].text
        # Рахуємо слова в повному скрипті
        import re
        script_match = re.search(r'📝 ПОВНИЙ СКРИПТ:(.*?)(?:🔍|$)', result, re.DOTALL)
        word_count = len(script_match.group(1).split()) if script_match else 0
        return jsonify({"result": result, "word_count": word_count})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/generate_prompt", methods=["POST"])
def api_generate_prompt():
    """Повертає готовий запит для ручної генерації в Claude (місток копіювання, без ключа)."""
    try:
        topic = request.json.get("topic", "").strip()
        extra = request.json.get("extra", "").strip()
        if not topic:
            return jsonify({"error": "Вкажи тему"})
        system_prompt, user_prompt = _build_script_prompt(topic, extra)
        return jsonify({"prompt": system_prompt + "\n\n" + user_prompt})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/roast_carousel", methods=["POST"])
def api_roast_carousel():
    try:
        api_key = ANTHROPIC_KEY or ""
        if not api_key:
            return jsonify({"error": "Немає API-ключа Anthropic"})

        files = request.files.getlist("slides")
        if not files or len(files) == 0:
            return jsonify({"error": "Завантаж хоча б один слайд"})

        extra = request.form.get("extra", "").strip()
        mode = request.form.get("mode", "roast")

        # Завантажуємо топ-рілси для стилю
        data = load_data()
        with_text = [r for r in data if r.get("transcript") and r.get("insights", {}).get("views", 0) > 0]
        with_text.sort(key=lambda x: x.get("insights", {}).get("views", 0), reverse=True)
        top3 = with_text[:3]
        style_examples = "\n\n---\n\n".join([
            f"[{r.get('timestamp','')[:10]} · {r['insights']['views']:,} views]\n{r['transcript'][:400]}"
            for r in top3
        ])

        # Збираємо content для Claude: спершу всі слайди, потім промпт
        content = []

        for i, f in enumerate(files):
            img_bytes = f.read()
            mime = f.content_type or "image/jpeg"
            if mime not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                mime = "image/jpeg"
            b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
            content.append({
                "type": "text",
                "text": f"Слайд {i+1} з {len(files)}:"
            })
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": b64}
            })

        # Завантажуємо аналіз для профілю стилю
        analysis = {}
        afile = f"{DIR}/analysis.json"
        if os.path.exists(afile):
            with open(afile, encoding="utf-8") as f_a:
                analysis = json.load(f_a)
        hook_words = [w["word"] for w in analysis.get("hook_winners", [])[:5]]
        casual_better = (analysis.get("casualness", {}).get("high_casual", {}).get("avg_views", 0) >
                         analysis.get("casualness", {}).get("low_casual", {}).get("avg_views", 0))

        mode_prompts = {
            "roast": f"""Це контент, який я хочу розібрати / прожарити у своєму рілсі.
{'Контекст: ' + extra if extra else ''}

Завдання: напиши скрипт-розбір у моєму стилі.
- Почни з «Розбираємо...» + жорстке твердження
- Розбери, що не так, що маніпулятивне, що брехня — конкретно
- Наприкінці — чіткий висновок

Формат:
🎣 ХУК:
[перші 2-3 фрази]

📝 ПОВНИЙ СКРИПТ:
[готовий до запису]

🔍 ЩО НЕ ТАК:
[за пунктами]""",

            "discuss": f"""Це контент на тему, яку я хочу обговорити в рілсі / висловити свою позицію.
{'Моя позиція / що хочу сказати: ' + extra if extra else ''}

Завдання: напиши скрипт, де я висловлююся про це у своєму стилі.
- Моя точка зору чітка, не нейтральна
- Можна погоджуватися або не погоджуватися з контентом

Формат:
🎣 ХУК:
[перші 2-3 фрази]

📝 ПОВНИЙ СКРИПТ:
[готовий до запису]""",

            "react": f"""Це контент, на який я хочу зробити реакцію в рілсі.
{'Контекст: ' + extra if extra else ''}

Завдання: напиши скрипт реакції — живий, емоційний, у моєму стилі.
- Показую контент + коментую на ходу
- Енергія та емоція важливіші за структуру

Формат:
🎣 ХУК:
[перші 2-3 фрази]

📝 ПОВНИЙ СКРИПТ:
[готовий до запису]""",

            "custom": f"""Ось зображення та завдання:
{extra if extra else 'Напиши скрипт за цим контентом у моєму стилі.'}

Формат:
🎣 ХУК:
[перші 2-3 фрази]

📝 ПОВНИЙ СКРИПТ:
[готовий до запису]""",
        }

        task = mode_prompts.get(mode, mode_prompts["roast"])

        content.append({
            "type": "text",
            "text": f"""Вивчи мій стиль за цими топ-рілсами:

{style_examples}

---

{task}

Загальні правила:
- Жвавий розмовний стиль — енергійно, без зупинки
- {'Можна неформально — у мене це працює' if casual_better else 'Розмовний стиль'}
- {'Слова-магніти в хуку: ' + ', '.join(hook_words) if hook_words else ''}
- 130-180 слів"""
        })

        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": content}]
        )
        return jsonify({"result": msg.content[0].text, "slides_count": len(files)})

    except Exception as e:
        return jsonify({"error": str(e)})


if __name__ == "__main__":
    # Перший запуск — завантажуємо дані, якщо їх немає
    if not os.path.exists(DATA_FILE):
        print("📥 Перший запуск — завантажую рілси...")
        sync_reels(False)
    if not os.path.exists(POSTS_FILE):
        print("📥 Перший запуск — завантажую дописи...")
        sync_posts()
    port = int(os.environ.get("PORT", "8080"))
    print(f"🚀 Дашборд: http://localhost:{port}")
    app.run(port=port, debug=False)
