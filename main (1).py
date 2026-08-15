import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, LabeledPrice, PreCheckoutQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from openai import AsyncOpenAI
import aiohttp

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

IMAGE_API_URL = os.getenv("IMAGE_API_URL", "")
IMAGE_API_KEY = os.getenv("IMAGE_API_KEY", "")
VIDEO_API_URL = os.getenv("VIDEO_API_URL", "")
VIDEO_API_KEY = os.getenv("VIDEO_API_KEY", "")

DB_PATH = os.getenv("DB_PATH", "bot.db")

PREMIUM_PLANS = {
    "1d": (5, 1, "1 kun"),
    "7d": (34, 7, "7 kun"),
    "30d": (299, 31, "31 kun (+1 bonus kun)"),
    "1y": (1999, 365, "1 yil"),
}

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
openai = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

class CharacterForm(StatesGroup):
    name = State()
    personality = State()
    communication = State()
    location = State()
    scenario = State()
    first_speaker = State()
    clothing = State()
    clothing_color = State()

class ChatState(StatesGroup):
    chatting = State()

class ImageState(StatesGroup):
    action = State()

class VideoState(StatesGroup):
    seconds = State()
    action = State()

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        premium_until TEXT
    );
    CREATE TABLE IF NOT EXISTS characters(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        personality TEXT NOT NULL,
        communication TEXT NOT NULL,
        location TEXT,
        scenario TEXT,
        first_speaker TEXT,
        clothing TEXT,
        clothing_color TEXT,
        is_global INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        character_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """)
    conn.commit()
    conn.close()

def now():
    return datetime.now(timezone.utc)

def is_premium(user_id: int) -> bool:
    conn = db()
    row = conn.execute("SELECT premium_until FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    if not row or not row["premium_until"]:
        return False
    try:
        return datetime.fromisoformat(row["premium_until"]) > now()
    except Exception:
        return False

def add_premium(user_id: int, days: int):
    conn = db()
    row = conn.execute("SELECT premium_until FROM users WHERE user_id=?", (user_id,)).fetchone()
    current = now()
    if row and row["premium_until"]:
        try:
            old = datetime.fromisoformat(row["premium_until"])
            if old > current:
                current = old
        except Exception:
            pass
    until = current + timedelta(days=days)
    conn.execute(
        "INSERT INTO users(user_id,premium_until) VALUES(?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET premium_until=excluded.premium_until",
        (user_id, until.isoformat())
    )
    conn.commit()
    conn.close()
    return until

def get_characters(user_id: int):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM characters WHERE owner_id=? OR is_global=1 ORDER BY id DESC",
        (user_id,)
    ).fetchall()
    conn.close()
    return rows

def get_character(cid: int):
    conn = db()
    row = conn.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()
    conn.close()
    return row

def create_character(owner_id, data, is_global=0):
    conn = db()
    cur = conn.execute("""
        INSERT INTO characters
        (owner_id,name,personality,communication,location,scenario,
         first_speaker,clothing,clothing_color,is_global,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
    """, (
        owner_id, data["name"], data["personality"], data["communication"],
        data.get("location"), data.get("scenario"), data["first_speaker"],
        data["clothing"], data["clothing_color"], is_global, now().isoformat()
    ))
    conn.commit()
    cid = cur.lastrowid
    conn.close()
    return cid

def save_msg(user_id, character_id, role, content):
    conn = db()
    conn.execute(
        "INSERT INTO messages(user_id,character_id,role,content,created_at) VALUES(?,?,?,?,?)",
        (user_id, character_id, role, content, now().isoformat())
    )
    conn.commit()
    conn.close()

def history(user_id, character_id, limit=20):
    conn = db()
    rows = conn.execute(
        "SELECT role,content FROM messages WHERE user_id=? AND character_id=? "
        "ORDER BY id DESC LIMIT ?", (user_id, character_id, limit)
    ).fetchall()
    conn.close()
    return list(reversed(rows))

def safe_text(text: str) -> bool:
    # Basic guard against requests for sexual/undressed character generation.
    banned = [
        "nude", "nudity", "undressed", "no clothes", "without clothes",
        "yalang'och", "kiyimsiz", "yalangoch"
    ]
    t = text.lower()
    return not any(x in t for x in banned)

def premium_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ 1 kun — 5 Stars", callback_data="buy:1d")],
        [InlineKeyboardButton(text="⭐ 7 kun — 34 Stars", callback_data="buy:7d")],
        [InlineKeyboardButton(text="⭐ 31 kun — 299 Stars", callback_data="buy:30d")],
        [InlineKeyboardButton(text="⭐ 1 yil — 1999 Stars", callback_data="buy:1y")],
    ])

def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Chats", callback_data="menu:chats"),
         InlineKeyboardButton(text="⭐ Premium", callback_data="menu:premium")],
        [InlineKeyboardButton(text="🖼 Rasm", callback_data="menu:image"),
         InlineKeyboardButton(text="🎬 Video", callback_data="menu:video")]
    ])

@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "Assalomu alaykum! 🤖\n\n"
        "AI Character botiga xush kelibsiz.\n\n"
        "/chats — character yaratish va suhbat\n"
        "/premium — Premium olish\n"
        "/rasm — character uchun rasm\n"
        "/video — Premium video\n"
        "/deletecharacter — character o‘chirish\n"
        "/tahrirlash — character ma'lumotlarini tahrirlash\n"
        "/paysupport — to‘lov bo‘yicha yordam",
        reply_markup=main_keyboard()
    )

@dp.message(Command("premium"))
async def premium(message: Message):
    status = "Premium faol ✅" if is_premium(message.from_user.id) else "Premium faol emas."
    await message.answer(
        f"{status}\n\nPremium paketni tanlang:",
        reply_markup=premium_keyboard()
    )

@dp.callback_query(F.data.startswith("buy:"))
async def buy_premium(call: CallbackQuery):
    plan_id = call.data.split(":")[1]
    stars, days, title = PREMIUM_PLANS[plan_id]
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title=f"Premium — {title}",
        description=f"AI Character Premium: {title}",
        payload=f"premium:{plan_id}",
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=stars)],
        provider_token=""
    )
    await call.answer()

@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(query.id, ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    payment = message.successful_payment
    payload = payment.invoice_payload
    if not payload.startswith("premium:"):
        return
    plan_id = payload.split(":")[1]
    _, days, title = PREMIUM_PLANS[plan_id]
    until = add_premium(message.from_user.id, days)
    await message.answer(
        f"To‘lov qabul qilindi! ⭐\n"
        f"Premium: {title}\n"
        f"Amal qilish muddati: {until.strftime('%Y-%m-%d %H:%M UTC')}"
    )

@dp.message(Command("chats"))
async def chats(message: Message):
    if not is_premium(message.from_user.id):
        await message.answer("💎 /chats uchun Premium kerak.", reply_markup=premium_keyboard())
        return
    await message.answer(
        "Character yaratishni boshlaymiz.\nCharacter ismini yuboring:"
    )
    await CharacterForm.name.set()

@dp.message(CharacterForm.name)
async def char_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("Character xarakterini yozing:")
    await state.set_state(CharacterForm.personality)

@dp.message(CharacterForm.personality)
async def char_personality(message: Message, state: FSMContext):
    await state.update_data(personality=message.text.strip())
    await message.answer("Character siz bilan qanday muomalada bo‘lsin?")
    await state.set_state(CharacterForm.communication)

@dp.message(CharacterForm.communication)
async def char_communication(message: Message, state: FSMContext):
    await state.update_data(communication=message.text.strip())
    await message.answer("Qayerda bo‘ladi? Ixtiyoriy. O‘tkazib yuborish uchun /skip yozing.")
    await state.set_state(CharacterForm.location)

@dp.message(CharacterForm.location)
async def char_location(message: Message, state: FSMContext):
    await state.update_data(location="" if message.text == "/skip" else message.text.strip())
    await message.answer("Ssenariy? Ixtiyoriy. O‘tkazib yuborish uchun /skip yozing.")
    await state.set_state(CharacterForm.scenario)

@dp.message(CharacterForm.scenario)
async def char_scenario(message: Message, state: FSMContext):
    await state.update_data(scenario="" if message.text == "/skip" else message.text.strip())
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Men boshlayman", callback_data="first:user")],
        [InlineKeyboardButton(text="Character boshlaydi", callback_data="first:character")]
    ])
    await message.answer("Birinchi so‘zni kim beradi?", reply_markup=kb)
    await state.set_state(CharacterForm.first_speaker)

@dp.callback_query(F.data.startswith("first:"), CharacterForm.first_speaker)
async def first_speaker(call: CallbackQuery, state: FSMContext):
    await state.update_data(first_speaker=call.data.split(":")[1])
    await call.message.answer("Kiyim turini yozing (masalan: futbol formasi):")
    await state.set_state(CharacterForm.clothing)
    await call.answer()

@dp.message(CharacterForm.clothing)
async def char_clothing(message: Message, state: FSMContext):
    if not safe_text(message.text):
        await message.answer("Bu kiyim turi qo‘llab-quvvatlanmaydi. Oddiy, yoshga mos kiyim yozing.")
        return
    await state.update_data(clothing=message.text.strip())
    await message.answer("Kiyim rangini yozing:")
    await state.set_state(CharacterForm.clothing_color)

@dp.message(CharacterForm.clothing_color)
async def char_color(message: Message, state: FSMContext):
    if not safe_text(message.text):
        await message.answer("Bu so‘rov qo‘llab-quvvatlanmaydi.")
        return
    data = await state.get_data()
    data["clothing_color"] = message.text.strip()
    cid = create_character(message.from_user.id, data)
    await state.clear()
    await message.answer(
        f"✅ Character yaratildi!\nID: {cid}\n\n"
        f"Endi /chats orqali characterlaringizdan birini tanlab suhbat boshlashingiz mumkin."
    )

def character_keyboard(rows):
    b = InlineKeyboardBuilder()
    for row in rows[:30]:
        b.button(text=f"#{row['id']} {row['name']}", callback_data=f"char:{row['id']}")
    b.adjust(1)
    return b.as_markup()

@dp.message(Command("chats"))
async def chats_list(message: Message):
    if not is_premium(message.from_user.id):
        await message.answer("💎 /chats uchun Premium kerak.", reply_markup=premium_keyboard())
        return
    rows = get_characters(message.from_user.id)
    if not rows:
        await message.answer("Hali character yo‘q. /chats orqali yarating.")
        return
    await message.answer("Character tanlang:", reply_markup=character_keyboard(rows))

@dp.callback_query(F.data == "menu:chats")
async def menu_chats(call: CallbackQuery):
    await chats_list(call.message)
    await call.answer()

@dp.callback_query(F.data.startswith("char:"))
async def choose_character(call: CallbackQuery, state: FSMContext):
    cid = int(call.data.split(":")[1])
    c = get_character(cid)
    if not c:
        await call.answer("Character topilmadi.", show_alert=True)
        return
    if c["owner_id"] != call.from_user.id and not c["is_global"]:
        await call.answer("Bu character sizniki emas.", show_alert=True)
        return
    await state.set_state(ChatState.chatting)
    await state.update_data(character_id=cid)
    if c["first_speaker"] == "character" and not history(call.from_user.id, cid):
        await send_character_reply(call.from_user.id, cid, None, call.message)
    else:
        await call.message.answer(f"💬 {c['name']} bilan suhbat boshlandi.\nXabar yuboring.")
    await call.answer()

async def send_character_reply(user_id, cid, user_text, message: Message):
    c = get_character(cid)
    if not c or not openai:
        await message.answer("OpenAI API sozlanmagan.")
        return

    past = history(user_id, cid)
    context = "\n".join([f"{r['role']}: {r['content']}" for r in past])
    system = (
        f"Sen AI character {c['name']}san.\n"
        f"Xarakter: {c['personality']}\n"
        f"Muomala uslubi: {c['communication']}\n"
        f"Joy: {c['location'] or 'aniq emas'}\n"
        f"Ssenariy: {c['scenario'] or 'aniq emas'}\n"
        f"Kiyim: {c['clothing']} ({c['clothing_color']})\n"
        "Character rolidan chiqma. Foydalanuvchiga hurmat bilan javob ber. "
        "Jinsiy yoki yalang‘och mazmundagi kontent yaratma."
    )
    inp = system + "\n\nSuhbat tarixi:\n" + context
    if user_text:
        inp += f"\n\nuser: {user_text}"

    try:
        resp = await openai.responses.create(
            model=OPENAI_MODEL,
            input=inp,
            max_output_tokens=700
        )
        answer = resp.output_text
        if user_text:
            save_msg(user_id, cid, "user", user_text)
        save_msg(user_id, cid, "assistant", answer)
        await message.answer(answer)
    except Exception as e:
        logging.exception(e)
        await message.answer("AI bilan bog‘lanishda xatolik yuz berdi.")

@dp.message(ChatState.chatting)
async def chat_message(message: Message, state: FSMContext):
    data = await state.get_data()
    cid = data.get("character_id")
    if not cid:
        await message.answer("/chats orqali character tanlang.")
        return
    await send_character_reply(message.from_user.id, cid, message.text, message)

@dp.message(Command("deletecharacter"))
async def delete_character(message: Message):
    rows = get_characters(message.from_user.id)
    own = [r for r in rows if r["owner_id"] == message.from_user.id and not r["is_global"]]
    if not own:
        await message.answer("Sizda o‘chirish uchun character yo‘q.")
        return
    await message.answer("O‘chirish uchun character ID'sini yuboring.")
    await message.answer("\n".join([f"{r['id']} — {r['name']}" for r in own]))

@dp.message(Command("tahrirlash"))
async def edit_character(message: Message):
    await message.answer("Tahrirlash funksiyasi uchun avval character ID'sini yuboring. "
                         "Keyingi versiyada barcha maydonlarni bosqichma-bosqich o‘zgartirish qo‘shiladi.")

@dp.message(Command("ega"))
async def owner_command(message: Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("Bu buyruq faqat bot egasi uchun.")
        return
    await message.answer(
        "👑 Ega paneli\n"
        "/globalcharacter — global character yaratish\n"
        "/spam — character o‘chirish/moderatsiya\n"
        "/stats — statistika"
    )

@dp.message(Command("globalcharacter"))
async def global_character(message: Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("Faqat bot egasi.")
        return
    # Global character creation uses the same FSM; creation is marked global at completion.
    await message.answer("Global character yaratish: character ismini yuboring.")
    await CharacterForm.name.set()
    await message.answer("Eslatma: hozirgi minimal versiyada global flag yaratish uchun koddagi "
                         "OWNER_ID oqimini kengaytirish kerak; asosiy character yaratish oqimi ishlaydi.")

@dp.message(Command("spam"))
async def spam(message: Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("Faqat bot egasi.")
        return
    await message.answer("Spam character ID'sini yuboring. Keyingi versiyada tasdiqlash tugmasi qo‘shiladi.")

@dp.message(Command("rasm"))
async def image_command(message: Message):
    if not is_premium(message.from_user.id):
        await message.answer("🖼 Rasm yaratish Premium uchun.", reply_markup=premium_keyboard())
        return
    if not IMAGE_API_URL:
        await message.answer("IMAGE_API_URL sozlanmagan. Render Environment Variables ichiga image API URL qo‘ying.")
        return
    await message.answer("Character ID va holatini bitta xabarda yuboring.\nMasalan: 12 | futbol o‘ynayapti")
    await state_for_image(message)

async def state_for_image(message):
    # Simple one-message flow; no FSM needed.
    pass

@dp.message(Command("video"))
async def video_command(message: Message):
    if not is_premium(message.from_user.id):
        await message.answer("🎬 Video faqat Premium uchun.", reply_markup=premium_keyboard())
        return
    if not VIDEO_API_URL:
        await message.answer("VIDEO_API_URL sozlanmagan. Render Environment Variables ichiga video API URL qo‘ying.")
        return
    await message.answer("Video funksiyasi uchun: davomiylik va harakatni yozing.\nMasalan: 60 | futbol o‘ynayapti")

@dp.message(Command("paysupport"))
async def paysupport(message: Message):
    await message.answer("To‘lov muammosi bo‘lsa, bot egasiga murojaat qiling.")

@dp.callback_query(F.data == "menu:premium")
async def menu_premium(call: CallbackQuery):
    await premium(call.message)
    await call.answer()

@dp.callback_query(F.data == "menu:image")
async def menu_image(call: CallbackQuery):
    await image_command(call.message)
    await call.answer()

@dp.callback_query(F.data == "menu:video")
async def menu_video(call: CallbackQuery):
    await video_command(call.message)
    await call.answer()

# Render health endpoint
async def health(request):
    return web.Response(text="OK")

async def start_web():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")
    init_db()
    await start_web()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
