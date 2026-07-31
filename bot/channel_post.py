"""
Rich channel publishing (Bot API 10.1 Rich Messages).

Any user (and the owner) can register a Telegram channel where the bot is an
administrator, compose a post — by hand or with AI — preview it, and publish
it as a native Rich Message (headings, tables, LaTeX, task lists, quotes,
collapsible details, links) plus optional images / native image slider.

Commands
    /addchannel @channel        register a channel (bot must be admin there)
    /channels                   list your channels
    /delchannel @channel        unregister
    /post [markdown]            compose & publish (buttons + preview)
    /aipost <topic>             let AI write the post, then preview & publish
    /postformat                 cheat-sheet of the supported rich markup

Draft extras (any line):
    !img <url>          attach an image  (2+ → native slider / album)
    !title <text>       bold H1 title placed on top
"""
from __future__ import annotations

import asyncio
import html
import logging
import re

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters, ApplicationHandlerStop,
)

from . import db, richmsg, richsend
from .config import OWNER_ID
from .providers import REGISTRY
from .utils import clean_text, format_ai_answer

log = logging.getLogger("channel_post")

# user_id -> {"mode": ..., "chat_id": int|None, "draft": str, "images": [url]}
_STATE: dict[int, dict] = {}

FORMAT_HELP = """# Rich post format

**Headings** `# H1` `## H2` · **Bold** `**text**` · *Italic* `*text*`
Spoiler `||secret||` · Strike `~~text~~` · Code `` `x` ``

## Table
```
| Feature | Status |
|:--------|:------:|
| Rich    |   ✅   |
```

## Math
`$E = mc^2$`  or a block with `$$ … $$`

## Task list
- [x] done
- [ ] pending

> Quote line
```python
print("code block")
```

**Link:** `[Telegram](https://telegram.org)`
**Image:** `!img https://example.com/pic.jpg`  (2+ → native slider)
**Title:** `!title My update`
"""

_AI_PROMPT = (
    "You are a professional Telegram channel copywriter. Write a post about the "
    "topic below using Telegram Rich Message markdown: a short '# ' title, tight "
    "paragraphs, '## ' sections when useful, bullet or task lists, a compact table "
    "only when it adds value, `code` for technical bits, a `>` quote for the key "
    "takeaway, and bold for emphasis. Keep it under 250 words, no HTML, no "
    "explanations about yourself. Reply with the post only.\n\nTopic: "
)


# --------------------------------------------------------------- utilities
def _esc(s: str) -> str:
    return html.escape(s or "")


def _parse_draft(text: str) -> tuple[str, list[str], str | None]:
    """Split a draft into (markdown, image urls, title)."""
    body, images, title = [], [], None
    for line in (text or "").splitlines():
        st = line.strip()
        low = st.lower()
        if low.startswith("!img "):
            url = st[5:].strip()
            if url.startswith("http"):
                images.append(url)
            continue
        if low.startswith("!title "):
            title = st[7:].strip() or None
            continue
        body.append(line)
    return "\n".join(body).strip(), images, title


async def _ai_write(topic: str) -> str | None:
    for key in ("g", "pr", "co"):
        meta = REGISTRY.get(key)
        if not meta:
            continue
        try:
            out = await asyncio.wait_for(meta[1](_AI_PROMPT + topic, []), timeout=120)
            if (out or "").strip():
                return out.strip()
        except Exception:
            continue
    for _, (_, fn) in REGISTRY.items():
        try:
            out = await asyncio.wait_for(fn(_AI_PROMPT + topic, []), timeout=120)
            if (out or "").strip():
                return out.strip()
        except Exception:
            continue
    return None


async def _resolve_channel(context, ref: str):
    """Accept @name, t.me/name, or a numeric id → telegram Chat, or None."""
    ref = (ref or "").strip()
    m = re.search(r"(?:t\.me/|@)([A-Za-z0-9_]{4,})", ref)
    target = f"@{m.group(1)}" if m else ref
    try:
        return await context.bot.get_chat(target)
    except Exception:
        return None


async def _bot_is_admin(context, chat_id: int) -> bool:
    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat_id, me.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def _user_is_admin(context, chat_id: int, uid: int) -> bool:
    if uid == OWNER_ID:
        return True
    try:
        member = await context.bot.get_chat_member(chat_id, uid)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def _channels_kb(uid: int, action: str):
    rows = []
    for chat_id, _own, title, username in await db.list_channels(uid):
        label = title or (f"@{username}" if username else str(chat_id))
        rows.append([InlineKeyboardButton(label[:40], callback_data=f"cp:{action}:{chat_id}")])
    rows.append([InlineKeyboardButton("Cancel", callback_data="cp:cancel:0")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------- commands
async def cmd_addchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid = update.effective_user.id
    ref = " ".join(context.args or []).strip()
    if not ref:
        await msg.reply_text(
            "Usage: <code>/addchannel @yourchannel</code>\n\n"
            "1. Add this bot to the channel\n"
            "2. Make it an <b>administrator</b> with “Post messages”\n"
            "3. Run the command again.",
            parse_mode=ParseMode.HTML)
        return
    chat = await _resolve_channel(context, ref)
    if not chat:
        await msg.reply_text("Channel not found. Make sure the bot is a member/admin there.")
        return
    if not await _bot_is_admin(context, chat.id):
        await msg.reply_text("I'm not an administrator in that channel yet.")
        return
    if not await _user_is_admin(context, chat.id, uid):
        await msg.reply_text("Only administrators of that channel can register it.")
        return
    await db.add_channel(chat.id, uid, chat.title or "", chat.username or "")
    await msg.reply_text(
        f"✅ Registered <b>{_esc(chat.title or str(chat.id))}</b>.\n"
        "Now use /post to publish a rich message there.",
        parse_mode=ParseMode.HTML)


async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await db.list_channels(update.effective_user.id)
    if not rows:
        await update.effective_message.reply_text(
            "No channels registered yet. Use /addchannel @yourchannel.")
        return
    lines = ["<b>Your channels</b>"]
    for chat_id, _own, title, username in rows:
        tag = f" (@{username})" if username else ""
        lines.append(f"• {_esc(title or str(chat_id))}{_esc(tag)} — <code>{chat_id}</code>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_delchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ref = " ".join(context.args or []).strip()
    if not ref:
        await update.effective_message.reply_text("Usage: /delchannel @yourchannel")
        return
    chat = await _resolve_channel(context, ref)
    chat_id = chat.id if chat else (int(ref) if re.fullmatch(r"-?\d+", ref) else None)
    if chat_id is None:
        await update.effective_message.reply_text("Channel not found.")
        return
    await db.remove_channel(chat_id, update.effective_user.id)
    await update.effective_message.reply_text("Removed.")


async def cmd_postformat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if await richsend.reply(msg, FORMAT_HELP):
        return
    await msg.reply_text(format_ai_answer(FORMAT_HELP), parse_mode=ParseMode.HTML,
                         disable_web_page_preview=True)


async def cmd_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid = update.effective_user.id
    rows = await db.list_channels(uid)
    if not rows:
        await msg.reply_text(
            "First register a channel: /addchannel @yourchannel\n"
            "See /postformat for the supported rich markup.")
        return
    draft = (msg.text or "").split(None, 1)
    draft = draft[1].strip() if len(draft) > 1 else ""
    st = _STATE.setdefault(uid, {})
    st.update({"draft": draft, "chat_id": None, "mode": None})

    if not draft:
        st["mode"] = "await_text"
        await msg.reply_text(
            "Send me the post content now (rich markdown).\n"
            "Extras: <code>!img &lt;url&gt;</code>, <code>!title &lt;text&gt;</code>\n"
            "Cheat-sheet: /postformat · /cancelpost to abort.",
            parse_mode=ParseMode.HTML)
        return
    await _choose_channel(msg, uid)


async def cmd_aipost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid = update.effective_user.id
    if not await db.list_channels(uid):
        await msg.reply_text("First register a channel: /addchannel @yourchannel")
        return
    topic = " ".join(context.args or []).strip()
    if not topic:
        _STATE.setdefault(uid, {}).update({"mode": "await_topic", "draft": "", "chat_id": None})
        await msg.reply_text("What should the post be about? Send the topic now.")
        return
    await _generate_and_preview(msg, uid, topic)


async def cmd_cancelpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _STATE.pop(update.effective_user.id, None)
    await update.effective_message.reply_text("Post composer cancelled.")


# ----------------------------------------------------------------- helpers
async def _generate_and_preview(msg, uid: int, topic: str):
    note = await msg.reply_text("Writing the post…")
    text = await _ai_write(topic)
    try:
        await note.delete()
    except Exception:
        pass
    if not text:
        await msg.reply_text("No AI provider is available right now.")
        return
    _STATE.setdefault(uid, {}).update({"draft": text, "mode": None})
    await _choose_channel(msg, uid)


async def _choose_channel(msg, uid: int):
    rows = await db.list_channels(uid)
    if len(rows) == 1:
        _STATE.setdefault(uid, {})["chat_id"] = rows[0][0]
        await _preview(msg, uid)
        return
    await msg.reply_text("Which channel?", reply_markup=await _channels_kb(uid, "ch"))


async def _preview(msg, uid: int):
    st = _STATE.get(uid) or {}
    body, images, title = _parse_draft(st.get("draft", ""))
    st["images"] = images
    st["title"] = title
    if not body and not images:
        await msg.reply_text("The draft is empty.")
        return
    ch = await db.get_channel(st.get("chat_id"))
    name = (ch[2] if ch and ch[2] else str(st.get("chat_id")))
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📤 Publish", callback_data="cp:go:1"),
        InlineKeyboardButton("✖ Cancel", callback_data="cp:cancel:0"),
    ]])
    header = f"<b>Preview → {_esc(name)}</b>"
    if images:
        header += f"  ·  {len(images)} image(s)"
    sent = await richsend.reply(msg, (f"# {title}\n\n" if title else "") + body)
    if not sent:
        await msg.reply_text(
            f"{header}\n\n{format_ai_answer(body)}",
            parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    await msg.reply_text(header + "\n\nPublish this post?", parse_mode=ParseMode.HTML,
                         reply_markup=kb)


async def _publish(context, uid: int) -> str:
    st = _STATE.get(uid) or {}
    chat_id = st.get("chat_id")
    body, images, title = _parse_draft(st.get("draft", ""))
    if not chat_id:
        return "No channel selected."
    if not await _bot_is_admin(context, chat_id):
        return "I'm no longer an administrator in that channel."

    md = (f"# {title}\n\n" if title else "") + body
    posted = False

    if images:
        if len(images) > 1 and richsend.enabled():
            mid = await richmsg.send_slideshow(chat_id, images[:10], caption=title or "")
            posted = bool(mid)
        if not posted:
            try:
                if len(images) == 1:
                    await context.bot.send_photo(chat_id, images[0], caption=title or "")
                else:
                    await context.bot.send_media_group(
                        chat_id, [InputMediaPhoto(u) for u in images[:10]])
                posted = True
            except Exception as e:
                log.debug("channel images failed: %s", e)

    if md.strip():
        mid = await richsend.post(chat_id, md)
        if not mid:
            text = format_ai_answer(md)
            try:
                await context.bot.send_message(
                    chat_id, text[:4000], parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False)
            except Exception:
                await context.bot.send_message(chat_id, clean_text(md)[:4000])
        posted = True

    _STATE.pop(uid, None)
    return "✅ Published." if posted else "Nothing was posted."


# ---------------------------------------------------------------- callbacks
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    uid = q.from_user.id
    await q.answer()
    _, action, arg = (data.split(":", 2) + ["", ""])[:3]

    if action == "cancel":
        _STATE.pop(uid, None)
        try:
            await q.edit_message_text("Cancelled.")
        except Exception:
            pass
        return
    if action == "ch":
        _STATE.setdefault(uid, {})["chat_id"] = int(arg)
        try:
            await q.edit_message_text("Channel selected.")
        except Exception:
            pass
        await _preview(q.message, uid)
        return
    if action == "go":
        result = await _publish(context, uid)
        try:
            await q.edit_message_text(result)
        except Exception:
            await q.message.reply_text(result)


# -------------------------------------------------------------- text input
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    uid = update.effective_user.id
    st = _STATE.get(uid)
    if not st or not st.get("mode"):
        return
    text = (msg.text or msg.caption or "").strip()
    if not text:
        return
    mode = st["mode"]
    st["mode"] = None
    if mode == "await_text":
        st["draft"] = text
        await _choose_channel(msg, uid)
    elif mode == "await_topic":
        await _generate_and_preview(msg, uid, text)
    raise ApplicationHandlerStop


# ------------------------------------------------------------- registration
def register(app: Application):
    app.add_handler(CommandHandler("addchannel", cmd_addchannel))
    app.add_handler(CommandHandler("channels",   cmd_channels))
    app.add_handler(CommandHandler("delchannel", cmd_delchannel))
    app.add_handler(CommandHandler("post",       cmd_post))
    app.add_handler(CommandHandler("aipost",     cmd_aipost))
    app.add_handler(CommandHandler("postformat", cmd_postformat))
    app.add_handler(CommandHandler("cancelpost", cmd_cancelpost))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^cp:"))
    # Runs before every other text handler, but only consumes the update
    # while the composer is armed for this user.
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_text),
        group=-4,
    )
