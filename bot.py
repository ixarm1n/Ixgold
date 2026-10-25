import os
import asyncio
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import feedparser
import pandas as pd
import yfinance as yf
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.environ["BOT_TOKEN"]
SYMBOL = "GC=F"  # قیمت آتی طلا (اونس)
TIMEFRAMES = ["5m", "15m", "30m", "1h", "4h", "1d"]
PERIODS = {"5m": "30d", "15m": "30d", "30m": "30d", "1h": "60d", "4h": "60d", "1d": "1y"}

NEWS_URL = (
    "https://news.google.com/rss/search?"
    "q=gold+price+OR+federal+reserve+OR+inflation+OR+dollar&hl=en-US&gl=US&ceid=US:en"
)
BULLISH = ["rate cut", "dovish", "weak dollar", "dollar falls", "yields fall", "safe haven",
           "war", "geopolitical", "tension", "gold rises", "gold jumps", "gold hits record"]
BEARISH = ["rate hike", "hawkish", "strong dollar", "dollar rises", "yields rise",
           "gold falls", "gold drops", "gold slips", "risk-on"]

logging.basicConfig(level=logging.INFO)


# ---------- داده ----------
def get_data(tf: str) -> pd.DataFrame:
    interval = "1h" if tf == "4h" else tf  # تایم ۴ ساعته از روی ۱ ساعته ساخته میشه
    df = yf.download(SYMBOL, period=PERIODS[tf], interval=interval,
                     progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close"]].dropna()
    if tf == "4h":
        df = df.resample("4h").agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
        ).dropna()
    return df


# ---------- پرایس اکشن ----------
def find_swings(df: pd.DataFrame, n: int = 2):
    highs, lows = [], []
    for i in range(n, len(df) - n):
        w = df.iloc[i - n:i + n + 1]
        if df["High"].iloc[i] == w["High"].max():
            highs.append(float(df["High"].iloc[i]))
        if df["Low"].iloc[i] == w["Low"].min():
            lows.append(float(df["Low"].iloc[i]))
    return highs, lows


def atr(df: pd.DataFrame, period: int = 14) -> float:
    prev = df["Close"].shift()
    tr = pd.concat([df["High"] - df["Low"],
                    (df["High"] - prev).abs(),
                    (df["Low"] - prev).abs()], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def candle_pattern(df: pd.DataFrame) -> str:
    c, p = df.iloc[-2], df.iloc[-3]  # آخرین کندل بسته‌شده
    body = abs(c.Close - c.Open)
    rng = c.High - c.Low
    if rng == 0:
        return "بدون الگو"
    upper = c.High - max(c.Open, c.Close)
    lower = min(c.Open, c.Close) - c.Low
    if p.Close < p.Open and c.Close > c.Open and c.Close >= p.Open and c.Open <= p.Close:
        return "اینگالف صعودی 🟢"
    if p.Close > p.Open and c.Close < c.Open and c.Open >= p.Close and c.Close <= p.Open:
        return "اینگالف نزولی 🔴"
    if body <= 0.1 * rng:
        return "دوجی (تردید بازار) ⚪"
    if lower >= 2 * body and upper <= body:
        return "پین‌بار صعودی / چکش 🟢"
    if upper >= 2 * body and lower <= body:
        return "پین‌بار نزولی / شوتینگ استار 🔴"
    return "بدون الگوی خاص"


def analyze(tf: str) -> str:
    df = get_data(tf)
    if len(df) < 30:
        return f"[{tf}] داده کافی نیست."
    price = float(df["Close"].iloc[-1])
    highs, lows = find_swings(df)
    if len(highs) < 2 or len(lows) < 2:
        return f"[{tf}] ساختار بازار مشخص نیست."

    hh = highs[-1] > highs[-2]
    hl = lows[-1] > lows[-2]
    if hh and hl:
        trend, bias = "صعودی (HH + HL) 📈", "تمایل به خرید در اصلاح‌ها"
    elif not hh and not hl:
        trend, bias = "نزولی (LH + LL) 📉", "تمایل به فروش در پولبک‌ها"
    else:
        trend, bias = "رنج / نامشخص ↔️", "بهتره منتظر شکست سطوح بمونی"

    res = [h for h in highs[-8:] if h > price]
    sup = [l for l in lows[-8:] if l < price]
    resistance = min(res) if res else None
    support = max(sup) if sup else None
    a = atr(df)

    lines = [
        f"⏱ تایم‌فریم {tf}",
        f"قیمت: {price:,.1f}",
        f"روند: {trend}",
        f"مقاومت نزدیک: {resistance:,.1f}" if resistance else "مقاومت نزدیک: (سقف جدید)",
        f"حمایت نزدیک: {support:,.1f}" if support else "حمایت نزدیک: (کف جدید)",
        f"کندل آخر: {candle_pattern(df)}",
        f"نوسان (ATR): {a:,.1f}",
        f"نتیجه: {bias}",
    ]
    return "\n".join(lines)


# ---------- اخبار ----------
def get_news() -> str:
    feed = feedparser.parse(NEWS_URL)
    items, score = [], 0
    for e in feed.entries[:25]:
        t = e.title.lower()
        s = sum(k in t for k in BULLISH) - sum(k in t for k in BEARISH)
        if s != 0:
            items.append((s, e.title))
        score += s
    if not items:
        return "خبر مهم و حساسی پیدا نشد."
    mood = "صعودی 🟢" if score > 0 else "نزولی 🔴" if score < 0 else "خنثی ⚪"
    out = [f"📰 حس کلی خبرها برای طلا: {mood}\n"]
    for s, title in items[:6]:
        out.append(f"{'🟢' if s > 0 else '🔴'} {title}")
    return "\n".join(out)


# ---------- دستورات تلگرام ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! من ربات تحلیلگر طلا هستم 🥇\n\n"
        "/analysis - تحلیل همه تایم‌فریم‌ها (۵ دقیقه تا ۱ روز)\n"
        "/analysis 15m - تحلیل یه تایم‌فریم (5m 15m 30m 1h 4h 1d)\n"
        "/news - اخبار حساس و حس بازار\n"
        "/full - تحلیل کامل (قیمت + خبر)\n\n"
        "⚠️ این تحلیل‌ها توصیه مالی نیست."
    )


async def analysis(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tfs = TIMEFRAMES
    if ctx.args:
        tf = ctx.args[0].lower()
        if tf not in TIMEFRAMES:
            await update.message.reply_text("تایم‌فریم نامعتبر. از این‌ها استفاده کن: " + " ".join(TIMEFRAMES))
            return
        tfs = [tf]
    await update.message.reply_text("در حال تحلیل...")
    try:
        parts = [await asyncio.to_thread(analyze, tf) for tf in tfs]
        await update.message.reply_text("\n\n".join(parts))
    except Exception as ex:
        logging.exception(ex)
        await update.message.reply_text("خطا در گرفتن داده. چند دقیقه بعد دوباره امتحان کن.")


async def news(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text(await asyncio.to_thread(get_news))
    except Exception as ex:
        logging.exception(ex)
        await update.message.reply_text("خطا در گرفتن اخبار.")


async def full(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await analysis(update, ctx)
    await news(update, ctx)


# ---------- سرور کوچک برای هاست‌های رایگان ----------
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def serve():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), Health).serve_forever()


def main():
    threading.Thread(target=serve, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("analysis", analysis))
    app.add_handler(CommandHandler("news", news))
    app.add_handler(CommandHandler("full", full))
    app.run_polling()


if __name__ == "__main__":
    main()
