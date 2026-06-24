# TradeMouth 🧠

![TradeMouth — Socratic AI Trading Mentor](assets/hero_image.png)

> **A Socratic AI trading mentor that lives in your Telegram chat.**

Built for the **Bitget AI × Crypto Trading Hackathon 2026** — Track 1: Trading Agent.

Most trading bots give you a signal and a moon emoji. They don't ask questions. They don't remember your past losses. They don't tell you when you're about to break your own rules.

TradeMouth does.

---

## ✨ What it does

- 🧠 **Asks first** — Socratic method, not signal-slinging
- 📊 **Pulls live data** — price, RSI, EMA, volume, Fear & Greed
- 🧠 **Remembers you** — every trade logged, every loss referenced
- 🛡️ **Safety first** — spot only, no leverage, no custody
- ✅ **You decide** — real trade only after typed "yes"
- 🔍 **Backtests** — historical simulation on real Binance data
- 📓 **Journal** — your full trade history with pattern detection
- 🎤 **Voice support** — transcribe voice messages via Whisper

---

## 🎬 Demo

**Watch it work:** https://youtube.com/shorts/JnTMlclFo_4

Try the bot: `@TradeMouthBot` on Telegram

---

## 🏗️ Architecture

```
User (Telegram)
    ↓
Telegram Bot API
    ↓
TradeMouth (Python 3.12+ on Render)
    ↓
┌────────────┬──────────────┬──────────────┬──────────────┐
│ Qwen 2.5    │  Binance     │  Bitget      │  OpenRouter  │
│ (LLM via    │  Public API  │  HMAC API    │  (gateway)   │
│ OpenRouter) │  (price/RSI) │  (execution) │              │
└────────────┴──────────────┴──────────────┴──────────────┘
    ↓
Journal (JSON in /tmp)
```

**Design principles:**
- **Non-custodial** — your money stays in your Bitget account, the bot never has withdraw permission
- **No library bloat** — zero heavy Python libraries, just `requests` + stdlib
- **Resilient polling** — Telegram long-polling with error recovery
- **Multi-endpoint fallback** — Binance blocks cloud IPs? We try 3 alternatives + CoinGecko

---

## 🚀 Quick start

### Try it (no setup)
Just message `@TradeMouthBot` on Telegram and type `/start`.

### Run your own

**1. Clone the repo**
```bash
git clone https://github.com/Donyemiight/trademouth-bot.git
cd trademouth-bot
```

**2. Get API keys**
- **Telegram bot token** — message @BotFather
- **OpenRouter API key** — sign up at openrouter.ai (gives you Qwen access)
- **Bitget API key** (optional) — bitget.com → API Management (Read + Spot Trade only)

**3. Set environment variables**
```bash
export TELEGRAM_BOT_TOKEN="your_telegram_token"
export OPENROUTER_API_KEY="sk-or-v1-your_key"
export BITGET_API_KEY="bg_your_key"        # optional
export BITGET_SECRET="your_secret"          # optional
export BITGET_PASSPHRASE="your_passphrase"  # optional
```

**4. Install & run**
```bash
pip install -r requirements.txt
python bot.py
```

**5. Deploy to Render (one-click)**
1. Fork this repo
2. Connect to render.com
3. Render auto-detects `render.yaml`
4. Set the 3 (or 5) env vars in dashboard
5. Service goes live in 2-3 min

---

## 💬 Commands

| Command | What it does |
|---|---|
| `/start` | Welcome message + inline buttons |
| `/help` | List all commands |
| `/about` | What makes TradeMouth different |
| `analyze <ASSET>` | Live read with RSI, F&G, EMA, volume |
| `thinking about buying <ASSET>` | Socratic analysis + trade suggestion |
| `set my portfolio to <amount>` | Set account size for position sizing |
| `set my max to <pct>%` | Cap max position size |
| `/journal` | Your full trade history |
| `/positions` | Open positions |
| `/stats` | Win rate, P&L, asset breakdown, patterns |
| `/balance` | Bitget spot balance |
| `/strategies` | 10 starter strategies |
| `/backtest <cond> on <asset> <days>d` | Historical strategy simulation |
| `/alerts` | List active alerts |
| `yes` / `no` | Confirm or cancel pending trade |
| 🎤 Voice messages | Transcribe via Whisper + respond |

---

## 🛡️ Safety rails (built in)

- ✅ Spot trading only (no leverage, ever)
- ✅ No withdraw permission on the Bitget API key
- ✅ Max position cap (default 2% of stated portfolio, hard max 10%)
- ✅ Pending trades expire after 5 minutes
- ✅ Real trade execution requires typed "yes" (button taps don't count)
- ✅ Socratic method — never says "guaranteed" or "you'll make"
- ✅ Always ends responses with a question, not an order
- ✅ Suggests "wait" when setup isn't there
- ✅ **Owner-only real trading** — only the bot owner's Telegram user id can place real orders; everyone else gets demo fills regardless of `DEMO_MODE`
- ✅ **Read-only public key** — balances/positions displayed to public users go through a separate API key with trade permissions disabled
- ✅ **Open-source security** — every security check is documented inline in `bot.py` and visible in the GitHub repo

Send `/security` to the bot to see the security model in action.

---

## 🧠 Example conversation

> **You:** thinking about buying SOL at 155
>
> **TradeMouth:**
> *Read on SOL:*
> - RSI 1h at 60, slightly overbought
> - Fear & Greed Index at 23, low market sentiment
> - Price above EMA 20, momentum intact
> - **Your past SOL trade (June 10) had +6.2% on the same setup**
> - But your last loss was chasing a pump after F&G was over 75
>
> **Lean:** wait
> **Shape (if applicable):** small long, 1%, stop $145, target $162
> **Question:** what's your edge here?

> **You:** yes
>
> **TradeMouth:** ✅ Executed. Order ID: 123456789. Entry: $155. Size: $5 USDT.
> View on Bitget: https://www.bitget.com/spot/SOLUSDT

---

## 🛠️ Tech stack

| Layer | Tool | Why |
|---|---|---|
| **Bot framework** | Pure Python + `requests` (no library bloat) | Zero version conflicts, works on any Python 3.10+ |
| **LLM** | Qwen 2.5 via OpenRouter | Hackathon partner, strong reasoning |
| **Voice transcription** | Whisper Large V3 via OpenRouter | Free, accurate |
| **Market data** | Binance Public API | Free, real-time, no key needed |
| **Fallback** | CoinGecko | When Binance is blocked |
| **Execution** | Bitget HMAC-signed REST API | Hackathon host, spot trading |
| **Hosting** | Render free tier | Free, simple, reliable for hackathon |
| **Storage** | JSON file in `/tmp` | Simple, no DB needed for hackathon |

---

## 📁 Project structure

```
trademouth-bot/
├── bot.py              # The entire bot (1,056 lines, zero deps except requests)
├── requirements.txt    # Just `requests==2.31.0`
├── render.yaml         # Render deployment config
├── README.md           # You are here
├── LICENSE             # MIT
└── .gitignore          # Standard Python ignores
```

---

## 🎯 Hackathon submission

- **Track:** 1 — Trading Agent
- **Organizer:** [@Bitget_AI](https://x.com/Bitget_AI) + partners
- **Demo:** [https://youtube.com/shorts/JnTMlclFo_4](https://youtube.com/shorts/JnTMlclFo_4)
- **Live bot:** @TradeMouthBot
- **Submission form:** [https://forms.gle/CEGB6fRtuobD3bCj8](https://forms.gle/CEGB6fRtuobD3bCj8)

---

## 🤝 Contributing

Open an issue or PR. This was built for a hackathon but designed to be readable and extensible.

---

## 📜 License

MIT — see [LICENSE](LICENSE)

---

## 👤 Author

**Donyemiight** (Don Yemight)
- GitHub: [@Donyemiight](https://github.com/Donyemiight)
- Telegram: @donyemiight
- Built with 🔥 for the Bitget AI Hackathon 2026
