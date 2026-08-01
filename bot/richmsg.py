"""
Telegram Rich Messages (Bot API 10.1 / Telethon MTProto layer).

python-telegram-bot cannot send Rich Messages — they are an MTProto-only
construct (`InputRichMessageMarkdown` / `InputRichMessageHTML`). This module
runs a *side* Telethon bot client on the very same bot token, sharing the
running asyncio loop with PTB, and exposes small helpers:

    await richmsg.init()                      # safe, never raises
    richmsg.available()                       # bool
    await richmsg.send_markdown(chat, md)     # -> message id | None
    await richmsg.edit_markdown(chat, id, md) # -> bool
    await richmsg.stream_markdown(...)        # live block-by-block streaming
    draft = richmsg.Draft(chat)               # private-chat live draft
    await richmsg.send_slideshow(chat, imgs)  # native image slider

Everything degrades gracefully: if TELEGRAM_API_ID / TELEGRAM_API_HASH are
missing, Telethon is not installed, or the DC rejects the layer, every helper
returns None/False and the caller falls back to the classic PTB HTML path.
"""
from __future__ import annotations

import asyncio
import io
import logging
import urllib.request

from .config import (
    BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH,
    TELETHON_SESSION, TELETHON_SESSION_NAME,
)

log = logging.getLogger("richmsg")

try:  # Telethon is optional — the bot must still boot without it.
    from telethon import TelegramClient, functions, types, helpers
    from telethon.sessions import StringSession
    _TELETHON_OK = True
    _TELETHON_ERR = ""
except Exception as _e:  # pragma: no cover
    TelegramClient = functions = types = helpers = StringSession = None  # type: ignore
    _TELETHON_OK = False
    _TELETHON_ERR = str(_e)

_client = None
_ready = False
_rich_supported = True          # flipped off if the DC rejects rich_message
_init_lock: asyncio.Lock | None = None

MAX_RICH_LEN = 4000
STREAM_DELAY = 0.6


# ---------------------------------------------------------------- lifecycle
def _lock() -> asyncio.Lock:
    global _init_lock
    if _init_lock is None:
        _init_lock = asyncio.Lock()
    return _init_lock


def configured() -> bool:
    return bool(_TELETHON_OK and TELEGRAM_API_ID and TELEGRAM_API_HASH and BOT_TOKEN)


def available() -> bool:
    """True when rich messages can actually be sent right now."""
    return bool(_ready and _client is not None and _rich_supported)


def status() -> dict:
    return {
        "telethon": _TELETHON_OK,
        "telethon_error": _TELETHON_ERR,
        "configured": configured(),
        "connected": bool(_ready),
        "rich_supported": _rich_supported,
    }


async def init() -> bool:
    """Start the Telethon side-client. Never raises."""
    global _client, _ready
    if _ready:
        return True
    if not configured():
        if not _TELETHON_OK:
            log.info("Rich messages disabled: telethon unavailable (%s)", _TELETHON_ERR)
        else:
            log.info("Rich messages disabled: TELEGRAM_API_ID / TELEGRAM_API_HASH not set")
        return False
    async with _lock():
        if _ready:
            return True
        try:
            session = StringSession(TELETHON_SESSION) if TELETHON_SESSION else TELETHON_SESSION_NAME
            client = TelegramClient(
                session, int(TELEGRAM_API_ID), TELEGRAM_API_HASH,
                connection_retries=3, retry_delay=2, auto_reconnect=True,
            )
            await client.start(bot_token=BOT_TOKEN)
            _client = client
            _ready = True
            log.info("Rich message layer online (Telethon MTProto)")
            return True
        except Exception as e:
            log.warning("Rich message layer unavailable: %s", e)
            _client = None
            _ready = False
            return False


async def close() -> None:
    global _client, _ready
    try:
        if _client is not None:
            await _client.disconnect()
    except Exception:
        pass
    _client = None
    _ready = False


# ------------------------------------------------------------------- peers
def _peer(chat_id: int):
    """Build an InputPeer straight from a Bot-API chat id (no cache needed)."""
    cid = int(chat_id)
    if cid >= 0:
        return types.InputPeerUser(user_id=cid, access_hash=0)
    s = str(cid)
    if s.startswith("-100"):
        return types.InputPeerChannel(channel_id=int(s[4:]), access_hash=0)
    return types.InputPeerChat(chat_id=-cid)


async def _resolve(chat_id: int):
    """Prefer a cached/real entity, fall back to a synthetic peer."""
    try:
        return await _client.get_input_entity(int(chat_id))
    except Exception:
        return _peer(chat_id)


#: Errors that mean "this particular call was wrong" — never a reason to
#: disable the whole rich layer.
_SOFT_ERRORS = (
    "RICH_MESSAGE_CONTENT_REQUIRED", "RICH_MESSAGE_EMPTY", "MESSAGE_EMPTY",
    "MESSAGE_NOT_MODIFIED", "FLOOD", "TIMEOUT", "SLOWMODE",
    "MESSAGE_ID_INVALID", "PEER_ID_INVALID", "CHAT_WRITE_FORBIDDEN",
    "USER_IS_BLOCKED", "TOPIC_CLOSED",
)
#: Errors that really mean the DC/layer does not support rich messages.
_HARD_ERRORS = ("CONSTRUCTOR", "LAYER", "METHOD_INVALID", "RICH_MESSAGE_INVALID",
                "RICH_MESSAGE_UNSUPPORTED", "INPUT_RICH")


def _mark_unsupported(exc: Exception) -> None:
    global _rich_supported
    msg = str(exc).upper()
    if any(tok in msg for tok in _SOFT_ERRORS):
        return
    for token in _HARD_ERRORS:
        if token in msg:
            _rich_supported = False
            log.warning("Rich messages disabled at runtime: %s", exc)
            return


async def _call(request, timeout: float = 25.0):
    """Invoke a Telethon request with a hard timeout (never hangs the bot)."""
    return await asyncio.wait_for(_client(request), timeout=timeout)


def _plain(markdown: str) -> str:
    """Fallback message body Telegram shows on old clients."""
    from .utils import clean_text
    text = (clean_text(markdown) or markdown or "").strip()
    return (text or "…")[:MAX_RICH_LEN]


# ------------------------------------------------------------------ sending
async def send_markdown(chat_id: int, markdown: str, reply_to: int | None = None):
    """Send a native Rich Message. Returns the message id, or None on failure."""
    if not available() or not (markdown or "").strip():
        return None
    md = markdown[:MAX_RICH_LEN]
    try:
        kw = {}
        if reply_to:
            kw["reply_to"] = types.InputReplyToMessage(reply_to_msg_id=int(reply_to))
        res = await _call(functions.messages.SendMessageRequest(
            peer=await _resolve(chat_id),
            message=_plain(md),
            random_id=helpers.generate_random_long(),
            rich_message=types.InputRichMessageMarkdown(markdown=md),
            no_webpage=True,
            **kw,
        ), timeout=30.0)
        return _extract_id(res)
    except asyncio.TimeoutError:
        log.debug("send_markdown timed out")
        return None
    except Exception as e:
        _mark_unsupported(e)
        log.debug("send_markdown failed: %s", e)
        return None


async def edit_markdown(chat_id: int, message_id: int, markdown: str) -> bool:
    if not available() or not (markdown or "").strip():
        return False
    md = markdown[:MAX_RICH_LEN]
    try:
        await _call(functions.messages.EditMessageRequest(
            peer=await _resolve(chat_id),
            id=int(message_id),
            message=_plain(md),
            rich_message=types.InputRichMessageMarkdown(markdown=md),
            no_webpage=True,
        ), timeout=20.0)
        return True
    except asyncio.TimeoutError:
        return False
    except Exception as e:
        _mark_unsupported(e)
        log.debug("edit_markdown failed: %s", e)

        return False


def _extract_id(res) -> int | None:
    try:
        mid = getattr(res, "id", None)
        if isinstance(mid, int):
            return mid
    except Exception:
        pass
    for attr in ("updates", "update"):
        ups = getattr(res, attr, None)
        if ups is None:
            continue
        for u in (ups if isinstance(ups, list) else [ups]):
            m = getattr(u, "message", None)
            if m is not None and hasattr(m, "id"):
                return m.id
            mid = getattr(u, "id", None)
            if isinstance(mid, int):
                return mid
    return None


def _blocks(text: str) -> list[str]:
    return [b.strip() for b in (text or "").split("\n\n") if b.strip()]


async def stream_markdown(chat_id: int, text: str, reply_to: int | None = None,
                          delay: float = STREAM_DELAY):
    """Send the answer, revealing it block by block via message edits."""
    parts = _blocks(text)
    if not parts:
        return None
    mid = await send_markdown(chat_id, parts[0], reply_to=reply_to)
    if mid is None:
        return None
    for i in range(2, len(parts) + 1):
        await asyncio.sleep(delay)
        if not await edit_markdown(chat_id, mid, "\n\n".join(parts[:i])):
            break
    return mid


# ------------------------------------------------------------- live drafts
class Draft:
    """Live rich draft (private chats only — Telegram restriction)."""

    def __init__(self, chat_id: int):
        self.chat_id = int(chat_id)
        self.random_id = abs(helpers.generate_random_long()) if _TELETHON_OK else 0
        self._peer = None
        self._task: asyncio.Task | None = None
        self._html_thinking = True   # falls back to markdown if the DC refuses
        self.ok = available() and self.chat_id >= 0

    async def _push(self, rich) -> bool:
        if not self.ok:
            return False
        try:
            if self._peer is None:
                self._peer = await _resolve(self.chat_id)
            await _call(functions.messages.SetTypingRequest(
                peer=self._peer,
                action=types.InputSendMessageRichMessageDraftAction(
                    random_id=self.random_id,
                    rich_message=rich,
                ),
            ), timeout=10.0)
            return True
        except asyncio.TimeoutError:
            return False
        except Exception as e:
            up = str(e).upper()
            if "RICH_MESSAGE_CONTENT_REQUIRED" in up:
                # Empty/unsupported draft body — degrade this draft only,
                # never the global rich layer.
                self._html_thinking = False
                log.debug("draft content rejected, switching to markdown drafts")
                return False
            _mark_unsupported(e)
            self.ok = False
            log.debug("draft push failed: %s", e)
            return False

    async def html(self, html_text: str) -> bool:
        if not (html_text or "").strip():
            return False
        return await self._push(types.InputRichMessageHTML(html=html_text))

    async def markdown(self, md: str) -> bool:
        md = (md or "").strip()
        if not md:
            return False
        return await self._push(types.InputRichMessageMarkdown(markdown=md[:MAX_RICH_LEN]))

    async def thinking(self, label: str = "Thinking") -> bool:
        text = f"{label}…"
        if self._html_thinking:
            if await self.html(f"<tg-thinking>{text}</tg-thinking>"):
                return True
            if not self.ok:
                return False
        return await self.markdown(f"> _{text}_")



    def start_thinking(self, labels: list[str] | None = None, interval: float = 0.9):
        """Background 'thinking' animation until stop() is called."""
        labels = labels or ["🔍 Searching", "🧠 Analyzing", "✍️ Writing"]

        async def _loop():
            i = 0
            try:
                while self.ok:
                    if not await self.thinking(labels[i % len(labels)]):
                        return
                    i += 1
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            except Exception:
                return

        if self.ok:
            self._task = asyncio.create_task(_loop())
        return self._task

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
            self._task = None

    async def stream(self, text: str, delay: float = STREAM_DELAY) -> None:
        """Reveal the answer block-by-block inside the live draft."""
        await self.stop()
        parts = _blocks(text)
        for i in range(1, len(parts) + 1):
            if not await self.markdown("\n\n".join(parts[:i])):
                return
            await asyncio.sleep(delay)


# ---------------------------------------------------------------- slideshow
def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


async def _upload_photo(peer, source, name: str = "img.jpg"):
    """source: url string or raw bytes -> InputPhoto"""
    data = source if isinstance(source, (bytes, bytearray)) else await asyncio.to_thread(_fetch, source)
    file = await asyncio.wait_for(
        _client.upload_file(io.BytesIO(bytes(data)), file_name=name), timeout=90.0)
    res = await _call(functions.messages.UploadMediaRequest(
        peer=peer, media=types.InputMediaUploadedPhoto(file=file),
    ), timeout=90.0)
    p = res.photo
    return types.InputPhoto(id=p.id, access_hash=p.access_hash, file_reference=p.file_reference)



async def send_slideshow(chat_id: int, images: list, caption: str = "",
                         reply_to: int | None = None):
    """
    Native Telegram image slider (PageBlockSlideshow rich message).

    `images` items: url|bytes, or (url|bytes, caption) tuples.
    Returns message id or None.
    """
    if not available() or not images:
        return None
    try:
        peer = await _resolve(chat_id)
        photos, items = [], []
        for entry in images[:10]:
            if isinstance(entry, (tuple, list)):
                src, cap = entry[0], (entry[1] if len(entry) > 1 else "")
            else:
                src, cap = entry, ""
            photo = await _upload_photo(peer, src)
            photos.append(photo)
            items.append(types.PageBlockPhoto(
                photo_id=photo.id,
                caption=types.PageCaption(
                    text=types.TextPlain(text=str(cap or "")),
                    credit=types.TextEmpty(),
                ),
            ))
        if not items:
            return None
        rich = types.InputRichMessage(
            blocks=[types.PageBlockSlideshow(
                items=items,
                caption=types.PageCaption(
                    text=types.TextPlain(text=caption or ""),
                    credit=types.TextEmpty(),
                ),
            )],
            photos=photos,
        )
        kw = {}
        if reply_to:
            kw["reply_to"] = types.InputReplyToMessage(reply_to_msg_id=int(reply_to))
        res = await _call(functions.messages.SendMessageRequest(
            peer=peer,
            message=caption or "slideshow",
            random_id=helpers.generate_random_long(),
            rich_message=rich,
            **kw,
        ), timeout=60.0)
        return _extract_id(res)
    except asyncio.TimeoutError:
        log.debug("send_slideshow timed out")
        return None
    except Exception as e:
        _mark_unsupported(e)
        log.debug("send_slideshow failed: %s", e)

        return None


async def send_composed(chat_id: int, markdown: str, images: list,
                        placement: str = "inside_top", caption: str = ""):
    """Send markdown and uploaded media as one native Rich Message."""
    if not available() or not images:
        return None
    try:
        peer = await _resolve(chat_id)
        files, media_tags = [], []
        for index, entry in enumerate(images[:10], 1):
            source = entry[0] if isinstance(entry, (tuple, list)) else entry
            photo = await _upload_photo(peer, source, name=f"slide-{index}.jpg")
            media_id = f"slide{index}"
            files.append(types.InputRichFilePhoto(id=media_id, photo=photo))
            media_tags.append(f'<img src="tg://photo?id={media_id}">')
        if not files:
            return None
        safe_caption = (caption or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        slider = "<tg-slideshow>\n" + "\n".join(media_tags)
        if safe_caption:
            slider += f"\n<figcaption>{safe_caption}</figcaption>"
        slider += "\n</tg-slideshow>"
        text = (markdown or "").strip()
        composed = (f"{slider}\n\n{text}" if placement == "inside_top"
                    else f"{text}\n\n{slider}")
        result = await _call(functions.messages.SendMessageRequest(
            peer=peer,
            message=_plain(text or caption or "slideshow"),
            random_id=helpers.generate_random_long(),
            rich_message=types.InputRichMessageMarkdown(
                markdown=composed[:32768], files=files),
            no_webpage=True,
        ), timeout=90.0)
        return _extract_id(result)
    except asyncio.TimeoutError:
        log.debug("send_composed timed out")
        return None
    except Exception as e:
        _mark_unsupported(e)
        log.debug("send_composed failed: %s", e)
        return None
