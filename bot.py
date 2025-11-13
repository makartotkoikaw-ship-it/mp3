#!/usr/bin/env python3
"""
Ambot Converter (plain Python version)
- Run directly on Windows RDP / Linux VPS (no Docker required)
- Uses:
    python-telegram-bot==20.6
    yt-dlp
    aiosqlite
    apscheduler
- Set environment variables:
    BOT_TOKEN (required)
    ADMIN_TELEGRAM_ID (optional)
    DB_PATH (optional, default music.db)
    DAILY_LIMIT_PER_USER, COOLDOWN_SECONDS, GLOBAL_CONCURRENCY (optional tuning)
"""

import os
import asyncio
import tempfile
import csv
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List

import aiosqlite
from yt_dlp import YoutubeDL
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    CommandHandler,
    filters,
)

# ---------------- Configuration (env) ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required")

ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID")  # numeric id as string (optional)
DB_PATH = os.getenv("DB_PATH", "music.db")

# Costs mapping
AUDIO_COSTS = {128: 20, 192: 30, 320: 40}
VIDEO_COSTS = {144: 30, 360: 50, 720: 80, 1080: 120}

DAILY_REWARD = int(os.getenv("DAILY_REWARD", "20"))
REGISTER_BONUS = int(os.getenv("REGISTER_BONUS", "500"))

# Rate limiting & queue config (tunable)
DAILY_LIMIT_PER_USER = int(os.getenv("DAILY_LIMIT_PER_USER", "10"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "60"))
GLOBAL_CONCURRENCY = int(os.getenv("GLOBAL_CONCURRENCY", "3"))

# ---------------- Logging ----------------
LOG_PATH = os.getenv("AMBOT_LOG", "ambot.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler()
    ],
)
logger = logging.getLogger("ambot")

# ---------------- Inline keyboards ----------------
type_kb = InlineKeyboardMarkup(
    [[InlineKeyboardButton("AUDIO (mp3)", callback_data="type:audio"),
      InlineKeyboardButton("VIDEO (mp4)", callback_data="type:video")]]
)
audio_quality_kb = InlineKeyboardMarkup(
    [[InlineKeyboardButton("128 kbps", callback_data="audioq:128"),
      InlineKeyboardButton("192 kbps", callback_data="audioq:192")],
     [InlineKeyboardButton("320 kbps", callback_data="audioq:320")]]
)
video_quality_kb = InlineKeyboardMarkup(
    [[InlineKeyboardButton("144p", callback_data="videoq:144"),
      InlineKeyboardButton("360p", callback_data="videoq:360")],
     [InlineKeyboardButton("720p", callback_data="videoq:720"),
      InlineKeyboardButton("1080p", callback_data="videoq:1080")]]
)

# ---------------- In-memory state & concurrency ----------------
user_state: Dict[int, Dict[str, Any]] = {}
global_semaphore = asyncio.Semaphore(GLOBAL_CONCURRENCY)
user_queues: Dict[int, asyncio.Queue] = {}
user_queue_tasks: Dict[int, asyncio.Task] = {}
application_instance = None  # set in main()

# ---------------- Database init & helpers (aiosqlite) ----------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            fullname TEXT,
            coins INTEGER DEFAULT 0,
            music_count INTEGER DEFAULT 0,
            video_count INTEGER DEFAULT 0,
            registered_at TEXT,
            last_daily_date TEXT,
            daily_count INTEGER DEFAULT 0,
            daily_count_date TEXT,
            last_conversion_at TEXT
        );

        CREATE TABLE IF NOT EXISTS conversions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT,
            type TEXT,
            quality INTEGER,
            cost INTEGER,
            created_at TEXT,
            status TEXT DEFAULT 'done',
            refunded INTEGER DEFAULT 0
        );
        """)
        await db.commit()
        # best-effort migrations (ignore failures)
        migrations = [
            "ALTER TABLE users ADD COLUMN daily_count INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN daily_count_date TEXT",
            "ALTER TABLE users ADD COLUMN last_conversion_at TEXT",
            "ALTER TABLE conversions ADD COLUMN status TEXT DEFAULT 'done'",
            "ALTER TABLE conversions ADD COLUMN refunded INTEGER DEFAULT 0",
        ]
        for stmt in migrations:
            try:
                await db.execute(stmt)
                await db.commit()
            except Exception:
                pass

async def create_or_update_user(user_id: int, username: str, fullname: str, coins: int = 0):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id, username, fullname, coins) VALUES(?,?,?,?)",
            (user_id, username, fullname, coins)
        )
        await db.execute("UPDATE users SET username = ?, fullname = ? WHERE user_id = ?", (username, fullname, user_id))
        await db.commit()

async def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None

async def add_coins(user_id: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (amount, user_id))
        await db.commit()

async def set_last_daily(user_id: int, date_str: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_daily_date = ? WHERE user_id = ?", (date_str, user_id))
        await db.commit()

async def deduct_coins(user_id: int, amount: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        await cur.close()
        if not row:
            return False
        current = row[0]
        if current < amount:
            return False
        await db.execute("UPDATE users SET coins = coins - ? WHERE user_id = ?", (amount, user_id))
        await db.commit()
        return True

async def refund_coins(user_id: int, amount: int):
    await add_coins(user_id, amount)

async def increment_counter(user_id: int, typ: str):
    async with aiosqlite.connect(DB_PATH) as db:
        if typ == "audio":
            await db.execute("UPDATE users SET music_count = music_count + 1 WHERE user_id = ?", (user_id,))
        else:
            await db.execute("UPDATE users SET video_count = video_count + 1 WHERE user_id = ?", (user_id,))
        await db.commit()

async def decrement_counter(user_id: int, typ: str):
    async with aiosqlite.connect(DB_PATH) as db:
        if typ == "audio":
            await db.execute("UPDATE users SET music_count = CASE WHEN music_count>0 THEN music_count-1 ELSE 0 END WHERE user_id = ?", (user_id,))
        else:
            await db.execute("UPDATE users SET video_count = CASE WHEN video_count>0 THEN video_count-1 ELSE 0 END WHERE user_id = ?", (user_id,))
        await db.commit()

async def log_conversion(user_id: int, title: str, typ: str, quality: int, cost: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO conversions(user_id, title, type, quality, cost, created_at) VALUES(?,?,?,?,?,?)",
            (user_id, title, typ, quality, cost, datetime.utcnow().isoformat())
        )
        await db.commit()
        return cur.lastrowid

async def mark_conversion_refunded(conv_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE conversions SET status='refunded', refunded=1 WHERE id = ?", (conv_id,))
        await db.commit()

async def mark_conversion_failed(conv_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE conversions SET status='failed' WHERE id = ?", (conv_id,))
        await db.commit()

async def get_user_conversions(user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id, title, type, quality, cost, created_at, status, refunded FROM conversions WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]

async def all_users() -> List[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id, username, coins, music_count, video_count FROM users ORDER BY username")
        rows = await cur.fetchall()
        await cur.close()
        return rows

async def all_conversions_csv(tmp_csv_path: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, user_id, title, type, quality, cost, created_at, status, refunded FROM conversions ORDER BY created_at")
        rows = await cur.fetchall()
        await cur.close()
    with open(tmp_csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["id", "user_id", "title", "type", "quality", "cost", "created_at", "status", "refunded"])
        for r in rows:
            writer.writerow(r)

# ---------------- Rate limit helpers ----------------
async def get_user_rate_info(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT daily_count, daily_count_date, last_conversion_at FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        await cur.close()
        if row:
            return (row["daily_count"] or 0, row["daily_count_date"], row["last_conversion_at"])
        return 0, None, None

async def bump_user_daily_count(user_id: int):
    today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT daily_count, daily_count_date FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if not row:
            await db.execute("INSERT OR IGNORE INTO users(user_id, username, fullname, coins) VALUES(?,?,?,?)", (user_id, "", "", 0))
            await db.commit()
            daily_count = 0
            date_str = None
        else:
            daily_count, date_str = row[0] or 0, row[1]
        if date_str != today:
            await db.execute("UPDATE users SET daily_count = 1, daily_count_date = ? WHERE user_id = ?", (today, user_id))
        else:
            await db.execute("UPDATE users SET daily_count = daily_count + 1 WHERE user_id = ?", (user_id,))
        await db.commit()

async def set_user_last_conversion(user_id: int):
    ts = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_conversion_at = ? WHERE user_id = ?", (ts, user_id))
        await db.commit()

# ---------------- Utilities ----------------
def progress_bar(percent: float) -> str:
    filled = int(percent // 20)
    return " ".join("■" if i < filled else "□" for i in range(5))

def is_admin(user_id: int) -> bool:
    if ADMIN_TELEGRAM_ID and str(user_id) == str(ADMIN_TELEGRAM_ID):
        return True
    return False

# ---------------- Bot flow handlers ----------------
async def handle_start_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    text = (update.message.text or "").strip()

    await create_or_update_user(user_id, user.username or "", user.full_name or "")

    if text.lower() == "register":
        u = await get_user(user_id)
        if u and u.get("registered_at"):
            await update.message.reply_text("You're already registered.")
            return
        async with aiosqlite.connect(DB_PATH) as db:
            now = datetime.utcnow().isoformat()
            await db.execute("UPDATE users SET coins = coins + ?, registered_at = ? WHERE user_id = ?", (REGISTER_BONUS, now, user_id))
            await db.commit()
        await update.message.reply_text(f"Registered! You received {REGISTER_BONUS} coins.")
        return

    if text.lower() == "check":
        u = await get_user(user_id)
        if not u:
            await update.message.reply_text("You are not registered. Type `register` to register.")
            return
        msg = (f"Name : {u['fullname']}\n"
               f"Coins : {u['coins']}\n"
               f"Converted\n"
               f"Music : {u['music_count']}\n"
               f"Video : {u['video_count']}")
        await update.message.reply_text(msg)
        return

    if text.lower().startswith("history"):
        parts = text.split()
        n = 10
        if len(parts) >= 2 and parts[1].isdigit():
            n = int(parts[1])
        convs = await get_user_conversions(user_id, n)
        if not convs:
            await update.message.reply_text("No conversions yet.")
            return
        lines = []
        for c in convs:
            lines.append(f"{c['created_at'][:19]} | {c['type']} | {c['quality']} | {c['cost']}coins | {c['title']} | status:{c['status']}")
        await update.message.reply_text("\n".join(lines))
        return

    if "youtube.com" in text or "youtu.be" in text:
        await update.message.reply_text("I detected a URL — please send a title only, or use the mp3/mp4 commands for direct URL flows.")
        return

    await start_search_flow(update, context)

async def start_search_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    if not text:
        return
    user_state[user_id] = {"title": text, "chat_id": update.effective_chat.id, "from_user_id": user_id}
    try:
        await update.message.delete()
    except Exception:
        pass
    msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Ambot Converter\nTitle: {text}\nSelect type:", reply_markup=type_kb)
    user_state[user_id]["type_msg_id"] = msg.message_id

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    state = user_state.get(user_id, {})
    chat_id = query.message.chat_id

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=query.message.message_id)
    except Exception:
        pass

    if data.startswith("type:"):
        typ = data.split(":", 1)[1]
        state["type"] = typ
        user_state[user_id] = state
        if typ == "audio":
            kb = audio_quality_kb
            text = "Select audio quality:"
        else:
            kb = video_quality_kb
            text = "Select video quality:"
        msg = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
        state["quality_msg_id"] = msg.message_id
        return

    if data.startswith("audioq:") or data.startswith("videoq:"):
        kval = int(data.split(":", 1)[1])
        state["quality"] = kval
        user_state[user_id] = state

        if state.get("type") == "audio":
            cost = AUDIO_COSTS.get(kval, max(AUDIO_COSTS.values()))
        else:
            cost = VIDEO_COSTS.get(kval, max(VIDEO_COSTS.values()))
        state["cost"] = cost

        # rate-limit checks
        daily_count, daily_date, last_conv = await get_user_rate_info(user_id)
        today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        if daily_date != today:
            daily_count = 0
        if daily_count >= DAILY_LIMIT_PER_USER:
            await context.bot.send_message(chat_id=chat_id, text=f"You reached daily limit of {DAILY_LIMIT_PER_USER} conversions. Try again tomorrow.")
            user_state.pop(user_id, None)
            return
        if last_conv:
            try:
                last_ts = datetime.fromisoformat(last_conv)
                elapsed = (datetime.utcnow() - last_ts).total_seconds()
                if elapsed < COOLDOWN_SECONDS:
                    wait = int(COOLDOWN_SECONDS - elapsed)
                    await context.bot.send_message(chat_id=chat_id, text=f"You're converting too frequently. Please wait {wait}s.")
                    user_state.pop(user_id, None)
                    return
            except Exception:
                pass

        ok = await deduct_coins(user_id, cost)
        if not ok:
            await context.bot.send_message(chat_id=chat_id, text=f"Insufficient coins. This conversion costs {cost} coins.")
            user_state.pop(user_id, None)
            return

        await bump_user_daily_count(user_id)
        await set_user_last_conversion(user_id)

        await increment_counter(user_id, state.get("type"))
        conv_id = await log_conversion(user_id, state.get("title", "(unknown)"), state.get("type"), kval, cost)
        state["conversion_id"] = conv_id

        status_msg = await context.bot.send_message(chat_id=chat_id, text=f"Ambot Converter\nTitle: {state.get('title')}\nDuration: —:—\nStatus: {progress_bar(0)}\n(Queued)")
        state["status_msg_id"] = status_msg.message_id
        user_state[user_id] = state

        await enqueue_user_conversion(user_id, {"state": state})
        await context.bot.send_message(chat_id=chat_id, text="Your conversion has been queued. You'll be notified when it starts.")
        return

# ---------------- Queue & worker ----------------
async def enqueue_user_conversion(user_id: int, job_payload: dict):
    if user_id not in user_queues:
        user_queues[user_id] = asyncio.Queue()
    await user_queues[user_id].put(job_payload)
    if user_id not in user_queue_tasks or user_queue_tasks[user_id].done():
        user_queue_tasks[user_id] = asyncio.create_task(user_queue_worker(user_id))

async def user_queue_worker(user_id: int):
    q = user_queues[user_id]
    app = application_instance
    while not q.empty():
        job = await q.get()
        async with global_semaphore:
            st = job.get("state", {})
            chat_id = st.get("chat_id")
            status_msg_id = st.get("status_msg_id")
            # update queued->starting
            try:
                await app.bot.edit_message_text(chat_id=chat_id, message_id=status_msg_id,
                                                text=f"Ambot Converter\nTitle: {st.get('title')}\nDuration: —:—\nStatus: {progress_bar(0)}\n(Starting...)")
            except Exception:
                pass
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, blocking_download_and_process, st, user_id, app)
        q.task_done()
    user_queue_tasks.pop(user_id, None)
    user_queues.pop(user_id, None)

# ---------------- Blocking conversion (executor) ----------------
def blocking_download_and_process(state: Dict[str, Any], user_id: int, app):
    chat_id = state["chat_id"]
    title_query = state["title"]
    typ = state["type"]
    quality = state["quality"]
    cost = state["cost"]
    conv_id = state.get("conversion_id")

    progress = {"percent": 0.0, "status_text": "Starting...", "done": False, "filepath": None, "info": None}

    def ytdl_hook(d):
        status = d.get("status")
        if status == "downloading":
            p = d.get("percent") or 0.0
            progress["percent"] = float(p)
            progress["status_text"] = d.get("eta") and f"ETA {d.get('eta')}" or d.get("status")
        elif status == "finished":
            progress["percent"] = 90.0
            progress["status_text"] = "Post-processing..."
        elif status == "error":
            progress["status_text"] = "Error"

    search_url = f"ytsearch1:{title_query}"
    tempdir = tempfile.TemporaryDirectory()
    cwd = tempdir.name
    opts = {
        "outtmpl": os.path.join(cwd, "%(title)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [ytdl_hook],
    }

    if typ == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(quality),
        }]
    else:
        kval = int(quality)
        opts["format"] = f"bestvideo[height<={kval}]+bestaudio/best"
        opts["merge_output_format"] = "mp4"

    info = None
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(search_url, download=True)
            if "entries" in info and info["entries"]:
                info = info["entries"][0]
            filename = ydl.prepare_filename(info)
            if typ == "audio":
                filename = os.path.splitext(filename)[0] + ".mp3"
            else:
                filename = os.path.splitext(filename)[0] + ".mp4"
            filepath = os.path.join(cwd, filename)
            progress["filepath"] = filepath
            progress["done"] = True
            progress["percent"] = 100.0
            progress["info"] = info
    except Exception as e:
        progress["status_text"] = f"Error: {e}"
        progress["done"] = True
        progress["info"] = None

    loop = asyncio.get_event_loop()
    loop.create_task(async_finalize_and_update(progress, state, user_id, app))

async def async_finalize_and_update(progress: Dict[str, Any], state: Dict[str, Any], user_id: int, app):
    chat_id = state["chat_id"]
    status_msg_id = state.get("status_msg_id")
    title = state.get("title", "(unknown)")
    typ = state.get("type")
    cost = state.get("cost")
    conv_id = state.get("conversion_id")

    last_text = None
    info = progress.get("info")

    while not progress["done"]:
        p = progress.get("percent", 0.0)
        dur = "—:—"
        if info and info.get("duration"):
            seconds = int(info.get("duration"))
            m, s = divmod(seconds, 60)
            dur = f"{m}:{s:02d}"
        text = f"Ambot Converter\nTitle: {title}\nDuration: {dur}\nStatus: {progress_bar(p)}\n({p:.1f}% — {progress.get('status_text')})"
        if text != last_text:
            try:
                await app.bot.edit_message_text(chat_id=chat_id, message_id=status_msg_id, text=text)
            except Exception:
                pass
            last_text = text
        await asyncio.sleep(1.0)

    try:
        if progress.get("filepath") and Path(progress["filepath"]).exists():
            dur = "—:—"
            if info and info.get("duration"):
                seconds = int(info.get("duration"))
                m, s = divmod(seconds, 60)
                dur = f"{m}:{s:02d}"
            text = f"Ambot Converter\nTitle: {title}\nDuration: {dur}\nStatus: {progress_bar(100)}\n(Finished)"
            try:
                await app.bot.edit_message_text(chat_id=chat_id, message_id=status_msg_id, text=text)
            except Exception:
                pass

            send_name = f"{title}{Path(progress['filepath']).suffix}"
            try:
                with open(progress["filepath"], "rb") as fh:
                    await app.bot.send_document(chat_id=chat_id, document=fh, filename=send_name)
                # success -> nothing else
            except Exception as send_err:
                try:
                    await refund_coins(user_id, cost)
                    await decrement_counter(user_id, typ)
                    if conv_id:
                        await mark_conversion_refunded(conv_id)
                    await app.bot.send_message(chat_id=chat_id, text=(
                        "Could not send the file directly (likely too large). "
                        f"Your {cost} coins have been refunded. Try a lower quality."
                    ))
                except Exception:
                    await app.bot.send_message(chat_id=chat_id, text=(
                        "Could not send the file and refund failed. Please contact admin."
                    ))
            try:
                Path(progress["filepath"]).unlink()
            except Exception:
                pass
        else:
            if conv_id:
                await mark_conversion_failed(conv_id)
            try:
                await refund_coins(user_id, cost)
                await decrement_counter(user_id, typ)
            except Exception:
                pass
            try:
                await app.bot.edit_message_text(chat_id=chat_id, message_id=status_msg_id,
                                                text=f"Ambot Converter\nTitle: {title}\nStatus: Error during conversion.\n{progress.get('status_text')}")
            except Exception:
                await app.bot.send_message(chat_id=chat_id, text=f"Conversion failed: {progress.get('status_text')}")
    finally:
        try:
            await app.bot.delete_message(chat_id=chat_id, message_id=status_msg_id)
        except Exception:
            pass
        user_state.pop(user_id, None)

# ---------------- Admin commands ----------------
async def admin_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if not is_admin(caller.id):
        await update.message.reply_text("You are not authorized.")
        return
    rows = await all_users()
    if not rows:
        await update.message.reply_text("No users.")
        return
    lines = ["Username | Coins | Music | Video"]
    i = 1
    for r in rows:
        uid, uname, coins, music_c, video_c = r
        uname_display = uname or f"id:{uid}"
        lines.append(f"{i}: {uname_display} | {coins} | {music_c} | {video_c}")
        i += 1
    await update.message.reply_text("\n".join(lines))

async def admin_addcoins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if not is_admin(caller.id):
        await update.message.reply_text("You are not authorized.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /addcoins <@username|user_id> <amount>")
        return
    target = args[0]
    try:
        amount = int(args[1])
    except Exception:
        await update.message.reply_text("Invalid amount. Must be integer.")
        return
    tgt_user_id = None
    if target.startswith("@"):
        uname = target[1:]
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT user_id FROM users WHERE username = ?", (uname,))
            row = await cur.fetchone()
            await cur.close()
            if row:
                tgt_user_id = row[0]
            else:
                await update.message.reply_text(f"User @{uname} not found.")
                return
    else:
        try:
            tgt_user_id = int(target)
        except Exception:
            await update.message.reply_text("Target must be @username or user_id.")
            return
    await add_coins(tgt_user_id, amount)
    await update.message.reply_text(f"Updated user {tgt_user_id} by {amount} coins.")

async def user_history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    n = 10
    if args and args[0].isdigit():
        n = int(args[0])
    convs = await get_user_conversions(user.id, n)
    if not convs:
        await update.message.reply_text("No conversions found.")
        return
    lines = []
    for c in convs:
        lines.append(f"{c['created_at'][:19]} | {c['type']} | {c['quality']} | {c['cost']}coins | status:{c['status']} | {c['title']}")
    await update.message.reply_text("\n".join(lines))

async def export_logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if not is_admin(caller.id):
        await update.message.reply_text("You are not authorized.")
        return
    tmp_csv = Path(tempfile.gettempdir()) / f"ambot_conversions_{int(datetime.utcnow().timestamp())}.csv"
    await all_conversions_csv(str(tmp_csv))
    try:
        await context.bot.send_document(chat_id=update.effective_chat.id, document=InputFile(str(tmp_csv)))
    except Exception as e:
        await update.message.reply_text(f"Failed to send CSV: {e}")
    finally:
        try:
            tmp_csv.unlink()
        except Exception:
            pass

# ---------------- Scheduler: daily reward ----------------
def schedule_daily_reward(app):
    scheduler = AsyncIOScheduler(timezone="Asia/Manila")
    trigger = CronTrigger(hour=0, minute=0)
    async def give_daily():
        today_str = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT user_id, last_daily_date FROM users")
            rows = await cur.fetchall()
            await cur.close()
        for (uid, last_daily) in rows:
            try:
                if last_daily == today_str:
                    continue
                await add_coins(uid, DAILY_REWARD)
                await set_last_daily(uid, today_str)
                try:
                    await app.bot.send_message(chat_id=uid, text=f"You have receive {DAILY_REWARD}coins from admin every day enjoy")
                except Exception:
                    pass
            except Exception:
                pass
    scheduler.add_job(lambda: asyncio.create_task(give_daily()), trigger=trigger)
    scheduler.start()

# ---------------- Startup ----------------
async def on_startup(app):
    logger.info("Initializing DB and scheduler...")
    await init_db()
    schedule_daily_reward(app)
    logger.info("Startup complete.")

# ---------------- Main and run ----------------
def main():
    global application_instance
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    application_instance = app

    # register handlers
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_start_text))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(CommandHandler("admin_status", admin_status_cmd))
    app.add_handler(CommandHandler("addcoins", admin_addcoins_cmd))
    app.add_handler(CommandHandler("history", user_history_cmd))
    app.add_handler(CommandHandler("export_logs", export_logs_cmd))

    async def run():
        # initialize -> start -> polling -> wait closed
        await app.initialize()
        await on_startup(app)
        await app.start()
        logger.info("Bot running. Starting polling...")
        # start polling
        await app.updater.start_polling()
        # wait until stop
        await app.wait_closed()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Interrupted, shutting down...")

if __name__ == "__main__":
    main()
