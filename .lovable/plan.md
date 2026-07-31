# Plan — Bot API 10.1 "Markup Revolution" for AI replies

Scope per your answer: **only Markup Revolution**, applied to AI replies from every provider (Gemini, Perplexity, Copilot, custom OpenAI/Anthropic/Gemini). Guard Bots & Polls skipped.

## Reality check (important)

- Bot API 10.1 (June 2026) ships new native block-level entities (headings, dividers, lists, tables, LaTeX, collapsible details, media collages, etc.). The current dependency is `python-telegram-bot==21.6` (Oct 2024) which **does not know these entity types** as typed objects.
- Telegram's existing `parse_mode=HTML` parser still accepts new constructs only if Telegram has shipped HTML tags for them. Many of the 10.1 features require sending raw `entities[]` with new `type` values.
- Best-effort strategy: maximize what we can render via PTB 21.6's HTML parser today, and for the rest fall back to a graceful pseudo-rendering (Unicode rules, bullet glyphs, monospace tables) so nothing breaks if Telegram hasn't enabled the tag on a given client.

If/when you upgrade PTB to a version that exposes the new entity classes, we can swap the fallbacks for native entities behind the same helper — call sites won't change.

## What changes (files)

### 1. `bot/utils.py` — new `format_ai_answer` (v2)
Rewrite the markdown→Telegram-HTML converter so AI output renders the new structures. Same function name, same call sites, no caller changes.

Supported conversions:
- **Headings** `#`..`######` → `<b>` with Unicode size cues (`▎ ` rail + bold; H1 also uppercased), separated by blank lines. Wraps in `<blockquote>` for H1 to act like a page banner.
- **Dividers** `---` / `***` → full-width `────────────` rule line.
- **Paragraphs** — preserved, collapses 3+ newlines to 2.
- **Lists** — `-`, `*`, `+` → `• `; numbered lists keep numbering; nested lists indented with `  ↳ `. Task lists `- [ ]` / `- [x]` → `☐` / `☑`.
- **Tables** (GitHub-style `| a | b |`) → monospace `<pre>` block with column alignment (`:--`, `--:`, `:-:`) honored, box-drawing borders (`┌─┬─┐`), striped rows via spacing. Caption line `Table: ...` rendered as italic line above.
- **Block quotes** `>` → `<blockquote>`; `>>` (pull quote) → `<blockquote expandable>` with leading `❝`.
- **Collapsible details** `<details><summary>X</summary>…</details>` → `<blockquote expandable><b>X</b>\n…</blockquote>`.
- **Inline styles**: `**bold**`, `*italic*` / `_italic_`, `__underline__`, `~~strike~~` → `<s>`, `||spoiler||` → `<tg-spoiler>`, `` `code` `` → `<code>`, fenced ```lang blocks → `<pre><code class="language-…">`.
- **Sub/superscript**: `H~2~O` / `x^2^` → Unicode subscript/superscript map fallback (since PTB 21.6 has no tg-sub/tg-sup).
- **LaTeX**: `$inline$` and `$$block$$` → `<code>` (inline) / `<pre>` (block) with a `🧮 ` prefix and lightweight greek/operator substitutions (\\alpha→α, \\sum→∑, ^2→²) so it's readable even where MathML isn't rendered.
- **Anchors / in-document links** `[text](#anchor)` → `<b>text</b>` (no jumping, but visually marked). External `[text](http…)` → `<a href>`.
- **Maps** `!map[lat,lng]` → emits a special marker `📍 lat,lng` plus the helper returns sidecar metadata so the sender can `send_location` after the text (handled in step 3).
- **Media blocks** `![alt](url "credit")` → sidecar entry the sender uses to call `send_photo`/`send_video`/`send_audio` with caption+credit; in-text replaced with `🖼 alt`.
- **Slideshow / collage** consecutive media blocks separated by blank line → one `send_media_group` call.

All HTML-unsafe text is escaped; code-fence content is stashed first so substitutions don't touch it.

### 2. `bot/utils.py` — new return shape
`format_ai_answer(text)` keeps returning a `str` (backwards compatible). Add `format_ai_answer_rich(text) -> {text, attachments: [...], locations: [...]}` for callers that want the media/maps sidecar. Existing callers keep working unchanged.

### 3. `bot/handlers.py` — opt-in rich send for AI provider replies
Two call sites send AI provider output:
- `cmd_ai` flow around line 1129 (`/g`, `/pr`, `/co`, custom providers).
- Mention/guest handler streaming path (uses `safe_edit` / `stream_edit`).

For these two paths only (not for unrelated reply_text calls), switch to:
```python
rich = format_ai_answer_rich(answer)
await send_rich(message, rich)  # new helper in handlers.py
```
`send_rich` chunks `rich.text` with existing `chunk_text`, sends each chunk with `parse_mode=HTML`, then sends any `attachments` as `send_media_group` and any `locations` as `send_location`. Streaming variant edits the first chunk while delta grows, attachments sent once at finalization.

Other reply_text sites (admin, key inspect, ping, etc.) are not touched.

### 4. `bot/providers/__init__.py` — optional provider hint
Prepend a soft system hint to every provider's history payload telling the model it may use the new markup (headings, tables, LaTeX, spoilers, details, task lists, `!map[…]`, captioned media). This is appended only when the user hasn't supplied their own system prompt. Each provider factory gets the same one-liner so output is consistent across Gemini / Perplexity / Copilot / OpenAI-compat / Anthropic / Gemini-API.

### 5. Safety / fallback
- If `send_message` raises `BadRequest: can't parse entities` for a chunk, retry once with `parse_mode=None` and pre-stripped text via `clean_text`. Logged, never user-visible.
- Unicode sub/sup map covers digits + common letters; anything outside falls back to `_x` / `^x` literal.
- Table renderer truncates cells to keep total line ≤ ~70 chars so mobile (your 360px viewport) doesn't wrap awkwardly.

## Out of scope (per your choice)
- Guard Bots join-request handling.
- Poll option external-link media.
- Upgrading `python-telegram-bot` past 21.6.

## Verification
- Add a tiny `scripts/preview_markup.py` (local dev only, not wired into bot) that feeds a representative markdown sample through `format_ai_answer_rich` and prints the HTML + sidecar JSON, so you can eyeball before deploying.
- Build runs through the existing Procfile / `main.py` unchanged.

Approve and I'll implement.
