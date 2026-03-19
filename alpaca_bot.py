"""
Nexus Trading Bot v5 — Alpaca Paper Trading
100 tickers · 60s scans · Aggressive Mode
Stop: 7% | Take Profit: 18%
Telegram: Buy/Sell alerts + End of day summary only
"""

import os
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
#  CONFIG
# ─────────────────────────────────────────────
API_KEY          = os.environ.get("ALPACA_API_KEY", "")
SECRET_KEY       = os.environ.get("ALPACA_SECRET_KEY", "")
BASE_URL         = "https://paper-api.alpaca.markets"
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
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
#  WATCHLIST
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
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("nexus")

# ─────────────────────────────────────────────
#  DAILY TRACKING
# ─────────────────────────────────────────────
daily_stats = {
    "start_value": None,
    "trades_today": 0,
    "buys_today": 0,
    "sells_today": 0,
    "best_sell": None,
    "worst_sell": None,
}


# ─────────────────────────────────────────────
#  TELEGRAM  (only called on trades + EOD)
# ─────────────────────────────────────────────
def send_telegram(message: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        }, timeout=5)
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")


def send_daily_summary():
    try:
        acct          = get_account()
        portfolio_val = float(acct.portfolio_value)
        cash          = float(acct.cash)
        pnl_today     = float(acct.equity) - float(acct.last_equity)
        pnl_total     = portfolio_val - 100000
        pnl_pct_today = (pnl_today / float(acct.last_equity)) * 100
        positions     = get_positions()

        pos_lines = ""
        for ticker, pos in positions.items():
            cost    = float(pos.avg_entry_price)
            current = float(pos.current_price)
            pct     = (current - cost) / cost * 100
            arrow   = "📈" if pct >= 0 else "📉"
            pos_lines += f"  {arrow} {ticker}: {'+' if pct >= 0 else ''}{pct:.1f}%\n"
        if not pos_lines:
            pos_lines = "  No open positions\n"

        best  = f"{daily_stats['best_sell'][0]} +{daily_stats['best_sell'][1]:.1f}%" if daily_stats["best_sell"] else "N/A"
        worst = f"{daily_stats['worst_sell'][0]} {daily_stats['worst_sell'][1]:.1f}%" if daily_stats["worst_sell"] else "N/A"

        today_emoji = "📈" if pnl_today >= 0 else "📉"
        total_emoji = "📈" if pnl_total >= 0 else "📉"

        msg = (
            f"🌙 <b>Nexus Bot — End of Day Report</b>\n"
            f"📅 {datetime.now().strftime('%A, %B %d %Y')}\n"
            f"─────────────────────\n"
            f"{today_emoji} <b>Today's P&L:</b> {'+' if pnl_today >= 0 else ''}${pnl_today:.2f} ({'+' if pnl_pct_today >= 0 else ''}{pnl_pct_today:.2f}%)\n"
            f"{total_emoji} <b>Total P&L:</b> {'+' if pnl_total >= 0 else ''}${pnl_total:.2f}\n"
            f"💼 <b>Portfolio:</b> ${portfolio_val:,.2f}\n"
            f"💵 <b>Cash:</b> ${cash:,.2f}\n"
            f"─────────────────────\n"
            f"📊 <b>Trades today:</b> {daily_stats['trades_today']} ({daily_stats['buys_today']} buys, {daily_stats['sells_today']} sells)\n"
            f"🏆 <b>Best trade:</b> {best}\n"
            f"💀 <b>Worst trade:</b> {worst}\n"
            f"─────────────────────\n"
            f"📂 <b>Open positions ({len(positions)}/{MAX_OPEN_POSITIONS}):</b>\n"
            f"{pos_lines}"
            f"─────────────────────\n"
            f"See you tomorrow! 🚀"
        )
        send_telegram(msg)
        log.info("Daily summary sent to Telegram.")

        # Reset daily stats
        daily_stats["trades_today"] = 0
        daily_stats["buys_today"]   = 0
        daily_stats["sells_today"]  = 0
        daily_stats["best_sell"]    = None
        daily_stats["worst_sell"]   = None
        daily_stats["start_value"]  = None

    except Exception as e:
        log.error(f"Daily summary failed: {e}")


# ─────────────────────────────────────────────
#  ALPACA CLIENT
# ─────────────────────────────────────────────
api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL, api_version="v2")

def get_account():         return api.get_account()
def get_positions():       return {p.symbol: p for p in api.list_positions()}
def get_portfolio_value(): return float(api.get_account().portfolio_value)
def get_cash():            return float(api.get_account().cash)


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
@dataclass
class Signal:
    ticker: str
    action: str
    strategy: str
    strength: float
    reason: str


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
    ticker        = signal.ticker
    portfolio_val = get_portfolio_value()
    cash          = get_cash()
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
        daily_stats["trades_today"] += 1
        daily_stats["buys_today"]   += 1
        send_telegram(
            f"✅ <b>BUY {ticker}</b>\n"
            f"💰 Amount: ${dollars:.2f}\n"
            f"📊 Strategy: {signal.strategy}\n"
            f"📝 Reason: {signal.reason}\n"
            f"💪 Strength: {signal.strength:.0%}"
        )
        log.info(f"  ✅ BUY  {ticker:<6} ${dollars:>8.2f}  [{signal.strategy}]")
    except Exception as e:
        log.error(f"  ❌ BUY FAILED {ticker}: {e}")


def place_sell(signal: Signal, qty: str, entry_price: float = 0):
    try:
        current_price = float(api.get_latest_trade(signal.ticker).price) if entry_price else 0
        pnl_pct = ((current_price - entry_price) / entry_price * 100) if entry_price else 0
        api.submit_order(symbol=signal.ticker, qty=qty,
                         side="sell", type="market", time_in_force="day")
        daily_stats["trades_today"] += 1
        daily_stats["sells_today"]  += 1

        if entry_price:
            if daily_stats["best_sell"] is None or pnl_pct > daily_stats["best_sell"][1]:
                daily_stats["best_sell"] = (signal.ticker, pnl_pct)
            if daily_stats["worst_sell"] is None or pnl_pct < daily_stats["worst_sell"][1]:
                daily_stats["worst_sell"] = (signal.ticker, pnl_pct)

        emoji = "🟢" if pnl_pct >= 0 else "🔴"
        send_telegram(
            f"{emoji} <b>SELL {signal.ticker}</b>\n"
            f"📦 Qty: {qty}\n"
            f"📊 Strategy: {signal.strategy}\n"
            f"📝 Reason: {signal.reason}\n"
            f"{'📈' if pnl_pct >= 0 else '📉'} P&L: {'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}%"
        )
        log.info(f"  ✅ SELL {signal.ticker:<6} qty={qty}")
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


cycle_count    = 0
summary_sent   = False

def run_cycle():
    global cycle_count, summary_sent

    now    = datetime.utcnow()
    # Market closes 4pm ET = 20:00 UTC
    # Send summary at 20:10 UTC (4:10pm ET / 5:10pm Puerto Rico)
    is_eod = now.hour == 20 and now.minute == 10

    if is_eod and not summary_sent:
        send_daily_summary()
        summary_sent = True

    # Reset flag each morning at 13:00 UTC (9am ET)
    if now.hour == 13 and now.minute == 0:
        summary_sent = False

    if not is_market_open():
        log.info("Market closed — standing by.")
        return

    if daily_stats["start_value"] is None:
        daily_stats["start_value"] = get_portfolio_value()

    cycle_count += 1
    log.info("─" * 62)
    log.info(f"CYCLE #{cycle_count}  {now.strftime('%H:%M:%S UTC')}  scanning {len(WATCHLIST)} tickers...")

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
    log.info(f"  Portfolio: ${float(acct.portfolio_value):,.2f}  "
             f"Cash: ${float(acct.cash):,.2f}  "
             f"Today P&L: {'+' if pnl >= 0 else ''}${pnl:.2f}")


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────
def main():
    log.info("🚀 Nexus Trading Bot v5 — CLEAN NOTIFICATIONS")
    log.info(f"   Tickers   : {len(WATCHLIST)}")
    log.info(f"   Stop loss : {STOP_LOSS_PCT*100:.0f}%  |  Take profit: {TAKE_PROFIT_PCT*100:.0f}%")
    log.info(f"   Telegram  : Buy/Sell alerts + End of day summary only")
    log.info("─" * 62)

    send_telegram(
        "🚀 <b>Nexus Bot v5 is LIVE!</b>\n"
        "Notifications: trades only + end of day summary.\n"
        "Standing by for market open... 💰"
    )

    run_cycle()
    schedule.every(60).seconds.do(run_cycle)

    while True:
        schedule.run_pending()
        time.sleep(5)


if __name__ == "__main__":
    main()
