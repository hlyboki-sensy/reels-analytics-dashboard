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

def fetch_insights(media_id):
    r = requests.get(f"{BASE}/{media_id}/insights", params={
        "metric": "reach,saved,shares,total_interactions,views",
        "access_token": TOKEN
    }).json()
    result = {}
    for item in r.get("data", []):
        result[item["name"]] = item["values"][0]["value"]
    return result

_whisper_model = None

def _get_whisper_model():
    """Лінива ініціалізація моделі faster-whisper (вантажиться один раз)."""
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        size = os.environ.get("WHISPER_MODEL", "base")
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
        segments, _info = model.transcribe(video_path, language=lang)
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
            r = requests.get(url, params=params if not all_media else {}).json()
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
    """Приріст підписників по днях за останні ~90 днів."""
    try:
        from datetime import timedelta
        today = datetime.utcnow().date()
        since = (today - timedelta(days=90)).isoformat()
        until = today.isoformat()
        url = f"{BASE}/me/insights"
        params = {
            "metric": "follower_count",
            "period": "day",
            "since": since,
            "until": until,
            "access_token": TOKEN
        }
        r = requests.get(url, params=params).json()
        data = r.get("data", [])
        result = {}
        for item in data:
            if item["name"] == "follower_count":
                for v in item["values"]:
                    date = v["end_time"][:10]
                    result[date] = v["value"]
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/reanalyze")
def api_reanalyze():
    def run():
        import subprocess
        subprocess.run(["python3", f"{DIR}/analyze_patterns.py"], cwd=DIR)
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})

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
        print("📥 Перший запуск — завантажую дані...")
        sync_reels(False)
    print("🚀 Дашборд: http://localhost:8080")
    app.run(port=8080, debug=False)
