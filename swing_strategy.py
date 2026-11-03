"""Rule-based NSE swing-trading analysis and historical backtest.

The technical strategy is deterministic and explainable. Promoter purchases
are a catalyst score, never a standalone buy instruction. Historical tests
use next-session execution and conservative stop-first handling when both a
stop and target are touched on the same daily candle.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

import pandas as pd
import yfinance as yf

LOG = logging.getLogger("stock_rama_bot.swing")
HOLDING_DAYS = int(os.getenv("SWING_MAX_HOLD_DAYS", "20"))
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE_PCT", "1.0")) / 100.0
ROUND_TRIP_COST_BPS = float(os.getenv("BACKTEST_COST_BPS", "30"))
TRADING_CAPITAL = float(os.getenv("TRADING_CAPITAL_INR", "0"))
MIN_TRADE_AMOUNT_INR = float(os.getenv("MIN_TRADE_AMOUNT_INR", "100"))
MAX_TRADE_AMOUNT_INR = float(os.getenv("MAX_TRADE_AMOUNT_INR", "1500"))
MIN_BACKTEST_TRADES = 30


@lru_cache(maxsize=32)
def _history(ticker: str, period: str = "5y") -> pd.DataFrame:
    frame = yf.Ticker(ticker).history(
        period=period, interval="1d", auto_adjust=True, actions=False, timeout=25
    )
    if frame is None or frame.empty:
        raise ValueError(f"No daily price history returned for {ticker}")
    frame = frame.rename(columns={c: str(c).title() for c in frame.columns})
    required = {"Open", "High", "Low", "Close", "Volume"}
    if not required.issubset(frame.columns):
        raise ValueError(f"Price history for {ticker} is missing {required - set(frame.columns)}")
    frame = frame.dropna(subset=["Open", "High", "Low", "Close"]).copy()
    return frame


def _indicators(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    close = data["Close"]
    high = data["High"]
    low = data["Low"]
    volume = data["Volume"]

    data["SMA50"] = close.rolling(50).mean()
    data["SMA200"] = close.rolling(200).mean()
    data["SMA50_10D_AGO"] = data["SMA50"].shift(10)
    data["EMA20"] = close.ewm(span=20, adjust=False).mean()

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    data["RSI14"] = 100 - (100 / (1 + rs))
    data["RSI14"] = data["RSI14"].fillna(50)

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    data["MACD"] = ema12 - ema26
    data["MACD_SIGNAL"] = data["MACD"].ewm(span=9, adjust=False).mean()

    previous_close = close.shift(1)
    true_range = pd.concat(
        [(high - low), (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    data["ATR14"] = true_range.rolling(14).mean()
    data["AVG_VOLUME20"] = volume.rolling(20).mean()
    data["REL_VOLUME"] = volume / data["AVG_VOLUME20"].replace(0, float("nan"))
    data["PRIOR_HIGH20"] = high.rolling(20).max().shift(1)
    data["LOW10"] = low.rolling(10).min()
    data["PREV_CLOSE"] = close.shift(1)
    return data


def _market_states(index_frame: pd.DataFrame, dates: pd.Index) -> pd.Series:
    idx = _indicators(index_frame)
    states = pd.Series("UNKNOWN", index=idx.index, dtype="object")
    states.loc[idx["Close"] > idx["SMA200"]] = "BULL"
    states.loc[idx["Close"] < idx["SMA200"] * 0.98] = "BEAR"
    sideways = idx["SMA200"].notna() & ~(
        (idx["Close"] > idx["SMA200"]) | (idx["Close"] < idx["SMA200"] * 0.98)
    )
    states.loc[sideways] = "SIDEWAYS"
    return states.reindex(dates).ffill().fillna("UNKNOWN")


def _technical_score(row: pd.Series, market_state: str) -> int:
    score = 0
    if row["Close"] > row["SMA50"]:
        score += 10
    if row["SMA50"] > row["SMA200"]:
        score += 10
    if row["SMA50"] > row["SMA50_10D_AGO"]:
        score += 5

    if 50 <= row["RSI14"] <= 68:
        score += 10
    if row["MACD"] > row["MACD_SIGNAL"]:
        score += 5
    if row["Close"] > row["EMA20"]:
        score += 5

    if row["REL_VOLUME"] >= 1.3:
        score += 10
    if row["Close"] > row["PREV_CLOSE"]:
        score += 5

    if market_state == "BULL":
        score += 10
    elif market_state == "SIDEWAYS":
        score += 5

    atr = row["ATR14"]
    if pd.notna(atr) and atr > 0:
        score += 10
    return score


def _promoter_score(value_inr: float, transaction_count: int) -> int:
    if value_inr <= 0 or transaction_count <= 0:
        return 0
    if value_inr >= 100_000_000:
        score = 18
    elif value_inr >= 50_000_000:
        score = 16
    elif value_inr >= 10_000_000:
        score = 13
    else:
        score = 10
    if transaction_count >= 2:
        score += 2
    return min(score, 20)


def _trade_levels(row: pd.Series) -> dict[str, float]:
    atr = float(row["ATR14"])
    entry = float(row["High"]) * 1.001  # breakout trigger above the signal candle
    max_entry = entry + 0.5 * atr       # do not chase a large opening gap
    atr_stop = entry - 1.8 * atr
    support_stop = float(row["LOW10"]) - 0.2 * atr
    support_distance = entry - support_stop
    if 1.3 * atr <= support_distance <= 2.2 * atr:
        stop = support_stop
    else:
        stop = atr_stop
    risk = entry - stop
    return {
        "entry": entry,
        "max_entry": max_entry,
        "stop": stop,
        "risk_per_share": risk,
        "target1": entry + 2.0 * risk,
        "target2": entry + 3.0 * risk,
        "risk_reward": 2.0,
        "atr": atr,
    }


def _position_size(
    entry: float,
    risk_per_share: float,
    capital: float,
    risk_fraction: float = RISK_PER_TRADE,
    min_amount: float = MIN_TRADE_AMOUNT_INR,
    max_amount: float = MAX_TRADE_AMOUNT_INR,
) -> tuple[int | None, float | None, float | None]:
    """Calculate whole-share sizing within both the risk budget and hard order-value bounds."""
    if entry <= 0 or risk_per_share <= 0 or capital <= 0:
        return None, None, None
    risk_budget = capital * risk_fraction
    allocation_cap = min(capital, max_amount)
    risk_limited_shares = int(risk_budget / risk_per_share)
    allocation_limited_shares = int(allocation_cap / entry)
    shares = min(risk_limited_shares, allocation_limited_shares)
    position_value = shares * entry
    if shares <= 0 or position_value < min_amount or position_value > max_amount:
        return None, None, risk_budget
    return shares, position_value, risk_budget


def _backtest(data: pd.DataFrame, market: pd.Series) -> dict[str, Any]:
    """Walk-forward daily-bar simulation; signal at close, earliest fill next day."""
    trades: list[dict[str, float]] = []
    i = 205
    cost_fraction = ROUND_TRIP_COST_BPS / 10_000.0
    while i < len(data) - 1:
        row = data.iloc[i]
        state = str(market.iloc[i])
        needed = ["SMA50", "SMA200", "SMA50_10D_AGO", "ATR14", "REL_VOLUME"]
        if any(pd.isna(row[k]) for k in needed):
            i += 1
            continue
        levels = _trade_levels(row)
        score = _technical_score(row, state)
        # Historical technical-only entry: promoter filings are not backfilled here.
        if score < 60 or state != "BULL" or row["RSI14"] > 68 or row["RSI14"] < 50:
            i += 1
            continue

        entry_bar = data.iloc[i + 1]
        trigger = levels["entry"]
        if entry_bar["Open"] > levels["max_entry"]:
            i += 1  # opening gap too extended; no chase
            continue
        if entry_bar["High"] < trigger:
            i += 1
            continue
        entry = max(float(entry_bar["Open"]), trigger)
        stop = entry - levels["risk_per_share"]
        target = entry + 2.0 * levels["risk_per_share"]
        exit_price = None
        exit_index = min(i + 1 + HOLDING_DAYS, len(data) - 1)
        actual_exit_index = exit_index
        for j in range(i + 1, exit_index + 1):
            bar = data.iloc[j]
            hit_stop = bar["Low"] <= stop
            hit_target = bar["High"] >= target
            if hit_stop:  # pessimistic if both levels touch in one bar; respect stop gaps
                exit_price = min(float(bar["Open"]), stop)
                actual_exit_index = j
                break
            if hit_target:
                exit_price = target
                actual_exit_index = j
                break
        if exit_price is None:
            exit_price = float(data.iloc[exit_index]["Close"])
        gross_return = (exit_price - entry) / entry
        net_return = gross_return - cost_fraction
        r_multiple = (net_return * entry) / levels["risk_per_share"]
        trades.append({
            "r": r_multiple,
            "return_pct": net_return * 100,
            "entry": entry,
            "exit": exit_price,
        })
        i = actual_exit_index + 1

    if not trades:
        return {
            "trades": 0, "win_rate": None, "profit_factor": None,
            "expectancy_r": None, "max_drawdown_pct": None,
            "total_return_pct": None, "cost_bps": ROUND_TRIP_COST_BPS,
            "sample_ok": False,
        }

    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    profit_factor = sum(wins) / abs(sum(losses)) if losses and sum(losses) else (
        float("inf") if wins else 0.0
    )
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for r in rs:
        equity *= max(0.01, 1.0 + RISK_PER_TRADE * r)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100,
        "profit_factor": profit_factor,
        "expectancy_r": sum(rs) / len(rs),
        "max_drawdown_pct": max_drawdown * 100,
        "total_return_pct": (equity - 1) * 100,
        "cost_bps": ROUND_TRIP_COST_BPS,
        "sample_ok": len(trades) >= MIN_BACKTEST_TRADES,
    }


def analyze_symbol(
    symbol: str, promoter_value_inr: float, transaction_count: int
) -> dict[str, Any]:
    """Return a transparent swing setup plus a technical-only 5y backtest."""
    result: dict[str, Any] = {
        "symbol": symbol, "action": "INSUFFICIENT DATA", "score": None,
        "technical_score": None, "promoter_score": None, "backtest": None,
        "reason": None,
    }
    try:
        ticker = symbol.strip().upper()
        if not ticker or ticker == "UNKNOWN":
            raise ValueError("NSE symbol is unavailable")
        data = _indicators(_history(f"{ticker}.NS"))
        index_data = _history("^NSEI")
        market = _market_states(index_data, data.index)
        row = data.iloc[-1]
        required = ["SMA50", "SMA200", "SMA50_10D_AGO", "ATR14", "REL_VOLUME", "LOW10"]
        if any(pd.isna(row[k]) for k in required):
            raise ValueError("Not enough price history to calculate indicators")
        state = str(market.iloc[-1])
        levels = _trade_levels(row)
        tech = _technical_score(row, state)
        promoter = _promoter_score(promoter_value_inr, transaction_count)
        total = min(100, tech + promoter)

        backtest = _backtest(data, market)
        if state == "BEAR":
            action = "AVOID"
            reason = "NIFTY 50 market regime is bearish; moderate-risk entries are blocked."
        elif total >= 75 and tech >= 55 and levels["risk_reward"] >= 2:
            backtest_pass = (
                backtest.get("sample_ok")
                and backtest.get("expectancy_r") is not None
                and backtest["expectancy_r"] > 0
                and backtest.get("profit_factor", 0) >= 1.1
                and backtest.get("max_drawdown_pct", 100) <= 25
            )
            if backtest_pass:
                action = "BUY SETUP"
                reason = "Current setup passes the score rules and the 5y technical backtest validation gates."
            else:
                action = "WAIT"
                reason = "Current setup scores well, but the 5y technical backtest lacks a sufficiently strong validated edge (30+ trades, positive expectancy, profit factor ≥1.1 and drawdown ≤25%)."
        elif total >= 60:
            action = "WAIT"
            reason = "Some positive factors exist, but the full entry and validation conditions are not met."
        else:
            action = "AVOID"
            reason = "The combined technical and promoter score is below the minimum threshold."

        shares, position_value, risk_budget = _position_size(
            entry=levels["entry"],
            risk_per_share=levels["risk_per_share"],
            capital=TRADING_CAPITAL,
        )
        result.update({
            "action": action, "score": total, "technical_score": tech,
            "promoter_score": promoter, "market": state, "close": float(row["Close"]),
            **levels, "reason": reason, "risk_budget": risk_budget,
            "shares": shares, "position_value": position_value,
            "backtest": backtest,
        })
        return result
    except Exception as exc:
        LOG.warning("Swing analysis unavailable for %s: %s", symbol, exc)
        result["reason"] = str(exc)
        return result


def format_swing_analysis(result: dict[str, Any]) -> list[str]:
    lines = ["", "🧠 SWING-TRADING ALGORITHM"]
    if result.get("score") is None:
        lines.append(f"Decision: INSUFFICIENT DATA ({result.get('reason') or 'price data unavailable'})")
        return lines

    lines.extend([
        f"Decision: {result['action']} | Score: {result['score']}/100",
        f"Market regime: {result['market']} | Technical: {result['technical_score']}/80 "
        f"| Promoter catalyst: {result['promoter_score']}/20",
        f"Latest adjusted close: ₹{result['close']:,.2f}",
        f"Entry trigger: ₹{result['entry']:,.2f} to max ₹{result['max_entry']:,.2f}",
        f"Stop loss: ₹{result['stop']:,.2f}",
        f"Target 1 (2R): ₹{result['target1']:,.2f} | Target 2 (3R): ₹{result['target2']:,.2f}",
        f"Planned reward/risk: 2.0:1 | Risk/share: ₹{result['risk_per_share']:,.2f}",
    ])
    if result.get("shares"):
        lines.append(
            f"Position guide: {result['shares']} shares (~₹{result['position_value']:,.0f}); "
            f"risk budget ₹{result['risk_budget']:,.0f}; hard per-trade cap ₹{MAX_TRADE_AMOUNT_INR:,.0f}"
        )
    else:
        lines.append(
            f"Position sizing: no valid quantity within ₹{MIN_TRADE_AMOUNT_INR:,.0f}–₹{MAX_TRADE_AMOUNT_INR:,.0f} "
            "and the configured 1% risk budget; no order should be proposed."
        )
    lines.append(f"Reason: {result['reason']}")

    bt = result.get("backtest") or {}
    if bt.get("trades", 0) == 0:
        lines.append("5y technical backtest: no qualifying historical trades.")
    else:
        pf = bt["profit_factor"]
        pf_text = "∞" if pf == float("inf") else f"{pf:.2f}"
        lines.append(
            f"5y technical-only backtest: {bt['trades']} trades | Win {bt['win_rate']:.1f}% "
            f"| Profit factor {pf_text} | Expectancy {bt['expectancy_r']:.2f}R "
            f"| Max drawdown {bt['max_drawdown_pct']:.1f}% | Net model return "
            f"{bt['total_return_pct']:.1f}% (1% risk/trade, {bt['cost_bps']:.0f} bps round-trip costs)."
        )
        if not bt["sample_ok"]:
            lines.append("Backtest sample is small (<30 trades); treat its statistics as weak evidence.")
        lines.append("Backtest excludes historical promoter disclosures; it tests the technical rules only.")
    lines.append("Entry is a next-session breakout trigger, not an instruction to buy at any price. Skip if price opens above the maximum entry.")
    return lines
