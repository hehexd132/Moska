#!/usr/bin/env python3
"""
Moska - Tuomaksen ja Sannin arjen assistentti v2
"""

import os
import logging
import random
import sqlite3
import re
from datetime import datetime, timedelta
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TUOMAS_USERNAME = os.environ.get("TUOMAS_USERNAME", "tuomas").lower()
SANNI_USERNAME = os.environ.get("SANNI_USERNAME", "sanni").lower()

TIMEZONE = pytz.timezone("Europe/Helsinki")
DB_PATH = "moska.db"

# Conversation states
WAITING_MEAL_SELECTION = 1
WAITING_EXTRA_ITEMS = 2
WAITING_NEW_RECIPE_NAME = 3
WAITING_NEW_RECIPE_INGREDIENTS = 4
WAITING_DAILY_QUESTION_ANSWER = 5

# --- Siivoustehtävät ---
DAILY_TASKS = [
    "Tyhjää/korjaa tiskit",
    "Pyyhi pinnat",
    "Pikaimurointi",
    "Keräile tavarat paikoilleen",
]
WEEKLY_TASKS = [
    "Imurointi",
    "Pyyhkeiden vaihto",
    "Kukkien kastelu",
    "WC ja kylppärin pesu",
    "Pölyt ja jääkaapin pyyhkäisy",
]
MONTHLY_TASKS = [
    "Syvempi imurointi",
    "Pyyhkeet tarkemmin",
    "Kukat ja kukkaruukut",
    "WC ja kylppäri tarkemmin",
    "Pölyt ja jääkaappi tarkemmin",
    "Lattioiden pesu",
]

# --- Päivän kysymykset ---
DAILY_QUESTIONS = [
    "Mikä oli tänään parasta? 😊",
    "Mikä teki sinut iloiseksi tänään? 🌟",
    "Mitä hyvää tapahtui tänään? ✨",
    "Mistä asiasta olet tänään kiitollinen? 🙏",
    "Mikä yllätti sinut positiivisesti tänään? 🎉",
    "Mikä oli päivän kohokohta? ⭐",
    "Mitä uutta opit tai koit tänään? 💡",
]

# --- Reseptit ---
DEFAULT_RECIPES = {
    "Tavallinen kana ja riisi kermassa": ["Kanasuikale", "Ruokakerma", "Riisi"],
    "Siivet ja ranskalaiset": ["Mummon ranskalaiset", "Siivet"],
    "Kanakotzone Pizza": ["Tortillalätty", "Maustettu kanafile", "Ananas", "Aurajuusto", "Juustoraaste", "Jäävuorisalaatti", "Dippiainekset"],
    "Munarulla": ["Kananmuna", "Juustoraaste", "Vuolukana", "Tuorejuusto", "Raejuusto"],
    "Kanafile ja perunat": ["Kanafile (4-6 pihviä)", "Ruokakerma", "Perunat"],
    "Pestopasta kanalla": ["Kanafile", "GF pasta", "Pesto"],
    "Broilerikeitto": ["Peruna", "Porkkana", "Paprika", "Kanasuikale", "Sipuli", "Ruokakerma"],
    "Venetortillat": ["Venetortilla", "Kurkku", "Jäävuorisalaatti", "Viinirypäle", "Punasipuli", "Salsa", "Kermaviili", "Dippimauste"],
    "Jauheliharisotto": ["Riisi", "Jauheliha", "Paprika", "Porkkana", "Soija", "Tomaattipyre"],
    "Possuvartaat ja perunaa": ["Possuvartaat", "Peruna", "Chilisalaatti"],
    "Bigmac Bowli": ["Jauheliha", "Peruna", "Sipuli", "Jäävuorisalaatti", "Kurkku", "Suolakurkku", "Majoneesi", "Kreikkalainen jugurtti", "Sinappi"],
    "Makaroni ja jauhelihakastike": ["Jauheliha", "Makaroni", "Tacomauste"],
    "Pihvit ja peruna": ["Pihvit", "Peruna", "Crème fraîche"],
    "Lihapullat ja peruna": ["Lihapullat", "Ruskea kastike", "Ruokakerma", "Peruna"],
    "Tonnikalapasta": ["GF pasta", "Tonnikala", "Ruokakerma", "Tomaattipyre", "Sipuli"],
    "Ranch-kanafile salaatti": ["Kurkku", "Jäävuorisalaatti", "Viinirypäle", "Punasipuli", "Ranch kanafile"],
}

# ============================================================
# TIETOKANTA
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        person TEXT NOT NULL, description TEXT NOT NULL,
        event_date TEXT NOT NULL, event_time TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")

    c.execute("""CREATE TABLE IF NOT EXISTS chores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL, chore_type TEXT NOT NULL,
        task TEXT NOT NULL, assigned_to TEXT NOT NULL,
        done INTEGER DEFAULT 0, done_by TEXT, done_at TEXT)""")

    c.execute("""CREATE TABLE IF NOT EXISTS recipes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE, ingredients TEXT NOT NULL)""")

    c.execute("""CREATE TABLE IF NOT EXISTS daily_answers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL, person TEXT NOT NULL,
        question TEXT NOT NULL, answer TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")

    c.execute("""CREATE TABLE IF NOT EXISTS birthdays (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, birth_date TEXT NOT NULL,
        added_by TEXT)""")

    c.execute("""CREATE TABLE IF NOT EXISTS workouts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL, person TEXT NOT NULL,
        workout_type TEXT NOT NULL, details TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")

    conn.commit()

    for name, ingredients in DEFAULT_RECIPES.items():
        c.execute("INSERT OR IGNORE INTO recipes (name, ingredients) VALUES (?, ?)",
                  (name, ",".join(ingredients)))
    conn.commit()
    conn.close()


def get_db():
    return sqlite3.connect(DB_PATH)


# ============================================================
# APUFUNKTIOT
# ============================================================

def identify_user(update: Update) -> str:
    username = (update.effective_user.username or "").lower()
    first_name = (update.effective_user.first_name or "").lower()
    if username == TUOMAS_USERNAME or first_name == "tuomas":
        return "Tuomas"
    elif username == SANNI_USERNAME or first_name == "sanni":
        return "Sanni"
    return update.effective_user.first_name or "Tuntematon"


def parse_date_from_text(text: str) -> str:
    now = datetime.now(TIMEZONE)
    text_lower = text.lower()
    weekdays_fi = {
        "maanantaina": 0, "tiistaina": 1, "keskiviikkona": 2,
        "torstaina": 3, "perjantaina": 4, "lauantaina": 5, "sunnuntaina": 6,
        "maanantai": 0, "tiistai": 1, "keskiviikko": 2,
        "torstai": 3, "perjantai": 4, "lauantai": 5, "sunnuntai": 6,
    }
    for word, day_num in weekdays_fi.items():
        if word in text_lower:
            days_ahead = (day_num - now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            return (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    if "tänään" in text_lower:
        return now.strftime("%Y-%m-%d")
    if "huomenna" in text_lower:
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")
    return now.strftime("%Y-%m-%d")


def parse_time_from_text(text: str) -> Optional[str]:
    match = re.search(r"(\d{1,2})[.:](\d{2})(?:\s*-\s*\d{1,2}[.:]\d{2})?", text)
    if match:
        return f"{match.group(1)}:{match.group(2)}"
    return None


def get_recipes_list() -> list:
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, name, ingredients FROM recipes ORDER BY id")
    rows = c.fetchall()
    conn.close()
    return rows


def assign_chores_for_day(date_str: str, chore_type: str, tasks: list):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM chores WHERE date=? AND chore_type=?", (date_str, chore_type))
    if c.fetchone()[0] > 0:
        conn.close()
        return
    shuffled = tasks.copy()
    random.shuffle(shuffled)
    mid = len(shuffled) // 2
    for i, task in enumerate(shuffled):
        person = "Tuomas" if i < mid else "Sanni"
        if len(shuffled) % 2 == 1 and i == len(shuffled) - 1:
            c.execute("SELECT COUNT(*) FROM chores WHERE date=? AND chore_type=? AND assigned_to='Tuomas'", (date_str, chore_type))
            t = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM chores WHERE date=? AND chore_type=? AND assigned_to='Sanni'", (date_str, chore_type))
            s = c.fetchone()[0]
            person = "Tuomas" if t <= s else "Sanni"
        c.execute("INSERT INTO chores (date, chore_type, task, assigned_to) VALUES (?, ?, ?, ?)",
                  (date_str, chore_type, task, person))
    conn.commit()
    conn.close()


def get_chores_message(date_str: str) -> str:
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT task, assigned_to, done FROM chores WHERE date=? ORDER BY chore_type, assigned_to", (date_str,))
    rows = c.fetchall()
    conn.close()
    if not rows:
        return "Ei tehtäviä tälle päivälle."
    tuomas = [(r[0], r[2]) for r in rows if r[1] == "Tuomas"]
    sanni = [(r[0], r[2]) for r in rows if r[1] == "Sanni"]
    msg = f"🧹 *Päivän askareet {date_str}*\n\n👦 *Tuomas:*\n"
    for task, done in tuomas:
        msg += f"  {'✅' if done else '⬜'} {task}\n"
    msg += "\n👧 *Sanni:*\n"
    for task, done in sanni:
        msg += f"  {'✅' if done else '⬜'} {task}\n"
    return msg


def get_monthly_stats(year: int, month: int) -> str:
    conn = get_db()
    c = conn.cursor()
    month_str = f"{year}-{month:02d}"
    c.execute("SELECT assigned_to, COUNT(*), SUM(done) FROM chores WHERE date LIKE ? GROUP BY assigned_to",
              (f"{month_str}%",))
    rows = c.fetchall()
    conn.close()
    if not rows:
        return "Ei dataa tälle kuukaudelle."
    stats = {}
    for person, total, done in rows:
        pct = round((done / total * 100) if total > 0 else 0)
        stats[person] = (total, int(done or 0), pct)
    msg = f"📊 *Kuukauden siivoustulokset {month_str}*\n\n"
    winner = max(stats, key=lambda p: stats[p][2]) if stats else None
    for person, (total, done, pct) in stats.items():
        emoji = "👦" if person == "Tuomas" else "👧"
        msg += f"{emoji} *{person}:* {done}/{total} = *{pct}%*\n"
    if winner:
        loser = "Sanni" if winner == "Tuomas" else "Tuomas"
        msg += f"\n🏆 *{winner} voitti!*\n😅 {loser} tarjoaa lounaan {winner}:n valitsemassa ravintolassa!"
    return msg


def get_workout_summary(person: str) -> str:
    now = datetime.now(TIMEZONE)
    week_start = now - timedelta(days=now.weekday())
    week_end = week_start + timedelta(days=6)
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT date, workout_type, details FROM workouts
                 WHERE person=? AND date >= ? AND date <= ? ORDER BY date""",
              (person, week_start.strftime("%Y-%m-%d"), week_end.strftime("%Y-%m-%d")))
    rows = c.fetchall()
    conn.close()
    emoji = "👦" if person == "Tuomas" else "👧"
    msg = f"{emoji} *{person} — Tämä viikko*\n"
    if not rows:
        return msg + "Ei treenejä kirjattu.\n"
    for date, wtype, details in rows:
        dt = datetime.strptime(date, "%Y-%m-%d")
        day = ["Ma","Ti","Ke","To","Pe","La","Su"][dt.weekday()]
        msg += f"  🏋️ {day}: {wtype}"
        if details:
            msg += f" — {details}"
        msg += "\n"
    msg += f"  *Yhteensä: {len(rows)} treeniä*\n"
    return msg


def whose_turn_for_question() -> str:
    now = datetime.now(TIMEZONE)
    day_of_year = now.timetuple().tm_yday
    return "Tuomas" if day_of_year % 2 == 0 else "Sanni"


# ============================================================
# TELEGRAM HANDLERS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Moi! Olen *Moska*, teidän perheen assistentti!\n\nKirjoita *Help!* nähdäksesi kaikki komennot.",
        parse_mode="Markdown"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 *Moska — Komennot*\n\n"
        "📅 *Menot*\n"
        "`Tuomaksella tennistunti torstaina 18.30` — lisää meno\n"
        "`Menot` — tämän viikon menot\n"
        "`Menot ensi viikko` — ensi viikon menot\n\n"
        "🧹 *Siivous*\n"
        "`Askareet` — tämän päivän tehtävät\n"
        "`Tehty` — merkitsee omat tehtäväsi tehdyiksi\n"
        "`Tulokset` — kuukauden siivouspisteet\n\n"
        "🏋️ *Treeni*\n"
        "`Treeni: juoksu 5km` — kirjaa treeni\n"
        "`Treeni: saliharjoitus 1h` — kirjaa treeni\n"
        "`Treenit` — viikon treenisummary\n\n"
        "🎂 *Syntymäpäivät*\n"
        "`Muista: Äidin synttärit 15.8` — lisää muistutus\n"
        "`Synttärit` — listaa kaikki\n\n"
        "🛒 *Ruokalista*\n"
        "`Ruokalista` — aloittaa ostoslistan\n"
        "`Uusi resepti` — lisää resepti\n"
        "`Reseptit` — kaikki reseptit\n\n"
        "`Help!` — tämä ohje\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    text_lower = text.lower()
    user = identify_user(update)

    state = context.user_data.get("state")

    if state == WAITING_MEAL_SELECTION:
        await handle_meal_selection(update, context, text)
        return
    elif state == WAITING_EXTRA_ITEMS:
        await handle_extra_items(update, context, text)
        return
    elif state == WAITING_NEW_RECIPE_NAME:
        context.user_data["new_recipe_name"] = text
        context.user_data["state"] = WAITING_NEW_RECIPE_INGREDIENTS
        await update.message.reply_text(
            f"👍 Nimi: *{text}*\n\nListaa ainesosat pilkulla eroteltuna:",
            parse_mode="Markdown"
        )
        return
    elif state == WAITING_NEW_RECIPE_INGREDIENTS:
        await save_new_recipe(update, context, text)
        return
    elif state == WAITING_DAILY_QUESTION_ANSWER:
        await save_daily_answer(update, context, text, user)
        return

    if text_lower in ["help!", "help", "apua", "komennot"]:
        await help_command(update, context)
        return

    if text_lower == "ruokalista":
        await start_shopping(update, context)
        return

    if text_lower == "uusi resepti":
        context.user_data["state"] = WAITING_NEW_RECIPE_NAME
        await update.message.reply_text("🍽️ Mikä on uuden reseptin nimi?")
        return

    if text_lower == "reseptit":
        await list_recipes(update, context)
        return

    if text_lower in ["menot", "viikon menot"]:
        await show_events(update, context, this_week=True)
        return

    if "ensi viikko" in text_lower and "menot" in text_lower:
        await show_events(update, context, this_week=False)
        return

    if text_lower in ["askareet", "tehtävät"]:
        await show_todays_chores(update, context)
        return

    if text_lower == "tehty":
        await mark_done(update, context, user)
        return

    if text_lower in ["tulokset", "pisteet"]:
        now = datetime.now(TIMEZONE)
        await update.message.reply_text(get_monthly_stats(now.year, now.month), parse_mode="Markdown")
        return

    if text_lower in ["treenit", "treeniviikko"]:
        msg = "🏋️ *Viikon treenit*\n\n"
        msg += get_workout_summary("Tuomas")
        msg += "\n"
        msg += get_workout_summary("Sanni")
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    if text_lower in ["synttärit", "syntymäpäivät"]:
        await list_birthdays(update, context)
        return

    # --- Treeni-kirjaus: "Treeni: juoksu 5km" ---
    if text_lower.startswith("treeni:"):
        details = text[7:].strip()
        parts = details.split(" ", 1)
        workout_type = parts[0] if parts else details
        extra = parts[1] if len(parts) > 1 else ""
        now = datetime.now(TIMEZONE)
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO workouts (date, person, workout_type, details) VALUES (?, ?, ?, ?)",
                  (now.strftime("%Y-%m-%d"), user, workout_type, extra))
        conn.commit()
        conn.close()
        await update.message.reply_text(
            f"🏋️ Treeni kirjattu! *{user}*: {details} 💪",
            parse_mode="Markdown"
        )
        return

    # --- Syntymäpäivä-muistutus: "Muista: Äidin synttärit 15.8" ---
    birthday_match = re.match(r"muista:\s*(.+?)\s+(\d{1,2})\.(\d{1,2})\.?$", text_lower)
    if birthday_match:
        name = birthday_match.group(1).strip()
        day = int(birthday_match.group(2))
        month = int(birthday_match.group(3))
        birth_date = f"{month:02d}-{day:02d}"
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO birthdays (name, birth_date, added_by) VALUES (?, ?, ?)",
                  (name, birth_date, user))
        conn.commit()
        conn.close()
        await update.message.reply_text(
            f"🎂 Muistiinpantu! *{name.capitalize()}* — {day}.{month}.\nMoska muistuttaa 3 päivää ennen ja tapahtumapäivänä! 🎉",
            parse_mode="Markdown"
        )
        return

    # --- Meno-lisäys luonnollisella kielellä ---
    keywords = ["tunti", "aika", "tapaaminen", "kokous", "lääkäri", "meno:",
                "maanantaina","tiistaina","keskiviikkona","torstaina",
                "perjantaina","lauantaina","sunnuntaina","tänään","huomenna"]
    person_mentioned = None
    if any(w in text_lower for w in ["tuomaksella","tuomaksen","tupella"]):
        person_mentioned = "Tuomas"
    elif any(w in text_lower for w in ["sannilla","sannin"]):
        person_mentioned = "Sanni"

    if person_mentioned and any(k in text_lower for k in keywords):
        event_date = parse_date_from_text(text)
        event_time = parse_time_from_text(text)
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO events (person, description, event_date, event_time) VALUES (?, ?, ?, ?)",
                  (person_mentioned, text, event_date, event_time))
        conn.commit()
        conn.close()
        time_str = f" klo {event_time}" if event_time else ""
        await update.message.reply_text(
            f"✅ Muistiinpantu! *{person_mentioned}*: {text}\n📅 {event_date}{time_str}",
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text(
        "🤔 En ymmärtänyt komentoa. Kirjoita *Help!* nähdäksesi ohjeet.",
        parse_mode="Markdown"
    )


# ============================================================
# MENOT
# ============================================================

async def show_events(update: Update, context: ContextTypes.DEFAULT_TYPE, this_week: bool = True):
    now = datetime.now(TIMEZONE)
    if this_week:
        week_start = now - timedelta(days=now.weekday())
        week_end = week_start + timedelta(days=6)
        label = "tämän viikon"
    else:
        week_start = now + timedelta(days=7 - now.weekday())
        week_end = week_start + timedelta(days=6)
        label = "ensi viikon"

    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT person, description, event_date, event_time FROM events
                 WHERE event_date >= ? AND event_date <= ?
                 ORDER BY event_date, event_time, person""",
              (week_start.strftime("%Y-%m-%d"), week_end.strftime("%Y-%m-%d")))
    rows = c.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text(f"📅 Ei menoja {label}.")
        return

    msg = f"📅 *Menot — {label}*\n\n"
    current_date = None
    weekday_fi = ["Ma","Ti","Ke","To","Pe","La","Su"]

    for person, desc, date, t in rows:
        if date != current_date:
            dt = datetime.strptime(date, "%Y-%m-%d")
            msg += f"\n*{weekday_fi[dt.weekday()]} {dt.strftime('%d.%m')}*\n"
            current_date = date
        time_str = f" 🕐 {t}" if t else ""
        emoji = "👦" if person == "Tuomas" else "👧"
        msg += f"  {emoji} {person}{time_str}: {desc}\n"

    await update.message.reply_text(msg, parse_mode="Markdown")


# ============================================================
# SIIVOUS
# ============================================================

async def show_todays_chores(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TIMEZONE)
    date_str = now.strftime("%Y-%m-%d")
    assign_chores_for_day(date_str, "daily", DAILY_TASKS)
    if now.weekday() == 0:
        assign_chores_for_day(date_str, "weekly", WEEKLY_TASKS)
    if now.day == 1:
        assign_chores_for_day(date_str, "monthly", MONTHLY_TASKS)
    await update.message.reply_text(get_chores_message(date_str), parse_mode="Markdown")


async def mark_done(update: Update, context: ContextTypes.DEFAULT_TYPE, user: str):
    now = datetime.now(TIMEZONE)
    date_str = now.strftime("%Y-%m-%d")
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE chores SET done=1, done_by=?, done_at=? WHERE date=? AND assigned_to=? AND done=0",
              (user, now.isoformat(), date_str, user))
    updated = c.rowcount
    conn.commit()
    conn.close()
    if updated > 0:
        await update.message.reply_text(f"✅ Hienoa *{user}*! {updated} tehtävää merkitty tehdyksi! 🌟", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"🤔 {user}, ei avoimia tehtäviä tänään tai kaikki jo tehty!", parse_mode="Markdown")


# ============================================================
# RUOKALISTA
# ============================================================

async def start_shopping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    recipes = get_recipes_list()
    msg = "🛒 *Mihin ruokiin haluat ainekset?*\n\n"
    for i, (_, name, _) in enumerate(recipes, 1):
        msg += f"({i}) {name}\n"
    msg += "\nVastaa numeroilla pilkulla eroteltuna, esim: `1,3,4,7`"
    context.user_data["state"] = WAITING_MEAL_SELECTION
    context.user_data["recipes_list"] = recipes
    await update.message.reply_text(msg, parse_mode="Markdown")


async def handle_meal_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    recipes = context.user_data.get("recipes_list") or get_recipes_list()
    try:
        selections = [int(x.strip()) for x in text.split(",")]
    except ValueError:
        await update.message.reply_text("❌ Kirjoita numerot pilkulla eroteltuna, esim: `1,3,7`")
        return

    selected = [recipes[n-1] for n in selections if 1 <= n <= len(recipes)]
    if not selected:
        await update.message.reply_text("❌ En löytänyt valitsemiasi ruokia.")
        return

    msg = "🧾 *Ostoslista:*\n\n"
    for _, name, ingredients_str in selected:
        msg += f"*{name}:*\n"
        for ing in ingredients_str.split(","):
            msg += f"  • {ing.strip()}\n"
        msg += "\n"

    msg += "Haluatko lisätä muita ostoksia? Kirjoita `Lisää` tai `Valmis`."
    context.user_data["state"] = WAITING_EXTRA_ITEMS
    context.user_data["shopping_list"] = msg
    await update.message.reply_text(msg, parse_mode="Markdown")


async def handle_extra_items(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    text_lower = text.lower()
    if text_lower in ["lisää", "lisää:"]:
        await update.message.reply_text("📝 Listaa lisäostokset pilkulla eroteltuna:")
        return
    if text_lower in ["valmis", "ok", "kiitos"]:
        context.user_data["state"] = None
        await update.message.reply_text("✅ Ostoslista valmis! Hyviä ostoksia! 🛍️")
        return
    existing = context.user_data.get("shopping_list", "")
    extra = [i.strip() for i in text.split(",")]
    msg = existing + "\n*Muut ostokset:*\n" + "".join(f"  • {i}\n" for i in extra)
    context.user_data["shopping_list"] = msg
    context.user_data["state"] = None
    await update.message.reply_text(msg + "\n✅ Lista täydennetty! 🛍️", parse_mode="Markdown")


async def list_recipes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    recipes = get_recipes_list()
    msg = "🍽️ *Kaikki reseptit:*\n\n"
    for i, (_, name, _) in enumerate(recipes, 1):
        msg += f"({i}) {name}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def save_new_recipe(update: Update, context: ContextTypes.DEFAULT_TYPE, ingredients_text: str):
    name = context.user_data.get("new_recipe_name", "")
    ingredients = [i.strip() for i in ingredients_text.split(",")]
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO recipes (name, ingredients) VALUES (?, ?)", (name, ",".join(ingredients)))
        conn.commit()
        await update.message.reply_text(
            f"✅ Resepti *{name}* lisätty!\n\n" + "\n".join(f"  • {i}" for i in ingredients),
            parse_mode="Markdown"
        )
    except sqlite3.IntegrityError:
        await update.message.reply_text(f"❌ Resepti *{name}* on jo olemassa.", parse_mode="Markdown")
    finally:
        conn.close()
    context.user_data["state"] = None


# ============================================================
# SYNTYMÄPÄIVÄT
# ============================================================

async def list_birthdays(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT name, birth_date FROM birthdays ORDER BY birth_date")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("🎂 Ei syntymäpäiviä muistissa. Lisää: `Muista: Äidin synttärit 15.8`")
        return
    msg = "🎂 *Syntymäpäivät:*\n\n"
    for name, bd in rows:
        month, day = bd.split("-")
        msg += f"  🎉 {name.capitalize()} — {day}.{month}.\n"
    await update.message.reply_text(msg, parse_mode="Markdown")


# ============================================================
# PÄIVÄN KYSYMYS
# ============================================================

async def save_daily_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, user: str):
    question = context.user_data.get("daily_question", "Mikä oli tänään parasta?")
    now = datetime.now(TIMEZONE)
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO daily_answers (date, person, question, answer) VALUES (?, ?, ?, ?)",
              (now.strftime("%Y-%m-%d"), user, question, text))
    conn.commit()
    conn.close()
    context.user_data["state"] = None
    await update.message.reply_text(
        f"💛 Kiitos *{user}*! Vastauksesi on tallennettu muistiin. 📖",
        parse_mode="Markdown"
    )


# ============================================================
# SCHEDULER
# ============================================================

async def send_daily_chores(bot: Bot):
    now = datetime.now(TIMEZONE)
    date_str = now.strftime("%Y-%m-%d")
    assign_chores_for_day(date_str, "daily", DAILY_TASKS)
    if now.weekday() == 0:
        assign_chores_for_day(date_str, "weekly", WEEKLY_TASKS)
    if now.day == 1:
        assign_chores_for_day(date_str, "monthly", MONTHLY_TASKS)
    msg = "☀️ *Päivän askareet on arvottu!*\n\n" + get_chores_message(date_str)
    if CHAT_ID:
        await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")


async def send_daily_question(bot: Bot, app):
    person = whose_turn_for_question()
    question = random.choice(DAILY_QUESTIONS)
    msg = f"{'👦' if person == 'Tuomas' else '👧'} *{person}* — illan kysymys:\n\n_{question}_\n\nVastaa tähän suoraan! 💬"
    if CHAT_ID:
        await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")


async def send_birthday_reminders(bot: Bot):
    now = datetime.now(TIMEZONE)
    today = now.strftime("%m-%d")
    in_3_days = (now + timedelta(days=3)).strftime("%m-%d")
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT name, birth_date FROM birthdays")
    rows = c.fetchall()
    conn.close()
    for name, bd in rows:
        if bd == today:
            msg = f"🎂🎉 *Tänään on {name.capitalize()}n syntymäpäivä!* 🎉🎂\n\nMuistakaa onnitella! 🥳"
            if CHAT_ID:
                await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
        elif bd == in_3_days:
            month, day = bd.split("-")
            msg = f"🎂 Muistutus! *{name.capitalize()}n* synttärit {day}.{month}. — 3 päivää jäljellä! 🎁"
            if CHAT_ID:
                await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")


async def send_weekly_workout_summary(bot: Bot):
    msg = "🏋️ *Viikon treeniyhteenveto*\n\n"
    msg += get_workout_summary("Tuomas")
    msg += "\n"
    msg += get_workout_summary("Sanni")
    if CHAT_ID:
        await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")


async def send_water_reminder(bot: Bot):
    msgs = [
        "💧 Muistakaa juoda vettä! Tavoite 2 litraa päivässä! 🌊",
        "💧 Hetki juoda lasillinen vettä! 😊",
        "💧 Vesimuistutus! Kehosi kiittää! 🚰",
    ]
    if CHAT_ID:
        await bot.send_message(chat_id=CHAT_ID, text=random.choice(msgs))


async def send_monthly_results(bot: Bot):
    now = datetime.now(TIMEZONE)
    msg = get_monthly_stats(now.year, now.month)
    if CHAT_ID:
        await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")


# ============================================================
# MAIN
# ============================================================

def main():
    if not TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN puuttuu!")
        return

    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)

    # Klo 12.00 — päivän askareet
    scheduler.add_job(lambda: app.create_task(send_daily_chores(app.bot)),
                      trigger="cron", hour=12, minute=0)

    # Klo 08.00 — syntymäpäivämuistutukset
    scheduler.add_job(lambda: app.create_task(send_birthday_reminders(app.bot)),
                      trigger="cron", hour=8, minute=0)

    # Klo 10.00 ja 15.00 — vesijuomismuistutus
    scheduler.add_job(lambda: app.create_task(send_water_reminder(app.bot)),
                      trigger="cron", hour=10, minute=0)
    scheduler.add_job(lambda: app.create_task(send_water_reminder(app.bot)),
                      trigger="cron", hour=15, minute=0)

    # Klo 21.00 — päivän kysymys
    scheduler.add_job(lambda: app.create_task(send_daily_question(app.bot, app)),
                      trigger="cron", hour=21, minute=0)

    # Sunnuntai klo 19.00 — viikon treenisummary
    scheduler.add_job(lambda: app.create_task(send_weekly_workout_summary(app.bot)),
                      trigger="cron", day_of_week="sun", hour=19, minute=0)

    # Kuukauden viimeinen päivä klo 20.00 — siivoustulokset
    scheduler.add_job(lambda: app.create_task(send_monthly_results(app.bot)),
                      trigger="cron", day="last", hour=20, minute=0)

    scheduler.start()
    print("🤖 Moska v2 käynnistyy...")
    app.run_polling()


if __name__ == "__main__":
    main()
