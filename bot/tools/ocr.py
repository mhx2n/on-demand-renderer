"""OCR tool — /ocr (reply to a photo or image document).
Shares key + daily-limit infra with /tr.
"""
from __future__ import annotations

import io
from typing import Optional

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from . import _mistral
from .. import richsend
from ..config import OWNER_ID
from ..utils import clean_text, format_ai_answer, safe_user_error

MAX_BYTES = 10 * 1024 * 1024  # 10 MB cap
MAX_OUT_CHARS = 3500  # Telegram message safety; longer goes as a file


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _is_owner(uid: int) -> bool:
    return uid == OWNER_ID


async def _get_image(update: Update, context: ContextTypes.DEFAULT_TYPE
                     ) -> tuple[Optional[bytes], str]:
    """Pull image bytes + mime from the replied message or current message."""
    msg = update.effective_message
    src = msg.reply_to_message or msg
    file_id = None
    mime = "image/jpeg"

    if src.photo:
        file_id = src.photo[-1].file_id
        mime = "image/jpeg"
    elif src.document and (src.document.mime_type or "").startswith("image/"):
        if src.document.file_size and src.document.file_size > MAX_BYTES:
            return None, ""
        file_id = src.document.file_id
        mime = src.document.mime_type or "image/jpeg"
    if not file_id:
        return None, ""

    f = await context.bot.get_file(file_id)
    buf = io.BytesIO()
    await f.download_to_memory(buf)
    data = buf.getvalue()
    if len(data) > MAX_BYTES:
        return None, ""
    return data, mime


def _parse_target(raw: str) -> Optional[str]:
    """Optional language argument: /ocr en  → translate result to English."""
    from .translate import LANG_NAMES
    parts = raw.split(None, 2)
    if len(parts) < 2:
        return None
    code = parts[1].strip().lower().lstrip("/")
    if code in LANG_NAMES:
        return LANG_NAMES[code]
    if len(code) <= 24 and code.replace("-", "").replace("_", "").isalpha():
        return code.capitalize()
    return None


async def cmd_ocr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid = update.effective_user.id

    # Key check first
    key = await _mistral.get_key()
    if not key:
        await msg.reply_text(
            "OCR is currently unavailable.\n"
            "The bot owner has not configured the AI engine yet."
        )
        return

    # Image required
    img_bytes, mime = await _get_image(update, context)
    if not img_bytes:
        await msg.reply_text(
            "Reply to a photo or image document with /ocr.\n\n"
            "Tips:\n"
            "• Use a clear, well-lit image\n"
            "• Avoid blurry / distorted text\n"
            "• Max 10 MB\n\n"
            "Optional: <code>/ocr &lt;lang&gt;</code> to also translate the extracted text.\n"
            "Example: <code>/ocr en</code> (reply to a Bangla image).",
            parse_mode=ParseMode.HTML,
        )
        return

    # Quota — owner unlimited
    if not _is_owner(uid):
        ok, used, limit = await _mistral.check_quota(uid, "ocr")
        if not ok:
            await msg.reply_text(
                f"Daily OCR limit reached ({limit}/day).\n"
                "Please try again tomorrow."
            )
            return

    target_lang = _parse_target(msg.text or "")
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    placeholder = await msg.reply_text("🔍 Extracting text from image…")

    prompt = (
        "You are a high-accuracy OCR + document-structuring engine. Read EVERY "
        "visible element of this image and reproduce it as Telegram Rich "
        "Message Markdown:\n"
        "• Keep the original language, wording, order and line breaks.\n"
        "• Tabular content → a GitHub pipe table with an alignment row.\n"
        "• Headings → '#'/'##', lists → '-' bullets or '1.' numbers, "
        "checkboxes → '- [ ]' / '- [x]'.\n"
        "• EVERY formula, equation, unit or mathematical symbol → LaTeX: "
        "$…$ inline and $$…$$ for display maths (\\frac, \\sqrt, \\int, \\sum, "
        "^, _). Never rewrite maths as plain ASCII.\n"
        "• Code / terminal text → a fenced ``` block. Handwriting → transcribe "
        "as-is.\n"
        "Do NOT translate, do NOT summarise, add no commentary. If there is no "
        "readable text, reply with exactly: NO_TEXT_FOUND"
    )

    try:
        raw = await _mistral.vision_extract(img_bytes, prompt, mime=mime, timeout=120)
        raw = (raw or "").strip()
        if raw.startswith("```") and raw.endswith("```") and raw.count("```") == 2:
            # unwrap an accidental whole-answer fence
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        if not raw or raw.upper().startswith("NO_TEXT_FOUND"):
            await placeholder.edit_text(
                "No readable text was found in this image.\n"
                "Try a clearer / higher-resolution photo."
            )
            return

        translated = ""
        if target_lang:
            try:
                await placeholder.edit_text("Text extracted. Translating…")
            except Exception:
                pass
            try:
                translated = await _mistral.chat(
                    messages=[
                        {"role": "system",
                         "content": f"You are a professional translator. Translate the user's text into {target_lang}. "
                                    "Keep ALL Markdown structure intact: tables stay tables, LaTeX "
                                    "($…$ / $$…$$) stays untouched, lists stay lists, code blocks stay "
                                    "verbatim. Reply with the translation only."},
                        {"role": "user", "content": raw},
                    ],
                    max_tokens=1800, temperature=0.2, timeout=60,
                )
                translated = (translated or "").strip()
            except Exception:
                translated = "[translation unavailable]"

        rich_md = f"## 🔎 OCR — Extracted\n\n{raw}"
        if translated:
            rich_md += f"\n\n## 🌐 Translation → *{target_lang}*\n\n{translated}"

        plain_len = len(rich_md)
        if plain_len <= MAX_OUT_CHARS:
            if await richsend.reply(msg, rich_md, placeholder=placeholder):
                return
            html_body = format_ai_answer(rich_md)
            try:
                await placeholder.edit_text(html_body, parse_mode=ParseMode.HTML,
                                            disable_web_page_preview=True)
                return
            except Exception:
                try:
                    await placeholder.edit_text(clean_text(rich_md)[:4000])
                    return
                except Exception:
                    pass

        # Long output -> send as .md file
        try:
            await placeholder.delete()
        except Exception:
            pass
        doc = io.BytesIO()
        doc.write(rich_md.encode("utf-8"))
        doc.seek(0)
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=doc, filename="ocr.md",
            caption="Extracted text (too long for a message).",
            reply_to_message_id=msg.message_id,
        )
    except Exception:
        try:
            await placeholder.edit_text(safe_user_error("OCR"))
        except Exception:
            pass



def register(app: Application):
    app.add_handler(CommandHandler("ocr", cmd_ocr))
