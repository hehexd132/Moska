#!/usr/bin/env python3
"""
Moska - Tuomaksen ja Sannin arjen assistentti
"""

import os
import logging
from dotenv import load_dotenv
load_dotenv()  # Lataa .env paikallisesti, Railwaylla käyttää automaattisesti ympäristömuuttujia
import random
import sqlite3
from datetime import datetime, timedelta, time
from typing import Optional
from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
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

    c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person TEXT NOT NULL,
            description TEXT NOT NULL,
            event_date TEXT NOT NULL,
            event_time TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS chores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            chore_type TEXT NOT NULL,
            task TEXT NOT NULL,
            assigned_to TEXT NOT NULL,
            done INTEGER DEFAULT 0,
            done_by TEXT,
            done_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS recipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            ingredients TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS shopping_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            selected_meals TEXT,
            extra_items TEXT,
            state TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()

    # Lisää default reseptit jos ei ole
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
    """Tunnistaa kumpi käyttäjä kirjoitti."""
    username = (update.effective_user.username or "").lower()
    first_name = (update.effective_user.first_name or "").lower()

    if username == TUOMAS_USERNAME or first_name == "tuomas":
        return "Tuomas"
    elif username == SANNI_USERNAME or first_name == "sanni":
        return "Sanni"
    else:
        return update.effective_user.first_name or "Tuntematon"


def parse_date_from_text(text: str) -> Optional[str]:
    """Yrittää poimia päivämäärän tai viikonpäivän tekstistä."""
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
            target = now + timedelta(days=days_ahead)
            return target.strftime("%Y-%m-%d")

    if "tänään" in text_lower:
        return now.strftime("%Y-%m-%d")
    if "huomenna" in text_lower:
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")

    return now.strftime("%Y-%m-%d")


def parse_time_from_text(text: str) -> Optional[str]:
    """Poimii kellonajan tekstistä, esim. 18.30 tai 18:30."""
    import re
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
    """Arpoo tehtävät tasaisesti Tuomakselle ja Sannille."""
    conn = get_db()
    c = conn.cursor()

    # Tarkista onko jo arvottu tälle päivälle
    c.execute("SELECT COUNT(*) FROM chores WHERE date=? AND chore_type=?", (date_str, chore_type))
    if c.fetchone()[0] > 0:
        conn.close()
        return

    shuffled = tasks.copy()
    random.shuffle(shuffled)
    mid = len(shuffled) // 2

    for i, task in enumerate(shuffled):
        person = "Tuomas" if i < mid else "Sanni"
        # Jos pariton määrä, viimeinen menee vuorotellen
        if len(shuffled) % 2 == 1 and i == len(shuffled) - 1:
            # Katsotaan kummalla on vähemmän tänään
            c.execute("SELECT COUNT(*) FROM chores WHERE date=? AND chore_type=? AND assigned_to='Tuomas'",
                      (date_str, chore_type))
            tuomas_count = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM chores WHERE date=? AND chore_type=? AND assigned_to='Sanni'",
                      (date_str, chore_type))
            sanni_count = c.fetchone()[0]
            person = "Tuomas" if tuomas_count <= sanni_count else "Sanni"

        c.execute("INSERT INTO chores (date, chore_type, task, assigned_to) VALUES (?, ?, ?, ?)",
                  (date_str, chore_type, task, person))

    conn.commit()
    conn.close()


def get_chores_message(date_str: str) -> str:
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT task, assigned_to, done, done_by
        FROM chores WHERE date=?
        ORDER BY chore_type, assigned_to
    """, (date_str,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        return "Ei tehtäviä tälle päivälle."

    tuomas_tasks = [(r[0], r[2]) for r in rows if r[1] == "Tuomas"]
    sanni_tasks = [(r[0], r[2]) for r in rows if r[1] == "Sanni"]

    msg = f"🧹 *Päivän askareet {date_str}*\n\n"
    msg += "👦 *Tuomas:*\n"
    for task, done in tuomas_tasks:
        emoji = "✅" if done else "⬜"
        msg += f"  {emoji} {task}\n"

    msg += "\n👧 *Sanni:*\n"
    for task, done in sanni_tasks:
        emoji = "✅" if done else "⬜"
        msg += f"  {emoji} {task}\n"

    return msg


def get_monthly_stats(year: int, month: int) -> str:
    conn = get_db()
    c = conn.cursor()

    month_str = f"{year}-{month:02d}"

    for person in ["Tuomas", "Sanni"]:
        c.execute("""
            SELECT COUNT(*) FROM chores
            WHERE date LIKE ? AND assigned_to=?
        """, (f"{month_str}%", person))

    c.execute("SELECT assigned_to, COUNT(*), SUM(done) FROM chores WHERE date LIKE ? GROUP BY assigned_to",
              (f"{month_str}%",))
    rows = c.fetchall()
    conn.close()

    stats = {}
    for person, total, done in rows:
        pct = round((done / total * 100) if total > 0 else 0)
        stats[person] = (total, done, pct)

    if not stats:
        return "Ei dataa tälle kuukaudelle."

    msg = f"📊 *Kuukauden tulokset {month_str}*\n\n"
    winner = None
    best_pct = -1

    for person, (total, done, pct) in stats.items():
        msg += f"{'👦' if person == 'Tuomas' else '👧'} *{person}:* {done}/{total} tehtävää = *{pct}%*\n"
        if pct > best_pct:
            best_pct = pct
            winner = person

    loser = "Sanni" if winner == "Tuomas" else "Tuomas"
    msg += f"\n🏆 *{winner} voitti!*\n"
    msg += f"😅 {loser} tarjoaa lounaan {winner}:n valitsemassa ravintolassa!"

    return msg


# ============================================================
# TELEGRAM HANDLAAJAT
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Moi! Olen *Moska*, teidän perheen assistentti!\n\n"
        "Kirjoita *Help!* nähdäksesi kaikki komennot.",
        parse_mode="Markdown"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 *Moska — Komennot*\n\n"
        "📅 *Menot*\n"
        "`Tuomaksella tennistunti torstaina 18.30` — lisää meno\n"
        "`Sannilla lääkäriaika huomenna 10.00` — lisää meno\n"
        "`Menot` — tämän viikon menot\n"
        "`Menot ensi viikko` — ensi viikon menot\n\n"
        "🧹 *Siivous*\n"
        "`Askareet` — tämän päivän tehtävät\n"
        "`Tehty` — merkitsee omat tehtäväsi tehdyiksi\n"
        "`Tulokset` — kuukauden siivouspisteet\n\n"
        "🛒 *Ruokalista*\n"
        "`Ruokalista` — aloittaa ostoslistan teon\n"
        "`Uusi resepti` — lisää uusi resepti Moskalle\n"
        "`Reseptit` — listaa kaikki reseptit\n\n"
        "ℹ️ *Muut*\n"
        "`Help!` — tämä ohje\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pääviestinkäsittelijä."""
    text = update.message.text.strip()
    text_lower = text.lower()
    user = identify_user(update)

    # --- Ruokalista flow ---
    state = context.user_data.get("state")

    if state == WAITING_MEAL_SELECTION:
        await handle_meal_selection(update, context, text)
        return
    elif state == WAITING_EXTRA_ITEMS:
        await handle_extra_items(update, context, text, user)
        return
    elif state == WAITING_NEW_RECIPE_NAME:
        context.user_data["new_recipe_name"] = text
        context.user_data["state"] = WAITING_NEW_RECIPE_INGREDIENTS
        await update.message.reply_text(
            f"👍 Reseptin nimi: *{text}*\n\nListaa nyt ainesosat pilkulla eroteltuna:\n_esim: Kanafile, Riisi, Ruokakerma_",
            parse_mode="Markdown"
        )
        return
    elif state == WAITING_NEW_RECIPE_INGREDIENTS:
        await save_new_recipe(update, context, text)
        return

    # --- Komennot ---
    if text_lower in ["help!", "help", "apua", "komennot"]:
        await help_command(update, context)
        return

    if text_lower == "ruokalista":
        await start_shopping(update, context)
        return

    if text_lower == "uusi resepti":
        context.user_data["state"] = WAITING_NEW_RECIPE_NAME
        await update.message.reply_text(
            "🍽️ Mikä on uuden reseptin nimi?",
            parse_mode="Markdown"
        )
        return

    if text_lower == "reseptit":
        await list_recipes(update, context)
        return

    if text_lower in ["menot", "mitä menoja", "viikon menot"]:
        await show_events(update, context, this_week=True)
        return

    if "ensi viikko" in text_lower and "menot" in text_lower:
        await show_events(update, context, this_week=False)
        return

    if text_lower in ["askareet", "tehtävät", "päivän askareet"]:
        await show_todays_chores(update, context)
        return

    if text_lower == "tehty":
        await mark_done(update, context, user)
        return

    if text_lower in ["tulokset", "pisteet", "kuukauden tulokset"]:
        now = datetime.now(TIMEZONE)
        msg = get_monthly_stats(now.year, now.month)
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    # --- Meno-lisäys (luonnollinen kieli) ---
    keywords = ["meno:", "tunti", "aika", "tapaaminen", "kokous", "lääkäri",
                "maanantaina", "tiistaina", "keskiviikkona", "torstaina",
                "perjantaina", "lauantaina", "sunnuntaina", "tänään", "huomenna"]

    person_mentioned = None
    if "tuomaksella" in text_lower or "tuomaksen" in text_lower or "tupella" in text_lower:
        person_mentioned = "Tuomas"
    elif "sannilla" in text_lower or "sannin" in text_lower:
        person_mentioned = "Sanni"

    if person_mentioned and any(k in text_lower for k in keywords):
        event_date = parse_date_from_text(text)
        event_time = parse_time_from_text(text)

        conn = get_db()
        c = conn.cursor()
        c.execute(
            "INSERT INTO events (person, description, event_date, event_time) VALUES (?, ?, ?, ?)",
            (person_mentioned, text, event_date, event_time)
        )
        conn.commit()
        conn.close()

        time_str = f" klo {event_time}" if event_time else ""
        await update.message.reply_text(
            f"✅ Muistiinpantu! *{person_mentioned}*: {text}\n📅 {event_date}{time_str}",
            parse_mode="Markdown"
        )
        return

    # Jos ei tunnisteta
    await update.message.reply_text(
        "🤔 En ymmärtänyt komentoa. Kirjoita *Help!* nähdäksesi ohjeet.",
        parse_mode="Markdown"
    )


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
    c.execute("""
        SELECT person, description, event_date, event_time
        FROM events
        WHERE event_date >= ? AND event_date <= ?
        ORDER BY event_date, event_time, person
    """, (week_start.strftime("%Y-%m-%d"), week_end.strftime("%Y-%m-%d")))
    rows = c.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text(f"📅 Ei menoja {label}.")
        return

    msg = f"📅 *Menot — {label}*\n\n"
    current_date = None
    weekday_fi = ["Ma", "Ti", "Ke", "To", "Pe", "La", "Su"]

    for person, desc, date, t in rows:
        if date != current_date:
            dt = datetime.strptime(date, "%Y-%m-%d")
            day_name = weekday_fi[dt.weekday()]
            msg += f"\n*{day_name} {dt.strftime('%d.%m')}*\n"
            current_date = date
        time_str = f" 🕐 {t}" if t else ""
        emoji = "👦" if person == "Tuomas" else "👧"
        msg += f"  {emoji} {person}{time_str}: {desc}\n"

    await update.message.reply_text(msg, parse_mode="Markdown")


async def show_todays_chores(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TIMEZONE)
    date_str = now.strftime("%Y-%m-%d")

    # Varmista että tehtävät on arvottu
    assign_chores_for_day(date_str, "daily", DAILY_TASKS)
    if now.weekday() == 0:  # Maanantai = viikoittaiset
        assign_chores_for_day(date_str, "weekly", WEEKLY_TASKS)
    if now.day == 1:  # Kuukauden 1. päivä = kuukausitehtävät
        assign_chores_for_day(date_str, "monthly", MONTHLY_TASKS)

    msg = get_chores_message(date_str)
    await update.message.reply_text(msg, parse_mode="Markdown")


async def mark_done(update: Update, context: ContextTypes.DEFAULT_TYPE, user: str):
    now = datetime.now(TIMEZONE)
    date_str = now.strftime("%Y-%m-%d")

    conn = get_db()
    c = conn.cursor()
    c.execute("""
        UPDATE chores SET done=1, done_by=?, done_at=?
        WHERE date=? AND assigned_to=? AND done=0
    """, (user, now.isoformat(), date_str, user))
    updated = c.rowcount
    conn.commit()
    conn.close()

    if updated > 0:
        await update.message.reply_text(
            f"✅ Hienoa *{user}*! Merkitsin {updated} tehtävääsi tehdyksi! 🌟",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"🤔 {user}, sinulla ei ole avoimia tehtäviä tänään tai ne on jo tehty!",
            parse_mode="Markdown"
        )


# ============================================================
# RUOKALISTA FLOW
# ============================================================

async def start_shopping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    recipes = get_recipes_list()
    msg = "🛒 *Mihin ruokiin haluat ainekset?*\n\n"
    for i, (rid, name, _) in enumerate(recipes, 1):
        msg += f"({i}) {name}\n"
    msg += "\nVastaa numeroilla pilkulla eroteltuna, esim: `1,3,4,7`"

    context.user_data["state"] = WAITING_MEAL_SELECTION
    context.user_data["recipes_list"] = recipes
    await update.message.reply_text(msg, parse_mode="Markdown")


async def handle_meal_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    recipes = context.user_data.get("recipes_list", [])
    if not recipes:
        recipes = get_recipes_list()

    try:
        selections = [int(x.strip()) for x in text.split(",")]
    except ValueError:
        await update.message.reply_text("❌ Kirjoita numerot pilkulla eroteltuna, esim: `1,3,7`")
        return

    selected = []
    for num in selections:
        if 1 <= num <= len(recipes):
            selected.append(recipes[num - 1])

    if not selected:
        await update.message.reply_text("❌ En löytänyt valitsemiasi ruokia. Yritä uudelleen.")
        return

    msg = "🧾 *Ostoslista:*\n\n"
    for _, name, ingredients_str in selected:
        ingredients = ingredients_str.split(",")
        msg += f"*{name}:*\n"
        for ing in ingredients:
            msg += f"  • {ing.strip()}\n"
        msg += "\n"

    msg += "Haluatko lisätä muita ostoksia? Kirjoita `Lisää` tai `Valmis` jos lista on täydellinen."

    context.user_data["state"] = WAITING_EXTRA_ITEMS
    context.user_data["shopping_list"] = msg
    await update.message.reply_text(msg, parse_mode="Markdown")


async def handle_extra_items(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, user: str):
    text_lower = text.lower()

    if text_lower in ["lisää", "lisää:"]:
        await update.message.reply_text(
            "📝 Listaa lisäostokset (esim: maitorahka, appelsiinimehu, leivät):"
        )
        return

    if text_lower in ["valmis", "ok", "kiitos"]:
        context.user_data["state"] = None
        await update.message.reply_text("✅ Ostoslista valmis! Hyviä ostoksia! 🛍️")
        return

    # Lisätään ylimääräiset ostokset
    existing = context.user_data.get("shopping_list", "")
    extra_items = [item.strip() for item in text.split(",")]

    msg = existing + "\n*Muut ostokset:*\n"
    for item in extra_items:
        msg += f"  • {item}\n"

    context.user_data["shopping_list"] = msg
    context.user_data["state"] = None
    await update.message.reply_text(
        msg + "\n✅ Lista täydennettty! Hyviä ostoksia! 🛍️",
        parse_mode="Markdown"
    )


async def list_recipes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    recipes = get_recipes_list()
    msg = "🍽️ *Kaikki reseptit:*\n\n"
    for i, (_, name, ingredients_str) in enumerate(recipes, 1):
        msg += f"({i}) *{name}*\n"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def save_new_recipe(update: Update, context: ContextTypes.DEFAULT_TYPE, ingredients_text: str):
    name = context.user_data.get("new_recipe_name", "")
    ingredients = [i.strip() for i in ingredients_text.split(",")]

    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO recipes (name, ingredients) VALUES (?, ?)",
                  (name, ",".join(ingredients)))
        conn.commit()
        await update.message.reply_text(
            f"✅ Resepti *{name}* lisätty!\n\n"
            f"Ainesosat:\n" + "\n".join(f"  • {i}" for i in ingredients),
            parse_mode="Markdown"
        )
    except sqlite3.IntegrityError:
        await update.message.reply_text(f"❌ Resepti nimellä *{name}* on jo olemassa.", parse_mode="Markdown")
    finally:
        conn.close()

    context.user_data["state"] = None
    context.user_data["new_recipe_name"] = None


# ============================================================
# SCHEDULER — automaattiset ilmoitukset
# ============================================================

async def send_daily_chores(bot: Bot):
    """Lähettää päivän askareet klo 12."""
    now = datetime.now(TIMEZONE)
    date_str = now.strftime("%Y-%m-%d")

    assign_chores_for_day(date_str, "daily", DAILY_TASKS)

    if now.weekday() == 0:
        assign_chores_for_day(date_str, "weekly", WEEKLY_TASKS)
    if now.day == 1:
        assign_chores_for_day(date_str, "monthly", MONTHLY_TASKS)

    msg = "☀️ *Päivän askareet on arvottu!*\n\n"
    msg += get_chores_message(date_str)

    if CHAT_ID:
        await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")


async def send_monthly_results(bot: Bot):
    """Kuukauden viimeisenä päivänä lähettää tulokset."""
    now = datetime.now(TIMEZONE)
    msg = get_monthly_stats(now.year, now.month)
    if CHAT_ID:
        await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")


# ============================================================
# MAIN
# ============================================================

def main():
    if not TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN puuttuu! Aseta ympäristömuuttuja.")
        return

    init_db()

    app = Application.builder().token(TOKEN).build()

    # Handlaajat
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Scheduler
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)

    # Klo 12 joka päivä — päivän askareet
    scheduler.add_job(
        lambda: app.create_task(send_daily_chores(app.bot)),
        trigger="cron",
        hour=12,
        minute=0,
    )

    # Kuukauden viimeisenä päivänä klo 20 — kuukausitulokset
    scheduler.add_job(
        lambda: app.create_task(send_monthly_results(app.bot)),
        trigger="cron",
        day="last",
        hour=20,
        minute=0,
    )

    scheduler.start()

    print("🤖 Moska käynnistyy...")
    app.run_polling()


if __name__ == "__main__":
    main()
