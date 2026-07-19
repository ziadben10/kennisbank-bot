"""
Kennisbank Bot v3 — digest + opvallendheids-alerts + random drops per onderwerp.

Onderwerpen (aan/uit via topics.json): crypto, gaza, autos, ai, tech

Modi:
  python3 bot_v3.py            # volledige ochtend-digest
  python3 bot_v3.py --check    # stil; alleen bij opvallende crypto-situaties
  python3 bot_v3.py --random   # dobbelsteen: soms een verrassingsdrop uit een random onderwerp

Spot-only kader. Analyses, geen koopcommando's. Jij beslist.
"""

import io
import json
import os
import random
import sys
import time

import requests
import feedparser
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timezone

# ----------------- CONFIG -----------------
COINS = ["BTCUSDT", "ETHUSDT"]
INTERVAL = "4h"
CANDLES = 200
EMA_FAST, EMA_SLOW = 20, 50
RSI_PERIOD, RSI_OVERBOUGHT, RSI_OVERSOLD = 14, 70, 30
VOLUME_SPIKE_FACTOR = 2.5

TOPIC_FEEDS = {
    "gaza": [
        ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
        ("BBC Midden-Oosten", "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml"),
    ],
    "autos": [
        ("Autoblog NL", "https://www.autoblog.nl/feed"),
        ("Top Gear", "https://www.topgear.com/feeds/all/rss.xml"),
    ],
    "ai": [
        ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
        ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ],
    "tech": [
        ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
        ("Tweakers", "https://feeds.feedburner.com/tweakers/mixed"),
    ],
}
TOPIC_EMOJI = {"crypto": "📊", "gaza": "🕊️", "autos": "🚗", "ai": "🤖", "tech": "💻"}

DEFAULT_TOPICS = {"enabled": ["crypto", "ai", "tech", "gaza", "autos"], "random_chance": 0.25}

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# -------------------------------------------


def load_topics() -> dict:
    """topics.json naast dit script; ontbreekt hij, dan defaults."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "topics.json")
    try:
        with open(path) as f:
            cfg = json.load(f)
        return {**DEFAULT_TOPICS, **cfg}
    except Exception:
        return dict(DEFAULT_TOPICS)


# ============ CRYPTO (zelfde motor als v2) ============

def get_klines(symbol, interval, limit):
    url = "https://api.binance.com/api/v3/klines"
    r = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=15)
    r.raise_for_status()
    data = r.json()
    times = [datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc) for c in data]
    return times, [float(c[4]) for c in data], [float(c[5]) for c in data]


def ema(prices, period):
    k = 2 / (period + 1)
    result = [sum(prices[:period]) / period]
    for p in prices[period:]:
        result.append(p * k + result[-1] * (1 - k))
    return [None] * (period - 1) + result


def rsi_series(prices, period=14):
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    values = [None] * period
    for i in range(period, len(gains) + 1):
        if i > period:
            avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        values.append(100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss))
    return values


def analyse(symbol):
    times, closes, volumes = get_klines(symbol, INTERVAL, CANDLES)
    ema_f, ema_s = ema(closes, EMA_FAST), ema(closes, EMA_SLOW)
    rsi_vals = rsi_series(closes, RSI_PERIOD)
    price, rsi_now = closes[-1], rsi_vals[-1]
    fast_now, slow_now, fast_prev, slow_prev = ema_f[-1], ema_s[-1], ema_f[-2], ema_s[-2]
    uptrend = fast_now > slow_now
    avg_vol = sum(volumes[-50:]) / 50
    change_24h = (closes[-1] - closes[-7]) / closes[-7] * 100
    change_7d = (closes[-1] - closes[-43]) / closes[-43] * 100
    change_30d = (closes[-1] - closes[0]) / closes[0] * 100
    hi, lo = max(closes), min(closes)

    opvallend = []
    if fast_prev <= slow_prev and fast_now > slow_now:
        opvallend.append("EMA bullish crossover — trenddraai omhoog")
    if fast_prev >= slow_prev and fast_now < slow_now:
        opvallend.append("EMA bearish crossover — trenddraai omlaag")
    if rsi_now >= RSI_OVERBOUGHT:
        opvallend.append(f"RSI overbought ({rsi_now:.0f}) — oververhit, correctierisico")
    if rsi_now <= RSI_OVERSOLD:
        opvallend.append(f"RSI oversold ({rsi_now:.0f}) — zwaar afgestraft, mogelijk herstel")
    if volumes[-1] > avg_vol * VOLUME_SPIKE_FACTOR:
        opvallend.append(f"Volumespike ({volumes[-1]/avg_vol:.1f}x gemiddeld)")
    if abs(change_24h) >= 5:
        opvallend.append(f"Grote 24u-beweging: {change_24h:+.1f}%")

    return {
        "symbol": symbol.replace("USDT", ""), "price": price, "rsi": rsi_now,
        "trend": "UP" if uptrend else "DOWN", "change_24h": change_24h,
        "change_7d": change_7d, "change_30d": change_30d,
        "range_hi": hi, "range_lo": lo, "opvallend": opvallend,
        "_plot": (times, closes, ema_f, ema_s, rsi_vals),
    }


def make_chart(res):
    times, closes, ema_f, ema_s, rsi_vals = res["_plot"]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7),
                                   gridspec_kw={"height_ratios": [3, 1]}, sharex=True)
    fig.patch.set_facecolor("#0d1117")
    for ax in (ax1, ax2):
        ax.set_facecolor("#0d1117")
        ax.tick_params(colors="#8b949e")
        for s in ax.spines.values():
            s.set_color("#30363d")
    ax1.plot(times, closes, color="#58a6ff", linewidth=1.8, label="Koers")
    ax1.plot(times, ema_f, color="#3fb950", linewidth=1.2, label=f"EMA {EMA_FAST}")
    ax1.plot(times, ema_s, color="#f85149", linewidth=1.2, label=f"EMA {EMA_SLOW}")
    ax1.set_title(f"{res['symbol']} — ${res['price']:,.0f}  ({res['change_24h']:+.1f}% 24u)",
                  color="#e6edf3", fontsize=14, fontweight="bold")
    ax1.legend(facecolor="#161b22", edgecolor="#30363d", labelcolor="#e6edf3", loc="upper left")
    ax1.grid(color="#21262d", linewidth=0.5)
    ax2.plot(times, rsi_vals, color="#d29922", linewidth=1.2)
    ax2.axhline(RSI_OVERBOUGHT, color="#f85149", linewidth=0.8, linestyle="--")
    ax2.axhline(RSI_OVERSOLD, color="#3fb950", linewidth=0.8, linestyle="--")
    ax2.set_ylim(0, 100)
    ax2.set_ylabel("RSI", color="#8b949e")
    ax2.grid(color="#21262d", linewidth=0.5)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%d-%m"))
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()


def crypto_text(res, uitgebreid=False):
    lines = [f"*{res['symbol']}* — ${res['price']:,.2f} ({res['change_24h']:+.1f}% 24u)",
             f"Trend: {res['trend']} | RSI: {res['rsi']:.1f}"]
    if uitgebreid:
        lines += [
            f"7 dagen: {res['change_7d']:+.1f}% | ~33 dagen: {res['change_30d']:+.1f}%",
            f"Range deze periode: ${res['range_lo']:,.0f} – ${res['range_hi']:,.0f}",
            f"Positie in range: {(res['price']-res['range_lo'])/(res['range_hi']-res['range_lo'])*100:.0f}%",
        ]
    if res["opvallend"]:
        lines.append("🔥 *Opvallend:*")
        lines += [f"  • {o}" for o in res["opvallend"]]
    elif not uitgebreid:
        lines.append("Geen bijzonderheden.")
    return "\n".join(lines)


# ============ NIEUWS ============

def feed_items(feeds, per_feed=3):
    out = []
    for name, url in feeds:
        try:
            for e in feedparser.parse(url).entries[:per_feed]:
                out.append((name, e.get("title", "").strip(), e.get("link", "")))
        except Exception as err:
            print(f"Feed {name} mislukt: {err}", file=sys.stderr)
    return out


def news_block(topic, per_feed=3):
    items = feed_items(TOPIC_FEEDS[topic], per_feed)
    if not items:
        return ""
    lines = [f"{TOPIC_EMOJI[topic]} *{topic.upper()}*"]
    for name, title, link in items:
        lines.append(f"• [{title}]({link}) — _{name}_")
    return "\n".join(lines)


# ============ TELEGRAM ============

def tg_send_text(text):
    if not TELEGRAM_TOKEN:
        print(text)
        return
    r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                            "parse_mode": "Markdown", "disable_web_page_preview": True},
                      timeout=15)
    r.raise_for_status()


def tg_send_photo(png, caption):
    if not TELEGRAM_TOKEN:
        print(f"[chart] {caption}")
        return
    r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                      data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "Markdown"},
                      files={"photo": ("chart.png", png, "image/png")}, timeout=30)
    r.raise_for_status()


# ============ DROPS ============

def drop_crypto():
    coin = random.choice(COINS)
    res = analyse(coin)
    tg_send_text("🎲 *Random drop — crypto deep-dive*")
    tg_send_photo(make_chart(res), crypto_text(res, uitgebreid=True))


def drop_news(topic):
    block = news_block(topic, per_feed=2)
    if block:
        tg_send_text(f"🎲 *Random drop*\n\n{block}")


def run_random(cfg):
    if random.random() > cfg.get("random_chance", 0.25):
        print("Dobbelsteen zegt: niet nu.")
        return
    enabled = cfg.get("enabled", [])
    if not enabled:
        print("Geen onderwerpen aan.")
        return
    topic = random.choice(enabled)
    print(f"Drop: {topic}")
    if topic == "crypto":
        drop_crypto()
    else:
        drop_news(topic)


# ============ DIGEST & CHECK ============

def run_digest(cfg):
    tg_send_text(f"☀️ *Dagelijkse digest* — {datetime.now().strftime('%d-%m-%Y')}")
    enabled = cfg.get("enabled", [])
    if "crypto" in enabled:
        for coin in COINS:
            try:
                res = analyse(coin)
                tg_send_photo(make_chart(res), crypto_text(res))
                time.sleep(1)
            except Exception as e:
                print(f"Fout bij {coin}: {e}", file=sys.stderr)
    for topic in enabled:
        if topic == "crypto":
            continue
        block = news_block(topic)
        if block:
            tg_send_text(block)
            time.sleep(1)
    tg_send_text("_Spot only • analyse, geen advies • jij beslist_")
    print("Digest verstuurd.")


def run_check(cfg):
    if "crypto" not in cfg.get("enabled", []):
        return
    alerts = []
    for coin in COINS:
        try:
            res = analyse(coin)
            if res["opvallend"]:
                alerts.append((res, make_chart(res)))
            time.sleep(1)
        except Exception as e:
            print(f"Fout bij {coin}: {e}", file=sys.stderr)
    if not alerts:
        print("Niets opvallends.")
        return
    tg_send_text("⚡ *Opvallende marktsituatie*")
    for res, png in alerts:
        tg_send_photo(png, crypto_text(res))


if __name__ == "__main__":
    cfg = load_topics()
    if "--check" in sys.argv:
        run_check(cfg)
    elif "--random" in sys.argv:
        run_random(cfg)
    else:
        run_digest(cfg)
