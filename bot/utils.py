import re
import os
import time
import html

# ---------------------------------------------------------------------------
# Bot API 10.1 "Markup Revolution" — rich AI-reply renderer.
# Converts loose AI-markdown into Telegram-safe HTML using every block-level
# and inline cue Telegram currently parses (b/i/u/s/code/pre/blockquote/
# tg-spoiler/a), plus Unicode fallbacks for things python-telegram-bot 21.6's
# HTML parser does not yet expose as tags (headings, dividers, tables,
# subscript/superscript, LaTeX, task lists, maps, media, collapsible details,
# pull quotes).
#
# Strategy: every HTML fragment we ourselves produce is stashed as an opaque
# placeholder BEFORE the bulk html.escape pass, then restored verbatim. That
# way we never have to "un-escape" tags, and user text inside our blocks is
# always escaped exactly once.
# ---------------------------------------------------------------------------

_HTML_TAG = re.compile(r"<[^>]+>")
_MULTI_NL = re.compile(r"\n{3,}")
_LATEX_BLOCK = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_LATEX_INLINE = re.compile(r"\\\((.*?)\\\)|\\\[(.*?)\\\]|(?<!\$)\$(?!\$)([^\n$]+?)(?<!\$)\$(?!\$)", re.DOTALL)
_CODE_FENCE = re.compile(r"```(\w*)\n?(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_STRIP_INLINE = re.compile(r"[`*_~#>|]")

_RULE = "─" * 28

# Unicode sub/superscript maps for ~x~ / ^x^ fallbacks.
_SUP = str.maketrans(
    "0123456789+-=()abcdefghijklmnoprstuvwxyzABDEGHIJKLMNOPRTUVW",
    "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻᴬᴮᴰᴱᴳᴴᴵᴶᴷᴸᴹᴺᴼᴾᴿᵀᵁⱽᵂ",
)
_SUB = str.maketrans(
    "0123456789+-=()aehijklmnoprstuvx",
    "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ",
)

_LATEX_TOKENS = {
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ",
    r"\epsilon": "ε", r"\zeta": "ζ", r"\eta": "η", r"\theta": "θ",
    r"\iota": "ι", r"\kappa": "κ", r"\lambda": "λ", r"\mu": "μ",
    r"\nu": "ν", r"\xi": "ξ", r"\pi": "π", r"\rho": "ρ",
    r"\sigma": "σ", r"\tau": "τ", r"\upsilon": "υ", r"\phi": "φ",
    r"\chi": "χ", r"\psi": "ψ", r"\omega": "ω",
    r"\Gamma": "Γ", r"\Delta": "Δ", r"\Theta": "Θ", r"\Lambda": "Λ",
    r"\Xi": "Ξ", r"\Pi": "Π", r"\Sigma": "Σ", r"\Phi": "Φ",
    r"\Psi": "Ψ", r"\Omega": "Ω",
    r"\sum": "∑", r"\prod": "∏", r"\int": "∫", r"\partial": "∂",
    r"\infty": "∞", r"\nabla": "∇", r"\pm": "±", r"\mp": "∓",
    r"\times": "×", r"\cdot": "·", r"\div": "÷",
    r"\leq": "≤", r"\geq": "≥", r"\neq": "≠", r"\approx": "≈",
    r"\equiv": "≡", r"\rightarrow": "→", r"\leftarrow": "←",
    r"\Rightarrow": "⇒", r"\Leftarrow": "⇐", r"\to": "→",
    r"\in": "∈", r"\notin": "∉", r"\subset": "⊂", r"\supset": "⊃",
    r"\cup": "∪", r"\cap": "∩", r"\forall": "∀", r"\exists": "∃",
    r"\ldots": "…", r"\cdots": "⋯",
}


def _latex_to_unicode(expr: str) -> str:
    """Best-effort LaTeX → Unicode (keeps unknown bits readable)."""
    t = expr
    t = re.sub(r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}", r"(\1)/(\2)", t)
    t = re.sub(r"\\sqrt\s*\{([^{}]+)\}", r"√(\1)", t)
    for k in sorted(_LATEX_TOKENS, key=len, reverse=True):
        t = t.replace(k, _LATEX_TOKENS[k])
    t = re.sub(r"\^\{([^{}]+)\}", lambda m: m.group(1).translate(_SUP), t)
    t = re.sub(r"_\{([^{}]+)\}", lambda m: m.group(1).translate(_SUB), t)
    t = re.sub(r"\^(\w)", lambda m: m.group(1).translate(_SUP), t)
    t = re.sub(r"_(\w)", lambda m: m.group(1).translate(_SUB), t)
    t = t.replace("{", "").replace("}", "")
    return t.strip()


# ---------------------------------------------------------------------------
# Professional-output sanitizer: removes model artefacts and chatty
# meta-commentary so every answer reads as a clean, finished document.
# ---------------------------------------------------------------------------
_ARTEFACT_PATTERNS = (
    re.compile(r"\{\s*/\*.*?\*/\s*\}", re.DOTALL),      # {/* Reason: ... */}
    re.compile(r"<!--.*?-->", re.DOTALL),               # HTML comments
    re.compile(r"^\s*\[Formatting rules[^\]]*\].*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*(?:Reason|Note to self|Internal note|Thought)\s*:\s.*$",
               re.MULTILINE | re.IGNORECASE),
)

#: Opening meta sentences (English + Bengali) that merely restate the request.
_META_LINE = re.compile(
    r"^\s*(?:"
    r"(?:sure|certainly|of course|absolutely|great question|good question)[!,.\s].*"
    r"|(?:since|as)\s+you\s+(?:asked|said|requested|mentioned|wrote)\b.*"
    r"|i(?:'ll| will| am going to)\s+(?:now\s+)?(?:give|provide|show|answer|solve)\b.*"
    r"|.{0,40}(?:যেহেতু)\s*(?:তুমি|আপনি|তুই)\s*(?:বলেছো|বলেছেন|চেয়েছো|চেয়েছেন|বললে|বলেছিলে).*"
    r"|(?:তুমি|আপনি|তুই)\s*(?:যেহেতু|যেভাবে)\s*(?:বলেছো|বলেছেন|চেয়েছো|চেয়েছেন).*"
    r"|(?:নিচে|নিম্নে)\s+.{0,60}(?:দিলাম|দেওয়া হলো|দেয়া হলো|উপস্থাপন করছি)\s*[—:\-]?\s*"
    r"|(?:আপনার|তোমার)\s+(?:প্রশ্ন|অনুরোধ)\s+অনুযায়ী.*"
    r")\s*$",
    re.IGNORECASE,
)


def sanitize_ai_text(text: str) -> str:
    """Strip artefacts + leading meta-commentary from a model answer."""
    if not text:
        return ""
    t = text.replace("\r\n", "\n")
    for pat in _ARTEFACT_PATTERNS:
        t = pat.sub("", t)

    lines = t.split("\n")
    while lines:                    # only strip meta while at the very top
        head = lines[0].strip()
        if not head:
            lines.pop(0)
            continue
        if _META_LINE.match(head):
            lines.pop(0)
            continue
        break
    t = "\n".join(lines)
    t = re.sub(r"[ \t]+$", "", t, flags=re.MULTILINE)
    t = _MULTI_NL.sub("\n\n", t)
    return t.strip()


_HEAD_MARK = {1: "🔹", 2: "▌", 3: "▸"}


def to_native_rich_markdown(text: str) -> str:
    """
    Normalise a model answer for Telegram's NATIVE Rich Message markdown.

    Native rich messages do render real headings, so `#`/`##`/`###` are kept
    (levels >3 are clamped to `###`), just tidied: single space after the
    hashes, no trailing hashes, no stray bold/italic markers in the title and
    a blank line before each heading.
    """
    t = sanitize_ai_text(text)
    if not t:
        return ""

    out: list[str] = []
    in_fence = False
    for line in t.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        m = re.match(r"^\s{0,3}(#{1,6})\s*(.+?)\s*#*\s*$", line)
        if m:
            level = min(len(m.group(1)), 3)
            title = m.group(2).strip().strip("*_ ").strip()
            if not title:
                continue
            if out and out[-1].strip():
                out.append("")
            out.append(f"{'#' * level} {title}")
            continue
        out.append(line)
    return _MULTI_NL.sub("\n\n", "\n".join(out)).strip()




def to_rich_markdown(text: str) -> str:
    """
    Normalise a model answer for Telegram's native Rich Message markdown.

    Telegram does not render ATX headings (`#`, `##`, `###`) — they show up as
    literal hashes — so they become bold heading lines instead.
    """
    t = sanitize_ai_text(text)
    if not t:
        return ""

    out: list[str] = []
    in_fence = False
    for line in t.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        m = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip().strip("*_ ")
            mark = _HEAD_MARK.get(level, "")
            if out and out[-1].strip():
                out.append("")
            out.append(f"**{mark} {title}**".replace("**  ", "** "))
            continue
        out.append(line)
    return _MULTI_NL.sub("\n\n", "\n".join(out)).strip()


def clean_text(text: str) -> str:
    """Strict plain-text fallback (no HTML)."""
    if not text:
        return ""
    t = sanitize_ai_text(text)
    t = _LATEX_BLOCK.sub(lambda m: _latex_to_unicode(m.group(1)), t)
    t = _LATEX_INLINE.sub(lambda m: _latex_to_unicode(next((g for g in m.groups() if g), "")), t)
    t = _HTML_TAG.sub("", t)
    t = _STRIP_INLINE.sub("", t)
    t = re.sub(r"^\s*[-+]\s+", "• ", t, flags=re.MULTILINE)
    t = _MULTI_NL.sub("\n\n", t)
    return t.strip()




def _render_table_html(lines: list[str]) -> str:
    rows = []
    for ln in lines:
        s = ln.strip()
        if s.startswith("|"):
            s = s[1:]
        if s.endswith("|"):
            s = s[:-1]
        rows.append([c.strip() for c in s.split("|")])
    if not rows:
        return ""
    align_row = None
    if len(rows) >= 2 and all(re.fullmatch(r":?-+:?", (c or "-").strip()) for c in rows[1]):
        align_row = rows[1]
        header, body = rows[0], rows[2:]
        aligns = []
        for c in align_row:
            l, r = c.startswith(":"), c.endswith(":")
            aligns.append("center" if (l and r) else "right" if r else "left")
    else:
        header, body = rows[0], rows[1:]
        aligns = ["left"] * len(rows[0])

    ncols = max(len(header), max((len(r) for r in body), default=0))
    header += [""] * (ncols - len(header))
    body = [r + [""] * (ncols - len(r)) for r in body]
    aligns += ["left"] * (ncols - len(aligns))

    max_w = 18
    def _trim(s):
        s = (s or "").replace("\n", " ")
        return s if len(s) <= max_w else s[: max_w - 1] + "…"

    header = [_trim(c) for c in header]
    body = [[_trim(c) for c in r] for r in body]

    widths = [len(header[i]) for i in range(ncols)]
    for r in body:
        for i in range(ncols):
            widths[i] = max(widths[i], len(r[i]))

    def _fmt(text, w, a):
        if a == "right": return text.rjust(w)
        if a == "center": return text.center(w)
        return text.ljust(w)

    def _line(left, mid, right):
        return left + mid.join("─" * (w + 2) for w in widths) + right

    out = [_line("┌", "┬", "┐"),
           "│ " + " │ ".join(_fmt(header[i], widths[i], aligns[i]) for i in range(ncols)) + " │",
           _line("├", "┼", "┤")]
    for r in body:
        out.append("│ " + " │ ".join(_fmt(r[i], widths[i], aligns[i]) for i in range(ncols)) + " │")
    out.append(_line("└", "┴", "┘"))
    return "<pre>" + html.escape("\n".join(out), quote=False) + "</pre>"


def _render_heading_html(level: int, text: str) -> str:
    text = html.escape(text.strip(), quote=False)
    if level == 1:
        return f"<blockquote><b>▎ {text.upper()}</b></blockquote>"
    if level == 2:
        return f"<b>▌ {text}</b>"
    if level == 3:
        return f"<b>▸ {text}</b>"
    return f"<b><i>{text}</i></b>"


def _inline_after_escape(t: str) -> str:
    """Apply inline transforms on already-HTML-escaped text."""
    # Spoiler ||x||
    t = re.sub(r"\|\|(.+?)\|\|", r"<tg-spoiler>\1</tg-spoiler>", t)
    # Strikethrough ~~x~~
    t = re.sub(r"~~(.+?)~~", r"<s>\1</s>", t)
    # Bold **x** / __x__
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t, flags=re.DOTALL)
    t = re.sub(r"__(.+?)__", r"<b>\1</b>", t, flags=re.DOTALL)
    # Italic _x_ / *x*
    t = re.sub(r"(?<!\w)_(?!\s)([^_\n]+?)(?<!\s)_(?!\w)", r"<i>\1</i>", t)
    t = re.sub(r"(?<!\*)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)", r"<i>\1</i>", t)
    # Subscript ~x~ / Superscript ^x^
    t = re.sub(r"~([A-Za-z0-9+\-=()]{1,8})~",
               lambda m: m.group(1).translate(_SUB), t)
    t = re.sub(r"\^([A-Za-z0-9+\-=()]{1,8})\^",
               lambda m: m.group(1).translate(_SUP), t)

    # Image ![alt](url "credit") — must run BEFORE plain link.
    def _img(m):
        alt = m.group(1) or "image"
        url = m.group(2)
        credit = m.group(3) or ""
        out = f'🖼 <a href="{url}">{alt}</a>'
        if credit:
            out += f' <i>— {credit}</i>'
        return out
    t = re.sub(
        r'!\[([^\]\n]*)\]\(([^)\s]+)(?:\s+(?:&quot;|")([^&"]*)(?:&quot;|"))?\)',
        _img, t,
    )

    # Markdown link [text](url) — # anchors become bold-only.
    def _link(m):
        text, url = m.group(1), m.group(2)
        if url.startswith("#"):
            return f"<b>{text}</b>"
        return f'<a href="{url}">{text}</a>'
    t = re.sub(r"\[([^\]\n]+)\]\(([^)\s]+)\)", _link, t)
    # Map !map[lat,lng]
    t = re.sub(r"!map\[([^\]]+)\]",
               lambda m: f"📍 <code>{html.escape(m.group(1).strip(), quote=False)}</code>", t)
    return t


def format_ai_answer(text: str) -> str:
    """Convert AI markdown-ish output → Telegram-safe rich HTML."""
    text = sanitize_ai_text(text)
    if not text:
        return ""



    # === Stash buckets — restored AFTER global html.escape ===
    html_blocks: list[str] = []          # opaque pre-built HTML
    code_blocks: list[tuple[str, str]] = []
    inline_codes: list[str] = []
    latex_blocks: list[str] = []
    latex_inlines: list[str] = []

    def _stash_html(frag: str) -> str:
        idx = len(html_blocks); html_blocks.append(frag)
        return f"\x00HB{idx}\x00"

    # 1) Fenced code blocks
    def _grab_code(m):
        lang = (m.group(1) or "").strip()
        code = m.group(2).rstrip()
        idx = len(code_blocks); code_blocks.append((lang, code))
        return f"\x00CB{idx}\x00"
    t = _CODE_FENCE.sub(_grab_code, text)

    # 2) LaTeX blocks + inline
    def _grab_lxb(m):
        idx = len(latex_blocks); latex_blocks.append(_latex_to_unicode(m.group(1)))
        return f"\x00LB{idx}\x00"
    def _grab_lxi(m):
        expr = next((g for g in m.groups() if g), "")
        idx = len(latex_inlines); latex_inlines.append(_latex_to_unicode(expr))
        return f"\x00LI{idx}\x00"
    t = _LATEX_BLOCK.sub(_grab_lxb, t)
    t = _LATEX_INLINE.sub(_grab_lxi, t)

    # 3) Inline `code`
    def _grab_ic(m):
        idx = len(inline_codes); inline_codes.append(m.group(1))
        return f"\x00IC{idx}\x00"
    t = _INLINE_CODE.sub(_grab_ic, t)

    # 4) Walk lines → mix of raw text and stashed HTML placeholders.
    lines = t.split("\n")
    out: list[str] = []
    i = 0

    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()

        # <details><summary>X</summary>...</details> (single or multi-line)
        m = re.match(r"<details>\s*<summary>(.*?)</summary>(.*)", stripped, re.IGNORECASE)
        if m:
            summary = m.group(1)
            inner_lines = []
            tail = m.group(2)
            # consume until </details>
            if "</details>" in tail.lower():
                inner_lines.append(re.sub(r"</details>.*", "", tail, flags=re.IGNORECASE))
                i += 1
            else:
                if tail.strip():
                    inner_lines.append(tail)
                i += 1
                while i < len(lines) and "</details>" not in lines[i].lower():
                    inner_lines.append(lines[i])
                    i += 1
                if i < len(lines):
                    last = re.sub(r"</details>.*", "", lines[i], flags=re.IGNORECASE)
                    if last.strip():
                        inner_lines.append(last)
                    i += 1
            inner_html = html.escape("\n".join(inner_lines).strip(), quote=False)
            out.append(_stash_html(
                f"<blockquote expandable><b>▼ {html.escape(summary, quote=False)}</b>\n{inner_html}</blockquote>"
            ))
            continue

        # Horizontal rule
        if re.fullmatch(r"\s*([-*_])\1{2,}\s*", raw):
            out.append(_stash_html(_RULE))
            i += 1
            continue

        # Heading
        m = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", raw)
        if m:
            out.append(_stash_html(_render_heading_html(len(m.group(1)), m.group(2))))
            i += 1
            continue

        # Table
        if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2:
            tbl: list[str] = []
            while i < len(lines):
                s = lines[i].strip()
                if s.startswith("|") and s.endswith("|") and s.count("|") >= 2:
                    tbl.append(lines[i])
                    i += 1
                else:
                    break
            out.append(_stash_html(_render_table_html(tbl)))
            continue

        # Quote (pull >>> or regular >)
        if stripped.startswith(">"):
            pull = stripped.startswith(">>")
            quote_lines: list[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                q = lines[i].strip().lstrip(">").lstrip()
                quote_lines.append(q)
                i += 1
            inner = html.escape("\n".join(quote_lines), quote=False)
            # Re-apply inline transforms inside quotes too.
            inner = _inline_after_escape(inner)
            if pull:
                out.append(_stash_html(f"<blockquote expandable>❝ {inner} ❞</blockquote>"))
            else:
                out.append(_stash_html(f"<blockquote>{inner}</blockquote>"))
            continue

        # Task list
        m = re.match(r"^(\s*)[-*+]\s+\[([ xX])\]\s+(.+)$", raw)
        if m:
            indent = "  " * (len(m.group(1)) // 2)
            mark = "☑" if m.group(2).lower() == "x" else "☐"
            out.append(f"{indent}{mark} {m.group(3)}")
            i += 1
            continue

        # Bullet list
        m = re.match(r"^(\s*)[-*+]\s+(.+)$", raw)
        if m:
            level = len(m.group(1)) // 2
            prefix = ("  " * level) + ("↳ " if level else "• ")
            out.append(f"{prefix}{m.group(2)}")
            i += 1
            continue

        # Numbered list
        m = re.match(r"^(\s*)(\d+)[.)]\s+(.+)$", raw)
        if m:
            level = len(m.group(1)) // 2
            out.append(("  " * level) + f"{m.group(2)}. {m.group(3)}")
            i += 1
            continue

        out.append(raw)
        i += 1

    t = "\n".join(out)

    # 5) Global HTML escape (placeholders survive — they're ASCII NULs).
    t = html.escape(t, quote=False)

    # 6) Inline transforms on escaped text.
    t = _inline_after_escape(t)

    # 7) Restore stashed HTML blocks.
    for idx, frag in enumerate(html_blocks):
        t = t.replace(f"\x00HB{idx}\x00", frag)

    # 8) Restore inline code.
    for idx, code in enumerate(inline_codes):
        safe = html.escape(code, quote=False)
        t = t.replace(f"\x00IC{idx}\x00", f"<code>{safe}</code>")

    # 9) Restore LaTeX.
    for idx, rendered in enumerate(latex_blocks):
        safe = html.escape(rendered, quote=False)
        t = t.replace(f"\x00LB{idx}\x00", f"<pre>🧮 {safe}</pre>")
    for idx, rendered in enumerate(latex_inlines):
        safe = html.escape(rendered, quote=False)
        t = t.replace(f"\x00LI{idx}\x00", f"<code>{safe}</code>")

    # 10) Restore fenced code blocks.
    for idx, (lang, code) in enumerate(code_blocks):
        safe_code = html.escape(code.replace("```", "''' "), quote=False)
        if lang:
            block = f'<pre><code class="language-{html.escape(lang, quote=True)}">{safe_code}</code></pre>'
        else:
            block = f"<pre>{safe_code}</pre>"
        t = t.replace(f"\x00CB{idx}\x00", block)

    t = _MULTI_NL.sub("\n\n", t)
    return t.strip()


def chunk_text(text: str, limit: int = 3800):
    """Split keeping line boundaries; tags rarely straddle 3800-char chunks."""
    text = text or ""
    if len(text) <= limit:
        yield text
        return
    buf = ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > limit:
            yield buf
            buf = ""
        buf += line
    if buf:
        yield buf


def escape_html(s: str) -> str:
    return html.escape(s or "", quote=False)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def safe_user_error(scope: str = "Request") -> str:
    return f"{scope} could not be completed right now. Please try again shortly."


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins:
        parts.append(f"{mins}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def process_metrics(started_at: int | None = None) -> dict:
    rss_bytes = 0
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss_kb = int(line.split()[1])
                    rss_bytes = rss_kb * 1024
                    break
    except Exception:
        pass

    try:
        load_avg = os.getloadavg()
    except Exception:
        load_avg = (0.0, 0.0, 0.0)

    now = int(time.time())
    return {
        "rss_bytes": rss_bytes,
        "load_1": load_avg[0],
        "load_5": load_avg[1],
        "load_15": load_avg[2],
        "cpu_count": os.cpu_count() or 1,
        "uptime_s": max(0, now - int(started_at or now)),
    }


# ---------------------------------------------------------------------------
# Shared "rich answer" instruction — appended to every AI prompt so that all
# providers (Gemini, Copilot, Perplexity, Mistral…) format maths, tables and
# structure exactly the way Telegram's Rich Messages render them.
# ---------------------------------------------------------------------------
RICH_PROMPT_HINT = (
    "[Formatting rules — follow strictly]\n"
    "Answer in Markdown that Telegram Rich Messages can render natively:\n"
    "• Headings `#`..`###`, **bold**, *italic*, ~~strike~~, ||spoiler||, "
    "`inline code`, ```fenced code```, > quotes, bullet / numbered / task lists.\n"
    "• Tables: GitHub pipe tables with an alignment row, ≤4 columns and short "
    "cells so they stay readable on mobile.\n"
    "• MATH IS MANDATORY IN LaTeX: wrap every inline formula, variable, unit or "
    "number-with-symbols in $…$ and every derivation, equation or multi-step "
    "result in a $$…$$ block. Never write maths as plain ASCII (no ^, no /, no "
    "sqrt()). Use \\frac, \\sqrt, \\int, \\sum, \\lim, \\times, \\cdot, "
    "\\Rightarrow, subscripts and superscripts. Show step-by-step derivations, "
    "each step on its own $$…$$ line, and put the final result in a "
    "$$\\boxed{…}$$ block.\n"
    "• Use <details><summary>…</summary>…</details> for long optional extras.\n"
    "• Answer in the same language the user wrote in.\n"
    "[Voice — strict]\n"
    "• Output ONLY the finished answer, like a published document.\n"
    "• Never restate, quote or reference the user's request (no \"since you "
    "asked\", no \"তুমি যেহেতু বলেছো\", no \"নিচে দিলাম\"), never open with "
    "Sure/Certainly/Of course, and never describe what you are about to do.\n"
    "• No meta-commentary, no self-reference, no notes about these rules, no "
    "code comments such as {/* … */} or <!-- … --> in the answer.\n"
    "• Start directly with the first heading or the first sentence of content."
)

