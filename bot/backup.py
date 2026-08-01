"""
Owner backup console (MongoDB mirror + SQLite).

/backup shows a native Rich Message dashboard:
  • connection state of the MongoDB mirror
  • exactly what is saved, per collection, with document counts
  • storage used / free against the 512 MB free-tier budget
  • the local SQLite file size

Inline actions (owner only):
  🔄 Refresh · 📦 Export JSON · 🗄 SQLite file · 👥 Recent users
  🧹 Wipe mirror (two-step confirmation)
"""
from __future__ import annotations

import io
import json
import logging
import os
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

from . import db, mongo, richsend
from .config import OWNER_ID, DB_PATH
from .utils import format_ai_answer

log = logging.getLogger("backup")


def _human(n: int | float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} GB"


def _bar(pct: float, width: int = 20) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(width * pct / 100))
    return "█" * filled + "░" * (width - filled)


def _kb(confirm: bool = False) -> InlineKeyboardMarkup:
    if confirm:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("⚠️ Yes, delete everything", callback_data="bk:wipe_yes"),
             InlineKeyboardButton("✖ Cancel", callback_data="bk:refresh")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="bk:refresh"),
         InlineKeyboardButton("👥 Recent users", callback_data="bk:users")],
        [InlineKeyboardButton("📦 Export JSON", callback_data="bk:export"),
         InlineKeyboardButton("🗄 SQLite file", callback_data="bk:sqlite")],
        [InlineKeyboardButton("🧹 Wipe mirror", callback_data="bk:wipe")],
    ])


async def _report() -> str:
    counts = await mongo.collection_counts()
    stats = await mongo.db_stats()
    local = await db.stats()

    try:
        sqlite_size = os.path.getsize(DB_PATH)
    except Exception:
        sqlite_size = 0

    lines = ["# 🛡 Backup Console", ""]
    if not mongo.enabled():
        lines += [
            "> **MongoDB mirror is OFF** — running SQLite-only.",
            "",
            "Set `MONGODB_URI` to enable off-box backups that survive redeploys.",
            "",
            "## Local store",
            "",
            "| Item | Value |",
            "|:-----|------:|",
            f"| SQLite file | `{DB_PATH}` |",
            f"| File size | {_human(sqlite_size)} |",
            f"| Users | {local.get('users', 0)} |",
            f"| Messages | {local.get('messages', 0)} |",
        ]
        return "\n".join(lines)

    lines += ["> ✅ **Mirror online** — every owner-managed change is saved to MongoDB.", ""]

    lines += ["## What is saved", "", "| Collection | Documents |", "|:-----------|----------:|"]
    total_docs = 0
    for name in mongo.MIRRORED:
        c = counts.get(name, 0)
        if c and c > 0:
            total_docs += c
        lines.append(f"| `{name}` | {c if c >= 0 else '—'} |")
    lines.append(f"| **Total** | **{total_docs}** |")
    lines.append("")

    if stats:
        used, quota, free = stats["used"], stats["quota"], stats["free"]
        pct = (used / quota * 100) if quota else 0
        lines += [
            "## Storage",
            "",
            f"`{_bar(pct)}`  **{pct:.2f}%** used",
            "",
            "| Metric | Size |",
            "|:-------|-----:|",
            f"| Data | {_human(stats['data_size'])} |",
            f"| Storage | {_human(stats['storage_size'])} |",
            f"| Indexes | {_human(stats['index_size'])} |",
            f"| **Used** | **{_human(used)}** |",
            f"| **Free** | **{_human(free)}** |",
            f"| Quota | {_human(quota)} |",
            "",
        ]

    lines += [
        "## Local SQLite",
        "",
        "| Item | Value |",
        "|:-----|------:|",
        f"| File | `{DB_PATH}` |",
        f"| Size | {_human(sqlite_size)} |",
        f"| Users | {local.get('users', 0)} |",
        f"| Messages | {local.get('messages', 0)} |",
        f"| Banned | {local.get('banned', 0)} |",
        f"| Errors logged | {local.get('errors', 0)} |",
        "",
        "> Use **📦 Export JSON** for a full mirror dump, or **🗄 SQLite file** "
        "for the raw database.",
    ]
    return "\n".join(lines)


async def _send(msg, text: str, kb: InlineKeyboardMarkup | None = None):
    """Rich-first delivery with an HTML fallback, always keeping the keyboard."""
    sent = None
    if richsend.enabled():
        try:
            await richsend.deliver(msg.chat_id, text, stream=False)
            sent = True
        except Exception as e:
            log.debug("rich backup panel failed: %s", e)
            sent = None
    if sent:
        return await msg.reply_text("Backup actions:", reply_markup=kb)
    return await msg.reply_text(
        format_ai_answer(text), parse_mode=ParseMode.HTML,
        disable_web_page_preview=True, reply_markup=kb)


# ------------------------------------------------------------------ command
async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if update.effective_user.id != OWNER_ID:
        await msg.reply_text("Owner only.")
        return
    await _send(msg, await _report(), _kb())


# ---------------------------------------------------------------- callbacks
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if uid != OWNER_ID:
        await q.answer("Owner only.", show_alert=True)
        return
    await q.answer()
    action = (q.data or "").split(":", 1)[1] if ":" in (q.data or "") else ""

    if action == "refresh":
        try:
            await q.edit_message_text("Refreshing…")
        except Exception:
            pass
        await _send(q.message, await _report(), _kb())
        return

    if action == "users":
        rows = await mongo.recent_users(15)
        if not rows:
            rows_local = await db.top_users(15)
            lines = ["# 👥 Recent saved users", "", "| User | Username | Messages |",
                     "|:-----|:---------|---------:|"]
            for r in rows_local:
                lines.append(f"| `{r[0]}` | {('@' + r[2]) if r[2] else '—'} | {r[3]} |")
        else:
            lines = ["# 👥 Recently saved users (mirror)", "",
                     "| User | Username | Messages | Banned |",
                     "|:-----|:---------|---------:|:------:|"]
            for r in rows:
                lines.append(
                    f"| `{r.get('_id')}` | "
                    f"{('@' + r['username']) if r.get('username') else '—'} | "
                    f"{r.get('msg_count', 0)} | {'🚫' if r.get('is_banned') else '✅'} |")
        await _send(q.message, "\n".join(lines), _kb())
        return

    if action == "export":
        data = await mongo.export_all()
        if not data:
            await q.message.reply_text("Mirror is empty or disabled — nothing to export.")
            return
        payload = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        buf = io.BytesIO(payload.encode("utf-8"))
        buf.name = f"mongo-backup-{time.strftime('%Y%m%d-%H%M%S')}.json"
        counts = ", ".join(f"{k}: {len(v)}" for k, v in data.items())
        await q.message.reply_document(
            buf, caption=f"📦 Full mirror export\n{counts}")
        return

    if action == "sqlite":
        try:
            size = os.path.getsize(DB_PATH)
            with open(DB_PATH, "rb") as fh:
                blob = fh.read()
        except Exception as e:
            await q.message.reply_text(f"Could not read the database file: {e}")
            return
        if size > 45 * 1024 * 1024:
            await q.message.reply_text(
                f"Database file is {_human(size)} — too large for Telegram upload.")
            return
        buf = io.BytesIO(blob)
        buf.name = f"bot-{time.strftime('%Y%m%d-%H%M%S')}.db"
        await q.message.reply_document(buf, caption=f"🗄 SQLite database · {_human(size)}")
        return

    if action == "wipe":
        counts = await mongo.collection_counts()
        total = sum(c for c in counts.values() if c and c > 0)
        try:
            await q.edit_message_text(
                f"⚠️ This deletes <b>{total}</b> mirrored documents from MongoDB "
                "permanently.\nLocal SQLite data is not touched.",
                parse_mode=ParseMode.HTML, reply_markup=_kb(confirm=True))
        except Exception:
            await q.message.reply_text(
                f"⚠️ Delete all {total} mirrored documents?",
                reply_markup=_kb(confirm=True))
        return

    if action == "wipe_yes":
        res = await mongo.wipe()
        summary = "\n".join(f"• `{k}` → {v} deleted" for k, v in res.items()) or "Nothing to delete."
        try:
            await q.edit_message_text("🧹 Mirror wiped.")
        except Exception:
            pass
        await _send(q.message, f"# 🧹 Mirror wiped\n\n{summary}", _kb())
        return


def register(app: Application):
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^bk:"))
