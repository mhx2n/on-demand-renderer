<div align="center">

# 🤖 Renderix — Advanced Multi-AI Telegram Bot

**Production-grade · Async · Rich Formatting · 24/7 Ready**

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue?style=flat-square&logo=python)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Telegram-26A5E4?style=flat-square&logo=telegram)](https://telegram.org)
[![Deploy](https://img.shields.io/badge/Deploy-Render%2FVPS-purple?style=flat-square)](https://render.com)

</div>

---

## ✨ What is Renderix?

Renderix is a fully asynchronous, production-ready Telegram bot that brings **multiple AI providers**, **rich text formatting** (Bot API 10.1 Markup Revolution), **media tools**, and **utility commands** into one powerful companion.

> 🚀 **New:** Bot API 10.1 *Markup Revolution* support — AI replies now render with **headings, tables, LaTeX, lists, quotes, spoilers, task lists, and more**!

---

## 🧠 AI Providers

Talk to the best AI models with a single command:

| Command | Provider | Description |
|---------|----------|-------------|
| `/g` · `.g` | **Gemini** | Google Gemini chat |
| `/pr` · `.pr` | **Perplexity** | Perplexity AI with web search |
| `/co` · `.co` | **Copilot** | Microsoft Copilot chat |
| `/key` · `.key` | **API Inspector** | Inspect *any* AI API key — shows models, limits, quota |
| `/tryke` | **Try Model** | Run a prompt on the last-inspected key |

**Supported key inspection:** OpenAI, Anthropic, Google Gemini, Groq, OpenRouter, Cohere, DeepSeek, xAI, Together AI, and any OpenAI-compatible endpoint.

**Reply-to-continue:** Reply to any bot answer and the conversation continues in that provider's context.

---

## 🎨 Markup Revolution (Bot API 10.1)

AI replies are automatically formatted with Telegram's richest styling:

- 📝 **Multi-level headings** — H1–H3 with visual size cues
- 📊 **Tables** — GitHub-style markdown tables rendered as monospace grids
- 🧮 **LaTeX** — Inline `\( ... \)` and block `$$ ... $$` converted to Unicode math symbols
- ✅ **Task lists** — `☐` / `☑` checkboxes
- 💬 **Block quotes** — Expandable pull quotes with attribution
- 👻 **Spoilers** — Hidden content tap-to-reveal
- 🔗 **Links & anchors** — Clickable references
- 🗺️ **Maps** — `!map[lat,lng]` syntax → Telegram location messages
- 🖼️ **Captioned media** — AI-generated image descriptions with text
- 🎞️ **Slideshows** — Multi-image carousels from AI responses

> All formatting gracefully falls back to plain text if Telegram's parser rejects it.

---

## 🛠️ Complete Command List

### 🤖 AI Tools

| Command | Description |
|---------|-------------|
| `/g <prompt>` | Ask **Gemini** |
| `/pr <prompt>` | Ask **Perplexity** |
| `/co <prompt>` | Ask **Copilot** |
| `/key <API_KEY>` | Inspect any AI API key |
| `/tryke <model> <prompt>` | Try a model from inspected key |

### 📝 Text Tools

| Command | Description |
|---------|-------------|
| `/en <format> <text>` | Encode (Base64 / Hex / Binary / URL / ROT13) |
| `/de <format> <text>` | Decode from any common format |
| `/text <style> <text>` | Change case, reverse, etc. |
| `/wc <text>` | Word & character count |
| `/style <text>` | 49+ Unicode stylish fonts with live preview buttons |

### 🌐 Language Tools

| Command | Description |
|---------|-------------|
| `/spell <text>` | Spelling suggestions |
| `/gra <text>` | AI grammar correction |
| `/syn <word>` | Synonyms |
| `/prn <word>` | Phonetic + audio pronunciation |
| `/tr [lang] <text>` | AI translation (auto-detect or specify language) |
| `/ocr` | Extract text from replied photo (optional: translate) |

### 🖼️ Photo Tools

| Command | Description |
|---------|-------------|
| `/bg` | Remove background (reply to photo) |
| `/enh` | Sharpen + colour boost (reply to photo) |
| `/res` | Resize to presets: YouTube, Instagram, Twitter, HD, 4K |

### 🔧 Utilities

| Command | Description |
|---------|-------------|
| `/dl <url>` | Download **Facebook / Instagram / TikTok** video |
| `/dla <url>` | Extract audio (MP3) from social links |
| `/short <url>` | URL shortener |
| `/info` | Your Telegram account details |
| `/m2t [ext] [text]` | Convert text → 70+ file formats (txt, md, json, py, js, sql, docx...) |
| `/time <country>` | World clock + monthly calendar |
| `/vnote` | Convert reply-video → circular Telegram video note |
| `/convert [type] [value]` | Universal converter: number systems, encoding, units, hashing |
| `/top` | Top 10 most active users |
| `/ping` | Bot latency check |
| `/help [topic]` | AI-summarised help |

### 👑 Owner Commands (Hidden)

| Command | Description |
|---------|-------------|
| `/owner` | Owner menu |
| `/stats` | Users, messages, errors, channel stats |
| `/logs [n]` | Last `n` log entries |
| `/users` | Active user count |
| `/setchannel <user>` | Set/change force-join channel (`off` to disable) |
| `/ban <id>` · `/unban <id>` | Block / unblock users |
| `/announce <text>` | Broadcast to all users |
| `/addprovider` | Add custom AI provider at runtime |
| `/delprovider` | Remove custom provider |
| `/providers` | List all providers (built-in + custom) |

> **Tip:** Every command also works with a **dot prefix** — e.g., `.g hello` instead of `/g hello`

---

## 🔒 Guard Bot (Join Request Handler)

Renderix can act as a **guard bot** for your groups and channels:

- ✅ Auto-approve join requests
- ❌ Decline suspicious requests
- 📝 Queue requests for manual review
- 🧩 **Captcha challenge** — Simple math/emoji verification before approval

---

## 🚀 Quick Start

```bash
# 1. Clone & enter
git clone <repo> && cd <repo>

# 2. Configure
cp .env.example .env
# Edit .env: BOT_TOKEN, OWNER_ID, optionally FORCE_JOIN_CHANNEL

# 3. Install & run
pip install -r requirements.txt
python main.py
```

Health check: `http://localhost:10000/health`

---

## 🌐 Deploy to Render (Free Tier)

1. Push to GitHub.
2. On Render: **New → Blueprint** → point to this repo (`render.yaml` included).
3. Set env vars: `BOT_TOKEN`, `OWNER_ID`, optional `FORCE_JOIN_CHANNEL`.
4. Deploy. Render's health check hits `/` and keeps the service alive 24/7.

---

## 🖥️ Deploy to VPS

```bash
git clone <repo> && cd <repo>
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # edit
nohup python main.py > bot.log 2>&1 &
```

Or use the included `systemd.service.example` for a proper system service.

---

## ⚙️ Configuration (`.env`)

| Variable | Required | Description |
|----------|----------|-------------|
| `BOT_TOKEN` | ✅ | Telegram bot token from [@BotFather](https://t.me/BotFather) |
| `OWNER_ID` | ✅ | Your Telegram numeric user ID |
| `FORCE_JOIN_CHANNEL` | ❌ | Channel username (no @) for force-join gate |
| `PUBLIC_URL` | ❌ | Public URL shown in `/start` when deployed |
| `PORT` | ❌ | Web server port (default: `10000`) |
| `DB_PATH` | ❌ | SQLite path (default: `bot.db`) |
| `MONGODB_URI` | ❌ | Optional MongoDB for persistent settings |
| `YT_COOKIES_FILE` | ❌ | Path to exported cookies for restricted downloads |

---

## 🔌 Extending with New AI Providers

```python
# bot/providers/__init__.py
register("ds", "DeepSeek", deepseek.ask)
```

Drop a new module in `bot/providers/`, call `register()`, and the bot automatically picks it up — slash command, dot command, menu button, and reply-to-continue all work instantly.

---

## 🛡️ Safety & Limits

| Feature | Behaviour |
|---------|-----------|
| **Output sanitisation** | HTML tags, Markdown, LaTeX auto-escaped or converted |
| **Chunking** | Long answers split into 4000-character messages |
| **Timeouts** | All provider calls wrapped with 120s timeout |
| **Error logging** | Exceptions logged to DB, never silently swallowed |
| **Rate limiting** | Downloads run one-at-a-time to keep free hosting stable |
| **Force-join** | Configurable channel membership gate |
| **Ban system** | Owner can block/unblock users |

---

## 📁 Project Structure

```
.
├── bot/
│   ├── handlers.py          # All command handlers & UI
│   ├── utils.py              # Rich markup renderer (Bot API 10.1)
│   ├── db.py                 # SQLite persistence
│   ├── downloader.py         # Social media downloader
│   ├── keycheck.py           # API key inspector
│   ├── providers/            # AI provider adapters
│   │   ├── gemini.py
│   │   ├── perplexity.py
│   │   ├── copilot.py
│   │   └── __init__.py
│   └── tools/                # Text, language, photo tools
├── main.py                   # Entry point + Flask health server
├── requirements.txt
├── render.yaml               # Render Blueprint
├── systemd.service.example
└── README.md                 # You are here!
```

---

## 📜 License

MIT License — free to use, modify, and distribute.

---

<div align="center">

**Made with 💙 for the Telegram community**

[Report Issue](https://github.com/yourusername/renderix/issues) · [Request Feature](https://github.com/yourusername/renderix/issues) · [Donate](https://github.com/sponsors)

</div>
