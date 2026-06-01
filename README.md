# Reels Analytics Dashboard

Локальний дашборд аналітики Instagram Reels: підтягує всі рілси акаунта через Instagram Graph API, рахує перегляди, охоплення, збереження, репости, ER, динаміку підписників — і показує це у зручному вебінтерфейсі. Додатково вміє розшифровувати голос у рілсах (faster-whisper) та генерувати скрипти у стилі автора (опційно, через Anthropic Claude).

Працює повністю локально на твоєму комп'ютері. Дані нікуди не надсилаються, окрім офіційного Instagram API.

---

## Можливості

- **Дашборд** — ключові метрики, графіки переглядів, таблиця всіх рілсів.
- **Підписники** — динаміка приросту за останні ~90 днів.
- **Патерни мовлення** — аналіз хуків, слів-магнітів, довжини, розмовності (потрібні транскрипти).
- **Скрипти** — повні розшифровки рілсів.
- **Інсайти** — рекомендації «на що тиснути» на основі твоїх даних.
- **Генератор** *(опційно, потрібен ключ Anthropic)* — скрипти у твоєму стилі + розбір каруселей.

---

## Вимоги

- **Python 3.10+** (працює і на 3.14).
- Instagram-акаунт **Professional** (Business або Creator).
- Instagram-токен типу **Instagram API with Instagram Login** (див. нижче).
- *(Опційно)* ключ **Anthropic** для ШІ-функцій.

ffmpeg **не потрібен** — розшифровка працює через вбудований у faster-whisper декодер.

---

## Встановлення

```bash
# 1. Клонувати репозиторій
git clone https://github.com/<your-username>/reels-analytics-dashboard.git
cd reels-analytics-dashboard

# 2. (Рекомендовано) віртуальне середовище
python3 -m venv .venv
source .venv/bin/activate      # macOS / Linux
# .venv\Scripts\activate       # Windows

# 3. Залежності
pip install -r requirements.txt

# 4. Налаштування
cp .env.example .env
# відкрий .env і впиши INSTAGRAM_TOKEN (див. нижче)

# 5. Запуск
python3 app.py
# відкрий у браузері http://localhost:8080
```

На першому запуску дашборд сам підтягне список рілсів.

---

## Як отримати Instagram-токен

Потрібен токен типу **Instagram API with Instagram Login** (починається з `IGAA…`).

1. Переконайся, що акаунт **Professional** (Instagram → Налаштування → Тип акаунта).
2. Зайди на [developers.facebook.com](https://developers.facebook.com) → створи застосунок (тип **Business**).
3. Додай продукт **Instagram** → **API setup with Instagram login**.
4. У **App roles → Roles → Instagram Testers** додай свій Instagram-нік і надішли запрошення.
5. Прийми запрошення: Instagram → Налаштування → **Apps and websites → Tester invites → Accept**
   (або на вебі: `instagram.com/accounts/manage_access/`).
6. Повернись у **API setup with Instagram login** → **Generate token** навпроти акаунта.
7. Скопіюй токен у `.env` у рядок `INSTAGRAM_TOKEN=`.

> Токен діє обмежений час. Коли протермінується — згенеруй новий тим самим способом.

---

## Розшифровка голосу (опційно)

У вебінтерфейсі натисни синхронізацію з розшифровкою (whisper). Перший запуск завантажить модель (~140 МБ для `base`). Розмір моделі та мову можна змінити в `.env` (`WHISPER_MODEL`, `WHISPER_LANGUAGE`). Розшифровка вмикає вкладки «Патерни мовлення», «Скрипти» та якісний «Генератор».

---

## ШІ-генерація (опційно)

Впиши `ANTHROPIC_API_KEY` у `.env`. Ключ створюється на [console.anthropic.com](https://console.anthropic.com) (платний, окремо від безкоштовного claude.ai). Без ключа вся статистика працює — вимикається лише вкладка «Генератор».

---

## Приватність і безпека

- `.env`, `reels_data.json`, `analysis.json` та тека `audio/` — у `.gitignore` і **ніколи не потрапляють у git**.
- Токени тримай лише в `.env`. Не вставляй їх у код чи в публічні місця.
- Дашборд слухає лише `localhost` — недоступний ззовні.

---

## Ліцензія

MIT.
