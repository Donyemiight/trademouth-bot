#!/usr/bin/env python3
"""
TradeMouth v2 — production deploy version
Same logic as trademouth_v2.py, but reads config from environment variables
provided by the hosting platform.
"""

import os
import json
import time
import logging
import asyncio
import re
import urllib.parse
import hmac
import hashlib
import base64
from datetime import datetime, timedelta
from pathlib import Path

# Telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, CallbackQueryHandler,
)
import requests

# ---------- Config ----------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
QWEN_MODEL = os.environ.get("QWEN_MODEL", "qwen/qwen-2.5-72b-instruct")
WHISPER_MODEL = "openai/whisper-large-v3"

BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
BITGET_SECRET = os.environ.get("BITGET_SECRET", "")
BITGET_PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "")

# Use a writable path on the server (Render gives /tmp + ephemeral disk)
JOURNAL_PATH = Path("/tmp/journal.json")
ALERTS_PATH = Path("/tmp/alerts.json")
PENDING_TTL_SECONDS = 300
MAX_POSITION_PCT = 2.0
SUPPORTED_ASSETS = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX",
    "LINK", "MATIC", "DOT", "ARB", "OP", "TON", "SUI", "APT",
    "NEAR", "ATOM", "LTC", "TRX", "INJ", "TIA", "SEI", "WLD",
    "PEPE", "WIF", "BONK", "SHIB",
]
ASSET_ALIASES = {
    "BITCOIN": "BTC", "ETHEREUM": "ETH", "SOLANA": "SOL",
    "BINANCE": "BNB", "RIPPLE": "XRP", "CARDANO": "ADA",
}

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("trademouth")


def load_journal():
    if JOURNAL_PATH.exists():
        try: return json.loads(JOURNAL_PATH.read_text())
        except: pass
    return {"users": {}}

def save_journal(j):
    JOURNAL_PATH.write_text(json.dumps(j, indent=2))

def user_state(j, user_id):
    j["users"].setdefault(str(user_id), {
        "stated_portfolio_usdt": None, "max_position_pct": MAX_POSITION_PCT,
        "trades": [], "pending_trade": None, "pending_at": None, "style_notes": [],
    })
    return j["users"][str(user_id)]


QWEN_SYSTEM_PROMPT = """You are TradeMouth, a Socratic trading mentor that lives in a user's Telegram chat. You help them THINK through trades, you don't trade for them, and you never promise profits.

Voice: calm, sharp, slightly dry. Senior trader who's seen it all. Short sentences. Never saccharine. No "great question", no "I'd be happy to help".

Hard rules (non-negotiable):
1. When user states intent, FIRST move is usually a clarifying Socratic question, not a recommendation.
2. Always show 2-4 reasoning bullets. One line each. Real data preferred over platitudes.
3. NEVER use: "guaranteed", "risk-free", "easy money", "you'll make", "to the moon", "100x", "moonshot".
4. NEVER recommend leverage. We are spot only. If asked, redirect: "I'm a spot-only mentor. Let's talk position size instead."
5. Default position size cap is 2% of stated portfolio.
6. End every reply with ONE Socratic question.
7. Keep total response under 200 words. This is a chat, not a research report.
8. ONE emoji max per message.
9. Always reference the user's past trades if available, especially their losses.
10. "wait" is a valid answer. If the setup isn't there, say so plainly.

Output format (markdown):
[trade_consideration or analyze_asset]
**Read on [ASSET]:**
- [factor 1]
- [factor 2]
- [factor 3]
- [factor 4 — past trade pattern if exists]

**Lean:** long / short / wait
**Shape (if applicable):** entry $X, size 1-2%, stop $Y (-Z%), target $W (+V%)
**Question:** [socratic follow-up]
"""


def call_qwen(messages, temperature=0.4, model=None):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://trademouth.app",
        "X-Title": "TradeMouth",
    }
    payload = {
        "model": model or QWEN_MODEL,
        "messages": [{"role": "system", "content": QWEN_SYSTEM_PROMPT}] + messages,
        "temperature": temperature, "max_tokens": 700,
    }
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def get_market_snapshot(asset):
    asset_u = asset.upper().strip()
    sym = f"{asset_u}USDT"
    out = {"asset": asset_u, "ok": False}
    try:
        r = requests.get("https://api.binance.com/api/v3/klines",
            params={"symbol": sym, "interval": "1h", "limit": 100}, timeout=10)
        if r.status_code != 200: return {**out, "err": f"{sym} not found"}
        k = r.json()
        closes = [float(c[4]) for c in k]
        highs = [float(c[2]) for c in k]
        lows = [float(c[3]) for c in k]
        vols = [float(c[5]) for c in k]
        price = closes[-1]
        change_pct_24h = (closes[-1]/closes[-24]-1)*100 if len(closes)>=24 else 0
        gains, losses = [], []
        for i in range(-14, 0):
            d = closes[i] - closes[i-1]
            (gains if d>0 else losses).append(abs(d))
        avg_g = sum(gains)/14 if gains else 0
        avg_l = sum(losses)/14 if losses else 0
        rs = avg_g/avg_l if avg_l else 0
        rsi = 100-(100/(1+rs)) if rs else 50
        ema = sum(closes[-20:])/20
        avg_vol = sum(vols[:-1])/(len(vols)-1) if len(vols)>1 else vols[-1]
        out.update({"ok": True, "price": price, "change_pct_24h": round(change_pct_24h, 2),
            "high_24h": max(highs[-24:]) if len(highs)>=24 else max(highs),
            "low_24h": min(lows[-24:]) if len(lows)>=24 else min(lows),
            "rsi_1h": round(rsi, 1), "ema_20_1h": round(ema, 2),
            "vol_ratio_last_vs_avg": round(vols[-1]/avg_vol, 2) if avg_vol else 1})
    except Exception as e: out["err"] = str(e)
    return out


def get_fng():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        return int(r.json()["data"][0]["value"])
    except: return -1


def bitget_request(method, path, params=None, body=None):
    if not BITGET_API_KEY: return {"err": "Bitget not configured"}
    base = "https://api.bitget.com"
    ts = str(int(time.time()*1000))
    qs = "?"+urllib.parse.urlencode(params) if params else ""
    body_str = json.dumps(body) if body else ""
    prehash = ts+method.upper()+path+qs+body_str
    sign = base64.b64encode(hmac.new(BITGET_SECRET.encode(), prehash.encode(), hashlib.sha256).digest()).decode()
    headers = {"ACCESS-KEY": BITGET_API_KEY, "ACCESS-SIGN": sign, "ACCESS-TIMESTAMP": ts,
               "ACCESS-PASSPHRASE": BITGET_PASSPHRASE, "Content-Type": "application/json"}
    if method.upper()=="GET": r = requests.get(base+path+qs, headers=headers, timeout=15)
    else: r = requests.post(base+path+qs, headers=headers, data=body_str, timeout=15)
    try: return r.json()
    except: return {"err": r.text[:200]}


def place_spot_order(symbol, side, quote_usdt):
    return bitget_request("POST", "/api/v2/spot/trade/place-order", body={
        "symbol": symbol, "side": side.lower(), "orderType": "market",
        "quoteOrderQty": f"{quote_usdt:.2f}", "force": "gtc"})


def classify_intent(text):
    t = text.lower().strip()
    if not t: return "chitchat"
    if t.startswith("/start") or re.match(r"^(hi|hello|hey|yo)[\s!.]*$", t): return "chitchat"
    if t.startswith("/help") or "what can you do" in t: return "help"
    if t.startswith("/journal") or "my trades" in t or "win rate" in t: return "journal_review"
    if t.startswith("/strategies"): return "strategies"
    if t.startswith("/balance"): return "balance"
    if t in ("yes","do it","execute","y","go","send it","ship it","lock it in"): return "confirm_execute"
    if t in ("no","cancel","wait","skip","n","not now","nope","nah"): return "cancel_execute"
    if any(w in t for w in ("buy ","sell ","long ","short ","execute ","place order")): return "execute_intent"
    if any(w in t for w in ("thinking about","considering","looking at","should i","what do you think","i want to")): return "trade_consideration"
    if any(w in t for w in ("analyze","what's happening","read on","your view","thoughts on","how's ","what about")): return "analyze_asset"
    return "analyze_asset"


def extract_asset(text):
    upper = text.upper()
    for t in SUPPORTED_ASSETS:
        if re.search(rf"\b{t}\b", upper): return t
    for name, ticker in ASSET_ALIASES.items():
        if name in upper: return ticker
    return None


def parse_portfolio_set(text):
    m = re.search(r"portfolio[^\d]*(\d+(?:\.\d+)?)", text.lower())
    return float(m.group(1)) if m else None


def parse_max_size(text):
    m = re.search(r"max[^\d]*(\d+(?:\.\d+)?)", text.lower())
    return float(m.group(1)) if m else None


def build_user_context(user_text, snapshot, fng, user):
    asset = snapshot.get("asset", "")
    trades = user.get("trades", [])
    similar = [t for t in trades if asset and t.get("asset","").upper()==asset.upper()][-3:]
    similar_text = "\n".join(f"- {t['ts'][:10]} {t['asset']} {t['direction']} -> {t.get('outcome','open')} ({t.get('user_stated_thesis','no thesis')})" for t in similar) or "No prior trades on this asset."
    market_text = (f"Asset: {snapshot.get('asset','?')}\nPrice: {snapshot.get('price','n/a')}\n24h change: {snapshot.get('change_pct_24h','n/a')}%\nRSI 1h: {snapshot.get('rsi_1h','n/a')}\nEMA 20 1h: {snapshot.get('ema_20_1h','n/a')}\nF&G Index: {fng}") if snapshot.get("ok") else f"Market data unavailable: {snapshot.get('err','unknown')}"
    user_profile = f"Stated portfolio: {user.get('stated_portfolio_usdt') or 'not set'} USDT\nMax position %: {user.get('max_position_pct', MAX_POSITION_PCT)}%\nPast trades on {asset or 'this asset'}:\n{similar_text}"
    return [{"role": "user", "content": f"User message: {user_text}\n\nMarket context:\n{market_text}\n\nUser profile:\n{user_profile}\n\nRespond as TradeMouth. Ask one Socratic question first. Show 2-4 reasoning bullets. If you suggest a trade, give entry/size/stop/target. If not a strong setup, say 'wait'."}]


def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Analyze", callback_data="cmd_analyze"),
         InlineKeyboardButton("📓 Journal", callback_data="cmd_journal")],
        [InlineKeyboardButton("🎯 Strategies", callback_data="cmd_strategies"),
         InlineKeyboardButton("💰 Balance", callback_data="cmd_balance")],
    ])


async def start_cmd(update, ctx):
    await update.message.reply_text(
        "🧠 **TradeMouth**\n\nI'm a Socratic trading mentor. I won't give you signals — I'll help you think.\n\nTry:\n• *analyze SOL* — get my read on any coin\n• *I'm thinking about buying ETH at 3500* — let's workshop it\n• *my journal* — review your past trades\n• *set my portfolio to 5000* — I'll size positions off this\n\nBuilt for the Bitget AI Hackathon. No promises, no leverage, no custody of your funds.",
        reply_markup=main_menu_kb())


async def help_cmd(update, ctx):
    await update.message.reply_text(
        "**TradeMouth commands**\n\n/start — intro\n/help — this\n/journal — your trade history\n/strategies — 10 starter strategies\n/balance — your Bitget spot balance\n\nOr just type:\n• *analyze BTC*\n• *thinking about buying SOL*\n• *set my portfolio to 5000*\n• *yes* / *no* — to confirm or cancel a pending trade")


async def journal_cmd(update, ctx):
    j = load_journal()
    u = user_state(j, update.effective_user.id)
    trades = u.get("trades", [])
    if not trades:
        await update.message.reply_text("No trades yet. Type `thinking about buying X` to start.", reply_markup=main_menu_kb())
        return
    lines = [f"**Your last {min(len(trades), 10)} trades:**\n"]
    for t in trades[-10:]:
        lines.append(f"- {t['ts'][:10]} {t['asset']} {t['direction']} {t.get('size_usdt',0):.0f}U -> {t.get('outcome','open')} ({t.get('user_stated_thesis','—')[:40]})")
    closed = [t for t in trades if t.get("outcome") and t["outcome"]!="open"]
    wins = sum(1 for t in closed if t["outcome"].startswith("+"))
    win_rate = (wins/len(closed)*100) if closed else 0
    lines.append(f"\n**Total:** {len(trades)} | Closed: {len(closed)} | Win rate: {win_rate:.0f}%")
    await update.message.reply_text("\n".join(lines), reply_markup=main_menu_kb())


async def strategies_cmd(update, ctx):
    await update.message.reply_text(
        "**10 starter strategies:**\n\n1. *RSI mean reversion* — buy RSI<30 4H\n2. *MACD continuation* — buy 1D MACD cross above 200 EMA\n3. *Fear & Greed contrarian* — buy F&G<25, sell F&G>60\n4. *DCA accumulator* — buy $X weekly\n5. *Breakout retest* — buy retest of broken resistance\n6. *Bollinger squeeze* — buy BB expansion\n7. *On-chain whale tracker*\n8. *Funding rate fade*\n9. *Earnings/news drift*\n10. *Personal rule engine* — your rules, I enforce them",
        reply_markup=main_menu_kb())


async def balance_cmd(update, ctx):
    if not BITGET_API_KEY:
        await update.message.reply_text("Bitget API not configured.", reply_markup=main_menu_kb())
        return
    res = bitget_request("GET", "/api/v2/spot/account/assets")
    if "err" in res:
        await update.message.reply_text(f"Error: {res['err']}")
        return
    lines = ["**Bitget spot balance:**\n"]
    for a in res.get("data", []):
        try:
            avail = float(a.get("available","0") or 0)
            frozen = float(a.get("frozen","0") or 0)
        except: continue
        if avail > 0 or frozen > 0:
            lines.append(f"- **{a.get('coin')}**: {avail} (frozen: {frozen})")
    if len(lines) == 1: lines.append("All zero.")
    await update.message.reply_text("\n".join(lines), reply_markup=main_menu_kb())


async def handle_text(update, ctx):
    text = update.message.text or ""
    user_id = update.effective_user.id
    j = load_journal()
    u = user_state(j, user_id)
    intent = classify_intent(text)
    
    p = parse_portfolio_set(text)
    if p:
        u["stated_portfolio_usdt"] = p
        save_journal(j)
        await update.message.reply_text(f"Got it. Portfolio: **{p:.0f} USDT**. Max per trade: {u['max_position_pct']}%.", reply_markup=main_menu_kb())
        return
    p = parse_max_size(text)
    if p:
        u["max_position_pct"] = min(p, 10.0)
        save_journal(j)
        await update.message.reply_text(f"Max position set to **{p}%** (capped at 10%).")
        return
    
    if intent == "help": await help_cmd(update, ctx); return
    if intent == "journal_review": await journal_cmd(update, ctx); return
    if intent == "strategies": await strategies_cmd(update, ctx); return
    if intent == "balance": await balance_cmd(update, ctx); return
    
    # Confirm / cancel
    pending = u.get("pending_trade")
    pending_at = u.get("pending_at")
    if pending and pending_at:
        try: age = (datetime.utcnow() - datetime.fromisoformat(pending_at.rstrip("Z"))).total_seconds()
        except: age = 0
        if age > PENDING_TTL_SECONDS:
            u["pending_trade"] = None; u["pending_at"] = None; pending = None
            save_journal(j)
    
    if intent == "confirm_execute" and pending:
        if not BITGET_API_KEY:
            await update.message.reply_text("Bitget API not configured.")
            return
        snap = get_market_snapshot(pending["asset"])
        if not snap.get("ok"):
            await update.message.reply_text(f"Couldn't get live price: {snap.get('err')}. Cancelled.")
            u["pending_trade"] = None; u["pending_at"] = None; save_journal(j)
            return
        size_usdt = pending.get("size_usdt", 0)
        if size_usdt <= 0:
            await update.message.reply_text("Trade size is zero. Set your portfolio first.")
            return
        result = place_spot_order(f"{pending['asset']}USDT", pending["direction"], size_usdt)
        if "err" in result:
            await update.message.reply_text(f"Bitget rejected: {result['err']}")
            return
        trade = dict(pending)
        trade.update({"ts": datetime.utcnow().isoformat()+"Z", "entry_price": snap["price"],
                      "order_result": result, "outcome": "open"})
        u["trades"].append(trade)
        u["pending_trade"] = None; u["pending_at"] = None
        save_journal(j)
        oid = (result.get("data") or {}).get("orderId") or "n/a"
        await update.message.reply_text(f"✅ **Executed.** Order ID: `{oid}`\nEntry: {snap['price']} | Size: {size_usdt:.2f} USDT\nView: /journal", reply_markup=main_menu_kb())
        return
    
    if intent == "cancel_execute":
        if pending:
            u["pending_trade"] = None; u["pending_at"] = None; save_journal(j)
            await update.message.reply_text("Cancelled.", reply_markup=main_menu_kb())
        else:
            await update.message.reply_text("Nothing pending.", reply_markup=main_menu_kb())
        return
    
    # Main flow
    asset = extract_asset(text) or "BTC"
    snapshot = get_market_snapshot(asset)
    fng = get_fng()
    messages = build_user_context(text, snapshot, fng, u)
    try: reply = call_qwen(messages)
    except Exception as e:
        log.exception("Qwen error")
        await update.message.reply_text(f"LLM error: {e}")
        return
    
    # Extract tentative trade
    rl = reply.lower()
    m = re.search(r"(?:size|position)[^\d]*(\d+(?:\.\d+)?)\s*%", rl)
    if m and u.get("stated_portfolio_usdt"):
        try:
            pct = min(float(m.group(1)), u["max_position_pct"])
            u["pending_trade"] = {
                "asset": asset,
                "direction": "buy" if any(w in text.lower() for w in ("buy","long","thinking about buying","i want to buy")) else "sell",
                "size_usdt": u["stated_portfolio_usdt"] * pct / 100,
                "stop_pct": 3.0, "target_pct": 5.0,
                "reasoning": reply[:500], "user_stated_thesis": text,
            }
            u["pending_at"] = datetime.utcnow().isoformat()+"Z"
            save_journal(j)
            reply += f"\n\n_I sketched a trade. Reply *yes* to send, *no* to cancel. (5 min)_"
        except: pass
    
    await update.message.reply_text(reply, reply_markup=main_menu_kb())


async def on_button(update, ctx):
    query = update.callback_query
    await query.answer()
    if query.data == "noop":
        await query.answer(text="Type 'yes' in chat to confirm — buttons don't execute", show_alert=False)
        return
    if query.data == "noop_cancel":
        await query.answer(text="Type 'no' to cancel", show_alert=False)
        return
    cmd_map = {"cmd_analyze": "/help", "cmd_journal": "/journal", "cmd_strategies": "/strategies", "cmd_balance": "/balance"}
    if query.data in cmd_map:
        await query.message.reply_text(f"Use: {cmd_map[query.data]}")


async def error_handler(update, ctx):
    log.warning(f"Update {update} caused error {ctx.error}")


def main():
    if not TELEGRAM_BOT_TOKEN or not OPENROUTER_API_KEY:
        log.error(f"Missing env vars. TELEGRAM_BOT_TOKEN={'set' if TELEGRAM_BOT_TOKEN else 'MISSING'}, OPENROUTER_API_KEY={'set' if OPENROUTER_API_KEY else 'MISSING'}")
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or OPENROUTER_API_KEY")
    log.info("TradeMouth v2 (cloud) starting... env vars OK")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("journal", journal_cmd))
    app.add_handler(CommandHandler("strategies", strategies_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)
    log.info("Bot is up.")
    # If PORT env var is set (Render web service), start a tiny HTTP server
    # so Render's health check passes. Otherwise just run polling.
    import os as _os
    port = _os.environ.get("PORT")
    if port:
        import threading
        from http.server import HTTPServer, BaseHTTPRequestHandler
        class HealthHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'TradeMouth is up')
            def log_message(self, *a, **k): pass
        def run_http():
            HTTPServer(("0.0.0.0", int(port)), HealthHandler).serve_forever()
        threading.Thread(target=run_http, daemon=True).start()
        log.info(f"Health server on port {port}")
    # Wrap run_polling in asyncio.run() to fix "no current event loop" on Python 3.10+
    asyncio.run(app.run_polling(allowed_updates=Update.ALL_TYPES))


if __name__ == "__main__":
    main()
