import os
import time
import sqlite3
import requests
from pathlib import Path
from urllib.parse import urlencode
from dotenv import load_dotenv
import telebot
from telebot import types

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
ERP_BASE = os.getenv("ERP_BASE", "https://acharyajava.uz/AcharyaInstituteUZB").rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi. .env faylni tekshiring.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

AUTH_URL = f"{ERP_BASE}/api/authenticate"
USER_DETAILS_URL = f"{ERP_BASE}/api/getUserDetailsById/{{user_id}}"
DUES_URL = f"{ERP_BASE}/api/student/getStudentDues/{{student_id}}"
START_CLICK_URL = f"{ERP_BASE}/api/student/startingOfClickPayment"

DB_PATH = str(BASE_DIR / "bot.db")

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users(
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            password TEXT,
            token TEXT,
            token_ts INTEGER,
            user_id INTEGER,
            student_id INTEGER,
            full_name TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            student_id INTEGER,
            amount INTEGER,
            transaction_param TEXT,
            pay_url TEXT,
            created_ts INTEGER
        )
    """)
    conn.commit()
    return conn

def get_user(telegram_id: int):
    conn = db()
    cur = conn.execute("""
        SELECT telegram_id, username, password, token, token_ts, user_id, student_id, full_name
        FROM users WHERE telegram_id=?
    """, (telegram_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    keys = ["telegram_id", "username", "password", "token", "token_ts", "user_id", "student_id", "full_name"]
    return dict(zip(keys, row))

def upsert_user(telegram_id: int, **fields):
    u = get_user(telegram_id)
    conn = db()
    if u is None:
        conn.execute("""
            INSERT INTO users(telegram_id, username, password, token, token_ts, user_id, student_id, full_name)
            VALUES(?,?,?,?,?,?,?,?)
        """, (
            telegram_id,
            fields.get("username"),
            fields.get("password"),
            fields.get("token"),
            fields.get("token_ts"),
            fields.get("user_id"),
            fields.get("student_id"),
            fields.get("full_name"),
        ))
    else:
        for k, v in fields.items():
            conn.execute(f"UPDATE users SET {k}=? WHERE telegram_id=?", (v, telegram_id))
    conn.commit()
    conn.close()

def save_payment_attempt(telegram_id: int, student_id: int, amount: int, transaction_param: str, pay_url: str):
    conn = db()
    conn.execute("""
        INSERT INTO payments(telegram_id, student_id, amount, transaction_param, pay_url, created_ts)
        VALUES(?,?,?,?,?,?)
    """, (telegram_id, student_id, amount, transaction_param, pay_url, int(time.time())))
    conn.commit()
    conn.close()

def list_payments(telegram_id: int, limit=10):
    conn = db()
    cur = conn.execute("""
        SELECT amount, transaction_param, pay_url, created_ts
        FROM payments WHERE telegram_id=? ORDER BY id DESC LIMIT ?
    """, (telegram_id, limit))
    rows = cur.fetchall()
    conn.close()
    return rows

STATE = {}

def set_state(uid, step, **tmp):
    STATE[uid] = {"step": step, "tmp": tmp}

def clear_state(uid):
    STATE.pop(uid, None)

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("🔐 Login", "👤 Profil")
    kb.add("📌 Qarzdorlik", "💳 To'lov qilish")
    kb.add("🧾 Mening to'lovlarim")
    return kb

def _headers_bearer(token: str):
    return {"Authorization": f"Bearer {token}"}

def _headers_token(token: str):
    return {"token": token}

def erp_authenticate(username: str, password: str):
    payload = {"username": username, "password": password}
    r = requests.post(AUTH_URL, json=payload, timeout=25)
    r.raise_for_status()
    data = r.json()

    if not data.get("success"):
        raise RuntimeError(f"Auth success=false: {data}")

    d = data.get("data") or {}
    token = d.get("token")
    user_id = d.get("userId")

    if not token or not user_id:
        raise RuntimeError(f"Auth response mos emas: {data}")

    return token, int(user_id)

def erp_get_user_details(token: str, user_id: int):
    url = USER_DETAILS_URL.format(user_id=user_id)
    r = requests.get(url, headers=_headers_bearer(token), timeout=25)
    if r.status_code in (401, 403):
        r = requests.get(url, headers=_headers_token(token), timeout=25)
    r.raise_for_status()
    data = r.json()

    if not data.get("success"):
        raise RuntimeError(f"UserDetails success=false: {data}")

    return data["data"]

def ensure_token(uid: int):
    u = get_user(uid)
    if not u or not u.get("token"):
        return None

    age = int(time.time()) - int(u.get("token_ts") or 0)
    if age < 25 * 60:
        return u["token"]

    if not u.get("username") or not u.get("password"):
        return None

    token, user_id = erp_authenticate(u["username"], u["password"])
    details = erp_get_user_details(token, user_id)
    student_id = int(details["empOrStdId"])
    full_name = details.get("name") or ""

    upsert_user(uid,
                token=token,
                token_ts=int(time.time()),
                user_id=user_id,
                student_id=student_id,
                full_name=full_name)
    return token

def erp_get_dues(uid: int, student_id: int):
    token = ensure_token(uid)
    if not token:
        raise RuntimeError("Avval Login qiling.")

    url = DUES_URL.format(student_id=student_id)

    r = requests.get(url, headers=_headers_bearer(token), timeout=25)
    if r.status_code in (401, 403):
        token = ensure_token(uid)
        r = requests.get(url, headers=_headers_bearer(token), timeout=25)

    if r.status_code in (401, 403):
        r = requests.get(url, headers=_headers_token(token), timeout=25)

    r.raise_for_status()
    return r.json()

def build_click_pay_url(start_resp: dict):
    base = start_resp["Url-Get"]
    params = {
        "service_id": str(start_resp["service_id"]),
        "merchant_id": str(start_resp["merchant_id"]),
        "merchant_user_id": str(start_resp.get("merchant_user_id", "")),
        "transaction_param": str(start_resp["transaction_param"]),
        "amount": str(int(float(start_resp["amount"]))),
        "return_url": str(start_resp["return_url"]),
    }
    params = {k: v for k, v in params.items() if v}
    return base + "?" + urlencode(params)

def erp_start_click_payment(uid: int, amount: int, student_id: int, full_name: str):
    token = ensure_token(uid)
    if not token:
        raise RuntimeError("Token yo'q. Avval Login qiling.")

    payload = {
        "amount": int(amount),
        "candidate_id": int(student_id),
        "candidate_name": full_name or "",
        "payment_for": "Due Payment",
    }

    r = requests.post(START_CLICK_URL, json=payload, headers=_headers_bearer(token), timeout=25)
    if r.status_code in (401, 403):
        token = ensure_token(uid)
        r = requests.post(START_CLICK_URL, json=payload, headers=_headers_bearer(token), timeout=25)

    if r.status_code in (401, 403):
        r = requests.post(START_CLICK_URL, json=payload, headers=_headers_token(token), timeout=25)

    r.raise_for_status()
    start_resp = r.json()
    pay_url = build_click_pay_url(start_resp)
    return start_resp, pay_url

@bot.message_handler(commands=["start"])
def start(m):
    bot.send_message(
        m.chat.id,
        "Salom! Acharya to'lov bot.\n\n"
        "🔐 Login qiling, keyin qarzdorlikni ko'rib, Click orqali to'lov qilasiz.",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda m: m.text == "🔐 Login")
def login_begin(m):
    set_state(m.from_user.id, "login_username")
    bot.send_message(m.chat.id, "Username yuboring (masalan: <code>ABT24CCS008</code>)")

@bot.message_handler(func=lambda m: m.text == "👤 Profil")
def profile(m):
    u = get_user(m.from_user.id)
    if not u or not u.get("student_id"):
        bot.send_message(m.chat.id, "Profil yo'q. Avval 🔐 Login qiling.", reply_markup=main_menu())
        return
    bot.send_message(
        m.chat.id,
        f"👤 <b>Profil</b>\n"
        f"User ID: <code>{u.get('user_id')}</code>\n"
        f"Student ID: <code>{u.get('student_id')}</code>\n"
        f"Ism: <b>{u.get('full_name') or '-'}</b>\n"
        f"Username: <code>{u.get('username') or '-'}</code>",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda m: m.text == "📌 Qarzdorlik")
def dues(m):
    uid = m.from_user.id
    u = get_user(uid)
    if not u or not u.get("student_id"):
        bot.send_message(m.chat.id, "Avval 🔐 Login qiling.", reply_markup=main_menu())
        return

    try:
        data = erp_get_dues(uid, int(u["student_id"]))
    except Exception as e:
        bot.send_message(m.chat.id, f"Xatolik: {e}", reply_markup=main_menu())
        return

    if not data.get("success"):
        bot.send_message(m.chat.id, f"ERP success=false: {data}", reply_markup=main_menu())
        return

    items = data.get("data", [])
    lines = ["📌 <b>Qarzdorlik (Dues)</b>\n"]
    due_years = []

    for it in items:
        year = it.get("year")
        fixed = float(it.get("fixed") or 0)
        paid = float(it.get("paid") or 0)
        duev = float(it.get("due") or 0)
        lines.append(f"Year {year}: Fixed {fixed:.0f} | Paid {paid:.0f} | Due <b>{duev:.0f}</b>")
        if duev > 0:
            due_years.append((year, int(duev)))

    bot.send_message(m.chat.id, "\n".join(lines), reply_markup=main_menu())

    if due_years:
        kb = types.InlineKeyboardMarkup()
        for year, amt in due_years:
            kb.add(types.InlineKeyboardButton(
                text=f"Year {year} due: {amt}",
                callback_data=f"pay_year:{year}:{amt}"
            ))
        bot.send_message(m.chat.id, "💳 Due bo'yicha tez to'lov:", reply_markup=kb)

@bot.message_handler(func=lambda m: m.text == "💳 To'lov qilish")
def pay_menu(m):
    uid = m.from_user.id
    u = get_user(uid)
    if not u or not u.get("student_id"):
        bot.send_message(m.chat.id, "Avval 🔐 Login qiling.", reply_markup=main_menu())
        return

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✍️ Istalgan miqdor to'lash", callback_data="pay_custom"))
    kb.add(types.InlineKeyboardButton("📌 Due bo'yicha to'lash (year tanlash)", callback_data="pay_due_pick"))
    bot.send_message(m.chat.id, "Qaysi rejimda to'laysiz?", reply_markup=kb)

@bot.message_handler(func=lambda m: m.text == "🧾 Men to'laganlar")
def my_payments(m):
    rows = list_payments(m.from_user.id, limit=10)
    if not rows:
        bot.send_message(m.chat.id, "Hozircha bot orqali boshlangan to'lovlar yo'q.", reply_markup=main_menu())
        return

    lines = ["🧾 <b>Oxirgi to'lov urinishlari</b>"]
    for amount, tp, url, ts in rows:
        dt = time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))
        lines.append(f"- <b>{amount}</b> so'm | <code>{tp}</code> | {dt}")
    bot.send_message(m.chat.id, "\n".join(lines), reply_markup=main_menu())

@bot.callback_query_handler(func=lambda c: True)
def callbacks(c):
    uid = c.from_user.id
    data = c.data

    if data == "pay_custom":
        bot.answer_callback_query(c.id)
        set_state(uid, "pay_custom_amount")
        bot.send_message(c.message.chat.id, "Miqdor kiriting (faqat raqam). Masalan: <code>120000</code>")
        return

    if data == "pay_due_pick":
        bot.answer_callback_query(c.id)
        u = get_user(uid)
        if not u or not u.get("student_id"):
            bot.send_message(c.message.chat.id, "Avval 🔐 Login qiling.", reply_markup=main_menu())
            return
        try:
            dues_data = erp_get_dues(uid, int(u["student_id"]))
            items = dues_data.get("data", [])
            due_years = [(it.get("year"), int(float(it.get("due") or 0)))
                         for it in items if float(it.get("due") or 0) > 0]

            if not due_years:
                bot.send_message(c.message.chat.id, "Qarzdorlik yo'q 🎉", reply_markup=main_menu())
                return

            kb = types.InlineKeyboardMarkup()
            for year, amt in due_years:
                kb.add(types.InlineKeyboardButton(
                    text=f"Year {year} — {amt}",
                    callback_data=f"pay_year:{year}:{amt}"
                ))
            bot.send_message(c.message.chat.id, "Year tanlang:", reply_markup=kb)
        except Exception as e:
            bot.send_message(c.message.chat.id, f"Xatolik: {e}", reply_markup=main_menu())
        return

    if data.startswith("pay_year:"):
        bot.answer_callback_query(c.id)
        _, year, amt = data.split(":")
        do_payment(c.message.chat.id, uid, int(amt), note=f"Year {year} due")
        return

def do_payment(chat_id: int, uid: int, amount: int, note: str = ""):
    if amount < 1000:
        bot.send_message(chat_id, "Miqdor juda kichik.", reply_markup=main_menu())
        return
    if amount > 200_000_000:
        bot.send_message(chat_id, "Miqdor juda katta.", reply_markup=main_menu())
        return

    u = get_user(uid)
    if not u or not u.get("student_id"):
        bot.send_message(chat_id, "Avval 🔐 Login qiling.", reply_markup=main_menu())
        return

    try:
        start_resp, pay_url = erp_start_click_payment(
            uid=uid,
            amount=amount,
            student_id=int(u["student_id"]),
            full_name=u.get("full_name") or ""
        )
        save_payment_attempt(uid, int(u["student_id"]), amount, start_resp["transaction_param"], pay_url)
    except Exception as e:
        bot.send_message(chat_id, f"To'lov yaratishda xatolik: {e}", reply_markup=main_menu())
        return

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("💳 Click orqali to'lash", url=pay_url))

    bot.send_message(
        chat_id,
        f"✅ To'lov yaratildi. {note}\n"
        f"Miqdor: <b>{amount}</b> so'm\n"
        f"Transaction: <code>{start_resp['transaction_param']}</code>\n\n"
        f"Quyidagi tugma orqali to'lang:",
        reply_markup=kb
    )

@bot.message_handler(func=lambda m: True)
def router(m):
    uid = m.from_user.id
    st = STATE.get(uid)
    if not st:
        return

    step = st["step"]

    if step == "login_username":
        username = m.text.strip()
        set_state(uid, "login_password", username=username)
        bot.send_message(m.chat.id, "Parolni yuboring:")
        return

    if step == "login_password":
        password = m.text.strip()
        username = st["tmp"]["username"]

        try:
            token, user_id = erp_authenticate(username, password)
            details = erp_get_user_details(token, user_id)
            student_id = int(details["empOrStdId"])
            full_name = details.get("name") or ""

            upsert_user(uid,
                        username=username,
                        password=password,
                        token=token,
                        token_ts=int(time.time()),
                        user_id=user_id,
                        student_id=student_id,
                        full_name=full_name)

            clear_state(uid)
            bot.send_message(
                m.chat.id,
                "✅ Login OK!\n"
                f"User ID: <code>{user_id}</code>\n"
                f"Student ID: <code>{student_id}</code>\n"
                f"Ism: <b>{full_name or '-'}</b>",
                reply_markup=main_menu()
            )
        except Exception as e:
            clear_state(uid)
            bot.send_message(m.chat.id, f"❌ Login xato: {e}", reply_markup=main_menu())
        return

    if step == "pay_custom_amount":
        try:
            amount = int(m.text.strip().replace(" ", ""))
        except:
            bot.send_message(m.chat.id, "Faqat raqam yuboring. Masalan: <code>120000</code>")
            return

        clear_state(uid)
        do_payment(m.chat.id, uid, amount, note="Custom amount")
        return

if __name__ == "__main__":
    print("Bot started...")
    bot.infinity_polling(skip_pending=True)
