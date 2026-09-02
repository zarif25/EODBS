import sys
import os
import json
import logging
import time as time_module
from datetime import datetime, time, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import yfinance as yf
import requests
import holidays
import html


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")

# --- CONFIGURATION ---
# EOD Bursa Scanner: runs once daily at 8:00 AM MYT (pre-market), scanning
# the previous trading day's End-Of-Day data. Only two signals are checked:
#   1. Bullish Zone   -> Price > EMA20 > EMA50 > EMA200
#   2. Pending Breakout -> price approaching (but not yet at) its 52-week high
# Both signals additionally require: close above previous daily close, and
# volume above 500,000 shares.
MIN_PRICE = 0.205
MAX_PRICE = 7.05
MIN_VOLUME = 500_000
PENDING_BREAKOUT_PCT = 7.0

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STOCKS_FILE = os.path.join(BASE_DIR, "stocks.json")
DEDUP_FILE = os.path.join(BASE_DIR, "alerted_today.json")

def load_tickers():
    """Reads stocks.json and extracts the ticker codes and names."""
    try:
        with open(STOCKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            logging.info(f"✅ Successfully loaded {len(data)} tickers from stocks.json")
            return data
    except FileNotFoundError:
        logging.error(f"❌ {STOCKS_FILE} not found!")
        return []
    except Exception as e:
        logging.error(f"❌ Error reading stocks.json: {e}")
        return []

STOCKS = load_tickers()

def get_bursa_tickers():
    """Returns a list of ticker codes for test_setup.py compatibility."""
    return [stock.get("code") for stock in STOCKS if stock.get("code")]

def analyze(ticker):
    """Runs signal analysis for a single ticker for test_setup.py compatibility."""
    try:
        df = get_history(ticker)
        if df.empty:
            return None
        signals = compute_signals(df)
        if signals:
            return {"signals": signals}
    except Exception as e:
        logging.error(f"Error analyzing {ticker}: {e}")
    return None

# Match the names in your scanner.yml file
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID")


# Instantiate Malaysian holidays across past, current, and upcoming year
_cur_year = datetime.now().year
MY_HOLIDAYS = holidays.MY(years=[_cur_year - 1, _cur_year, _cur_year + 1])

def should_run():
    """Check if it's a weekday and not a Malaysian public holiday.

    This EOD scanner fires once daily at 8:00 AM MYT (before market open),
    so there is no intraday trading-hours window to check here — the
    GitHub Actions cron schedule is what controls the time of day.
    """
    import sys
    if os.getenv("FORCE_RUN") == "true" or "--force" in sys.argv:
        logging.info("💪 Force run enabled. Bypassing schedule/holiday checks.")
        return True

    now = datetime.now(timezone(timedelta(hours=8)))

    # Check if weekend (Saturday=5, Sunday=6)
    if now.weekday() >= 5:
        logging.info("📆 Today is a weekend. Skipping scan.")
        return False

    # Check if public holiday in Malaysia
    if now.date() in MY_HOLIDAYS:
        logging.info("🎉 Today is a Malaysian Public Holiday. Skipping scan.")
        return False

    return True

# --- DEDUP LOGIC ---
def get_today_str():
    # Get today's date in Malaysia Time
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def load_alerted_today():
    """Loads the list of already alerted stocks for today."""
    today = get_today_str()
    try:
        with open(DEDUP_FILE, "r") as f:
            data = json.load(f)
            # If the file is from a previous day, reset the list
            if data.get("date") == today:
                return set(data.get("alerted", []))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return set()

def save_alerted_today(alerted_set):
    """Saves the updated list back to the JSON file."""
    today = get_today_str()
    data = {
        "date": today,
        "alerted": list(alerted_set)
    }
    with open(DEDUP_FILE, "w") as f:
        json.dump(data, f, indent=4)

def get_history(ticker):
    df = yf.download(
        ticker,
        period="2y",
        interval="1d",
        progress=False,
        auto_adjust=False
    )
    if df is None or df.empty:
        return pd.DataFrame()
    
    # Fix for newer yfinance versions returning MultiIndex columns
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
        
    df = df.dropna().copy()
    df.columns = [str(c).capitalize() for c in df.columns]
    return df

def compute_signals(df):
    """EOD Bursa Scanner signal engine.

    Only two signals are scanned for:
      - Bullish Zone     : Price > EMA20 > EMA50 > EMA200
      - Pending Breakout : price within PENDING_BREAKOUT_PCT of the 52-week high

    Rule (applies before either signal can fire): today's close must be
    above the previous trading day's close. The volume-above-500,000 rule
    is enforced separately in scan_ticker()/main() alongside the price-range
    filter, matching how the original scanner applies its pre-conditions.
    """
    if len(df) < 250:
        return []

    df = df.copy()
    df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()
    df["EMA50"] = df["Close"].ewm(span=50, adjust=False).mean()
    df["EMA200"] = df["Close"].ewm(span=200, adjust=False).mean()

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    current_price = float(latest["Close"])
    prev_close = float(prev["Close"])

    # Rule: price must close above yesterday's / previous daily close
    if current_price <= prev_close:
        return []

    signals = []

    # Bullish Zone (Price > EMA20 > EMA50 > EMA200)
    if not pd.isna(latest["EMA20"]) and not pd.isna(latest["EMA50"]) and not pd.isna(latest["EMA200"]):
        if current_price > latest["EMA20"] > latest["EMA50"] > latest["EMA200"]:
            signals.append("Bullish Zone")

    high_52w = float(df.tail(252)["High"].max())

    # Pending Breakout (within PENDING_BREAKOUT_PCT of 52WH, but not yet at it)
    if high_52w > 0 and high_52w * (1 - PENDING_BREAKOUT_PCT / 100) <= current_price < high_52w * 0.995:
        signals.append("Pending Breakout")

    return signals

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("⚠️ Telegram credentials missing in GitHub Secrets.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    
    try:
        r = requests.post(url, data=payload, timeout=20)
        r.raise_for_status()
        return True
    except requests.exceptions.HTTPError as e:
        err_msg = e.response.text if e.response is not None else str(e)
        if e.response is not None and e.response.status_code == 429:
            try:
                retry_after = int(e.response.json().get("parameters", {}).get("retry_after", 30))
            except Exception:
                retry_after = 30
            logging.warning(f"⚠️ Telegram rate limit hit (429). Retrying after {retry_after} seconds...")
            time_module.sleep(retry_after)
            try:
                r = requests.post(url, data=payload, timeout=20)
                r.raise_for_status()
                return True
            except Exception as retry_err:
                logging.error(f"❌ Telegram API retry failed: {retry_err}")
                return False
        else:
            logging.error(f"❌ Telegram HTTP Error: {err_msg}")
            return False
    except Exception as e:
        logging.error(f"❌ Telegram connection/request failed: {e}")
        return False

TELEGRAM_MAX_CHARS = 4096
TABLE_NAME_WIDTH = 15  # Syarikat column is truncated to this width so rows stay aligned

def _fmt_row(code_w, name_w, price_w, trigger_w, code, name, harga, trigger):
    name_trunc = (name[: TABLE_NAME_WIDTH - 1] + "…") if len(name) > TABLE_NAME_WIDTH else name
    return f"{code:<{code_w}} {name_trunc:<{name_w}} {harga:<{price_w}} {trigger:<{trigger_w}}"

def format_results_table(results):
    """Builds the EOD scan result as a monospace table with columns:
    Code, Syarikat, Harga, Trigger — one row per stock that matched.

    Telegram caps a single message at 4096 characters, so if the full
    table doesn't fit, it is split across multiple messages, each a
    complete, independently-readable table (repeats the header/date).
    Returns a list of message strings (empty list if no results).
    """
    if not results:
        return []

    today = get_today_str()
    title = f"<b>📊 EOD Bursa Scanner — {today}</b>\n\n"

    code_w = max(4, max(len(r["ticker"]) for r in results))
    name_w = min(TABLE_NAME_WIDTH, max(8, max(len(r["name"]) for r in results)))
    price_w = 7
    trigger_w = max(7, max(len(", ".join(r["signals"])) for r in results))

    header_row = _fmt_row(code_w, name_w, price_w, trigger_w, "Code", "Syarikat", "Harga", "Trigger")
    sep_row = "-" * len(header_row)

    def wrap(lines):
        body = html.escape("\n".join(lines))
        return f"{title}<pre>{body}</pre>"

    messages = []
    current_lines = [header_row, sep_row]

    for r in results:
        row = _fmt_row(
            code_w, name_w, price_w, trigger_w,
            r["ticker"], r["name"], f"{r['price']:.3f}", ", ".join(r["signals"]),
        )
        # +1 rough allowance for the newline joining this row
        if len(wrap(current_lines + [row])) > TELEGRAM_MAX_CHARS - 20:
            messages.append(wrap(current_lines))
            current_lines = [header_row, sep_row]
        current_lines.append(row)

    messages.append(wrap(current_lines))
    return messages

def scan_ticker(ticker, name, alerted_set=None):
    try:
        df = get_history(ticker)
        if df.empty:
            logging.info(f"📊 {name} ({ticker}): No data found, skip.")
            return None

        # --- PRE-CONDITIONS ---
        # 1. Price Range check
        current_price = float(df.iloc[-1]["Close"])
        if not (MIN_PRICE <= current_price <= MAX_PRICE):
            logging.info(f"💰 {name} ({ticker}): Price {current_price:.3f} out of range ({MIN_PRICE} - {MAX_PRICE}), skip.")
            return None

        # 2. Volume filter (must be above MIN_VOLUME)
        current_volume = float(df.iloc[-1]["Volume"])
        if current_volume <= MIN_VOLUME:
            logging.info(f"📊 {name} ({ticker}): Volume {current_volume:,.0f} <= {MIN_VOLUME:,.0f}, skip.")
            return None

        signals = compute_signals(df)
        if not signals:
            logging.info(f"🚫 {name} ({ticker}): No signal triggered.")
            return None

        return {
            "ticker": ticker,
            "name": name,
            "price": current_price,
            "signals": signals
        }
        
    except Exception as e:
        logging.error(f"❌ {name} ({ticker}): Error occurred - {e}")
        return None

def main():
    logging.info("🤖 Starting EOD Bursa Scanner...")
    
    # Check if we should run today
    if not should_run():
        logging.info("⏹️ Script finished early due to weekend, holiday, or outside trading hours.")
        return

    if not STOCKS:
        logging.error("❌ No stocks loaded. Exiting.")
        return

    # Load today's already alerted stocks
    alerted_set = load_alerted_today()
    logging.info(f"📋 {len(alerted_set)} stocks have already been alerted today.")

    stocks_to_scan = []
    for stock in STOCKS:
        ticker = stock.get("code")
        name = stock.get("name", ticker)
        if ticker in alerted_set:
            logging.info(f"⏳ {name} ({ticker}): Already alerted today, skip.")
        elif ticker:
            stocks_to_scan.append(stock)

    if not stocks_to_scan:
        logging.info("📋 All stocks have already been alerted today. Nothing to scan.")
        return

    tickers_to_download = [s.get("code") for s in stocks_to_scan]
    logging.info(f"Downloading data for {len(tickers_to_download)} stocks in bulk...")

    try:
        df_all = yf.download(
            tickers_to_download,
            period="2y",
            interval="1d",
            progress=False,
            group_by="ticker",
            auto_adjust=False
        )
    except Exception as e:
        logging.error(f"❌ Failed to bulk download tickers: {e}")
        return

    results = []
    logging.info("Analyzing stock data...")

    for stock in stocks_to_scan:
        ticker = stock.get("code")
        name = stock.get("name", ticker)
        
        try:
            # Check if ticker is present in downloaded columns
            if isinstance(df_all.columns, pd.MultiIndex):
                if ticker not in df_all.columns:
                    logging.info(f"📊 {name} ({ticker}): No data found in bulk download, skip.")
                    continue
                df = df_all[ticker].dropna(subset=["Close", "Volume"]).copy()
            else:
                df = df_all.dropna(subset=["Close", "Volume"]).copy()

            if df.empty:
                logging.info(f"📊 {name} ({ticker}): Data is empty after dropna, skip.")
                continue

            # Standardize columns to match compute_signals expectations
            df.columns = [str(c).capitalize() for c in df.columns]

            # --- PRE-CONDITIONS ---
            # 1. Price Range check
            current_price = float(df.iloc[-1]["Close"])
            if not (MIN_PRICE <= current_price <= MAX_PRICE):
                logging.info(f"💰 {name} ({ticker}): Price {current_price:.3f} out of range ({MIN_PRICE} - {MAX_PRICE}), skip.")
                continue

            # 2. Volume filter (must be above MIN_VOLUME)
            current_volume = float(df.iloc[-1]["Volume"])
            if current_volume <= MIN_VOLUME:
                logging.info(f"📊 {name} ({ticker}): Volume {current_volume:,.0f} <= {MIN_VOLUME:,.0f}, skip.")
                continue

            signals = compute_signals(df)
            if not signals:
                logging.info(f"🚫 {name} ({ticker}): No signal triggered.")
                continue

            results.append({
                "ticker": ticker,
                "name": name,
                "price": current_price,
                "signals": signals
            })

        except Exception as e:
            logging.error(f"❌ {name} ({ticker}): Error occurred during scan - {e}")

    logging.info(f"Scan finished. Found {len(results)} stocks with signals.")

    # Send one consolidated table (Code / Syarikat / Harga / Trigger) instead
    # of one message per stock. Split across multiple messages only if the
    # table doesn't fit Telegram's 4096-character limit.
    if not results:
        logging.info("📭 No stocks matched Bullish Zone / Pending Breakout today. No Telegram message sent.")
        return

    results.sort(key=lambda x: x["ticker"])
    messages = format_results_table(results)
    logging.info(f"Sending {len(messages)} Telegram message(s) covering {len(results)} matching stock(s)...")

    all_sent = True
    for i, msg in enumerate(messages, start=1):
        if send_telegram(msg):
            logging.info(f"🚀 Sent EOD summary table {i}/{len(messages)} to Telegram.")
        else:
            logging.error(f"❌ Failed to send EOD summary table {i}/{len(messages)} to Telegram.")
            all_sent = False
        # Sleep briefly between messages to respect Telegram rate limits
        time_module.sleep(0.5)

    if all_sent:
        for res in results:
            alerted_set.add(res["ticker"])
        save_alerted_today(alerted_set)
    else:
        logging.error("❌ Not all summary messages sent successfully; alerted_today.json left unchanged.")
        sys.exit(1)

if __name__ == "__main__":
    main()
