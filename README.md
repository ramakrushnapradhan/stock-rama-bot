# Stock Rama — NSE Promoter + Swing Trading Research Bot

A GitHub Actions Telegram bot that monitors NSE Regulation 7(2) insider disclosures and attaches a transparent, rule-based swing-trading analysis. It is a research and decision-support tool, not an auto-trading system.

## What it does

1. Reads NSE's current insider-trading filing list and downloads the XBRL disclosures.
2. Filters promoter/promoter-group market purchases and aggregates disclosed value by company over a rolling 7-day window.
3. For companies with at least ₹5 lakh of qualifying purchases, fetches daily adjusted price history and evaluates a 2–4 week swing setup.
4. Sends a Telegram alert with BUY SETUP / WAIT / AVOID / INSUFFICIENT DATA, score, entry trigger, maximum acceptable entry, stop, targets, position sizing guidance and a historical technical backtest.
5. Independently scans the NIFTY 500 each run, so technical swing candidates can be found even when there are no promoter disclosures.
6. Stores sent disclosure keys to avoid repeat promoter alerts.

## Swing strategy — moderate risk

The signal is deliberately explainable. A promoter purchase is a catalyst, not a standalone buy reason.

| Component | Maximum points | Rules |
|---|---:|---|
| Trend | 25 | Close above SMA50; SMA50 above SMA200; SMA50 rising over 10 sessions |
| Momentum | 20 | RSI(14) 50–68; MACD above signal; close above EMA20 |
| Volume | 15 | Volume at least 1.3× its 20-session average; positive close |
| Market regime | 10 | NIFTY 50 above its 200-session average |
| Risk setup | 10 | ATR-based stop can be calculated |
| Promoter catalyst | 20 | Disclosed market-buy value, with a small bonus for multiple qualifying transactions |
| **Total** | **100** | |

Decision rules:
- **BUY SETUP:** total score at least 75, technical score at least 55, and NIFTY regime not bearish.
- **WAIT:** score at least 60 but not all BUY conditions are satisfied.
- **AVOID:** bearish NIFTY regime or score below 60.
- **INSUFFICIENT DATA:** price history or required inputs are unavailable. Missing data never becomes a BUY.

The entry is a breakout trigger just above the signal day's high. The bot also gives a maximum acceptable entry (0.5 ATR above the trigger); it says not to chase an opening gap beyond that level. The initial stop is normally 1.8 ATR below entry, with a nearby 10-session swing-low adjustment when it falls inside the allowed stop band. Target 1 is 2R and Target 2 is 3R. The plan assumes a maximum holding period of 20 trading sessions.

Position sizing uses at most 1% of configured trading capital as planned loss:
`shares = min(floor((capital × 1%) / (entry − stop)), floor(min(capital, ₹1,500) / entry))`
The hard per-trade allocation range is ₹100–₹1,500. The ₹1,500 ceiling applies even when `TRADING_CAPITAL_INR` is higher. If no whole-share quantity satisfies the minimum, maximum and risk budget together, the scanner does not propose a position. The 1% is a risk budget, not a guarantee; gaps can cause larger losses.

## Independent market scanner

The workflow downloads the NIFTY 500 constituent list and scans those liquid NSE-listed stocks using daily technical indicators, without requiring promoter purchases. It ranks candidates, then runs the five-year technical backtest on the top five candidates before sending Telegram analysis. This is a broad liquid-stock universe, not every security listed on NSE; smaller and less-liquid stocks are not included.

## Backtesting

The bot uses Yahoo Finance daily adjusted OHLCV history through yfinance for its current technical snapshot and runs a five-year daily-bar, technical-only walk-forward simulation for each qualifying symbol:
- Signal calculated using data available at that day's close; earliest entry is the next session.
- Entry requires the next session to trade through the breakout trigger without opening above the maximum acceptable entry.
- Stop and 2R target are evaluated on subsequent daily OHLC bars; if both are touched on one bar, the stop is assumed to occur first.
- A position is closed at stop, target, or after 20 trading sessions.
- A configurable round-trip cost estimate is deducted from each trade.
- Reports trade count, win rate, profit factor, expectancy in R, maximum drawdown and simulated return at 1% risk per trade.
- Fewer than 30 trades is flagged as a small sample.

**Important backtest limitations:** historical promoter disclosures are not currently replayed, so this tests the technical rules, not the incremental predictive value of promoter buying. The history provider's current ticker universe may omit delisted securities, so survivorship bias remains. Daily candles cannot model exact intraday order-book fills. The 30 bps cost is an estimate, not a broker-specific fee calculation. The simulation is not proof of future profitability.

A strategy should only be considered for paper trading after it has enough trades, positive after-cost expectancy, tolerable drawdown, and robust results across different market periods and nearby parameter settings. Backtests must avoid look-ahead bias and include realistic execution costs; see [backtesting validation guidance](https://learn.aivistacapital.com/learning-center/strategies-systems/research-and-testing/) and [NSE historical reports](https://www.nseindia.com/static/resources/historical-reports-capital-market-daily-monthly-archives).

## Telegram approval and execution status

Eligible BUY SETUP proposals can be sent from the GitHub Actions scanners to a Cloudflare Worker. The Worker validates the request, creates a five-minute Telegram approval card, authorizes only the configured numeric Telegram user ID, and records Approve/Reject decisions in Cloudflare D1.

**Important:** The Cloudflare Worker is currently restricted to approval and notification only. It does not place Zerodha orders. Zerodha requires a whitelisted static IP for API order placement from 1 April 2026; a standard Cloudflare Worker or GitHub-hosted runner does not provide a dedicated fixed outbound IP. Do not enable live execution or add Kite credentials to this Worker.

A separate executor running from a whitelisted static IP must be implemented and tested before Telegram approvals can place real broker orders. Until then, place trades manually in Zerodha Kite after reviewing the alert. Zerodha does not offer a Kite Connect sandbox; API orders can be real orders.

See the [beginner setup guide](cloudflare/BEGINNER_SETUP.md) for exact Cloudflare, GitHub, and Telegram steps. The guide includes a test workflow that sends a clearly marked TESTONLY approval card and never sends a broker order.

## Strategy environment defaults

| Variable | Default | Meaning |
|---|---:|---|
| `MIN_VALUE_INR` | 500000 | Minimum promoter purchase aggregate (₹5 lakh) |
| `LOOKBACK_DAYS` | 7 | Filing lookback |
| `TRADING_CAPITAL_INR` | 10000 | Total capital used to calculate the 1% risk budget; not the per-trade ceiling |
| `MIN_TRADE_AMOUNT_INR` | 100 | Minimum proposed order value |
| `MAX_TRADE_AMOUNT_INR` | 1500 | Hard maximum proposed order value per trade |
| `RISK_PER_TRADE_PCT` | 1.0 | Planned capital risk per trade |
| `BACKTEST_COST_BPS` | 30 | Estimated round-trip cost in basis points |
| `SWING_MAX_HOLD_DAYS` | 20 | Maximum backtest holding sessions |

## Schedule

GitHub Actions is configured to run Monday–Friday at 9:30 PM India time (16:00 UTC), plus manual dispatch. GitHub scheduled workflows can be delayed and do not guarantee exact execution time.

## Safety and execution limits

- Live order execution is disabled by default. When deliberately enabled, only an explicit Approve BUY callback from the configured Telegram user can submit a CNC LIMIT buy.
- The service checks the ₹100–₹1,500 proposal value and available equity margin before submission. A limit order may remain open or be rejected; approval is not a fill guarantee.
- The background reconciler checks broker order status and submits a SELL stop-limit order only for confirmed buy fills. A stop-limit order is not guaranteed to execute, and the monitoring service must remain online. Check the actual order book and net position after every trade.
- Do not assume a stop-loss eliminates gap risk or guarantees an exit price. The service is an early implementation and must be validated with tiny controlled quantities before any live use.
- Daily Kite authentication is required; an expired access token prevents order execution.
- Keep the webhook service private except for its HTTPS webhook endpoints. Use strong unique secrets, a persistent disk and a single service instance.
- BUY SETUP is a rules-based candidate, not a prediction or guarantee. Check disclosures, results, corporate actions, liquidity, circuit limits and live bid/ask spread.
- This tool is not investment advice. Do not trade solely from Telegram alerts.

## Source data

- [NSE Insider Trading disclosures](https://www.nseindia.com/companies-listing/corporate-filings-insider-trading)
- [NSE historical market reports](https://www.nseindia.com/static/resources/historical-reports-capital-market-daily-monthly-archives)
- [NSE impact cost explanation](https://www.nseindia.com/static/products-services/indices-impact-cost)
