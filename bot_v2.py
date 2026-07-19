"""
Kennisbank Bot v2 — dagelijkse digest via Telegram.

Onderdelen:
  1. Crypto-analyse (BTC/ETH spot) met chart-afbeelding (koers + EMA20/50 + RSI)
  2. Opvallendheids-alerts: alleen melding bij RSI-extremen, EMA-crossovers, volumespikes
  3. AI/tech-nieuws via RSS (gratis, geen API key nodig)

Spot-only kader. Analyses, geen koopcommando's. Jij beslist.

Gebruik:
  export TELEGRAM_TOKEN="..."
  export TELEGRAM_CHAT_ID="..."
  python3 bot_v2.py            # volledige dagelijkse digest
  python3 bot_v2.py --check    # alleen checken op opvallendheden (voor elk-uur-cron)
"""

import io
import os
import sys
import time

import requests
import feedparser
import matplotlib

matplotlib.use("Agg")  # geen scherm nodig (VPS)
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timezone

# ----------------- CONFIG -----------------
COINS = ["BTCUSDT", "ETHUSDT"]
INTERVAL = "4h"
CANDLES = 200
EMA_FAST = 20
EMA_SLOW = 50
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
VOLUME_SPIKE_FACTOR = 2.5   # volume > 2.5x gemiddelde = opvallend

NEWS_FEEDS = [
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/technology-lab"),
]
NEWS_PER_FEED = 3

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# -------------------------------------------


# ============ DATA & INDICATOREN ============

def get_klines(symbol: str, interval: str, limit: int):
    """Haal candles op: (timestamps, closes, volumes)."""
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    times = [datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc) for c in data]
    closes = [float(c[4]) for c in data]
    volumes = [float(c[5]) for c in data]
    return times, closes, volumes


def ema(prices, period):
    k = 2 / (period + 1)
    result = [sum(prices[:period]) / period]
    for price in prices[period:]:
        result.append(price * k + result[-1] * (1 - k))
    # vul begin op zodat lengte gelijk is aan prices (voor plotten)
    return [None] * (period - 1) + result


def rsi_series(prices, period=14):
    """RSI voor elke candle (Wilder's smoothing)."""
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
        if avg_loss == 0:
            values.append(100.0)
        else:
            rs = avg_gain / avg_loss
            values.append(100 - (100 / (1 + rs)))
    return values


# ============ ANALYSE ============

def analyse(symbol: str) -> dict:
    times, closes, volumes = get_klines(symbol, INTERVAL, CANDLES)
    ema_f = ema(closes, EMA_FAST)
    ema_s = ema(closes, EMA_SLOW)
    rsi_vals = rsi_series(closes, RSI_PERIOD)

    price = closes[-1]
    rsi_now = rsi_vals[-1]
    fast_now, slow_now = ema_f[-1], ema_s[-1]
    fast_prev, slow_prev = ema_f[-2], ema_s[-2]
    uptrend = fast_now > slow_now

    avg_vol = sum(volumes[-50:]) / 50
    vol_spike = volumes[-1] > avg_vol * VOLUME_SPIKE_FACTOR

    # 24h verandering (6 candles van 4h)
    change_24h = (closes[-1] - closes[-7]) / closes[-7] * 100

    opvallend = []
    if fast_prev <= slow_prev and fast_now > slow_now:
        opvallend.append("EMA bullish crossover — trenddraai omhoog")
    if fast_prev >= slow_prev and fast_now < slow_now:
        opvallend.append("EMA bearish crossover — trenddraai omlaag")
    if rsi_now >= RSI_OVERBOUGHT:
        opvallend.append(f"RSI overbought ({rsi_now:.0f}) — markt oververhit, correctierisico")
    if rsi_now <= RSI_OVERSOLD:
        opvallend.append(f"RSI oversold ({rsi_now:.0f}) — zwaar afgestraft, mogelijk herstel")
    if vol_spike:
        opvallend.append(f"Volumespike ({volumes[-1]/avg_vol:.1f}x gemiddeld) — grote spelers actief")
    if abs(change_24h) >= 5:
        opvallend.append(f"Grote 24u-beweging: {change_24h:+.1f}%")

    return {
        "symbol": symbol.replace("USDT", ""),
        "price": price,
        "rsi": rsi_now,
        "trend": "UP" if uptrend else "DOWN",
        "change_24h": change_24h,
        "opvallend": opvallend,
        "_plot": (times, closes, ema_f, ema_s, rsi_vals),
    }


def make_chart(result: dict) -> bytes:
    """Teken koers + EMA's + RSI, retourneer PNG-bytes."""
    times, closes, ema_f, ema_s, rsi_vals = result["_plot"]

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(10, 7), gridspec_kw={"height_ratios": [3, 1]}, sharex=True
    )
    fig.patch.set_facecolor("#0d1117")
    for ax in (ax1, ax2):
        ax.set_facecolor("#0d1117")
        ax.tick_params(colors="#8b949e")
        for spine in ax.spines.values():
            spine.set_color("#30363d")

    ax1.plot(times, closes, color="#58a6ff", linewidth=1.8, label="Koers")
    ax1.plot(times, ema_f, color="#3fb950", linewidth=1.2, label=f"EMA {EMA_FAST}")
    ax1.plot(times, ema_s, color="#f85149", linewidth=1.2, label=f"EMA {EMA_SLOW}")
    ax1.set_title(
        f"{result['symbol']} — ${result['price']:,.0f}  ({result['change_24h']:+.1f}% 24u)",
        color="#e6edf3", fontsize=14, fontweight="bold",
    )
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


def crypto_text(result: dict) -> str:
    lines = [
        f"*{result['symbol']}* — ${result['price']:,.2f} ({result['change_24h']:+.1f}% 24u)",
        f"Trend: {result['trend']} | RSI: {result['rsi']:.1f}",
    ]
    if result["opvallend"]:
        lines.append("🔥 *Opvallend:*")
        lines += [f"  • {o}" for o in result["opvallend"]]
    else:
        lines.append("Geen bijzonderheden — rustige dag.")
    return "\n".join(lines)


# ============ NIEUWS ============

def get_news() -> str:
    blocks = ["📰 *AI & Tech nieuws*"]
    for name, url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(url)
            items = feed.entries[:NEWS_PER_FEED]
            if not items:
                continue
            blocks.append(f"\n_{name}_")
            for e in items:
                title = e.get("title", "").strip()
                link = e.get("link", "")
                blocks.append(f"• [{title}]({link})")
        except Exception as err:
            print(f"Feed {name} mislukt: {err}", file=sys.stderr)
    return "\n".join(blocks) if len(blocks) > 1 else ""


# ============ TELEGRAM ============

def tg_send_text(text: str):
    if not TELEGRAM_TOKEN:
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID, "text": text,
        "parse_mode": "Markdown", "disable_web_page_preview": True,
    }, timeout=15)
    r.raise_for_status()


def tg_send_photo(png: bytes, caption: str):
    if not TELEGRAM_TOKEN:
        print(f"[chart] {caption}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    r = requests.post(
        url,
        data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "Markdown"},
        files={"photo": ("chart.png", png, "image/png")},
        timeout=30,
    )
    r.raise_for_status()


# ============ MAIN ============

def run_digest():
    """Volledige dagelijkse digest: charts + analyse + nieuws."""
    tg_send_text(f"☀️ *Dagelijkse digest* — {datetime.now().strftime('%d-%m-%Y')}")
    for coin in COINS:
        try:
            res = analyse(coin)
            tg_send_photo(make_chart(res), crypto_text(res))
            time.sleep(1)
        except Exception as e:
            print(f"Fout bij {coin}: {e}", file=sys.stderr)
    news = get_news()
    if news:
        tg_send_text(news)
    tg_send_text("_Spot only • analyse, geen advies • jij beslist_")
    print("Digest verstuurd.")


def run_check():
    """Stil checken; alleen sturen bij opvallendheden. Voor elk-uur-cron."""
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
    if "--check" in sys.argv:
        run_check()
    else:
        run_digest()
