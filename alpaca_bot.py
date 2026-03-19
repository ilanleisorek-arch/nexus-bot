"""
╔══════════════════════════════════════════════════════════════╗
║           NEXUS TRADING BOT  v3  — Alpaca Paper Trading      ║
║           100 tickers · 60s scans · Aggressive Mode          ║
║           Stop: 7%  |  Take Profit: 18%                      ║
║           Telegram notifications enabled                     ║
╚══════════════════════════════════════════════════════════════╝

SETUP (one-time):
  1. Create a free account at https://alpaca.markets
  2. Go to Paper Trading → API Keys → Generate new key
  3. Paste your API_KEY and SECRET_KEY below
  4. Install deps:
       pip install alpaca-trade-api pandas numpy ta schedule requests
  5. Run:
       python3 alpaca_bot.py
"""

import time
import logging
import schedule
import requests
import pandas as pd
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import alpaca_trade_api as tradeapi
from alpaca_trade_api.rest import TimeFrame
import ta

# ─────────────────────────────────────────────
#  CONFIG  —  paste your keys here
# ─────────────────────────────────────────────
API_KEY    = "YOUR_ALPACA_API_KEY"
SECRET_KEY = "YOUR_ALPACA_SECRET_KEY"
BASE_URL   = "https://paper-api.alpaca.markets"

TELEGRAM_TOKEN   = "YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "5383766592"

# ─────────────────────────────────────────────
#  RISK PARAMETERS
# ─────────────────────────────────────────────
MAX_POSITION_PCT   = 0.15
STOP_LOSS_PCT      = 0.07
TAKE_PROFIT_PCT    = 0.18
MAX_OPEN_POSITIONS = 8
MIN_CASH_RESERVE   = 0.08
SCAN_WORKERS       = 10

# ─────────────────────────────────────────────
#  100-TICKER WATCHLIST
# ─────────────────────────────────────────────
WATCHLIST = [
    "NVDA", "AMD", "TSLA", "META", "GOOGL", "AMZN", "MSFT", "AAPL",
    "NFLX", "ORCL", "ADBE", "CRM", "NOW", "SNOW",
    "COIN", "MSTR", "BITO", "MARA", "RIOT", "HUT", "CLSK", "BTBT",
    "PYPL", "SQ", "AFRM", "SOFI", "UPST",
    "SMCI", "AVGO", "QCOM", "MU", "INTC", "ARM", "MRVL", "LRCX",
    "AMAT", "KLAC", "ASML", "TER", "ONTO",
    "PLTR", "RBLX", "DKNG", "HOOD", "SOUN", "BBAI",
    "IONQ", "RGTI", "QUBT", "QBTS",
    "RIVN", "LCID", "NIO", "LI", "XPEV", "CHPT", "BLNK", "PLUG",
    "FCEL", "BE", "ENPH", "SEDG",
    "MRNA", "BNTX", "CRSP", "BEAM", "EDIT", "NTLA", "RXRX",
    "KURA", "VERV",
    "TQQQ", "SOXL", "LABU", "FNGU", "TECL", "UPRO",
    "SPY", "QQQ", "ARKK", "ARKG", "ARKW", "XLK", "XBI", "SMH",
    "IWM", "JETS",
    "GME", "AMC", "SPCE", "WKHS", "NKLA", "OSTK",
    "CLOV", "SKLZ", "OPEN", "LMND", "ROOT",
    "UWMC", "RKT", "HIMS", "ACHR", "JOBY",
]
WATCHLIST = list(dict.fromkeys(WATCHLIST))[:100]

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.FileHandler("nexus_bot.log"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("nexus")


# ─────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(message: str):
    """Send a message to Telegram. Fails silently so bot keeps running."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        }, timeout=5)
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")


# ─────────────────────────────────────────────
#  DATA STRUCTURES
# ─────────────────────────────────────────────
@dataclass
class Signal:
    ticker: str
    action: str
    strategy: str
    strength: float
    reason: str


# ─────────────────────────────────────────────
#  ALPACA CLIENT
# ─────────────────────────────────────────────
api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL, api_version="v2")

def get_account():          return api.get_account()
def get_positions():        return {p.symbol: p for p in api.list_positions()}
def get_portfolio_value():  return float(api.get_account().portfolio_value)
def get_cash():             return float(api.get_account().cash)


# ─────────────────────────────────────────────
#  MARKET DATA
# ─────────────────────────────────────────────
def get_bars(ticker: str, days: int = 20) -> Optional[pd.DataFrame]:
    for attempt in range(3):
        try:
            end   = datetime.utcnow()
            start = end - timedelta(days=days)
            bars  = api.get_bars(
                ticker, TimeFrame.Hour,
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                limit=400, adjustment="raw",
            ).df
            if bars.empty or len(bars) < 20:
                return None
            return bars.reset_index()
        except Exception:
            if attempt == 2:
                return None
            time.sleep(0.5)


# ─────────────────────────────────────────────
#  STRATEGIES
# ─────────────────────────────────────────────
def momentum_signal(ticker, df):
    close   = df["close"]
    ma_fast = close.ewm(span=8).mean()
    ma_slow = close.ewm(span=21).mean()
    rsi     = ta.momentum.RSIIndicator(close, window=14).rsi()
    rsi_now = rsi.iloc[-1]
    if ma_fast.iloc[-1] > ma_slow.iloc[-1] and ma_fast.iloc[-2] <= ma_slow.iloc[-2] and rsi_now < 68:
        gap = (ma_fast.iloc[-1] - ma_slow.iloc[-1]) / ma_slow.iloc[-1]
        return Signal(ticker, "BUY", "Momentum", min(gap * 25 + 0.4, 1.0), f"EMA cross ↑ RSI={rsi_now:.0f}")
    if ma_fast.iloc[-1] < ma_slow.iloc[-1] and ma_fast.iloc[-2] >= ma_slow.iloc[-2] and rsi_now > 35:
        return Signal(ticker, "SELL", "Momentum", 0.7, f"EMA cross ↓ RSI={rsi_now:.0f}")
    return Signal(ticker, "HOLD", "Momentum", 0.0, "")


def breakout_signal(ticker, df):
    close     = df["close"]
    volume    = df["volume"]
    high20    = close.rolling(20).max().shift(1)
    low20     = close.rolling(20).min().shift(1)
    vol_ratio = volume.iloc[-1] / (volume.rolling(20).mean().iloc[-1] + 1e-9)
    if close.iloc[-1] > high20.iloc[-1] and vol_ratio > 1.4:
        pct = (close.iloc[-1] - high20.iloc[-1]) / high20.iloc[-1]
        return Signal(ticker, "BUY", "Breakout", min(pct * 35 + 0.45, 1.0), f"20-bar high break vol={vol_ratio:.1f}x")
    if close.iloc[-1] < low20.iloc[-1] and vol_ratio > 1.4:
        return Signal(ticker, "SELL", "Breakout", 0.8, f"20-bar low break vol={vol_ratio:.1f}x")
    return Signal(ticker, "HOLD", "Breakout", 0.0, "")


def mean_reversion_signal(ticker, df):
    close   = df["close"]
    bb      = ta.volatility.BollingerBands(close, window=20, window_dev=2)
    rsi     = ta.momentum.RSIIndicator(close, window=14).rsi()
    rsi_now = rsi.iloc[-1]
    price   = close.iloc[-1]
    if price <= bb.bollinger_lband().iloc[-1] and rsi_now < 32:
        pct = (bb.bollinger_lband().iloc[-1] - price) / bb.bollinger_lband().iloc[-1]
        return Signal(ticker, "BUY", "MeanRev", min(pct * 20 + 0.45, 1.0), f"BB lower RSI={rsi_now:.0f}")
    if price >= bb.bollinger_hband().iloc[-1] and rsi_now > 70:
        return Signal(ticker, "SELL", "MeanRev", 0.75, f"BB upper RSI={rsi_now:.0f}")
    return Signal(ticker, "HOLD", "MeanRev", 0.0, "")


def macd_signal(ticker, df):
    close    = df["close"]
    macd_ind = ta.trend.MACD(close, window_slow=26, window_fast=12, window_sign=9)
    macd     = macd_ind.macd()
    sig      = macd_ind.macd_signal()
    hist     = macd_ind.macd_diff()
    if macd.iloc[-1] > sig.iloc[-1] and macd.iloc[-2] <= sig.iloc[-2] and hist.iloc[-1] > hist.iloc[-2]:
        return Signal(ticker, "BUY", "MACD", min(abs(float(hist.iloc[-1])) * 10 + 0.5, 1.0), "MACD bullish cross")
    if macd.iloc[-1] < sig.iloc[-1] and macd.iloc[-2] >= sig.iloc[-2] and hist.iloc[-1] < hist.iloc[-2]:
        return Signal(ticker, "SELL", "MACD", 0.7, "MACD bearish cross")
    return Signal(ticker, "HOLD", "MACD", 0.0, "")


# ─────────────────────────────────────────────
#  SIGNAL AGGREGATOR
# ─────────────────────────────────────────────
def evaluate_ticker(ticker: str) -> Optional[Signal]:
    df = get_bars(ticker)
    if df is None:
        return None
    signals = [momentum_signal(ticker, df), breakout_signal(ticker, df),
               mean_reversion_signal(ticker, df), macd_signal(ticker, df)]
    buys  = [s for s in signals if s.action == "BUY"]
    sells = [s for s in signals if s.action == "SELL"]
    if len(buys) >= 2:
        best = max(buys, key=lambda s: s.strength)
        return Signal(ticker, "BUY", " + ".join(s.strategy for s in buys),
                      best.strength, " | ".join(s.reason for s in buys))
    if len(sells) >= 2:
        best = max(sells, key=lambda s: s.strength)
        return Signal(ticker, "SELL", " + ".join(s.strategy for s in sells),
                      best.strength, " | ".join(s.reason for s in sells))
    active = [s for s in signals if s.action != "HOLD"]
    if active:
        best = max(active, key=lambda s: s.strength)
        if best.strength >= 0.80:
            return best
    return None


def scan_all_tickers() -> list:
    results = []
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as executor:
        futures = {executor.submit(evaluate_ticker, t): t for t in WATCHLIST}
        for future in as_completed(futures):
            sig = future.result()
            if sig is not None:
                results.append(sig)
    return sorted(results, key=lambda s: s.strength, reverse=True)


# ─────────────────────────────────────────────
#  ORDER EXECUTION
# ─────────────────────────────────────────────
def place_buy(signal: Signal, positions: dict):
    ticker = signal.ticker
    portfolio_val = get_portfolio_value()
    cash = get_cash()
    if len(positions) >= MAX_OPEN_POSITIONS or ticker in positions:
        return
    avail = max(cash - portfolio_val * MIN_CASH_RESERVE, 0)
    if avail < 5:
        return
    dollars = round(min(portfolio_val * MAX_POSITION_PCT * signal.strength, avail), 2)
    if dollars < 1:
        return
    try:
        api.submit_order(symbol=ticker, notional=dollars,
                         side="buy", type="market", time_in_force="day")
        msg = (f"✅ <b>BUY {ticker}</b>\n"
               f"💰 Amount: ${dollars:.2f}\n"
               f"📊 Strategy: {signal.strategy}\n"
               f"📝 Reason: {signal.reason}\n"
               f"💪 Strength: {signal.strength:.0%}")
        log.info(f"  ✅ BUY  {ticker:<6} ${dollars:>8.2f}  [{signal.strategy}]")
        send_telegram(msg)
    except Exception as e:
        log.error(f"  ❌ BUY FAILED {ticker}: {e}")


def place_sell(signal: Signal, qty: str, entry_price: float = 0):
    try:
        current = float(api.get_last_trade(signal.ticker).price) if entry_price else 0
        pnl_pct = ((current - entry_price) / entry_price * 100) if entry_price else 0
        api.submit_order(symbol=signal.ticker, qty=qty,
                         side="sell", type="market", time_in_force="day")
        emoji = "🟢" if pnl_pct >= 0 else "🔴"
        msg = (f"{emoji} <b>SELL {signal.ticker}</b>\n"
               f"📦 Qty: {qty}\n"
               f"📊 Strategy: {signal.strategy}\n"
               f"📝 Reason: {signal.reason}\n"
               f"{'📈' if pnl_pct >= 0 else '📉'} P&L: {'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}%")
        log.info(f"  ✅ SELL {signal.ticker:<6} qty={qty}")
        send_telegram(msg)
    except Exception as e:
        log.error(f"  ❌ SELL FAILED {signal.ticker}: {e}")


# ─────────────────────────────────────────────
#  STOP LOSS / TAKE PROFIT
# ─────────────────────────────────────────────
def check_exit_rules(positions: dict):
    for ticker, pos in positions.items():
        cost    = float(pos.avg_entry_price)
        current = float(pos.current_price)
        pct     = (current - cost) / cost
        if pct <= -STOP_LOSS_PCT:
            place_sell(Signal(ticker, "SELL", "🛑 StopLoss", 1.0,
                              f"Stop loss at {pct*100:.1f}%"), pos.qty, cost)
        elif pct >= TAKE_PROFIT_PCT:
            place_sell(Signal(ticker, "SELL", "🎯 TakeProfit", 1.0,
                              f"Take profit at {pct*100:.1f}%"), pos.qty, cost)


# ─────────────────────────────────────────────
#  MAIN CYCLE
# ─────────────────────────────────────────────
def is_market_open() -> bool:
    try:
        return api.get_clock().is_open
    except Exception:
        return False


cycle_count = 0

def run_cycle():
    global cycle_count
    if not is_market_open():
        log.info("Market closed — standing by.")
        return

    cycle_count += 1
    log.info("─" * 62)
    log.info(f"CYCLE #{cycle_count}  {datetime.now().strftime('%H:%M:%S')}  scanning {len(WATCHLIST)} tickers...")

    positions = get_positions()
    check_exit_rules(positions)
    positions = get_positions()
    signals   = scan_all_tickers()

    buy_sigs  = [s for s in signals if s.action == "BUY"]
    sell_sigs = [s for s in signals if s.action == "SELL"]
    log.info(f"  Signals → {len(buy_sigs)} BUY  {len(sell_sigs)} SELL")

    for sig in sell_sigs:
        if sig.ticker in positions:
            place_sell(sig, positions[sig.ticker].qty,
                       float(positions[sig.ticker].avg_entry_price))

    positions = get_positions()
    for sig in buy_sigs:
        place_buy(sig, positions)
        positions = get_positions()

    acct = get_account()
    pnl  = float(acct.equity) - float(acct.last_equity)
    portfolio_val = float(acct.portfolio_value)
    log.info(f"  Portfolio: ${portfolio_val:,.2f}  "
             f"Cash: ${float(acct.cash):,.2f}  "
             f"Today P&L: {'+' if pnl >= 0 else ''}${pnl:.2f}  "
             f"Positions: {len(get_positions())}/{MAX_OPEN_POSITIONS}")

    # Send a summary every 10 cycles
    if cycle_count % 10 == 0:
        send_telegram(
            f"📊 <b>Nexus Bot — Cycle #{cycle_count} Update</b>\n"
            f"💼 Portfolio: ${portfolio_val:,.2f}\n"
            f"💵 Cash: ${float(acct.cash):,.2f}\n"
            f"{'📈' if pnl >= 0 else '📉'} Today P&L: {'+' if pnl >= 0 else ''}${pnl:.2f}\n"
            f"📂 Open positions: {len(get_positions())}/{MAX_OPEN_POSITIONS}"
        )


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────
def main():
    log.info("🚀 Nexus Trading Bot v3 — AGGRESSIVE MODE + TELEGRAM")
    log.info(f"   Tickers   : {len(WATCHLIST)}")
    log.info(f"   Scan freq : every 60 seconds")
    log.info(f"   Stop loss : {STOP_LOSS_PCT*100:.0f}%  |  Take profit: {TAKE_PROFIT_PCT*100:.0f}%")
    log.info(f"   Strategies: Momentum + Breakout + MeanRev + MACD")
    log.info("─" * 62)

    send_telegram(
        "🚀 <b>Nexus Trading Bot v3 is LIVE!</b>\n"
        f"📋 Watching {len(WATCHLIST)} tickers\n"
        "⚡ Scanning every 60 seconds\n"
        "🛑 Stop loss: 7%  |  🎯 Take profit: 18%\n"
        "You'll get notified on every trade. Let's get it! 💰"
    )

    run_cycle()
    schedule.every(60).seconds.do(run_cycle)

    while True:
        schedule.run_pending()
        time.sleep(5)


if __name__ == "__main__":
    main()
