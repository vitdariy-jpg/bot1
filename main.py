import json
import base64
import time
import re
import urllib.parse
import requests
import telebot
from google.oauth2 import service_account
from google.auth.transport.requests import Request
import telebot
from telebot import apihelper

# Настройка прокси для бесплатного тарифа PythonAnywhere
apihelper.proxy = {'https': 'http://proxy.server:3128'}
# ---------------- ТВОИ НАСТРОЙКИ ----------------
BOT_TOKEN = "8695529242:AAFfGqyZ50jBbVdIAABGhWTJIr9BLJxv_FE"
# Вставь сюда свой API-ключ с ClaudeHub (начинается на sk-...)
CLAUDEHUB_API_KEY = "sk-hub-q0epg97Xu4EcvvF2iGtupbLBYYKGrpKi"
SPREADSHEET_ID = "1kP9LL6rPb4-gr4S287MVfrKz7Bgg8XlQ0ceeOHdZ_KA"
# Ключ Groq (console.groq.com/keys)
GROQ_API_KEY = "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"
GROQ_MODEL = "qwen/qwen3.8-27b"
# ------------------------------------------------

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
creds = service_account.Credentials.from_service_account_file(
    'credentials.json', scopes=SCOPES
)

# Кэш названия листа, чтобы не дёргать API метаданных на каждое сообщение
_sheet_title_cache = None


def get_access_token():
    if not creds.valid:
        creds.refresh(Request())
    return creds.token


def get_first_sheet_title(token, force_refresh=False):
    global _sheet_title_cache
    if _sheet_title_cache and not force_refresh:
        return _sheet_title_cache

    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}?fields=sheets.properties.title"
    headers = {"Authorization": f"Bearer {token}"}
    res = requests.get(url, headers=headers, timeout=20)
    if res.status_code != 200:
        raise Exception(f"Ошибка получения метаданных таблицы ({res.status_code}): {res.text}")

    _sheet_title_cache = res.json()['sheets'][0]['properties']['title']
    return _sheet_title_cache


def append_to_sheet(row_data):
    token = get_access_token()
    sheet_title = get_first_sheet_title(token)

    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{sheet_title}!A1:append?valueInputOption=RAW"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {"values": [row_data]}

    res = requests.post(url, headers=headers, json=payload, timeout=20)

    # Если название листа устарело (переименовали лист) — обновим кэш и попробуем ещё раз
    if res.status_code == 400:
        sheet_title = get_first_sheet_title(token, force_refresh=True)
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{sheet_title}!A1:append?valueInputOption=RAW"
        res = requests.post(url, headers=headers, json=payload, timeout=20)

    if res.status_code != 200:
        raise Exception(f"Ошибка Google Sheets API ({res.status_code}): {res.text}")
    return res.json()


def clean_phone_number(phone_str):
    """Очищает телефон до формата 77072180202. Возвращает первый валидный номер."""
    if not phone_str or phone_str == "-":
        return None

    # Заявка может содержать несколько телефонов через пробел — проверяем все по очереди,
    # а не только первый, вдруг первый номер распознался некорректно
    for candidate in phone_str.split():
        digits = "".join(filter(str.isdigit, candidate))

        if len(digits) == 11 and digits.startswith("8"):
            digits = "7" + digits[1:]
        elif len(digits) == 10:
            digits = "7" + digits

        if len(digits) == 11 and digits.startswith("7"):
            return digits

    return None


def clean_city_name(city_str):
    """Убирает указания стран в скобках, например (KZ), (RU), (BY)."""
    if not city_str or city_str == "-":
        return city_str
    cleaned = re.sub(r'\s*\([A-Za-z]{2,3}\)', '', city_str)
    return cleaned.strip()


def escape_markdown(text):
    """Экранирует спецсимволы Markdown (legacy), чтобы Telegram не ломался на *, _, ` и [."""
    if text is None:
        return "-"
    text = str(text)
    for ch in ['_', '*', '`', '[']:
        text = text.replace(ch, '\\' + ch)
    return text


def analyze_image(image_bytes, mime_type="image/jpeg"):
    base64_image = base64.b64encode(image_bytes).decode('utf-8')

    prompt = """
    Считай данные со скриншота заявки на грузоперевозку и верни СТРОГО JSON-объект с ключами:
    {
      "departure": "пункт выезда (город/регион/страна)",
      "arrival": "пункт прибытия (город/регион/страна)",
      "price": "цена/ставка с указанием валюты и типа расчета (например, '450 000 тнг (Безналичный)')",
      "date": "дата погрузки/выполнения",
      "capacity": "машина, кубатура и тоннаж (например, 'тент / 20 т')",
      "phone": "номера телефонов",
      "details": "наименование груза, габариты, тип погрузки и прочие доп. параметры"
    }

    Правила:
    1. Для departure и arrival: указывай только названия городов без кода страны в скобках (без (KZ), (RU)).
    2. Для price: укажи сумму с валютой и тип расчета в скобках. ЕСЛИ ЦЕНА/СТАВКА НЕ УКАЗАНА НА КАРТИНКЕ — СТРОГО СТАВЬ "-". Не придумывай сумму!
    3. Для capacity: выдели тип авто и тоннаж.
    4. Для phone: раздели несколько телефонов СТРОГО ПРОБЕЛОМ.
    5. Для details: собери наименование груза и условия.
    6. Если любого поля нет на картинке — ставь "-".
    7. Верни ТОЛЬКО ЧИСТЫЙ JSON без markdown-тегов и без комментариев.
    """

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{base64_image}"
                        }
                    }
                ]
            }
        ],
        "temperature": 0.1,
        "max_completion_tokens": 1000,
        "reasoning_effort": "low"
    }

    last_error = None
    res = None
    for attempt in range(3):
        try:
            start_time = time.time()
            res = requests.post(url, headers=headers, json=payload, timeout=60)
            elapsed = time.time() - start_time
            print(f"[{GROQ_MODEL}] Ответ за {elapsed:.1f} сек, статус {res.status_code}")

            if res.status_code in (503, 429):
                last_error = f"Модель перегружена (код {res.status_code})"
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
            break
        except requests.exceptions.Timeout:
            last_error = "Groq не ответил за 60 секунд"
            continue
        except requests.exceptions.RequestException as e:
            last_error = str(e)
            continue

    if res is None:
        raise Exception(f"Не удалось достучаться до Groq после 3 попыток: {last_error}")

    if res.status_code != 200:
        raise Exception(f"Ошибка API ({res.status_code}): {res.text}")

    raw_text = res.json()['choices'][0]['message']['content'].strip()

    if raw_text.startswith("```"):
        raw_text = re.sub(r'^```[a-zA-Z]*\n', '', raw_text)
        raw_text = re.sub(r'\n```$', '', raw_text)

    try:
        return json.loads(raw_text.strip())
    except json.JSONDecodeError:
        # Модель могла добавить лишний текст до/после JSON — пробуем вырезать сам объект
        match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise Exception("Не удалось распознать ответ ИИ как JSON. Попробуйте другое фото.")


bot = telebot.TeleBot(BOT_TOKEN)


@bot.message_handler(commands=['start'])
def start_cmd(message):
    bot.reply_to(message, "Привет! Отправь скриншот заявки, и я внесу данные в Google Таблицу.")


@bot.message_handler(content_types=['photo', 'document'])
def handle_photo(message):
    status_msg = bot.reply_to(message, "🔄 Читаю скриншот...")

    try:
        mime_type = "image/jpeg"

        if message.content_type == 'photo':
            file_id = message.photo[-1].file_id
        elif message.content_type == 'document':
            doc_mime = message.document.mime_type
            if not doc_mime or not doc_mime.startswith('image/'):
                bot.edit_message_text("❌ Это не изображение!", chat_id=status_msg.chat.id, message_id=status_msg.message_id)
                return
            file_id = message.document.file_id
            mime_type = doc_mime

        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        data = analyze_image(downloaded_file, mime_type=mime_type)

        dep = clean_city_name(data.get("departure", "-"))
        arr = clean_city_name(data.get("arrival", "-"))
        price = data.get("price", "-")
        date = data.get("date", "-")
        capacity = data.get("capacity", "-")
        phone = data.get("phone", "-")
        details = data.get("details", "-")

        row = [dep, arr, price, date, capacity, phone, details]
        append_to_sheet(row)

        res_text = (
            "✅ *Данные внесены в таблицу!*\n\n"
            f"📍 *Выезд:* {escape_markdown(dep)}\n"
            f"🏁 *Прибытие:* {escape_markdown(arr)}\n"
            f"💰 *Цена / Расчет:* {escape_markdown(price)}\n"
            f"📅 *Дата:* {escape_markdown(date)}\n"
            f"🚛 *Машина / Тоннаж:* {escape_markdown(capacity)}\n"
            f"📞 *Телефон(ы):* {escape_markdown(phone)}\n"
            f"📦 *Груз / Доп. параметры:* {escape_markdown(details)}"
        )

        reply_markup = None
        clean_phone = clean_phone_number(phone)

        if clean_phone:
            message_text = f"Здравствуйте. {dep} - {arr} актуально?"
            encoded_text = urllib.parse.quote(message_text)
            wa_web_link = f"https://web.whatsapp.com/send?phone={clean_phone}&text={encoded_text}"

            reply_markup = telebot.types.InlineKeyboardMarkup()
            wa_button = telebot.types.InlineKeyboardButton(text="💬 Открыть в WhatsApp Web", url=wa_web_link)
            reply_markup.add(wa_button)

        bot.edit_message_text(
            res_text,
            chat_id=status_msg.chat.id,
            message_id=status_msg.message_id,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )

    except Exception as e:
        try:
            bot.edit_message_text(f"❌ Ошибка: {str(e)}", chat_id=status_msg.chat.id, message_id=status_msg.message_id)
        except Exception:
            # Если даже редактирование сообщения об ошибке не прошло (например, из-за Markdown) —
            # отправим новое сообщение без форматирования
            bot.send_message(status_msg.chat.id, f"❌ Ошибка: {str(e)}")


if __name__ == "__main__":
    print("🚀 Бот успешно запущен!")
    bot.infinity_polling()
