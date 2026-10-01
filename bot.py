"""Утренний Telegram-бот: сводка (погода, одежда, расписание, выход из дома, сон, планы).
Все личные данные пользователь вводит прямо в боте. Запуск: python bot.py"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove)

import planner as P

try:  # необязательно: токен можно положить в файл .env
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

log = logging.getLogger("morning-bot")
DB_PATH = os.getenv("DB_PATH", "bot.db")
router = Router()
esc = html.escape

MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📋 Сводка"), KeyboardButton(text="📝 Планы")],
              [KeyboardButton(text="📅 Расписание"), KeyboardButton(text="⚙️ Настройки")]],
    resize_keyboard=True,
)


def B(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ═════════════════════════ база данных ═════════════════════════

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY,
  city TEXT, lat REAL, lon REAL, tz TEXT,
  has_work INTEGER DEFAULT 0, has_study INTEGER DEFAULT 0,
  wake TEXT DEFAULT '07:00', sleep_h REAL DEFAULT 8, prep_min INTEGER DEFAULT 40,
  c_hw INTEGER DEFAULT 0, c_hs INTEGER DEFAULT 0, c_ws INTEGER DEFAULT 0,
  buffer_min INTEGER DEFAULT 10,
  reminders INTEGER DEFAULT 1, onboarded INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS blocks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, weekday INTEGER NOT NULL,
  kind TEXT NOT NULL, t_start INTEGER NOT NULL, t_end INTEGER NOT NULL, title TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS day_wake(
  user_id INTEGER, weekday INTEGER, wake TEXT, PRIMARY KEY(user_id, weekday)
);
CREATE TABLE IF NOT EXISTS plans(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, date TEXT NOT NULL,
  part TEXT NOT NULL, text TEXT NOT NULL, done INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sent(
  user_id INTEGER, key TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(user_id, key)
);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def q(sql: str, args: tuple = (), *, one: bool = False, many: bool = False):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, args)
        res = None
        if one:
            row = await cur.fetchone()
            res = dict(row) if row else None
        elif many:
            res = [dict(r) for r in await cur.fetchall()]
        await db.commit()
        return res


async def get_user(uid: int):
    return await q("SELECT * FROM users WHERE user_id=?", (uid,), one=True)


async def was_sent(uid: int, key: str) -> bool:
    return bool(await q("SELECT 1 AS x FROM sent WHERE user_id=? AND key=?", (uid, key), one=True))


async def set_sent(uid: int, key: str):
    await q("INSERT OR IGNORE INTO sent(user_id, key) VALUES(?,?)", (uid, key))


def tz_of(u) -> ZoneInfo:
    try:
        return ZoneInfo(u.get("tz") or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def local_now(u) -> datetime:
    return datetime.now(tz_of(u))


def commute_of(u) -> P.Commute:
    return P.Commute(u["c_hw"], u["c_hs"], u["c_ws"], u["buffer_min"])


async def wake_for(u, wd: int) -> int:
    row = await q("SELECT wake FROM day_wake WHERE user_id=? AND weekday=?", (u["user_id"], wd), one=True)
    t = P.parse_time(row["wake"]) if row else None
    if t is None:
        t = P.parse_time(u["wake"])
    return 7 * 60 if t is None else t


async def get_items(u, wd: int) -> list[P.Item]:
    rows = await q("SELECT * FROM blocks WHERE user_id=? AND weekday=? ORDER BY t_start",
                   (u["user_id"], wd), many=True)
    out = []
    for r in rows:
        if r["kind"] == "work" and not u["has_work"]:
            continue
        if r["kind"] == "study" and not u["has_study"]:
            continue
        out.append(P.Item(r["kind"], r["t_start"], r["t_end"], r["title"] or ""))
    return out


async def add_blocks(uid: int, kind: str, entries) -> int:
    added = 0
    for wd, s, e, title in entries:
        dup = await q("SELECT 1 AS x FROM blocks WHERE user_id=? AND weekday=? AND kind=? AND t_start=? AND t_end=?",
                      (uid, wd, kind, s, e), one=True)
        if dup:
            continue
        await q("INSERT INTO blocks(user_id, weekday, kind, t_start, t_end, title) VALUES(?,?,?,?,?,?)",
                (uid, wd, kind, s, e, title))
        added += 1
    return added


# ═════════════════════════ внешние сервисы (Open-Meteo, без ключей) ═════════════════════════

async def http_json(url: str, params: dict):
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.get(url, params=params) as r:
            r.raise_for_status()
            return await r.json()


async def geocode(name: str) -> list[dict]:
    data = await http_json("https://geocoding-api.open-meteo.com/v1/search",
                           {"name": name, "count": 5, "language": "ru", "format": "json"})
    return data.get("results") or []


async def fetch_weather(u) -> dict | None:
    try:
        return await http_json("https://api.open-meteo.com/v1/forecast", {
            "latitude": u["lat"], "longitude": u["lon"], "timezone": u["tz"] or "auto",
            "wind_speed_unit": "ms", "forecast_days": 2,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "hourly": "temperature_2m,apparent_temperature,precipitation_probability,"
                      "precipitation,weather_code,wind_speed_10m",
        })
    except Exception:
        log.exception("weather fetch failed")
        return None


def window_stats(w: dict, ds: str, h0: int, h1: int) -> dict | None:
    h = w.get("hourly") or {}
    times = h.get("time") or []
    idx = [i for i, t in enumerate(times) if t.startswith(ds) and h0 <= int(t[11:13]) <= h1]
    if not idx:
        return None

    def col(name):
        return [h[name][i] for i in idx if h.get(name) and h[name][i] is not None]

    temp, feel = col("temperature_2m"), col("apparent_temperature")
    if not temp:
        return None
    feel = feel or temp
    codes = col("weather_code")
    code = 0
    if codes:
        worst = max(codes)
        code = worst if worst >= 51 else codes[len(codes) // 2]
    return {
        "tmin": min(temp), "tmax": max(temp), "fmin": min(feel), "fmax": max(feel),
        "pop": max(col("precipitation_probability") or [0]),
        "mm": sum(col("precipitation") or [0]),
        "wind": max(col("wind_speed_10m") or [0]),
        "code": code,
    }


def deg(x) -> str:
    v = round(x)
    return f"{v:+d}°" if v else "0°"


def weather_block(u, w: dict, st: dict) -> str:
    cur = w.get("current") or {}
    lines = [f"🌤 <b>Погода — {esc((u['city'] or '').split(',')[0])}</b>"]
    if cur.get("temperature_2m") is not None:
        d, e = P.wmo(cur.get("weather_code"))
        feel = cur.get("apparent_temperature", cur["temperature_2m"])
        lines.append(f"{e} Сейчас {deg(cur['temperature_2m'])}, ощущается как {deg(feel)}" + (f" — {d}" if d else ""))
    lines.append(f"📈 В течение дня: от {deg(st['tmin'])} до {deg(st['tmax'])}")
    d2, e2 = P.wmo(st["code"])
    if st["code"] >= 45 and d2:
        lines.append(f"{e2} Ожидается: {d2}")
    if st["pop"] >= 20 or st["mm"] >= 0.2:
        lines.append(f"☔ Осадки: вероятность до {st['pop']:.0f}%, около {st['mm']:.1f} мм")
    lines.append(f"💨 Ветер до {st['wind']:.0f} м/с")
    return "\n".join(lines)


# ═════════════════════════ планы ═════════════════════════

PARTS = {"morning": "🌅 Утро", "day": "☀️ День", "evening": "🌆 Вечер"}


def human_day(ds: str, today: date) -> str:
    d = date.fromisoformat(ds)
    if d == today:
        return "сегодня"
    if d == today + timedelta(days=1):
        return "завтра"
    return f"{d.day} {P.MONTHS_GEN[d.month - 1]} ({P.DAYS_SHORT[d.weekday()]})"


async def plans_text(uid: int, ds: str, title: str, evening_label: str | None = None) -> str:
    rows = await q("SELECT * FROM plans WHERE user_id=? AND date=? ORDER BY id", (uid, ds), many=True)
    if not rows:
        return f"📝 <b>{title}</b>\nПока пусто — добавь через «📝 Планы»."
    out = [f"📝 <b>{title}</b>"]
    for part, label in PARTS.items():
        items = [r for r in rows if r["part"] == part]
        if not items:
            continue
        if part == "evening" and evening_label:
            label = evening_label
        out.append(label)
        for r in items:
            t = esc(r["text"])
            out.append(f"   {'✅ <s>' + t + '</s>' if r['done'] else '• ' + t}")
    return "\n".join(out)


def plans_kb(rows: list[dict], ds: str) -> InlineKeyboardMarkup:
    keyboard = []
    for r in rows:
        keyboard.append([B(f"{'✅' if r['done'] else '⬜️'} {r['text'][:28]}", f"pl:t:{r['id']}:{ds}"),
                         B("🗑", f"pl:d:{r['id']}:{ds}")])
    keyboard.append([B("Сегодня", "pl:v:today"), B("Завтра", "pl:v:tomorrow"), B("📅 Дата", "pl:date")])
    keyboard.append([B("➕ Добавить план", f"pl:add:{ds}")])
    return kb(keyboard)


async def reply(target, text: str, markup=None, edit: bool = False):
    if isinstance(target, CallbackQuery):
        if edit:
            try:
                await target.message.edit_text(text, reply_markup=markup)
            except TelegramBadRequest:
                pass
        else:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


async def show_plans(target, uid: int, ds: str | None = None, edit: bool = False):
    u = await get_user(uid)
    today = local_now(u).date()
    ds = ds or today.isoformat()
    rows = await q("SELECT * FROM plans WHERE user_id=? AND date=? ORDER BY id", (uid, ds), many=True)
    text = await plans_text(uid, ds, f"Планы на {human_day(ds, today)}")
    await reply(target, text, plans_kb(rows, ds), edit=edit)


# ═════════════════════════ сводка ═════════════════════════

async def build_brief(u, day: date) -> str:
    uid, wd, ds = u["user_id"], day.weekday(), day.isoformat()
    items = await get_items(u, wd)
    sessions = P.build_sessions(items)
    trips = P.build_trips(sessions, commute_of(u))
    wake, prep = await wake_for(u, wd), u["prep_min"]

    parts = [f"☀️ <b>Доброе утро!</b>\n{P.DAYS_FULL[wd]}, {day.day} {P.MONTHS_GEN[day.month - 1]}"]

    # погода + одежда
    h0 = wake // 60
    h1 = min(23, max(trips[-1].target // 60 + 1, 18)) if trips else 20
    h1 = max(h1, h0)
    w = await fetch_weather(u)
    st = window_stats(w, ds, h0, h1) if w else None
    if w and st:
        parts.append(weather_block(u, w, st))
        tips = P.clothing_advice(st["fmin"], st["fmax"], st["pop"], st["mm"], st["wind"],
                                 st["code"] in P.SNOW_CODES)
        parts.append("👕 <b>Что надеть</b>\n" + "\n".join(tips))
    else:
        parts.append("🌤 Погоду сейчас получить не удалось — попробуй позже командой /brief.")

    # день
    if sessions:
        parts.append("📅 <b>План дня</b>\n" + "\n".join(P.day_lines(sessions, trips, wake, prep)))
        first = trips[0]
        if first.frm == "home":
            latest = first.leave - prep
            if wake > latest:
                parts.append(f"⚠️ С подъёмом в {P.fmt(wake)} ты не успеешь собраться: "
                             f"вставать нужно не позже {P.fmt(latest)}.")
    elif u["has_work"] or u["has_study"]:
        parts.append("📅 В расписании на сегодня ничего нет — свободный день 🎉")

    # планы
    label = f"🌆 Вечер (после {P.fmt(trips[-1].target)}, когда будешь дома)" if trips else None
    parts.append(await plans_text(uid, ds, "Планы на день", label))

    # сон
    sl = int(round(u["sleep_h"] * 60))
    wake_next = await wake_for(u, (wd + 1) % 7)
    bed = P.bedtime(wake_next, u["sleep_h"])
    if wake_next - sl >= 0:
        parts.append(f"🌙 Чтобы выспаться ({u['sleep_h']:g} ч) к подъёму в {P.fmt(wake_next)}, "
                     f"ложись не позже {P.fmt(bed)} (уже после полуночи).")
    else:
        parts.append(f"🌙 Сегодня ложись до <b>{P.fmt(bed)}</b> — так выспишься ({u['sleep_h']:g} ч) "
                     f"к подъёму в {P.fmt(wake_next)}.")
    return "\n\n".join(parts)


async def send_long(bot: Bot, chat_id: int, text: str, markup=None):
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for i, ch in enumerate(chunks):
        await bot.send_message(chat_id, ch.rstrip(), reply_markup=markup if i == len(chunks) - 1 else None)


async def safe_send(bot: Bot, uid: int, text: str):
    try:
        await bot.send_message(uid, text)
    except TelegramForbiddenError:
        log.warning("user %s blocked the bot", uid)


# ═════════════════════════ состояния ═════════════════════════

class Onb(StatesGroup):
    city = State()
    wake = State()
    sleep = State()
    prep = State()
    buffer = State()
    c_hw = State()
    c_hs = State()
    c_ws = State()
    sched_work = State()
    sched_study = State()


class Edit(StatesGroup):
    city = State()
    wake = State()
    num = State()
    sched = State()
    plan_text = State()
    plan_date = State()


ORDER = ["wake", "sleep", "prep", "buffer", "c_hw", "c_hs", "c_ws", "sched_work", "sched_study"]

NUM = {  # шаг → (колонка, тип, мин, макс)
    "sleep": ("sleep_h", float, 3, 14),
    "prep": ("prep_min", int, 0, 240),
    "buffer": ("buffer_min", int, 0, 60),
    "c_hw": ("c_hw", int, 0, 300),
    "c_hs": ("c_hs", int, 0, 300),
    "c_ws": ("c_ws", int, 0, 300),
}

WORK_HELP = (
    "💼 <b>График работы.</b> Пришли строки: день(и) и время.\n"
    "<code>пн-пт 09:00-17:00</code>\n<code>сб 10:00-14:00</code>\n\n"
    "День можно разбить на несколько блоков (например, работа 9–12 и снова 15–17) — "
    "просто напиши две строки на один день:\n"
    "<code>пн 09:00-12:00</code>\n<code>пн 15:00-17:00</code>\n\n"
    "Можно слать несколько сообщений. Закончишь — жми «Готово»."
)
STUDY_HELP = (
    "🎓 <b>Расписание пар.</b> Каждая пара — отдельная строка: день, начало–конец и название.\n"
    "<code>пн 08:30-10:00 Математика</code>\n<code>пн 10:10-11:40 Физика</code>\n\n"
    "Если пара повторяется в несколько дней: <code>пн,ср 13:00-14:30 Английский</code> "
    "или <code>пн-пт 08:30-10:00</code>.\n"
    "Перемены бот посчитает сам по промежуткам между парами. Если колледж у тебя посреди рабочего дня "
    "(например, с 12 до 15), просто укажи эти пары на нужный день.\n\n"
    "Можно слать несколько сообщений. Закончишь — жми «Готово»."
)
PROMPTS = {
    "wake": "⏰ Во сколько ты обычно встаёшь? Напиши время, например <code>07:00</code>.\n"
            "(Для отдельных дней потом можно задать другое время в «⚙️ Настройки».)",
    "sleep": "😴 Сколько часов сна тебе нужно, чтобы выспаться? Например <code>8</code>.",
    "prep": "🚿 Сколько минут тебе нужно на сборы после подъёма (умыться, позавтракать, одеться)?",
    "buffer": "⏳ Какой запас времени закладывать на дорогу, минут? Обычно 5–10. Если не нужен — <code>0</code>.",
    "c_hw": "🚌 Сколько минут добираться от дома до работы?",
    "c_hs": "🚌 Сколько минут добираться от дома до колледжа?",
    "c_ws": "🚌 Сколько минут добираться от работы до колледжа (и обратно)?",
    "sched_work": WORK_HELP,
    "sched_study": STUDY_HELP,
}


def applicable(step: str, u) -> bool:
    w, s = bool(u["has_work"]), bool(u["has_study"])
    return {"prep": w or s, "buffer": w or s, "c_hw": w, "c_hs": s, "c_ws": w and s,
            "sched_work": w, "sched_study": s}.get(step, True)


async def ask_next(bot: Bot, chat_id: int, uid: int, state: FSMContext, current: str | None):
    u = await get_user(uid)
    idx = ORDER.index(current) + 1 if current else 0
    while idx < len(ORDER) and not applicable(ORDER[idx], u):
        idx += 1
    if idx >= len(ORDER):
        await finish_onboarding(bot, chat_id, uid, state)
        return
    step = ORDER[idx]
    await state.set_state(getattr(Onb, step))
    markup = kb([[B("✅ Готово", f"onb:done:{step}")]]) if step.startswith("sched_") else None
    await bot.send_message(chat_id, PROMPTS[step], reply_markup=markup)


async def finish_onboarding(bot: Bot, chat_id: int, uid: int, state: FSMContext):
    await q("UPDATE users SET onboarded=1 WHERE user_id=?", (uid,))
    await state.clear()
    u = await get_user(uid)
    extra = ""
    if not (u["has_work"] or u["has_study"]):
        extra = "\nРаз ни работы, ни учёбы нет — буду присылать погоду, одежду, твои планы и напоминать про сон."
    await bot.send_message(
        chat_id,
        "🎉 <b>Всё настроено!</b> Каждое утро в твоё время подъёма я буду присылать сводку, "
        "а в течение дня — напоминания, когда выходить, когда закончить работу и когда ложиться спать."
        + extra + "\n\nВсё можно поменять в «📅 Расписание» и «⚙️ Настройки». Вот сводка на сегодня:",
        reply_markup=MENU,
    )
    today = local_now(u).date()
    await send_long(bot, chat_id, await build_brief(u, today))
    await set_sent(uid, f"brief:{today.isoformat()}")


# ═════════════════════════ онбординг ═════════════════════════

@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    await q("INSERT INTO users(user_id) VALUES(?) ON CONFLICT(user_id) DO NOTHING", (uid,))
    u = await get_user(uid)
    if u["onboarded"]:
        await m.answer("С возвращением! Выбирай в меню 👇", reply_markup=MENU)
        return
    await state.set_state(Onb.city)
    await state.update_data(mode="onb")
    await m.answer(
        "👋 Привет! Я твой утренний помощник: сводка погоды, что надеть, расписание пар и работы, "
        "когда выходить из дома и когда ложиться спать.\n\n"
        "Сначала немного познакомимся. <b>В каком городе ты живёшь?</b> (нужно для погоды)",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменил. Выбирай в меню 👇", reply_markup=MENU)


@router.message(StateFilter(Onb.city, Edit.city))
async def got_city(m: Message, state: FSMContext):
    try:
        res = await geocode((m.text or "").strip())
    except Exception:
        log.exception("geocode failed")
        await m.answer("Не получилось связаться с сервисом погоды. Попробуй ещё раз чуть позже.")
        return
    if not res:
        await m.answer("Не нашёл такой город 🤔 Попробуй написать иначе, например «Казань» или «Минск».")
        return
    await state.update_data(cands=res)
    rows = []
    for i, r in enumerate(res):
        label = ", ".join(x for x in (r.get("name"), r.get("admin1"), r.get("country")) if x)
        rows.append([B(label[:60], f"city:{i}")])
    await m.answer("Выбери свой город:", reply_markup=kb(rows))


@router.callback_query(F.data.startswith("city:"))
async def pick_city(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cands = data.get("cands") or []
    i = int(cb.data.split(":")[1])
    if i >= len(cands):
        await cb.answer("Список устарел — введи город заново", show_alert=True)
        return
    r = cands[i]
    label = ", ".join(x for x in (r.get("name"), r.get("admin1"), r.get("country")) if x)
    await q("UPDATE users SET city=?, lat=?, lon=?, tz=? WHERE user_id=?",
            (label, r["latitude"], r["longitude"], r.get("timezone") or "UTC", cb.from_user.id))
    await cb.message.edit_text(f"📍 Город: {esc(label)}")
    await cb.answer()
    if data.get("mode") == "onb":
        await cb.message.answer("💼 Ты сейчас работаешь?", reply_markup=kb([[B("Да", "onb:work:1"), B("Нет", "onb:work:0")]]))
    else:
        await state.clear()
        await cb.message.answer("Город обновлён ✅", reply_markup=MENU)


@router.callback_query(F.data.startswith("onb:work:"))
async def onb_work(cb: CallbackQuery):
    val = int(cb.data.split(":")[2])
    await q("UPDATE users SET has_work=? WHERE user_id=?", (val, cb.from_user.id))
    await cb.message.edit_text(f"💼 Работа: {'да' if val else 'нет'}")
    await cb.answer()
    await cb.message.answer("🎓 Ты учишься (колледж)?", reply_markup=kb([[B("Да", "onb:study:1"), B("Нет", "onb:study:0")]]))


@router.callback_query(F.data.startswith("onb:study:"))
async def onb_study(cb: CallbackQuery, state: FSMContext):
    val = int(cb.data.split(":")[2])
    await q("UPDATE users SET has_study=? WHERE user_id=?", (val, cb.from_user.id))
    await cb.message.edit_text(f"🎓 Учёба: {'да' if val else 'нет'}")
    await cb.answer()
    await ask_next(cb.bot, cb.message.chat.id, cb.from_user.id, state, None)


@router.message(Onb.wake)
async def onb_wake(m: Message, state: FSMContext):
    t = P.parse_time(m.text)
    if t is None:
        await m.answer("Не понял время. Напиши, например, <code>07:30</code>.")
        return
    await q("UPDATE users SET wake=? WHERE user_id=?", (P.fmt(t), m.from_user.id))
    await ask_next(m.bot, m.chat.id, m.from_user.id, state, "wake")


def parse_num(text, cast, lo, hi):
    try:
        v = cast((text or "").strip().replace(",", ".").split()[0])
    except Exception:
        return None
    return v if lo <= v <= hi else None


@router.message(StateFilter(Onb.sleep, Onb.prep, Onb.buffer, Onb.c_hw, Onb.c_hs, Onb.c_ws))
async def onb_num(m: Message, state: FSMContext):
    step = (await state.get_state()).split(":")[1]
    col, cast, lo, hi = NUM[step]
    v = parse_num(m.text, cast, lo, hi)
    if v is None:
        await m.answer(f"Нужно число от {lo} до {hi}. Попробуй ещё раз.")
        return
    await q(f"UPDATE users SET {col}=? WHERE user_id=?", (v, m.from_user.id))
    await ask_next(m.bot, m.chat.id, m.from_user.id, state, step)


@router.message(StateFilter(Onb.sched_work, Onb.sched_study))
async def onb_sched(m: Message, state: FSMContext):
    step = (await state.get_state()).split(":")[1]
    kind = "work" if step == "sched_work" else "study"
    ok, errs = P.parse_schedule_text(m.text or "", kind)
    msg = ""
    if ok:
        n = await add_blocks(m.from_user.id, kind, ok)
        msg += f"✅ Добавил записей: {n}. Можно прислать ещё или нажать «Готово».\n"
    if errs:
        msg += "\n⚠️ Не разобрал:\n" + "\n".join(errs) + "\n\nФормат: <code>пн-пт 09:00-17:00 Название</code>"
    await m.answer(msg or "Не понял. Формат: <code>пн-пт 09:00-17:00</code>",
                   reply_markup=kb([[B("✅ Готово", f"onb:done:{step}")]]))


@router.callback_query(F.data.startswith("onb:done:"))
async def onb_done(cb: CallbackQuery, state: FSMContext):
    step = cb.data.split(":")[2]
    await cb.answer()
    if (await state.get_state() or "").split(":")[-1] != step:
        return
    await ask_next(cb.bot, cb.message.chat.id, cb.from_user.id, state, step)


# ═════════════════════════ расписание ═════════════════════════

async def schedule_text(uid: int) -> str:
    rows = await q("SELECT * FROM blocks WHERE user_id=? ORDER BY weekday, t_start", (uid,), many=True)
    wk = {r["weekday"]: r["wake"] for r in await q("SELECT * FROM day_wake WHERE user_id=?", (uid,), many=True)}
    out = ["📅 <b>Твоё расписание</b>"]
    for wd in range(7):
        day = [r for r in rows if r["weekday"] == wd]
        head = f"\n<b>{P.DAYS_FULL[wd]}</b>" + (f"  (подъём {wk[wd]})" if wd in wk else "")
        out.append(head)
        if not day:
            out.append("   — свободно")
        for r in day:
            title = esc(r["title"] or P.TITLE[r["kind"]])
            out.append(f"   {P.ICON[r['kind']]} {P.fmt(r['t_start'])}–{P.fmt(r['t_end'])} {title}")
    return "\n".join(out)


def schedule_kb() -> InlineKeyboardMarkup:
    return kb([[B("➕ Работа", "sch:add:work"), B("➕ Пары", "sch:add:study")],
               [B("🗑 Удалить запись", "sch:del"), B("🧹 Очистить день", "sch:clr")],
               [B("⏰ Подъём по дням", "sch:wake")]])


def day_picker(prefix: str) -> InlineKeyboardMarkup:
    btns = [B(P.DAYS_SHORT[i], f"{prefix}:{i}") for i in range(7)]
    return kb([btns[:4], btns[4:]])


async def show_schedule(target, edit: bool = False):
    await reply(target, await schedule_text(target.from_user.id), schedule_kb(), edit=edit)


@router.callback_query(F.data.startswith("sch:add:"))
async def sch_add(cb: CallbackQuery, state: FSMContext):
    kind = cb.data.split(":")[2]
    await state.set_state(Edit.sched)
    await state.update_data(kind=kind)
    await cb.answer()
    await cb.message.answer((WORK_HELP if kind == "work" else STUDY_HELP).replace("Закончишь — жми «Готово».", "Для отмены — /cancel."))


@router.message(Edit.sched)
async def edit_sched(m: Message, state: FSMContext):
    kind = (await state.get_data())["kind"]
    ok, errs = P.parse_schedule_text(m.text or "", kind)
    if not ok:
        await m.answer("Не получилось разобрать.\n" + "\n".join(errs) +
                       "\n\nФормат: <code>пн-пт 09:00-17:00 Название</code>. Отмена — /cancel.")
        return
    uid = m.from_user.id
    n = await add_blocks(uid, kind, ok)
    note = ""
    u = await get_user(uid)
    if not u[f"has_{kind}"]:
        await q(f"UPDATE users SET has_{kind}=1 WHERE user_id=?", (uid,))
        note = "\n💡 Я включил " + ("работу" if kind == "work" else "учёбу") + \
               " в настройках — не забудь указать время в пути: «⚙️ Настройки» → «Дорога»."
    await state.clear()
    await m.answer(f"✅ Добавлено записей: {n}." + (("\n⚠️ Не разобрал:\n" + "\n".join(errs)) if errs else "") + note)
    await show_schedule(m)


@router.callback_query(F.data == "sch:del")
async def sch_del(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer("Из какого дня удалить запись?", reply_markup=day_picker("sch:dd"))


async def day_delete_kb(uid: int, wd: int) -> InlineKeyboardMarkup:
    rows = await q("SELECT * FROM blocks WHERE user_id=? AND weekday=? ORDER BY t_start", (uid, wd), many=True)
    keyboard = [[B(f"🗑 {P.fmt(r['t_start'])}–{P.fmt(r['t_end'])} {r['title'] or P.TITLE[r['kind']]}"[:60],
                   f"sch:dx:{r['id']}:{wd}")] for r in rows]
    return kb(keyboard or [[B("Пусто — назад к расписанию", "sch:back")]])


@router.callback_query(F.data.startswith("sch:dd:"))
async def sch_dd(cb: CallbackQuery):
    wd = int(cb.data.split(":")[2])
    await cb.answer()
    await cb.message.edit_text(f"{P.DAYS_FULL[wd]}: что удалить?", reply_markup=await day_delete_kb(cb.from_user.id, wd))


@router.callback_query(F.data.startswith("sch:dx:"))
async def sch_dx(cb: CallbackQuery):
    _, _, bid, wd = cb.data.split(":")
    await q("DELETE FROM blocks WHERE id=? AND user_id=?", (int(bid), cb.from_user.id))
    await cb.answer("Удалено")
    await cb.message.edit_text(f"{P.DAYS_FULL[int(wd)]}: что удалить ещё?",
                               reply_markup=await day_delete_kb(cb.from_user.id, int(wd)))


@router.callback_query(F.data == "sch:back")
async def sch_back(cb: CallbackQuery):
    await cb.answer()
    await show_schedule(cb)


@router.callback_query(F.data == "sch:clr")
async def sch_clr(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer("Какой день очистить полностью?", reply_markup=day_picker("sch:cd"))


@router.callback_query(F.data.startswith("sch:cd:"))
async def sch_cd(cb: CallbackQuery):
    wd = int(cb.data.split(":")[2])
    await q("DELETE FROM blocks WHERE user_id=? AND weekday=?", (cb.from_user.id, wd))
    await cb.answer("Очищено")
    await cb.message.edit_text(f"🧹 {P.DAYS_FULL[wd]} очищен.")


WAKE_HELP = ("⏰ Напиши время подъёма.\n"
             "• Для всех дней: <code>07:00</code>\n"
             "• Для конкретных дней: <code>пн-пт 06:30</code> или <code>сб,вс 09:00</code>\n"
             "• Вернуть обычное для дней: <code>сб,вс сброс</code>\n"
             "Для отмены — /cancel.")


@router.callback_query(F.data.in_({"sch:wake", "set:wake"}))
async def ask_wake(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Edit.wake)
    await cb.answer()
    await cb.message.answer(WAKE_HELP)


async def apply_wake(uid: int, text: str) -> str | None:
    t = P.parse_time(text)
    if t is not None:
        await q("UPDATE users SET wake=? WHERE user_id=?", (P.fmt(t), uid))
        return f"⏰ Подъём по умолчанию: {P.fmt(t)}"
    mt = re.match(r"^\s*(.+?)\s+(\S+)\s*$", text or "")
    if not mt:
        return None
    days = P.parse_days(mt.group(1))
    arg = mt.group(2).lower()
    if days is None:
        return None
    names = ", ".join(P.DAYS_SHORT[d] for d in days)
    if arg in ("сброс", "обычно"):
        for d in days:
            await q("DELETE FROM day_wake WHERE user_id=? AND weekday=?", (uid, d))
        return f"⏰ {names}: подъём как по умолчанию"
    t = P.parse_time(arg)
    if t is None:
        return None
    for d in days:
        await q("INSERT INTO day_wake(user_id, weekday, wake) VALUES(?,?,?) "
                "ON CONFLICT(user_id, weekday) DO UPDATE SET wake=excluded.wake", (uid, d, P.fmt(t)))
    return f"⏰ {names}: подъём в {P.fmt(t)}"


@router.message(Edit.wake)
async def edit_wake(m: Message, state: FSMContext):
    res = await apply_wake(m.from_user.id, m.text or "")
    if not res:
        await m.answer("Не понял.\n\n" + WAKE_HELP)
        return
    await state.clear()
    await m.answer(res + "\nСводка будет приходить в это время.", reply_markup=MENU)


# ═════════════════════════ настройки ═════════════════════════

async def settings_view(uid: int):
    u = await get_user(uid)
    text = "\n".join([
        "⚙️ <b>Настройки</b>",
        f"📍 Город: {esc(u['city'] or '—')}",
        f"💼 Работа: {'да' if u['has_work'] else 'нет'}   🎓 Учёба: {'да' if u['has_study'] else 'нет'}",
        f"⏰ Подъём: {u['wake']}   😴 Сон: {u['sleep_h']:g} ч",
        f"🚿 Сборы: {u['prep_min']} мин   ⏳ Запас на дорогу: {u['buffer_min']} мин",
        f"🚌 Дом–работа: {u['c_hw']} мин, дом–колледж: {u['c_hs']} мин, работа–колледж: {u['c_ws']} мин",
        f"🔔 Напоминания: {'включены' if u['reminders'] else 'выключены'}",
    ])
    rows = [[B("📍 Город", "set:city"), B("⏰ Подъём", "set:wake")],
            [B("😴 Сон", "set:num:sleep"), B("🚿 Сборы", "set:num:prep"), B("⏳ Запас", "set:num:buffer")]]
    road = []
    if u["has_work"]:
        road.append(B("🚌 Дом–работа", "set:num:c_hw"))
    if u["has_study"]:
        road.append(B("🚌 Дом–колледж", "set:num:c_hs"))
    if u["has_work"] and u["has_study"]:
        road.append(B("🚌 Работа–колледж", "set:num:c_ws"))
    if road:
        rows.append(road)
    rows.append([B(f"💼 Работа: {'выкл' if u['has_work'] else 'вкл'}", "set:flag:work"),
                 B(f"🎓 Учёба: {'выкл' if u['has_study'] else 'вкл'}", "set:flag:study")])
    rows.append([B(f"🔔 Напоминания: {'выкл' if u['reminders'] else 'вкл'}", "set:rem")])
    return text, kb(rows)


async def show_settings(target, edit: bool = False):
    text, markup = await settings_view(target.from_user.id)
    await reply(target, text, markup, edit=edit)


@router.callback_query(F.data == "set:city")
async def set_city(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Edit.city)
    await state.update_data(mode="edit")
    await cb.answer()
    await cb.message.answer("📍 В каком городе ты теперь? Напиши название. (Отмена — /cancel)")


@router.callback_query(F.data.startswith("set:num:"))
async def set_num(cb: CallbackQuery, state: FSMContext):
    step = cb.data.split(":")[2]
    await state.set_state(Edit.num)
    await state.update_data(step=step)
    await cb.answer()
    await cb.message.answer(PROMPTS[step] + "\n(Отмена — /cancel)")


@router.message(Edit.num)
async def edit_num(m: Message, state: FSMContext):
    step = (await state.get_data())["step"]
    col, cast, lo, hi = NUM[step]
    v = parse_num(m.text, cast, lo, hi)
    if v is None:
        await m.answer(f"Нужно число от {lo} до {hi}. Попробуй ещё раз.")
        return
    await q(f"UPDATE users SET {col}=? WHERE user_id=?", (v, m.from_user.id))
    await state.clear()
    await m.answer("Сохранил ✅")
    await show_settings(m)


@router.callback_query(F.data.startswith("set:flag:"))
async def set_flag(cb: CallbackQuery):
    kind = cb.data.split(":")[2]
    u = await get_user(cb.from_user.id)
    await q(f"UPDATE users SET has_{kind}=? WHERE user_id=?", (0 if u[f"has_{kind}"] else 1, cb.from_user.id))
    await cb.answer()
    await show_settings(cb, edit=True)


@router.callback_query(F.data == "set:rem")
async def set_rem(cb: CallbackQuery):
    u = await get_user(cb.from_user.id)
    await q("UPDATE users SET reminders=? WHERE user_id=?", (0 if u["reminders"] else 1, cb.from_user.id))
    await cb.answer()
    await show_settings(cb, edit=True)


# ═════════════════════════ планы: обработчики ═════════════════════════

@router.callback_query(F.data.startswith("pl:v:"))
async def pl_view(cb: CallbackQuery):
    which = cb.data.split(":")[2]
    u = await get_user(cb.from_user.id)
    today = local_now(u).date()
    ds = (today + timedelta(days=1 if which == "tomorrow" else 0)).isoformat()
    await cb.answer()
    await show_plans(cb, cb.from_user.id, ds, edit=True)


@router.callback_query(F.data.startswith("pl:t:") | F.data.startswith("pl:d:"))
async def pl_toggle_delete(cb: CallbackQuery):
    _, action, pid, ds = cb.data.split(":", 3)
    if action == "t":
        await q("UPDATE plans SET done=1-done WHERE id=? AND user_id=?", (int(pid), cb.from_user.id))
    else:
        await q("DELETE FROM plans WHERE id=? AND user_id=?", (int(pid), cb.from_user.id))
    await cb.answer()
    await show_plans(cb, cb.from_user.id, ds, edit=True)


@router.callback_query(F.data.startswith("pl:add:"))
async def pl_add(cb: CallbackQuery):
    ds = cb.data.split(":", 2)[2]
    await cb.answer()
    await cb.message.answer("Когда в этот день?", reply_markup=kb(
        [[B("🌅 Утро", f"pl:part:{ds}:morning"), B("☀️ День", f"pl:part:{ds}:day"),
          B("🌆 Вечер", f"pl:part:{ds}:evening")]]))


@router.callback_query(F.data.startswith("pl:part:"))
async def pl_part(cb: CallbackQuery, state: FSMContext):
    _, _, ds, part = cb.data.split(":")
    await state.set_state(Edit.plan_text)
    await state.update_data(ds=ds, part=part)
    await cb.answer()
    await cb.message.edit_text(f"{PARTS[part]}: напиши план. Можно несколько строк — каждая станет отдельным планом. "
                               "(Отмена — /cancel)")


@router.message(Edit.plan_text)
async def pl_text(m: Message, state: FSMContext):
    data = await state.get_data()
    lines = [ln.strip(" •-\t") for ln in (m.text or "").splitlines() if ln.strip(" •-\t")]
    if not lines:
        await m.answer("Напиши текст плана.")
        return
    for ln in lines:
        await q("INSERT INTO plans(user_id, date, part, text) VALUES(?,?,?,?)",
                (m.from_user.id, data["ds"], data["part"], ln[:200]))
    await state.clear()
    await show_plans(m, m.from_user.id, data["ds"])


@router.callback_query(F.data == "pl:date")
async def pl_date(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Edit.plan_date)
    await cb.answer()
    await cb.message.answer("📅 Напиши дату, например <code>15.10</code> или <code>15.10.2026</code>. (Отмена — /cancel)")


@router.message(Edit.plan_date)
async def pl_date_text(m: Message, state: FSMContext):
    mt = re.match(r"^\s*(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\s*$", m.text or "")
    u = await get_user(m.from_user.id)
    today = local_now(u).date()
    try:
        if not mt:
            raise ValueError
        y = int(mt.group(3)) if mt.group(3) else today.year
        y = y + 2000 if y < 100 else y
        d = date(y, int(mt.group(2)), int(mt.group(1)))
        if not mt.group(3) and d < today:
            d = d.replace(year=d.year + 1)
    except ValueError:
        await m.answer("Не понял дату. Пример: <code>15.10</code>")
        return
    await state.clear()
    await show_plans(m, m.from_user.id, d.isoformat())


# ═════════════════════════ команды и меню ═════════════════════════

async def need_onboarding(m: Message) -> bool:
    u = await get_user(m.from_user.id)
    if not u or not u["onboarded"]:
        await m.answer("Сначала давай познакомимся — нажми /start")
        return True
    return False


@router.message(Command("brief"))
@router.message(F.text == "📋 Сводка")
async def cmd_brief(m: Message):
    if await need_onboarding(m):
        return
    u = await get_user(m.from_user.id)
    await send_long(m.bot, m.chat.id, await build_brief(u, local_now(u).date()))


@router.message(Command("plans"))
@router.message(F.text == "📝 Планы")
async def cmd_plans(m: Message):
    if await need_onboarding(m):
        return
    await show_plans(m, m.from_user.id)


@router.message(Command("schedule"))
@router.message(F.text == "📅 Расписание")
async def cmd_schedule(m: Message):
    if await need_onboarding(m):
        return
    await show_schedule(m)


@router.message(Command("settings"))
@router.message(F.text == "⚙️ Настройки")
async def cmd_settings(m: Message):
    if await need_onboarding(m):
        return
    await show_settings(m)


@router.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer("Я присылаю утреннюю сводку и напоминания.\n\n"
                   "/brief — сводка сейчас\n/plans — планы на утро/день/вечер\n"
                   "/schedule — расписание работы и пар\n/settings — город, подъём, дорога, сон\n"
                   "/cancel — отменить ввод\n/start — настроить заново (если ещё не настроено)")


@router.message()
async def fallback(m: Message):
    if await need_onboarding(m):
        return
    await m.answer("Не понял 🤔 Пользуйся меню внизу или командой /help.", reply_markup=MENU)


# ═════════════════════════ планировщик (утро, выход, конец работы, сон) ═════════════════════════

async def process_user(bot: Bot, u: dict):
    uid = u["user_id"]
    now = local_now(u)
    today, wd = now.date(), now.weekday()
    ds = today.isoformat()
    nowm = now.hour * 60 + now.minute
    wake = await wake_for(u, wd)

    # утренняя сводка
    if wake <= nowm < wake + 180 and not await was_sent(uid, f"brief:{ds}"):
        await send_long(bot, uid, await build_brief(u, today))
        await set_sent(uid, f"brief:{ds}")

    if not u["reminders"]:
        return

    async def once(key: str, text: str):
        if not await was_sent(uid, key):
            await safe_send(bot, uid, text)
            await set_sent(uid, key)

    sessions = P.build_sessions(await get_items(u, wd))
    trips = P.build_trips(sessions, commute_of(u))

    # пора выходить
    for t in trips:
        if t.to != "home" and t.leave <= nowm < t.leave + 15:
            await once(f"leave:{ds}:{t.leave}",
                       f"🚪 <b>Пора выходить {P.FROM_RU[t.frm]} {P.TO_RU[t.to]}!</b>\n"
                       f"Дорога ~{t.travel} мин, начало в {P.fmt(t.target)}.")

    # конец рабочего блока / рабочего дня
    for i, s in enumerate(sessions):
        if s.kind != "work" or not (s.end <= nowm < s.end + 15):
            continue
        if i == len(sessions) - 1:
            evening = await q("SELECT text FROM plans WHERE user_id=? AND date=? AND part='evening' AND done=0 ORDER BY id",
                              (uid, ds), many=True)
            text = "🏁 <b>Рабочий день закончен</b> — пора заканчивать работу и отдыхать 🙌"
            if evening:
                text += "\n\n🌆 Планы на вечер:\n" + "\n".join(f"• {esc(r['text'])}" for r in evening)
        else:
            nxt = trips[i + 1]
            text = (f"⏱ <b>Рабочий блок закончен.</b> Дальше: {P.TO_RU[nxt.to]} — "
                    f"выходить в {P.fmt(nxt.leave)}, начало в {P.fmt(nxt.target)}.")
        await once(f"workend:{ds}:{s.end}", text)

    # пора ложиться
    sl = int(round(u["sleep_h"] * 60))
    wake_next = await wake_for(u, (wd + 1) % 7)
    cands = []
    if wake_next - sl < 0:
        cands.append((wake_next - sl + 1440, wake_next))
    if wake - sl >= 0:
        cands.append((wake - sl, wake))
    for bed, w in cands:
        if bed <= nowm < bed + 30:
            await once(f"bed:{ds}:{bed}",
                       f"🌙 <b>Пора ложиться спать!</b> Подъём в {P.fmt(w)} — так выспишься ({u['sleep_h']:g} ч).")


_last_cleanup: date | None = None


async def tick(bot: Bot):
    global _last_cleanup
    if _last_cleanup != date.today():
        await q("DELETE FROM sent WHERE ts < datetime('now', '-3 days')")
        _last_cleanup = date.today()
    for u in await q("SELECT * FROM users WHERE onboarded=1", many=True):
        try:
            await process_user(bot, u)
        except Exception:
            log.exception("scheduler error for user %s", u["user_id"])


async def scheduler(bot: Bot):
    while True:
        try:
            await tick(bot)
        except Exception:
            log.exception("tick failed")
        await asyncio.sleep(30)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Не задан BOT_TOKEN. Получи токен у @BotFather и запусти: BOT_TOKEN=... python bot.py")
    await init_db()
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="brief", description="Сводка на сегодня"),
        BotCommand(command="plans", description="Планы на утро/день/вечер"),
        BotCommand(command="schedule", description="Расписание работы и пар"),
        BotCommand(command="settings", description="Настройки"),
        BotCommand(command="help", description="Помощь"),
    ])
    task = asyncio.create_task(scheduler(bot))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
