"""Independent daily swing scanner for the NIFTY 500 universe.

Scans a broad, liquid NSE universe without requiring promoter disclosures.
Only the highest-ranked technical candidates receive the slower 5-year
backtest. This is a research alert, not an order-placement system.
"""
from __future__ import annotations

import io
import logging
import os

import pandas as pd
import requests
import yfinance as yf

from nse_insider_bot import send_telegram
from trade_proposals import submit_trade_proposal
from swing_strategy import (
    _indicators,
    _market_states,
    _technical_score,
    analyze_symbol,
    format_swing_analysis,
    _history,
)

LOG = logging.getLogger("stock_rama_bot.market_scan")
NIFTY500_CSV = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
TOP_N = int(os.getenv("SCAN_TOP_N", "5"))
BATCH_SIZE = 40


def fetch_universe() -> list[str]:
    response = requests.get(
        NIFTY500_CSV,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"},
        timeout=30,
    )
    response.raise_for_status()
    frame = pd.read_csv(io.StringIO(response.text))
    symbol_column = next((c for c in frame.columns if str(c).strip().lower() == "symbol"), None)
    if not symbol_column:
        raise ValueError("NIFTY 500 constituent file has no Symbol column.")
    symbols = sorted({
        str(value).strip().upper()
        for value in frame[symbol_column].dropna()
        if str(value).strip()
    })
    if len(symbols) < 400:
        raise ValueError(f"NIFTY 500 universe looks incomplete: only {len(symbols)} symbols.")
    return symbols


def _ticker_frame(download: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
    if not isinstance(download.columns, pd.MultiIndex):
        return download if len(download.columns) else None
    for level in (0, 1):
        if ticker in download.columns.get_level_values(level):
            try:
                result = download.xs(ticker, axis=1, level=level)
                if isinstance(result, pd.DataFrame) and not result.empty:
                    return result
            except KeyError:
                pass
    return None


def collect_candidates(symbols: list[str]) -> list[tuple[int, str]]:
    index_data = _history("^NSEI", period="1y")
    ranked: list[tuple[int, str]] = []
    for offset in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[offset:offset + BATCH_SIZE]
        tickers = [f"{symbol}.NS" for symbol in batch]
        try:
            prices = yf.download(
                tickers=tickers,
                period="1y",
                interval="1d",
                auto_adjust=True,
                group_by="ticker",
                threads=True,
                progress=False,
                timeout=35,
            )
        except Exception as exc:
            LOG.warning("Price batch %d failed: %s", offset // BATCH_SIZE + 1, exc)
            continue
        for symbol, ticker in zip(batch, tickers):
            frame = _ticker_frame(prices, ticker)
            if frame is None or frame.empty:
                continue
            try:
                frame = frame.rename(columns={c: str(c).title() for c in frame.columns})
                data = _indicators(frame)
                market = _market_states(index_data, data.index)
                row = data.iloc[-1]
                required = ["SMA50", "SMA200", "SMA50_10D_AGO", "ATR14", "REL_VOLUME", "LOW10"]
                if any(pd.isna(row.get(key)) for key in required):
                    continue
                state = str(market.iloc[-1])
                if state == "BEAR" or not (45 <= row["RSI14"] <= 72):
                    continue
                if row["Close"] <= row["EMA20"]:
                    continue
                score = _technical_score(row, state)
                if score >= 50:
                    ranked.append((score, symbol))
            except Exception as exc:
                LOG.debug("Skipping %s: %s", symbol, exc)
        LOG.info("Scanned NIFTY 500 batch %d/%d; candidates=%d",
                 offset // BATCH_SIZE + 1, (len(symbols) + BATCH_SIZE - 1) // BATCH_SIZE,
                 len(ranked))
    return sorted(ranked, reverse=True)


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        symbols = fetch_universe()
        LOG.info("Loaded %d NIFTY 500 constituents.", len(symbols))
        candidates = collect_candidates(symbols)
        selected = candidates[:TOP_N]
        LOG.info("Technical candidates selected for 5y backtest: %s", selected)
        if not selected:
            send_telegram(
                "📊 STOCK RAMA — DAILY MARKET SCAN\n"
                f"Universe: NIFTY 500 ({len(symbols)} constituents)\n"
                "No stocks passed today's initial technical filters.\n"
                "No trade signal generated."
            )
            return 0

        for score, symbol in selected:
            result = analyze_symbol(symbol, 0, 0)
            approval = submit_trade_proposal(symbol, result, "NIFTY 500 swing scan")
            message = [
                "📊 STOCK RAMA — INDEPENDENT SWING SCAN",
                f"Universe: NIFTY 500 | Technical rank score: {score}/80",
                "Promoter disclosure is not required for this scan.",
                "Any position guide is limited to ₹1,500 per trade and the configured 1% risk budget.",
                "",
                *format_swing_analysis(result),
            ]
            send_telegram("\n".join(message))
        return 0
    except Exception:
        LOG.exception("Independent NIFTY 500 market scan failed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
