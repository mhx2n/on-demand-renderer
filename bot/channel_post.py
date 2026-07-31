"""
Rich publishing studio (Bot API 10.1 Rich Messages).

Any user can register a Telegram channel where the bot is an administrator,
compose a post — by hand, from a replied-to message, or with AI — refine it
conversationally by *replying* to the preview, attach one or many images
(native image slider), preview it, and publish it as a native Rich Message.

The owner can aim the very same composer at a broadcast to every bot user.

Commands
    /addchannel @channel        register a channel (bot must be admin there)
    /channels                   list your channels
    /delchannel @channel        unregister
    /post [markdown]            compose & publish (also works as a reply)
    /aipost <topic>             let AI write the post (also works as a reply)
    /addimg <url…>              attach image(s) to the current draft
    /clearimg                   drop attached images
    /linkpost <t.me link>       post into the channel of that post link
    /richcast [topic]           owner-only: same studio, broadcast to all users
    /postformat                 cheat-sheet of the supported rich markup
    /cancelpost                 abort

Refining
    Reply to the preview (or to the control message) with plain instructions —
    "make it shorter", "বাংলায় লেখো", "add a pricing table", "remove the quote"
    — and the post is regenerated, then previewed again.

Draft extras (any line):
    !img <url>          attach an image  (2+ → native slider)
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

# user_id -> state dict
_STATE: dict[int, dict] = {}

MAX_IMAGES = 10

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

## Refine
Reply to the preview with what you want changed — add, remove, shorten,
translate — and I rewrite the post, then show a new preview.
"""

_AI_PROMPT = (
    "You are a professional Telegram channel copywriter. Write a post about the "
    "topic below using Telegram Rich Message markdown: a short '# ' title, tight "
    "paragraphs, '## ' sections when useful, bullet or task lists, a compact table "
    "only when it adds value, `code` for technical bits, a `>` quote for the key "
    "takeaway, and bold for emphasis. Keep it under 250 words, no HTML, no "
    "explanations about yourself. Reply with the post only.\n\nTopic: "
)

_REFINE_PROMPT = (
    "You are editing a Telegram Rich Message post. Apply the user's instruction "
    "to the post below and return the COMPLETE rewritten post only — same rich "
    "markdown style (headings, tables, task lists, quotes, code, LaTeX), no HTML, "
    "no commentary, no explanation of the change. If the instruction asks for a "
    "different language, translate the whole post into that language.\n\n"
    "--- CURRENT POST ---\n{post}\n--- END POST ---\n\nInstruction: {instruction}"
)


# --------------------------------------------------------------- utilities
def _esc(s: str) -> str:
    return html.escape(s or "")


def _st(uid: int) -> dict:
    st = _STATE.get(uid)
    if st is None:
        st = {"mode": None, "target": None, "chat_id": None, "draft": "",
              "images": [], "ctrl_ids": set(), "busy": False}
        _STATE[uid] = st
    st.setdefault("images", [])
    st.setdefault("ctrl_ids", set())
    return st


def _track(uid: int, message) -> None:
    """Remember a bot message the user may reply to in order to refine."""
    if message is None:
        return
    st = _st(uid)
    ids: set = st["ctrl_ids"]
    ids.add(getattr(message, "message_id", 0))
    if len(ids) > 20:
        st["ctrl_ids"] = set(sorted(ids)[-20:])


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


def _all_images(uid: int, inline: list[str]) -> list:
    """Inline !img urls + explicitly attached images (urls or raw bytes)."""
    out: list = list(inline)
    for item in _st(uid)["images"]:
        if item not in out:
            out.append(item)
    return out[:MAX_IMAGES]


async def _ai(prompt: str) -> str | None:
    for key in ("g", "pr", "co"):
        meta = REGISTRY.get(key)
        if not meta:
            continue
        try:
            out = await asyncio.wait_for(meta[1](prompt, []), timeout=120)
            if (out or "").strip():
                return out.strip()
        except Exception:
            continue
    for _, (_, fn) in REGISTRY.items():
        try:
            out = await asyncio.wait_for(fn(prompt, []), timeout=120)
            if (out or "").strip():
                return out.strip()
        except Exception:
            continue
    return None


async def _ai_write(topic: str) -> str | None:
    return await _ai(_AI_PROMPT + topic)


async def _ai_refine(post: str, instruction: str) -> str | None:
    return await _ai(_REFINE_PROMPT.format(post=post, instruction=instruction))


async def _resolve_channel(context, ref: str):
    """Accept @name, t.me/name, or a numeric id → telegram Chat, or None."""
    ref = (ref or "").strip()
    m = re.search(r"(?:t\.me/|@)([A-Za-z0-9_]{4,})", ref)
    target = f"@{m.group(1)}" if m else ref
    try:
        return await context.bot.get_chat(target)
    except Exception:
        return None


# ------------------------------------------------------- channel post links
_POST_LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?t(?:elegram)?\.me/(c/(\d+)|[A-Za-z][A-Za-z0-9_]{3,})/(\d+)(?:/(\d+))?",
    re.IGNORECASE,
)


def find_post_link(text: str) -> tuple[str | int, int] | None:
    """Return (chat_ref, message_id) for the first t.me post link found."""
    m = _POST_LINK_RE.search(text or "")
    if not m:
        return None
    private_id, name_or_c, mid = m.group(2), m.group(1), m.group(3)
    # topic links (t.me/name/topic/msg) → last number is the message id
    if m.group(4):
        mid = m.group(4)
    try:
        message_id = int(mid)
    except Exception:
        return None
    if private_id:
        return int(f"-100{private_id}"), message_id
    return f"@{name_or_c}", message_id


async def _link_channel(context, uid: int, ref: str | int):
    """Resolve + authorise a channel from a post link, registering it silently."""
    chat = None
    try:
        chat = await context.bot.get_chat(ref)
    except Exception:
        chat = None
    if chat is None:
        return None, "I can't access that channel — add me there as an administrator first."
    if not await _bot_is_admin(context, chat.id):
        return None, "I'm not an administrator in that channel yet."
    if not await _user_is_admin(context, chat.id, uid):
        return None, "Only administrators of that channel can post through me."
    try:
        await db.add_channel(chat.id, uid, chat.title or "", chat.username or "")
    except Exception as e:
        log.debug("auto-register channel failed: %s", e)
    return chat, ""


async def _fetch_link_post(context, chat_id: int, message_id: int, scratch_chat: int):
    """Copy the linked post into the user's chat to read it, then remove it.

    Returns (text, image_bytes|None). Never raises.
    """
    text, image = "", None
    tmp = None
    try:
        tmp = await context.bot.forward_message(
            chat_id=scratch_chat, from_chat_id=chat_id,
            message_id=message_id, disable_notification=True)
        text = (getattr(tmp, "text", None) or getattr(tmp, "caption", None) or "").strip()
        image = await _photo_bytes(context, tmp)
    except Exception as e:
        log.debug("link post fetch failed: %s", e)
    finally:
        if tmp is not None:
            try:
                await context.bot.delete_message(scratch_chat, tmp.message_id)
            except Exception:
                pass
    return text, image



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


async def _photo_bytes(context, message) -> bytes | None:
    """Download the largest photo / image document of a message."""
    try:
        file_id = None
        if getattr(message, "photo", None):
            file_id = message.photo[-1].file_id
        elif getattr(message, "document", None) and \
                (message.document.mime_type or "").startswith("image/"):
            file_id = message.document.file_id
        if not file_id:
            return None
        f = await context.bot.get_file(file_id)
        return bytes(await f.download_as_bytearray())
    except Exception as e:
        log.debug("photo fetch failed: %s", e)
        return None


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


def _replied_text(msg) -> str:
    rep = getattr(msg, "reply_to_message", None)
    if not rep:
        return ""
    return (rep.text or rep.caption or "").strip()


async def _apply_link(context, msg, uid: int, *texts: str):
    """Detect a t.me post link, target that channel and pull the post content.

    Returns (handled_error: bool, source_text: str).
    """
    st = _st(uid)
    found = None
    for t in texts:
        found = find_post_link(t or "")
        if found:
            break
    if not found:
        return False, ""
    ref, mid = found
    chat, err = await _link_channel(context, uid, ref)
    if chat is None:
        await msg.reply_text(err)
        return True, ""
    st["chat_id"] = chat.id
    st["target"] = "channel"
    st["link_mid"] = mid
    text, image = await _fetch_link_post(context, chat.id, mid, msg.chat_id)
    if image:
        st["images"] = (st.get("images") or [])[:MAX_IMAGES - 1] + [image]
    await msg.reply_text(
        f"🔗 Target locked: <b>{_esc(chat.title or str(chat.id))}</b>"
        + ("\n📄 Linked post loaded as source." if text else ""),
        parse_mode=ParseMode.HTML)
    return False, text


async def cmd_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid = update.effective_user.id
    parts = (msg.text or "").split(None, 1)
    draft = parts[1].strip() if len(parts) > 1 else ""
    replied = _replied_text(msg)

    st = _st(uid)
    st.update({"draft": "", "chat_id": None, "mode": None,
               "target": "channel", "images": [], "ctrl_ids": set()})

    stop, linked = await _apply_link(context, msg, uid, draft, replied)
    if stop:
        return
    if linked:
        draft = draft or linked
    elif not draft:
        draft = replied
    # strip a bare link-only draft
    if draft and find_post_link(draft) and len(draft) < 120:
        draft = linked or ""
    st["draft"] = draft

    if not st.get("chat_id") and not await db.list_channels(uid):
        await msg.reply_text(
            "First register a channel: /addchannel @yourchannel\n"
            "Or reply to / paste a link of a post from your channel "
            "(https://t.me/yourchannel/123).\n"
            "See /postformat for the supported rich markup.")
        return

    rep = getattr(msg, "reply_to_message", None)
    if rep is not None:
        b = await _photo_bytes(context, rep)
        if b:
            st["images"].append(b)

    if not draft and not st["images"]:
        st["mode"] = "await_text"
        sent = await msg.reply_text(
            "Send me the post content now (rich markdown).\n"
            "Extras: <code>!img &lt;url&gt;</code>, <code>!title &lt;text&gt;</code>\n"
            "You can also send photos to attach them.\n"
            "Cheat-sheet: /postformat · /cancelpost to abort.",
            parse_mode=ParseMode.HTML)
        _track(uid, sent)
        return
    if st.get("chat_id"):
        await _preview(msg, uid)
    else:
        await _choose_channel(msg, uid)


async def cmd_aipost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid = update.effective_user.id
    topic = " ".join(context.args or []).strip()
    source = _replied_text(msg)
    st = _st(uid)
    st.update({"target": "channel", "chat_id": None, "draft": "",
               "images": [], "ctrl_ids": set(), "mode": None})

    stop, linked = await _apply_link(context, msg, uid, topic, source)
    if stop:
        return
    if linked:
        source = linked
    if topic and find_post_link(topic):
        topic = _POST_LINK_RE.sub("", topic).strip()

    if not st.get("chat_id") and not await db.list_channels(uid):
        await msg.reply_text(
            "First register a channel: /addchannel @yourchannel — or reply to a "
            "link of a post from your channel (https://t.me/yourchannel/123).")
        return

    rep = getattr(msg, "reply_to_message", None)
    if rep is not None:
        b = await _photo_bytes(context, rep)
        if b:
            st["images"].append(b)

    if source:
        topic = (f"{topic}\n\nSource material:\n{source}" if topic
                 else f"Turn this into a polished channel post:\n\n{source}")
    if not topic:
        st["mode"] = "await_topic"
        sent = await msg.reply_text("What should the post be about? Send the topic now.")
        _track(uid, sent)
        return

    await _generate_and_preview(msg, uid, topic)


async def cmd_richcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only: compose a rich post and broadcast it to every bot user."""
    msg = update.effective_message
    uid = update.effective_user.id
    if uid != OWNER_ID:
        await msg.reply_text("Owner only.")
        return
    arg = " ".join(context.args or []).strip()
    source = _replied_text(msg)
    st = _st(uid)
    st.update({"target": "broadcast", "chat_id": None, "draft": "",
               "images": [], "ctrl_ids": set(), "mode": None})

    rep = getattr(msg, "reply_to_message", None)
    if rep is not None:
        b = await _photo_bytes(context, rep)
        if b:
            st["images"].append(b)

    ai = arg.lower().startswith("ai ")
    if ai:
        arg = arg[3:].strip()
    if source and not arg:
        st["draft"] = source
        await _preview(msg, uid)
        return
    if ai or (source and arg):
        topic = arg
        if source:
            topic = f"{arg}\n\nSource material:\n{source}"
        await _generate_and_preview(msg, uid, topic)
        return
    if arg:
        st["draft"] = arg
        await _preview(msg, uid)
        return
    st["mode"] = "await_text"
    sent = await msg.reply_text(
        "Send the broadcast content now (rich markdown).\n"
        "Extras: <code>!img &lt;url&gt;</code>, <code>!title &lt;text&gt;</code>, photos.\n"
        "Tip: <code>/richcast ai &lt;topic&gt;</code> lets AI write it.\n"
        "/cancelpost to abort.",
        parse_mode=ParseMode.HTML)
    _track(uid, sent)


async def cmd_addimg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid = update.effective_user.id
    st = _st(uid)
    urls = [a for a in (context.args or []) if a.startswith("http")]
    rep = getattr(msg, "reply_to_message", None)
    if rep is not None:
        b = await _photo_bytes(context, rep)
        if b:
            st["images"].append(b)
    for u in urls:
        if u not in st["images"]:
            st["images"].append(u)
    st["images"] = st["images"][:MAX_IMAGES]
    if not st["images"]:
        await msg.reply_text("Send /addimg <url> or reply to a photo with /addimg.")
        return
    await msg.reply_text(
        f"🖼 {len(st['images'])} image(s) attached. "
        "They will be posted as a native slider when 2 or more.")


async def cmd_clearimg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _st(update.effective_user.id)["images"] = []
    await update.effective_message.reply_text("Images cleared.")


async def cmd_linkpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Post to the channel of a t.me post link (given or replied to)."""
    msg = update.effective_message
    uid = update.effective_user.id
    arg = " ".join(context.args or []).strip()
    replied = _replied_text(msg)
    st = _st(uid)
    st.update({"target": "channel", "chat_id": None, "draft": "",
               "images": [], "ctrl_ids": set(), "mode": None})

    stop, linked = await _apply_link(context, msg, uid, arg, replied)
    if stop:
        return
    if not st.get("chat_id"):
        await msg.reply_text(
            "Send or reply to a channel post link, e.g.\n"
            "<code>/linkpost https://t.me/yourchannel/123 write an update about X</code>",
            parse_mode=ParseMode.HTML)
        return

    instruction = _POST_LINK_RE.sub("", arg).strip()
    source = linked or (replied if not find_post_link(replied) else "")
    if instruction and source:
        topic = f"{instruction}\n\nSource material:\n{source}"
    elif instruction:
        topic = instruction
    elif source:
        topic = f"Turn this into a polished channel post:\n\n{source}"
    else:
        st["mode"] = "await_topic"
        sent = await msg.reply_text(
            "What should the post be about? Send the topic — or send the finished "
            "rich markdown and I'll publish it as-is.")
        _track(uid, sent)
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
    st = _st(uid)
    st.update({"draft": text, "mode": None})
    if st.get("target") == "broadcast" or st.get("chat_id"):
        await _preview(msg, uid)
        return
    await _choose_channel(msg, uid)



async def _regenerate(msg, uid: int, instruction: str):
    st = _st(uid)
    if st.get("busy"):
        return
    st["busy"] = True
    note = await msg.reply_text("Rewriting…")
    try:
        body, inline_imgs, title = _parse_draft(st.get("draft", ""))
        current = (f"# {title}\n\n" if title else "") + body
        out = await _ai_refine(current, instruction)
    finally:
        st["busy"] = False
    try:
        await note.delete()
    except Exception:
        pass
    if not out:
        await msg.reply_text("Could not rewrite the post — no AI provider answered.")
        return
    if inline_imgs:
        out = out + "\n" + "\n".join(f"!img {u}" for u in inline_imgs)
    st["draft"] = out
    await _preview(msg, uid)


async def _choose_channel(msg, uid: int):
    rows = await db.list_channels(uid)
    if len(rows) == 1:
        _st(uid)["chat_id"] = rows[0][0]
        await _preview(msg, uid)
        return
    sent = await msg.reply_text("Which channel?", reply_markup=await _channels_kb(uid, "ch"))
    _track(uid, sent)


async def _target_name(uid: int) -> str:
    st = _st(uid)
    if st.get("target") == "broadcast":
        return "All bot users (broadcast)"
    ch = await db.get_channel(st.get("chat_id"))
    return ch[2] if ch and ch[2] else str(st.get("chat_id"))


async def _preview(msg, uid: int):
    st = _st(uid)
    body, inline_imgs, title = _parse_draft(st.get("draft", ""))
    images = _all_images(uid, inline_imgs)
    st["title"] = title
    if not body and not images:
        await msg.reply_text("The draft is empty.")
        return
    if st.get("target") != "broadcast" and not st.get("chat_id"):
        await _choose_channel(msg, uid)
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Publish", callback_data="cp:go:1"),
         InlineKeyboardButton("♻️ Regenerate", callback_data="cp:regen:1")],
        [InlineKeyboardButton("🗑 Clear images", callback_data="cp:noimg:1"),
         InlineKeyboardButton("✖ Cancel", callback_data="cp:cancel:0")],
    ])
    name = await _target_name(uid)
    header = f"<b>Preview → {_esc(name)}</b>"
    if images:
        header += f"  ·  {len(images)} image(s)"

    md = (f"# {title}\n\n" if title else "") + body
    if images and len(images) > 1 and richsend.enabled():
        try:
            await richmsg.send_slideshow(msg.chat_id, images, caption=title or "")
        except Exception:
            pass
    elif images:
        try:
            src = images[0]
            await msg.reply_photo(src, caption=(title or None))
        except Exception:
            pass

    if md.strip():
        sent = await richsend.reply(msg, md)
        if not sent:
            await msg.reply_text(
                format_ai_answer(md), parse_mode=ParseMode.HTML,
                disable_web_page_preview=True)

    ctrl = await msg.reply_text(
        header + "\n\nPublish this post?\n"
        "<i>Reply to this message with instructions to rewrite it "
        "(shorten, translate, add/remove sections…).</i>",
        parse_mode=ParseMode.HTML, reply_markup=kb)
    _track(uid, ctrl)


async def _publish_channel(context, uid: int, chat_id: int) -> str:
    st = _st(uid)
    body, inline_imgs, title = _parse_draft(st.get("draft", ""))
    images = _all_images(uid, inline_imgs)
    if not await _bot_is_admin(context, chat_id):
        return "I'm no longer an administrator in that channel."

    md = (f"# {title}\n\n" if title else "") + body
    posted = False

    if images:
        if len(images) > 1 and richsend.enabled():
            try:
                posted = bool(await richmsg.send_slideshow(
                    chat_id, images, caption=title or ""))
            except Exception:
                posted = False
        if not posted:
            try:
                if len(images) == 1:
                    await context.bot.send_photo(chat_id, images[0], caption=title or None)
                else:
                    await context.bot.send_media_group(
                        chat_id, [InputMediaPhoto(u) for u in images])
                posted = True
            except Exception as e:
                log.debug("channel images failed: %s", e)

    if md.strip():
        mid = await richsend.post(chat_id, md)
        if not mid:
            try:
                await context.bot.send_message(
                    chat_id, format_ai_answer(md)[:4000], parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False)
            except Exception:
                await context.bot.send_message(chat_id, clean_text(md)[:4000])
        posted = True

    return "✅ Published." if posted else "Nothing was posted."


async def _broadcast(context, uid: int, status_msg) -> str:
    st = _st(uid)
    body, inline_imgs, title = _parse_draft(st.get("draft", ""))
    images = _all_images(uid, inline_imgs)
    md = (f"# {title}\n\n" if title else "") + body
    html_fallback = format_ai_answer(md)[:4000] if md.strip() else ""

    ids = await db.all_user_ids()
    total, ok, blocked, fail = len(ids), 0, 0, 0
    for i, target in enumerate(ids, 1):
        delivered = False
        try:
            if images:
                sent = False
                if len(images) > 1 and richsend.enabled():
                    try:
                        sent = bool(await richmsg.send_slideshow(
                            target, images, caption=title or ""))
                    except Exception:
                        sent = False
                if not sent:
                    if len(images) == 1:
                        await context.bot.send_photo(target, images[0],
                                                     caption=title or None)
                    else:
                        await context.bot.send_media_group(
                            target, [InputMediaPhoto(u) for u in images])
                delivered = True
            if md.strip():
                mid = await richsend.post(target, md)
                if not mid:
                    await context.bot.send_message(
                        target, html_fallback, parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True)
                delivered = True
            ok += 1 if delivered else 0
        except Exception as e:
            es = str(e).lower()
            if "blocked" in es or "deactivated" in es or "chat not found" in es:
                blocked += 1
            else:
                fail += 1
        if i % 25 == 0:
            await asyncio.sleep(1.0)
            try:
                await status_msg.edit_text(
                    f"Progress {i}/{total}\nDelivered: {ok}\n"
                    f"Blocked: {blocked}\nFailed: {fail}")
            except Exception:
                pass
    return (f"<b>Rich broadcast complete</b>\nTotal: {total}\nDelivered: {ok}\n"
            f"Blocked/deleted: {blocked}\nFailed: {fail}")


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
        _st(uid)["chat_id"] = int(arg)
        try:
            await q.edit_message_text("Channel selected.")
        except Exception:
            pass
        await _preview(q.message, uid)
        return

    if action == "noimg":
        _st(uid)["images"] = []
        try:
            await q.edit_message_text("Attached images cleared. Send /post again to re-preview.")
        except Exception:
            pass
        return

    if action == "regen":
        st = _st(uid)
        st["mode"] = "await_instruction"
        sent = await q.message.reply_text(
            "What should I change? Send the instruction "
            "(e.g. “shorter”, “বাংলায় লেখো”, “add a comparison table”).")
        _track(uid, sent)
        return

    if action == "go":
        st = _st(uid)
        if st.get("target") == "broadcast":
            if uid != OWNER_ID:
                await q.message.reply_text("Owner only.")
                return
            try:
                await q.edit_message_text("Broadcasting…")
            except Exception:
                pass
            result = await _broadcast(context, uid, q.message)
            _STATE.pop(uid, None)
            try:
                await q.message.reply_text(result, parse_mode=ParseMode.HTML)
            except Exception:
                pass
            return
        chat_id = st.get("chat_id")
        if not chat_id:
            await q.message.reply_text("No channel selected.")
            return
        result = await _publish_channel(context, uid, chat_id)
        _STATE.pop(uid, None)
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
    if not st:
        return
    text = (msg.text or msg.caption or "").strip()
    if not text or text.startswith("/"):
        return

    mode = st.get("mode")
    rep = getattr(msg, "reply_to_message", None)
    replying_to_bot = bool(
        rep is not None
        and getattr(rep, "from_user", None) is not None
        and rep.from_user.is_bot
        and (rep.message_id in st.get("ctrl_ids", set()) or st.get("draft"))
    )

    if mode == "await_text":
        st["mode"] = None
        st["draft"] = text
        if st.get("target") == "broadcast":
            await _preview(msg, uid)
        else:
            await _choose_channel(msg, uid)
        raise ApplicationHandlerStop

    if mode == "await_topic":
        st["mode"] = None
        await _generate_and_preview(msg, uid, text)
        raise ApplicationHandlerStop

    if mode == "await_instruction":
        st["mode"] = None
        await _regenerate(msg, uid, text)
        raise ApplicationHandlerStop

    # A channel post link retargets the current draft to that channel.
    if find_post_link(text):
        stop, linked = await _apply_link(context, msg, uid, text)
        if stop:
            raise ApplicationHandlerStop
        rest = _POST_LINK_RE.sub("", text).strip()
        if st.get("draft") and rest:
            await _regenerate(msg, uid, rest)
        elif st.get("draft"):
            await _preview(msg, uid)
        elif rest or linked:
            await _generate_and_preview(
                msg, uid,
                f"{rest}\n\nSource material:\n{linked}".strip() if linked else rest)
        raise ApplicationHandlerStop

    # Conversational refine: reply to the preview / control message
    if replying_to_bot and st.get("draft"):
        await _regenerate(msg, uid, text)
        raise ApplicationHandlerStop



async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Attach photos sent (or replied) while the composer is armed."""
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    uid = update.effective_user.id
    st = _STATE.get(uid)
    if not st:
        return
    if not (st.get("mode") or st.get("draft")):
        return
    data = await _photo_bytes(context, msg)
    if not data:
        return
    st["images"] = (st.get("images") or [])[:MAX_IMAGES - 1] + [data]
    caption = (msg.caption or "").strip()
    if caption and not st.get("draft"):
        st["draft"] = caption
        st["mode"] = None
    await msg.reply_text(
        f"🖼 Image attached ({len(st['images'])}). "
        "Send more, or use /post again to preview and publish."
        if not st.get("draft") else
        f"🖼 Image attached ({len(st['images'])}). "
        "Tap ♻️ Regenerate or reply to the preview to keep editing.")
    raise ApplicationHandlerStop


# ------------------------------------------------------------- registration
def register(app: Application):
    app.add_handler(CommandHandler("addchannel", cmd_addchannel))
    app.add_handler(CommandHandler("channels",   cmd_channels))
    app.add_handler(CommandHandler("delchannel", cmd_delchannel))
    app.add_handler(CommandHandler("post",       cmd_post))
    app.add_handler(CommandHandler("aipost",     cmd_aipost))
    app.add_handler(CommandHandler("richcast",   cmd_richcast))
    app.add_handler(CommandHandler("addimg",     cmd_addimg))
    app.add_handler(CommandHandler("clearimg",   cmd_clearimg))
    app.add_handler(CommandHandler("linkpost",   cmd_linkpost))
    app.add_handler(CommandHandler("postformat", cmd_postformat))
    app.add_handler(CommandHandler("cancelpost", cmd_cancelpost))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^cp:"))
    # Run before every other text/photo handler, but only consume the update
    # while the composer is armed for this user.
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_text),
        group=-4,
    )
    app.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo),
        group=-4,
    )
