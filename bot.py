#!/usr/bin/env python3
"""
TradeMouth — Zero-dependency Telegram bot (just requests + stdlib).

No python-telegram-bot. No version conflicts. No Python 3.14 issues.
Just direct calls to the Telegram Bot API.

This is the most boring, reliable bot you can write.
"""

import os
import json
import time
import logging
import re
import urllib.parse
import hmac
import hashlib
import base64
from datetime import datetime, timedelta
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

import requests

# ---------- Config ----------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
QWEN_MODEL = os.environ.get("QWEN_MODEL", "qwen/qwen-2.5-7b-instruct")

BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
BITGET_SECRET = os.environ.get("BITGET_SECRET", "")
BITGET_PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "")

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

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("trademouth")


# ---------- Telegram API helpers ----------
def tg_send(chat_id, text, reply_markup=None, parse_mode="Markdown"):
    """Send a message via the Telegram Bot API."""
    payload = {
        "chat_id": chat_id,
        "text": text[:4000],  # Telegram limit
        "parse_mode": parse_mode,
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(f"{TG_API}/sendMessage", json=payload, timeout=30)
        if r.status_code != 200:
            log.warning(f"Telegram send failed: {r.status_code} {r.text[:200]}")
            # Retry without parse_mode if Markdown broke
            if parse_mode == "Markdown":
                payload.pop("parse_mode", None)
                r = requests.post(f"{TG_API}/sendMessage", json=payload, timeout=30)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        log.warning(f"tg_send error: {e}")
        return None


def tg_answer_callback(callback_query_id, text=None, show_alert=False):
    """Answer a callback query (button press)."""
    try:
        requests.post(f"{TG_API}/answerCallbackQuery", json={
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        }, timeout=10)
    except Exception:
        pass


# ---------- Journal ----------
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


# ---------- Qwen ----------
QWEN_SYSTEM_PROMPT = """You are TradeMouth, a Socratic trading mentor that lives in a user's Telegram chat. You help them THINK through trades, you don't trade for them, and you never promise profits.

Voice: calm, sharp, slightly dry. Senior trader who's seen it all. Short sentences. Never saccharine.

Hard rules (non-negotiable):
1. When user states intent, FIRST move is usually a clarifying Socratic question, not a recommendation.
2. Always show 2-4 reasoning bullets. One line each.
3. NEVER use: "guaranteed", "risk-free", "easy money", "you'll make", "to the moon", "100x", "moonshot".
4. NEVER recommend leverage. Spot only. If asked, redirect: "I'm a spot-only mentor. Let's talk position size instead."
5. Default position size cap is 2% of stated portfolio.
6. End every reply with ONE Socratic question.
7. Keep total response under 200 words.
8. ONE emoji max per message.

Output format (markdown):
**Read on [ASSET]:**
- [factor 1]
- [factor 2]
- [factor 3]
- [factor 4 — past trade pattern if exists]

**Lean:** long / short / wait
**Shape (if applicable):** entry $X, size 1-2%, stop $Y (-Z%), target $W (+V%)
**Question:** [socratic follow-up]
"""


def call_qwen(messages, temperature=0.4):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://trademouth.app",
        "X-Title": "TradeMouth",
    }
    payload = {
        "model": QWEN_MODEL,
        "messages": [{"role": "system", "content": QWEN_SYSTEM_PROMPT}] + messages,
        "temperature": temperature, "max_tokens": 700,
    }
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    if r.status_code != 200:
        log.error(f"Qwen error {r.status_code}: {r.text[:500]}")
        raise RuntimeError(f"Qwen API error {r.status_code}: {r.text[:200]}")
    return r.json()["choices"][0]["message"]["content"]


# ---------- Market data ----------
def get_market_snapshot(asset):
    asset_u = asset.upper().strip()
    sym = f"{asset_u}USDT"
    out = {"asset": asset_u, "ok": False}

    # Try multiple endpoints (Binance often blocks cloud IPs)
    endpoints = [
        ("https://api.binance.com/api/v3/klines", "binance", "binance"),
        ("https://api1.binance.com/api/v3/klines", "binance1", "binance"),
        ("https://data-api.binance.vision/api/v3/klines", "data-api", "binance"),
    ]

    for api_url, name, source_type in endpoints:
        try:
            r = requests.get(api_url, params={"symbol": sym, "interval": "1h", "limit": 100}, timeout=8)
            if r.status_code != 200:
                log.info(f"{name} returned {r.status_code} for {sym}")
                continue
            k = r.json()
            if not k or len(k) < 50:
                continue
            closes = [float(c[4]) for c in k]
            highs = [float(c[2]) for c in k]
            lows = [float(c[3]) for c in k]
            vols = [float(c[5]) for c in k]
            price = closes[-1]
            change_pct_24h = (closes[-1] / closes[-24] - 1) * 100 if len(closes) >= 24 else 0
            gains, losses = [], []
            for i in range(-14, 0):
                d = closes[i] - closes[i - 1]
                (gains if d > 0 else losses).append(abs(d))
            avg_g = sum(gains) / 14 if gains else 0
            avg_l = sum(losses) / 14 if losses else 0
            rs = avg_g / avg_l if avg_l else 0
            rsi = 100 - (100 / (1 + rs)) if rs else 50
            ema = sum(closes[-20:]) / 20
            avg_vol = sum(vols[:-1]) / (len(vols) - 1) if len(vols) > 1 else vols[-1]
            out.update({
                "ok": True, "price": price, "change_pct_24h": round(change_pct_24h, 2),
                "high_24h": max(highs[-24:]) if len(highs) >= 24 else max(highs),
                "low_24h": min(lows[-24:]) if len(lows) >= 24 else min(lows),
                "rsi_1h": round(rsi, 1), "ema_20_1h": round(ema, 2),
                "vol_ratio_last_vs_avg": round(vols[-1] / avg_vol, 2) if avg_vol else 1,
                "source": name,
            })
            return out
        except Exception as e:
            log.info(f"{name} failed for {sym}: {e}")
            continue

    # Final fallback: CoinGecko (basic price only, no indicators)
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
            params={"ids": asset_u.lower(), "vs_currencies": "usd",
                    "include_24hr_change": "true", "include_24hr_vol": "true",
                    "include_high_low": "true"}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data:
                coin = data.get(asset_u.lower(), {})
                if coin:
                    price = coin.get("usd", 0)
                    change_24h = coin.get("usd_24h_change", 0)
                    out.update({
                        "ok": True, "price": price,
                        "change_pct_24h": round(change_24h, 2),
                        "high_24h": 0, "low_24h": 0,
                        "rsi_1h": -1, "ema_20_1h": 0,
                        "vol_ratio_last_vs_avg": 0,
                        "source": "coingecko",
                    })
                    return out
    except Exception as e:
        log.warning(f"coingecko fallback failed: {e}")

    out["err"] = "All market data sources unreachable from this server"
    return out


def get_fng():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        return int(r.json()["data"][0]["value"])
    except Exception:
        return -1


# ---------- Bitget ----------
def bitget_request(method, path, params=None, body=None):
    if not BITGET_API_KEY:
        return {"err": "Bitget API not configured."}
    base = "https://api.bitget.com"
    ts = str(int(time.time() * 1000))
    qs = "?" + urllib.parse.urlencode(params) if params else ""
    body_str = json.dumps(body) if body else ""
    prehash = ts + method.upper() + path + qs + body_str
    sign = base64.b64encode(
        hmac.new(BITGET_SECRET.encode(), prehash.encode(), hashlib.sha256).digest()
    ).decode()
    headers = {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": BITGET_PASSPHRASE,
        "Content-Type": "application/json",
    }
    url = base + path + qs
    if method.upper() == "GET":
        r = requests.get(url, headers=headers, timeout=15)
    else:
        r = requests.post(url, headers=headers, data=body_str, timeout=15)
    try: return r.json()
    except Exception: return {"err": r.text[:200]}


def place_spot_order(symbol, side, quote_usdt):
    body = {
        "symbol": symbol, "side": side.lower(), "orderType": "market",
        "quoteOrderQty": f"{quote_usdt:.2f}", "force": "gtc",
    }
    return bitget_request("POST", "/api/v2/spot/trade/place-order", body=body)


# ---------- Intent ----------
def classify_intent(text):
    t = text.lower().strip()
    if not t: return "chitchat"
    if t.startswith("/start") or re.match(r"^(hi|hello|hey|yo)[\s!.]*$", t): return "chitchat"
    if t.startswith("/help"): return "help"
    if t.startswith("/journal") or "my trades" in t: return "journal_review"
    if t.startswith("/strategies"): return "strategies"
    if t.startswith("/balance"): return "balance"
    if t in ("yes","do it","execute","y","go","send it","ship it"): return "confirm_execute"
    if t in ("no","cancel","wait","skip","n","not now","nope","nah"): return "cancel_execute"
    if any(w in t for w in ("buy ","sell ","long ","short ","execute ","place order")): return "execute_intent"
    if any(w in t for w in ("thinking about","considering","looking at","should i","what do you think")): return "trade_consideration"
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


# ---------- Keyboards ----------
def main_menu_kb():
    return {
        "inline_keyboard": [
            [{"text": "📊 Analyze", "callback_data": "hint_analyze"},
             {"text": "📓 Journal", "callback_data": "hint_journal"}],
            [{"text": "🎯 Strategies", "callback_data": "hint_strategies"},
             {"text": "💰 Balance", "callback_data": "hint_balance"}],
        ]
    }


def confirm_kb():
    return {
        "inline_keyboard": [[
            {"text": "✓ Type 'yes' to confirm", "callback_data": "noop_yes"},
            {"text": "✗ Type 'no' to cancel", "callback_data": "noop_no"},
        ]]
    }


# ---------- Message handlers ----------
def handle_start(chat_id):
    tg_send(chat_id,
        "🧠 *TradeMouth*\n\n"
        "I'm a Socratic trading mentor. I won't give you signals — I'll help you think.\n\n"
        "Try:\n"
        "• `analyze SOL` — get my read on any coin\n"
        "• `I'm thinking about buying ETH at 3500` — let's workshop it\n"
        "• `my journal` — review your past trades\n"
        "• `/strategies` — 10 starter strategies\n"
        "• `set my portfolio to 5000` — I'll size positions off this\n\n"
        "Built for the Bitget AI Hackathon. No promises, no leverage, no custody of your funds.",
        reply_markup=main_menu_kb())


def handle_help(chat_id):
    tg_send(chat_id,
        "*TradeMouth commands*\n\n"
        "/start — intro\n/help — this\n/journal — your trade history\n"
        "/strategies — 10 starter strategies\n/balance — your Bitget spot balance\n\n"
        "Or just type:\n"
        "• `analyze BTC`\n• `thinking about buying SOL`\n"
        "• `set my portfolio to 5000`\n• `yes` / `no` — confirm or cancel a pending trade")


def handle_journal(chat_id, user_id):
    j = load_journal()
    u = user_state(j, user_id)
    trades = u.get("trades", [])
    if not trades:
        tg_send(chat_id, "No trades yet. Type `thinking about buying X` to start a Socratic session.",
                reply_markup=main_menu_kb())
        return
    lines = [f"*Your last {min(len(trades), 10)} trades:*\n"]
    for t in trades[-10:]:
        lines.append(
            f"- {t['ts'][:10]} {t['asset']} {t['direction']} {t.get('size_usdt', 0):.0f}U "
            f"-> {t.get('outcome', 'open')} ({t.get('user_stated_thesis', '—')[:40]})"
        )
    closed = [t for t in trades if t.get("outcome") and t["outcome"] != "open"]
    wins = sum(1 for t in closed if t["outcome"].startswith("+"))
    win_rate = (wins / len(closed) * 100) if closed else 0
    lines.append(f"\n*Total:* {len(trades)} | Closed: {len(closed)} | Win rate: {win_rate:.0f}%")
    tg_send(chat_id, "\n".join(lines), reply_markup=main_menu_kb())


def handle_strategies(chat_id):
    tg_send(chat_id,
        "*10 starter strategies*\n\n"
        "1. *RSI mean reversion* — buy RSI<30 4H\n"
        "2. *MACD continuation* — buy 1D MACD cross above 200 EMA\n"
        "3. *Fear & Greed contrarian* — buy F&G<25, sell F&G>60\n"
        "4. *DCA accumulator* — buy $X weekly\n"
        "5. *Breakout retest* — buy retest of broken resistance\n"
        "6. *Bollinger squeeze* — buy BB expansion\n"
        "7. *On-chain whale tracker*\n"
        "8. *Funding rate fade*\n"
        "9. *Earnings/news drift*\n"
        "10. *Personal rule engine* — your rules, I enforce them",
        reply_markup=main_menu_kb())


def handle_balance(chat_id):
    if not BITGET_API_KEY:
        tg_send(chat_id, "Bitget API not configured.", reply_markup=main_menu_kb())
        return
    res = bitget_request("GET", "/api/v2/spot/account/assets")
    if "err" in res:
        tg_send(chat_id, f"Error: {res['err']}")
        return
    lines = ["*Bitget spot balance:*\n"]
    nonzero = 0
    for a in res.get("data", []):
        try:
            avail = float(a.get("available", "0") or 0)
            frozen = float(a.get("frozen", "0") or 0)
        except: continue
        if avail > 0 or frozen > 0:
            lines.append(f"- *{a.get('coin')}*: {avail} (frozen: {frozen})")
            nonzero += 1
    if nonzero == 0:
        lines.append("All zero.")
    tg_send(chat_id, "\n".join(lines), reply_markup=main_menu_kb())


def build_user_context(user_text, snapshot, fng, user):
    asset = snapshot.get("asset", "")
    trades = user.get("trades", [])
    similar = [t for t in trades if asset and t.get("asset", "").upper() == asset.upper()][-3:]
    similar_text = "\n".join(
        f"- {t['ts'][:10]} {t['asset']} {t['direction']} -> {t.get('outcome', 'open')} ({t.get('user_stated_thesis', 'no thesis')})"
        for t in similar) or "No prior trades on this asset."
    market_text = (
        f"Asset: {snapshot.get('asset', '?')}\n"
        f"Price: {snapshot.get('price', 'n/a')}\n"
        f"24h change: {snapshot.get('change_pct_24h', 'n/a')}%\n"
        f"RSI 1h: {snapshot.get('rsi_1h', 'n/a')}\n"
        f"EMA 20 (1h): {snapshot.get('ema_20_1h', 'n/a')}\n"
        f"Fear & Greed Index: {fng}\n"
    ) if snapshot.get("ok") else f"Market data unavailable: {snapshot.get('err', 'unknown')}"
    user_profile = (
        f"Stated portfolio: {user.get('stated_portfolio_usdt') or 'not set'} USDT\n"
        f"Max position %: {user.get('max_position_pct', MAX_POSITION_PCT)}%\n"
        f"Past trades on {asset or 'this asset'}:\n{similar_text}"
    )
    return [{
        "role": "user",
        "content": (
            f"User message: {user_text}\n\n"
            f"Market context:\n{market_text}\n\n"
            f"User profile:\n{user_profile}\n\n"
            "Respond as TradeMouth. Ask one Socratic question first. Show 2-4 reasoning bullets. "
            "If you suggest a trade, give entry/size/stop/target. If not a strong setup, say 'wait'."
        )
    }]


def handle_text(chat_id, user_id, text):
    j = load_journal()
    u = user_state(j, user_id)
    intent = classify_intent(text)

    p = parse_portfolio_set(text)
    if p:
        u["stated_portfolio_usdt"] = p
        save_journal(j)
        tg_send(chat_id,
            f"Got it. Portfolio: *{p:.0f} USDT*. Max per trade: {u['max_position_pct']}%.",
            reply_markup=main_menu_kb())
        return
    p = parse_max_size(text)
    if p:
        u["max_position_pct"] = min(p, 10.0)
        save_journal(j)
        tg_send(chat_id, f"Max position set to *{p}%* (capped at 10%).")
        return

    if intent == "help": handle_help(chat_id); return
    if intent == "journal_review": handle_journal(chat_id, user_id); return
    if intent == "strategies": handle_strategies(chat_id); return
    if intent == "balance": handle_balance(chat_id); return

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
            tg_send(chat_id, "Bitget API not configured.")
            return
        snap = get_market_snapshot(pending["asset"])
        if not snap.get("ok"):
            tg_send(chat_id, f"Couldn't get live price: {snap.get('err')}. Cancelled.")
            u["pending_trade"] = None; u["pending_at"] = None; save_journal(j)
            return
        size_usdt = pending.get("size_usdt", 0)
        if size_usdt <= 0:
            tg_send(chat_id, "Trade size is zero. Set your portfolio first.")
            return
        result = place_spot_order(f"{pending['asset']}USDT", pending["direction"], size_usdt)
        if "err" in result:
            tg_send(chat_id, f"Bitget rejected: {result['err']}")
            return
        trade = dict(pending)
        trade.update({
            "ts": datetime.utcnow().isoformat() + "Z",
            "entry_price": snap["price"],
            "order_result": result,
            "outcome": "open",
        })
        u["trades"].append(trade)
        u["pending_trade"] = None; u["pending_at"] = None
        save_journal(j)
        oid = (result.get("data") or {}).get("orderId") or "n/a"
        tg_send(chat_id,
            f"✅ *Executed.* Order ID: `{oid}`\n"
            f"Entry: {snap['price']} | Size: {size_usdt:.2f} USDT\n"
            f"View: /journal",
            reply_markup=main_menu_kb())
        return

    if intent == "cancel_execute":
        if pending:
            u["pending_trade"] = None; u["pending_at"] = None; save_journal(j)
            tg_send(chat_id, "Cancelled.", reply_markup=main_menu_kb())
        else:
            tg_send(chat_id, "Nothing pending.", reply_markup=main_menu_kb())
        return

    asset = extract_asset(text) or "BTC"
    snapshot = get_market_snapshot(asset)
    fng = get_fng()
    messages = build_user_context(text, snapshot, fng, u)
    try:
        reply = call_qwen(messages)
    except Exception as e:
        log.exception(f"Qwen error: {e}")
        # Fallback: send a basic data-driven response without LLM
        if snapshot.get("ok"):
            fng_text = f" | F&G: {fng}" if fng > 0 else ""
            fallback = (
                f"*Read on {snapshot['asset']}*\n\n"
                f"- Price: ${snapshot['price']} ({snapshot['change_pct_24h']:+.2f}% 24h)\n"
                f"- RSI 1h: {snapshot['rsi_1h']}{fng_text}\n"
                f"- 24h range: ${snapshot['low_24h']} – ${snapshot['high_24h']}\n\n"
                f"_LLM temporarily unavailable, showing raw data. Try again in a minute._\n\n"
                f"Question: what's your thesis here?"
            )
            tg_send(chat_id, fallback, reply_markup=main_menu_kb())
        else:
            tg_send(chat_id, f"LLM error: {str(e)[:200]}\n\nTry `/help` for commands.")
        return

    rl = reply.lower()
    m = re.search(r"(?:size|position)[^\d]*(\d+(?:\.\d+)?)\s*%", rl)
    if m and u.get("stated_portfolio_usdt"):
        try:
            pct = min(float(m.group(1)), u["max_position_pct"])
            u["pending_trade"] = {
                "asset": asset,
                "direction": "buy" if any(w in text.lower() for w in ("buy", "long", "thinking about buying", "i want to buy")) else "sell",
                "size_usdt": u["stated_portfolio_usdt"] * pct / 100,
                "stop_pct": 3.0, "target_pct": 5.0,
                "reasoning": reply[:500], "user_stated_thesis": text,
            }
            u["pending_at"] = datetime.utcnow().isoformat() + "Z"
            save_journal(j)
            reply += f"\n\n_I sketched a trade. Reply *yes* to send, *no* to cancel. (5 min)_"
            tg_send(chat_id, reply, reply_markup=confirm_kb())
            return
        except Exception as e:
            log.warning(f"Could not create pending trade: {e}")

    tg_send(chat_id, reply, reply_markup=main_menu_kb())


# ---------- Update dispatcher ----------
def handle_update(update):
    """Process a single Telegram update."""
    try:
        if "message" in update:
            msg = update["message"]
            chat_id = msg["chat"]["id"]
            user_id = msg["from"]["id"]
            text = msg.get("text", "").strip()

            if text == "/start":
                handle_start(chat_id); return
            if text == "/help":
                handle_help(chat_id); return
            if text == "/journal":
                handle_journal(chat_id, user_id); return
            if text == "/strategies":
                handle_strategies(chat_id); return
            if text == "/balance":
                handle_balance(chat_id); return
            if text:
                handle_text(chat_id, user_id, text); return

        if "callback_query" in update:
            cq = update["callback_query"]
            data = cq.get("data", "")
            chat_id = cq["message"]["chat"]["id"]
            if data == "hint_analyze":
                tg_answer_callback(cq["id"])
                tg_send(chat_id, "Type: `analyze SOL` (or any coin) for my read.")
            elif data == "hint_journal":
                tg_answer_callback(cq["id"])
                tg_send(chat_id, "Type: /journal")
            elif data == "hint_strategies":
                tg_answer_callback(cq["id"])
                tg_send(chat_id, "Type: /strategies")
            elif data == "hint_balance":
                tg_answer_callback(cq["id"])
                tg_send(chat_id, "Type: /balance")
            elif data == "noop_yes":
                tg_answer_callback(cq["id"], text="Type 'yes' in chat to confirm — buttons don't execute trades.", show_alert=False)
            elif data == "noop_no":
                tg_answer_callback(cq["id"], text="Type 'no' in chat to cancel.", show_alert=False)
            else:
                tg_answer_callback(cq["id"])
    except Exception as e:
        log.exception(f"handle_update error: {e}")


def get_updates_once():
    """Long-poll for new Telegram updates. Returns list of updates."""
    try:
        # Get current offset
        offset_file = Path("/tmp/tg_offset.txt")
        offset = 0
        if offset_file.exists():
            try: offset = int(offset_file.read_text().strip())
            except: pass

        params = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
        if offset: params["offset"] = offset
        r = requests.get(f"{TG_API}/getUpdates", params=params, timeout=35)
        if r.status_code != 200:
            log.warning(f"getUpdates failed: {r.status_code} {r.text[:200]}")
            return []
        data = r.json()
        if not data.get("ok"):
            return []
        results = data.get("result", [])
        if results:
            # Save next offset
            new_offset = max(u["update_id"] for u in results) + 1
            offset_file.write_text(str(new_offset))
        return results
    except requests.exceptions.Timeout:
        return []
    except Exception as e:
        log.warning(f"getUpdates error: {e}")
        return []


def run_bot():
    log.info("TradeMouth (zero-deps) starting long polling...")
    while True:
        updates = get_updates_once()
        for u in updates:
            handle_update(u)


def main():
    if not TELEGRAM_BOT_TOKEN or not OPENROUTER_API_KEY:
        log.error(f"Missing env vars")
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or OPENROUTER_API_KEY")
    log.info("TradeMouth v3 (zero-deps) starting... env vars OK")

    # HTTP health server for Render
    port = os.environ.get("PORT")
    if port:
        class HealthHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'TradeMouth is up')
            def log_message(self, *a, **k):
                pass
        def run_http():
            HTTPServer(("0.0.0.0", int(port)), HealthHandler).serve_forever()
        threading.Thread(target=run_http, daemon=True).start()
        log.info(f"Health server on port {port}")

    run_bot()


if __name__ == "__main__":
    main()
