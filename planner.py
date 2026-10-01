"""Чистая логика утреннего бота: время, расписание, дорога, сон, одежда.
Здесь нет Telegram и сети — модуль можно тестировать отдельно."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

DAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
DAYS_FULL = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]

FROM_RU = {"home": "из дома", "work": "с работы", "study": "из колледжа"}
TO_RU = {"home": "домой", "work": "на работу", "study": "в колледж"}
ICON = {"work": "💼", "study": "🎓"}
TITLE = {"work": "Работа", "study": "Учёба"}


# ───────────────────────── время ─────────────────────────

def fmt(m: int) -> str:
    m %= 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def dur(m: int) -> str:
    h, mm = divmod(int(m), 60)
    if h and mm:
        return f"{h} ч {mm} мин"
    if h:
        return f"{h} ч"
    return f"{mm} мин"


def parse_time(text) -> int | None:
    """'7', '07:30', '7.30', '0730' -> минуты от полуночи."""
    m = re.fullmatch(r"\s*(\d{1,2})(?:[:.]?(\d{2}))?\s*", text or "")
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2) or 0)
    if hh > 23 or mm > 59:
        return None
    return hh * 60 + mm


# ───────────────────────── дни недели ─────────────────────────

_PREFIX = [("пн", "пон"), ("вт", "вто"), ("ср", "сре"), ("чт", "чет"),
           ("пт", "пят"), ("сб", "суб"), ("вс", "воск")]


def _day_idx(tok: str) -> int | None:
    tok = tok.lower().strip(".")
    for i, prefixes in enumerate(_PREFIX):
        if any(tok.startswith(p) for p in prefixes):
            return i
    return None


def parse_days(text: str) -> list[int] | None:
    t = (text or "").lower().strip()
    if t in ("каждый день", "ежедневно", "все дни", "всю неделю", "каждый", "все"):
        return list(range(7))
    if t in ("будни", "будние", "по будням", "будние дни"):
        return list(range(5))
    if t in ("выходные", "выходные дни", "по выходным"):
        return [5, 6]
    t = re.sub(r"\s*[-–—]\s*", "-", t)
    out: list[int] = []
    for tok in re.split(r"[,\s]+", t):
        if not tok or tok in ("и", "в", "по"):
            continue
        if "-" in tok:
            a, b = tok.split("-", 1)
            ia, ib = _day_idx(a), _day_idx(b)
            if ia is None or ib is None:
                return None
            out += list(range(ia, ib + 1)) if ia <= ib else list(range(ia, 7)) + list(range(0, ib + 1))
        else:
            i = _day_idx(tok)
            if i is None:
                return None
            out.append(i)
    return sorted(set(out)) or None


_TIME = r"\d{1,2}(?:[:.]\d{2})?"
_LINE = re.compile(
    rf"^\s*(?P<days>[^\d]+?)\s+(?P<s>{_TIME})\s*[-–—]\s*(?P<e>{_TIME})\s*(?P<title>.*?)\s*$"
)


def parse_schedule_text(text: str, kind: str):
    """Строки вида 'пн-пт 09:00-17:00 Название' -> ([(weekday, start, end, title)], [ошибки])."""
    ok, errors = [], []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        safe = html.escape(line)
        m = _LINE.match(line)
        if not m:
            errors.append(f"«{safe}» — не понял формат")
            continue
        days = parse_days(m["days"])
        s, e = parse_time(m["s"]), parse_time(m["e"])
        if days is None:
            errors.append(f"«{safe}» — не понял дни недели")
            continue
        if s is None or e is None:
            errors.append(f"«{safe}» — не понял время")
            continue
        if e <= s:
            errors.append(f"«{safe}» — конец раньше начала")
            continue
        title = m["title"] or ("Работа" if kind == "work" else "")
        for d in days:
            ok.append((d, s, e, title))
    return ok, errors


# ───────────────────────── день: сессии и дорога ─────────────────────────

@dataclass
class Item:
    kind: str          # 'work' | 'study'
    start: int
    end: int
    title: str = ""


@dataclass
class Session:
    kind: str
    start: int
    end: int
    items: list = field(default_factory=list)


@dataclass
class Trip:
    frm: str
    to: str
    leave: int         # когда выходить
    travel: int        # минут в пути (без запаса)
    target: int        # начало занятия (для 'home' — время прибытия домой)
    warn: str = ""


@dataclass
class Commute:
    hw: int = 0        # дом ↔ работа
    hs: int = 0        # дом ↔ колледж
    ws: int = 0        # работа ↔ колледж
    buffer: int = 0    # запас на дорогу


def travel_time(c: Commute, a: str, b: str) -> int:
    if a == b:
        return 0
    k = {a, b}
    if k == {"home", "work"}:
        return c.hw
    if k == {"home", "study"}:
        return c.hs
    return c.ws


def build_sessions(items: list[Item]) -> list[Session]:
    """Подряд идущие элементы одного типа сливаются в одну 'сессию' (один поход в место)."""
    sessions: list[Session] = []
    for it in sorted(items, key=lambda i: (i.start, i.end)):
        if sessions and sessions[-1].kind == it.kind:
            s = sessions[-1]
            s.end = max(s.end, it.end)
            s.items.append(it)
        else:
            sessions.append(Session(it.kind, it.start, it.end, [it]))
    return sessions


def build_trips(sessions: list[Session], c: Commute) -> list[Trip]:
    trips: list[Trip] = []
    place, free = "home", None
    for s in sessions:
        base = travel_time(c, place, s.kind)
        leave = s.start - base - c.buffer
        warn = ""
        if free is not None and leave < free:
            if free + base > s.start:
                gap = s.start - free
                warn = (f"Не успеваешь {TO_RU[s.kind]}: на дорогу нужно {base} мин, "
                        f"а между делами только {max(gap, 0)} мин.")
            leave = free
        trips.append(Trip(place, s.kind, leave, base, s.start, warn))
        place, free = s.kind, s.end
    if sessions:
        base = travel_time(c, place, "home")
        trips.append(Trip(place, "home", free, base, free + base))
    return trips


def _trip_line(t: Trip) -> str:
    if t.to == "home":
        return (f"🏠 {fmt(t.leave)} — выходить {FROM_RU[t.frm]} домой "
                f"(дорога {t.travel} мин, дома в {fmt(t.target)})")
    icon = "🚪" if t.frm == "home" else "🚶"
    return (f"{icon} <b>{fmt(t.leave)}</b> — выходить {FROM_RU[t.frm]} {TO_RU[t.to]} "
            f"(в пути {t.travel} мин, начало в {fmt(t.target)})")


def day_lines(sessions, trips, wake=None, prep=0) -> list[str]:
    lines: list[str] = []
    if wake is not None:
        lines.append(f"⏰ {fmt(wake)} — подъём (на сборы ~{prep} мин)")
    n = 0
    for i, s in enumerate(sessions):
        t = trips[i]
        lines.append(_trip_line(t))
        if t.warn:
            lines.append(f"⚠️ {t.warn}")
        lines.append(f"{ICON[s.kind]} <b>{TITLE[s.kind]} {fmt(s.start)}–{fmt(s.end)}</b>")
        show = (s.kind == "study" or len(s.items) > 1
                or any(it.title not in ("", "Работа") for it in s.items))
        if show:
            prev = None
            for it in s.items:
                if prev is not None:
                    gap = it.start - prev.end
                    if 0 < gap <= 30:
                        lines.append(f"      ☕ перемена {gap} мин")
                    elif gap > 30:
                        lines.append(f"      🕓 окно {dur(gap)}")
                    elif gap < 0:
                        lines.append("      ⚠️ занятия пересекаются")
                if s.kind == "study":
                    n += 1
                    label = f"{n}) "
                else:
                    label = "• "
                title = html.escape(it.title)
                lines.append(f"   {label}{fmt(it.start)}–{fmt(it.end)} {title}".rstrip())
                prev = it
    if sessions:
        lines.append(_trip_line(trips[-1]))
    return lines


# ───────────────────────── сон ─────────────────────────

def bedtime(wake_min: int, sleep_h: float) -> int:
    return (wake_min - int(round(sleep_h * 60))) % 1440


# ───────────────────────── погода и одежда ─────────────────────────

WMO = {
    0: ("ясно", "☀️"), 1: ("в основном ясно", "🌤"), 2: ("переменная облачность", "⛅"),
    3: ("пасмурно", "☁️"), 45: ("туман", "🌫"), 48: ("туман с изморозью", "🌫"),
    51: ("лёгкая морось", "🌦"), 53: ("морось", "🌦"), 55: ("сильная морось", "🌦"),
    56: ("ледяная морось", "🌧"), 57: ("ледяная морось", "🌧"),
    61: ("небольшой дождь", "🌧"), 63: ("дождь", "🌧"), 65: ("сильный дождь", "🌧"),
    66: ("ледяной дождь", "🌧"), 67: ("сильный ледяной дождь", "🌧"),
    71: ("небольшой снег", "🌨"), 73: ("снег", "❄️"), 75: ("сильный снег", "❄️"),
    77: ("снежная крупа", "❄️"), 80: ("ливень", "🌧"), 81: ("сильный ливень", "🌧"),
    82: ("очень сильный ливень", "⛈"), 85: ("снегопад", "❄️"), 86: ("сильный снегопад", "❄️"),
    95: ("гроза", "⛈"), 96: ("гроза с градом", "⛈"), 99: ("сильная гроза с градом", "⛈"),
}
SNOW_CODES = {71, 73, 75, 77, 85, 86}


def wmo(code) -> tuple[str, str]:
    return WMO.get(code, ("", "🌡"))


def clothing_advice(feels_min, feels_max, pop, precip_mm, wind, snow) -> list[str]:
    ref = feels_min * 0.6 + feels_max * 0.4
    if ref <= -15:
        base = "🧥 Очень холодно: тёплый пуховик, термобельё, шапка, шарф, варежки и тёплая обувь."
    elif ref <= -5:
        base = "🧥 Мороз: зимняя куртка, шапка, шарф, перчатки и тёплая обувь."
    elif ref <= 5:
        base = "🧥 Холодно: тёплая куртка или пальто, шапка, шарф; перчатки лучше взять."
    elif ref <= 12:
        base = "🧥 Прохладно: демисезонная куртка, свитер или толстовка, закрытая обувь."
    elif ref <= 18:
        base = "🧥 Умеренно: лёгкая куртка или ветровка, под неё кофта/худи."
    elif ref <= 24:
        base = "👕 Тепло: футболка или рубашка, лёгкие брюки или джинсы; кофту возьми на вечер."
    else:
        base = "👕 Жарко: лёгкая светлая одежда, шорты или тонкие брюки, головной убор."
    tips = [base]
    if snow:
        tips.append("❄️ Будет снег — обувь с нескользящей подошвой и шапка/капюшон.")
    elif pop >= 50 or precip_mm >= 1:
        tips.append("☔ Высокий шанс осадков: зонт, водоотталкивающая куртка, обувь, которая не боится луж.")
    elif pop >= 30:
        tips.append("🌂 Возможен дождь — зонт в рюкзак не помешает.")
    if wind >= 10:
        tips.append("💨 Сильный ветер — нужна ветрозащитная одежда и капюшон.")
    if feels_max - feels_min >= 8:
        tips.append("🧅 Перепад температур большой — одевайся слоями.")
    if ref >= 25:
        tips.append("🧴 Возьми воду и не забудь солнцезащитный крем.")
    return tips
