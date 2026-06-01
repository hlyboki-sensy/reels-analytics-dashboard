import json, re, os
from collections import Counter

DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = f"{DIR}/reels_data.json"
ANALYSIS_FILE = f"{DIR}/analysis.json"

with open(DATA_FILE, encoding="utf-8") as f:
    raw = json.load(f)

# Збагачуємо метриками
reels = []
for r in raw:
    ins = r.get("insights", {})
    views = ins.get("views", 0)
    reels.append({**r,
        "views": views,
        "reach": ins.get("reach", 0),
        "saves": ins.get("saved", 0),
        "shares": ins.get("shares", 0),
        "interactions": ins.get("total_interactions", 0),
        "likes": r.get("like_count", 0),
        "er": round(ins.get("total_interactions", 0) / views * 100, 2) if views > 0 else 0,
        "save_rate": round(ins.get("saved", 0) / views * 100, 2) if views > 0 else 0,
        "date": r.get("timestamp", "")[:10],
        "transcript": r.get("transcript", ""),
        "word_count": len(r.get("transcript", "").split()) if r.get("transcript") else 0,
    })

reels_with_text = [r for r in reels if r["transcript"] and r["views"] > 0]
reels.sort(key=lambda x: x["views"], reverse=True)
n = len(reels_with_text)

print(f"📊 Аналізую {n} рілсів з транскриптами...\n")

# Ділимо на топ 33% і низ 33%
sorted_by_views = sorted(reels_with_text, key=lambda x: x["views"], reverse=True)
top_third = sorted_by_views[:max(1, n // 3)]
bot_third = sorted_by_views[max(1, n * 2 // 3):]

# Українські стоп-слова (службові, без яких аналіз чистіший)
STOP = {
    "і","й","в","у","на","з","із","зі","по","що","це","як","а","але","за","не",
    "я","ти","ми","ви","він","вона","воно","вони","то","так","зі","зо","ж","же",
    "би","б","вже","ще","ну","да","ні","від","до","при","для","всі","все","весь",
    "його","її","їх","був","була","було","будо","буде","будуть","є","там","тут",
    "ось","цей","ця","це","ці","той","та","те","ті","об","про","або","чи","мене",
    "тебе","нас","вас","мені","тобі","собі","цього","який","яка","яке","які","коли",
    "щоб","щоби","бо","теж","також","мій","моя","моє","мої","твій","свій","наш",
    "себе","їй","йому","нею","ним","нами","вами","ними","під","над","між","через",
    "якщо","тому","адже","оце","оцей","лише","тільки","навіть","саме","десь","якось"
}


def get_hook(text, words=15):
    """Перші N слів — це хук."""
    return " ".join(text.split()[:words])

def get_words(texts):
    """Усі значущі слова зі списку текстів (українська + кирилиця)."""
    all_words = []
    for t in texts:
        words = re.findall(r'[а-щьюяіїєґё]{3,}', t.lower())
        all_words.extend([w for w in words if w not in STOP])
    return all_words

def avg(lst, key):
    vals = [x[key] for x in lst if x[key] > 0]
    return round(sum(vals)/len(vals), 1) if vals else 0

# 1. ДОВЖИНА СКРИПТА vs МЕТРИКИ
print("📏 Аналіз довжини скрипта...")
buckets = {"короткий (0-50 слів)": [], "середній (51-100)": [], "довгий (101-150)": [], "дуже довгий (150+)": []}
for r in reels_with_text:
    wc = r["word_count"]
    if wc <= 50: buckets["короткий (0-50 слів)"].append(r)
    elif wc <= 100: buckets["середній (51-100)"].append(r)
    elif wc <= 150: buckets["довгий (101-150)"].append(r)
    else: buckets["дуже довгий (150+)"].append(r)

length_analysis = {}
for bucket, items in buckets.items():
    if items:
        length_analysis[bucket] = {
            "count": len(items),
            "avg_views": avg(items, "views"),
            "avg_er": avg(items, "er"),
            "avg_saves": avg(items, "saves"),
            "avg_shares": avg(items, "shares"),
        }

# 2. ХУКИ — перші слова топ vs низ
print("🎣 Аналіз хуків...")
top_hooks = [get_hook(r["transcript"]) for r in top_third]
bot_hooks = [get_hook(r["transcript"]) for r in bot_third]

top_hook_words = Counter(get_words(top_hooks))
bot_hook_words = Counter(get_words(bot_hooks))

# Слова, що в топ-хуках частіше, ніж у нижніх
hook_winners = []
for word, count in top_hook_words.most_common(50):
    top_freq = count / len(top_third)
    bot_freq = bot_hook_words.get(word, 0) / max(len(bot_third), 1)
    if top_freq > bot_freq * 1.5 and count >= 2:
        hook_winners.append({"word": word, "top_freq": round(top_freq, 2), "bot_freq": round(bot_freq, 2), "lift": round(top_freq / max(bot_freq, 0.01), 1)})

hook_winners.sort(key=lambda x: x["lift"], reverse=True)

# 3. СЛОВА-ПЕРЕМОЖЦІ (весь текст топ vs низ)
print("🏆 Аналіз слів-переможців...")
top_words = Counter(get_words([r["transcript"] for r in top_third]))
bot_words = Counter(get_words([r["transcript"] for r in bot_third]))

word_winners = []
for word, count in top_words.most_common(100):
    top_freq = count / len(top_third)
    bot_freq = bot_words.get(word, 0) / max(len(bot_third), 1)
    if top_freq > bot_freq * 1.3 and count >= 2:
        word_winners.append({"word": word, "top_freq": round(top_freq, 2), "bot_freq": round(bot_freq, 2), "lift": round(top_freq / max(bot_freq, 0.01), 1)})
word_winners.sort(key=lambda x: x["lift"], reverse=True)

# 4. ПАТЕРНИ ПОЧАТКУ — як починаються топ-рілси
print("🚀 Аналіз патернів початку...")
openers = []
for r in sorted_by_views[:20]:
    if r["transcript"]:
        first = r["transcript"].split(".")[0].strip()[:100]
        openers.append({"text": first, "views": r["views"], "er": r["er"], "date": r["date"]})

# 5. ПИТАННЯ vs ТВЕРДЖЕННЯ
print("❓ Питання vs твердження...")
def has_question(text): return "?" in text[:200]
q_reels = [r for r in reels_with_text if has_question(r["transcript"])]
s_reels = [r for r in reels_with_text if not has_question(r["transcript"])]

# 6. РОЗМОВНІСТЬ (неформальні розмовні маркери)
print("💬 Розмовність...")
casual_words = ["капець","блін","жесть","крінж","офігенно","офігеть","реально","взагалі",
                "просто","типу","короче","ваще","ну","от","оце","так от","серйозно",
                "божевілля","дикий","шалений","вау","ого","ой","агов"]
def casualness_score(text):
    text_lower = text.lower()
    return sum(1 for w in casual_words if w in text_lower)

for r in reels_with_text:
    r["casual_score"] = casualness_score(r["transcript"])

high_casual = [r for r in reels_with_text if r["casual_score"] >= 2]
low_casual = [r for r in reels_with_text if r["casual_score"] == 0]

# 7. ТОП-10 рілсів — повні скрипти
top10 = sorted(reels_with_text, key=lambda x: x["views"], reverse=True)[:10]

# 8. КОРЕЛЯЦІЯ слів зі збереженнями (save bait)
save_words = []
sorted_by_saves = sorted(reels_with_text, key=lambda x: x["save_rate"], reverse=True)
top_save = sorted_by_saves[:max(1, n//3)]
top_save_words = Counter(get_words([r["transcript"] for r in top_save]))
for word, count in top_save_words.most_common(30):
    if count >= 2:
        save_words.append({"word": word, "count": count})

# Збираємо все
analysis = {
    "total_reels": len(reels),
    "reels_with_transcripts": n,
    "overall": {
        "avg_views": avg(reels_with_text, "views"),
        "avg_er": avg(reels_with_text, "er"),
        "avg_saves": avg(reels_with_text, "saves"),
        "avg_word_count": round(sum(r["word_count"] for r in reels_with_text) / max(n,1)),
    },
    "length_analysis": length_analysis,
    "hook_winners": hook_winners[:15],
    "word_winners": word_winners[:20],
    "openers": openers,
    "questions_vs_statements": {
        "with_question": {"count": len(q_reels), "avg_views": avg(q_reels, "views"), "avg_er": avg(q_reels, "er")},
        "without_question": {"count": len(s_reels), "avg_views": avg(s_reels, "views"), "avg_er": avg(s_reels, "er")},
    },
    "casualness": {
        "high_casual": {"count": len(high_casual), "avg_views": avg(high_casual, "views"), "avg_er": avg(high_casual, "er"), "avg_saves": avg(high_casual, "saves")},
        "low_casual": {"count": len(low_casual), "avg_views": avg(low_casual, "views"), "avg_er": avg(low_casual, "er"), "avg_saves": avg(low_casual, "saves")},
    },
    "save_words": save_words[:15],
    "top10_scripts": [{"views": r["views"], "er": r["er"], "saves": r["saves"], "shares": r["shares"], "date": r["date"], "hook": get_hook(r["transcript"], 20), "transcript": r["transcript"], "word_count": r["word_count"]} for r in top10],
}

with open(ANALYSIS_FILE, "w", encoding="utf-8") as f:
    json.dump(analysis, f, ensure_ascii=False, indent=2)

print(f"\n✅ Аналіз збережено → {ANALYSIS_FILE}")
print(f"\n🔑 КЛЮЧОВІ ІНСАЙТИ:")
print(f"  Найкраща довжина скрипта: {max(length_analysis.items(), key=lambda x: x[1].get('avg_views',0))[0] if length_analysis else 'немає даних'}")
if high_casual and low_casual:
    print(f"  Розмовний стиль: {avg(high_casual,'views'):,} vs {avg(low_casual,'views'):,} переглядів")
if hook_winners:
    print(f"  Топ-слова в хуках: {', '.join(w['word'] for w in hook_winners[:5])}")
