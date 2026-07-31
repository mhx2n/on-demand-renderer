"""
High-level delivery of AI answers as native Telegram Rich Messages.

Thin orchestration layer over `bot.richmsg` so both the provider handlers
and the guest/@mention handler share the exact same behaviour:

    draft = await begin(chat_id)        # live "thinking" draft (private only)
    mid   = await deliver(chat_id, answer_markdown, title=..., draft=draft)

`deliver()` returns the sent message id, or None when rich messages are not
available — in that case the caller falls back to its classic HTML path.
"""
from __future__ import annotations

import logging

from . import richmsg
from .config import RICH_MESSAGES

log = logging.getLogger("richsend")


def enabled() -> bool:
    return bool(RICH_MESSAGES and richmsg.available())


def _compose(raw: str, title: str | None) -> str:
    from .utils import to_rich_markdown
    body = to_rich_markdown(raw or "")
    if not body:
        return ""
    return f"**{title}**\n\n{body}" if title else body



async def begin(chat_id: int):
    """Start a live rich draft with a thinking animation (private chats)."""
    if not enabled():
        return None
    try:
        draft = richmsg.Draft(chat_id)
        if not draft.ok:
            return None
        draft.start_thinking()
        return draft
    except Exception as e:
        log.debug("draft begin failed: %s", e)
        return None


async def cancel(draft) -> None:
    if draft is not None:
        try:
            await draft.stop()
        except Exception:
            pass


async def deliver(chat_id: int, raw_markdown: str, title: str | None = None,
                  reply_to: int | None = None, draft=None, stream: bool = True):
    """
    Send `raw_markdown` as a native Rich Message.

    Returns the message id on success, None when the caller should fall back.
    """
    if not enabled():
        await cancel(draft)
        return None
    md = _compose(raw_markdown, title)
    if not md:
        await cancel(draft)
        return None

    try:
        if draft is not None and getattr(draft, "ok", False):
            await draft.stop()
            if stream:
                await draft.stream(md)
            mid = await richmsg.send_markdown(chat_id, md, reply_to=reply_to)
            if mid:
                return mid
        if stream:
            mid = await richmsg.stream_markdown(chat_id, md, reply_to=reply_to)
            if mid:
                return mid
        return await richmsg.send_markdown(chat_id, md, reply_to=reply_to)
    except Exception as e:
        log.debug("rich deliver failed: %s", e)
        return None
    finally:
        await cancel(draft)


async def reply(message, raw_markdown: str, title: str | None = None,
                placeholder=None, stream: bool = False) -> bool:
    """
    Try to answer `message` with a native Rich Message.

    Returns True when the rich message was sent (caller should stop), False
    when the caller must fall back to its classic HTML path.
    """
    if not enabled() or not (raw_markdown or "").strip():
        return False
    try:
        chat_id = message.chat_id if hasattr(message, "chat_id") else message.chat.id
        mid = await deliver(
            chat_id, raw_markdown, title=title,
            reply_to=getattr(message, "message_id", None), stream=stream,
        )
    except Exception as e:
        log.debug("rich reply failed: %s", e)
        return False
    if not mid:
        return False
    if placeholder is not None:
        try:
            await placeholder.delete()
        except Exception:
            pass
    return True


async def edit_or_reply(placeholder, message, raw_markdown: str,
                        title: str | None = None, stream: bool = False) -> bool:
    """Same as `reply` but deletes the given placeholder on success."""
    return await reply(message, raw_markdown, title=title,
                       placeholder=placeholder, stream=stream)


async def post(chat_id: int, raw_markdown: str, title: str | None = None):
    """Post a rich message to any chat/channel. Returns message id or None."""
    return await deliver(chat_id, raw_markdown, title=title, stream=False)
