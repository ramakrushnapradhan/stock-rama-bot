#!/usr/bin/env python3
"""Monitor NSE insider-trading disclosures and send qualifying promoter-buy alerts.

This is an informational alert, not investment advice. NSE endpoints and schemas may
change; the script validates responses and logs failures rather than guessing.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

LOG = logging.getLogger("stock_rama_bot")
NSE_URL = "https://www.nseindia.com/api/corporate-officials?index=equities"
# NSE's PIT disclosures endpoint (the query parameter names are used by the public
# NSE website; NSE may change access controls or response fields without notice).
PIT_URL = "https://www.nseindia.com/api/corporate-announcements?index=equities"
STATE_FILE = Path(os.getenv("STATE_FILE", "alerted_transactions.json"))
MIN_VALUE_INR = float(os.getenv("MIN_VALUE_INR", "5000000"))  # ₹50 lakh
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "7"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@Stock_rama_2027_bot").strip()
NSE_API_URL = os.getenv("NSE_PIT_API_URL", "").strip()


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
        "Connection": "keep-alive",
    })
    return session


def fetch_json(session: requests.Session, url: str, *, params: dict | None = None) -> Any:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = session.get(url, params=params, timeout=25)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            LOG.warning("Fetch attempt %s/3 failed: %s", attempt + 1, exc)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Could not fetch NSE data: {last_error}") from last_error


def parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / (1000 if value > 10**11 else 1), timezone.utc)
        except (ValueError, OSError):
            return None
    text = str(value).strip()
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y",
                "%d-%b-%y", "%d/%m/%y"):
        try:
            return datetime.strptime(text[:11], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def to_number(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[^0-9.\-]", "", str(value).replace(",", ""))
    try:
        return float(text) if text else 0.0
    except ValueError:
        return 0.0


def first(row: dict, *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def normalize_rows(payload: Any) -> list[dict]:
    """Support common NSE response wrappers; avoid silently treating unknown data as rows."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "records", "dataList"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
            if isinstance(value, dict):
                nested = normalize_rows(value)
                if nested:
                    return nested
    raise ValueError("Unrecognized NSE payload shape; inspect the current NSE API response")


def is_promoter(row: dict) -> bool:
    role = str(first(row, "acqName", "personName", "category", "acquirerCategory",
                     "promoterCategory", "personCategory") or "").lower()
    # NSE commonly identifies promoter/promoter group in category fields.
    return "promoter" in role


def is_market_buy(row: dict) -> bool:
    mode = str(first(row, "acqmMode", "modeOfAcquisition", "acquisitionMode",
                     "transactionMode") or "").lower()
    tx_type = str(first(row, "tdpTransactionType", "transactionType",
                        "buySell", "transaction") or "").lower()
    is_buy = any(token in tx_type for token in ("buy", "purchase", "acquisition"))
    is_market = any(token in mode for token in ("market", "open market", "stock exchange"))
    return is_buy and is_market


def transaction_value(row: dict) -> float:
    direct = first(row, "secVal", "securitiesValue", "transactionValue",
                   "totalValue", "value")
    value = to_number(direct)
    if value > 0:
        return value
    quantity = to_number(first(row, "secAcq", "securitiesAcquired",
                               "quantity", "noOfShares", "shares"))
    price = to_number(first(row, "secPrice", "price", "avgPrice", "acquisitionPrice"))
    return quantity * price


def record_key(row: dict) -> str:
    # Prefer a disclosure/transaction identifier. Fall back to stable row fields.
    identifier = first(row, "seqNumber", "id", "transactionId", "acqId", "filingNo")
    if identifier:
        return str(identifier)
    fields = [
        first(row, "symbol", "scSymbol", "companySymbol"),
        first(row, "acqName", "personName", "acquirerName"),
        first(row, "tdpTransactionType", "transactionType"),
        first(row, "acqDt", "dateOfAcquisition", "transactionDate"),
        first(row, "secAcq", "quantity", "noOfShares"),
        first(row, "secVal", "transactionValue", "value"),
    ]
    return "|".join(str(x or "") for x in fields)


def load_state() -> set[str]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except FileNotFoundError:
        return set()
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("Could not read alert state; starting empty: %s", exc)
        return set()


def save_state(keys: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(keys), indent=2), encoding="utf-8")


def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured in GitHub Actions secrets")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    response = requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }, timeout=20)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API rejected message: {result}")


def format_alert(symbol: str, company: str, rows: list[dict], total: float) -> str:
    lines = [
        "📈 NSE PROMOTER BUY ALERT",
        f"Company: {company}",
        f"Symbol: {symbol}",
        f"Aggregate disclosed value: ₹{total:,.0f}",
        f"Transactions: {len(rows)}",
        "",
    ]
    for row in rows[:8]:
        name = first(row, "acqName", "personName", "acquirerName", "name") or "Promoter"
        date = first(row, "acqDt", "dateOfAcquisition", "transactionDate", "date") or "Date not listed"
        qty = first(row, "secAcq", "securitiesAcquired", "quantity", "noOfShares")
        value = transaction_value(row)
        lines.append(f"• {name} | {date} | Qty: {qty or 'N/A'} | ₹{value:,.0f}")
    if len(rows) > 8:
        lines.append(f"…and {len(rows) - 8} more qualifying disclosure(s).")
    lines.extend(["", "Source: NSE disclosure data. Verify the original filing before acting.",
                  "Informational only — not investment advice."])
    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    if not TELEGRAM_TOKEN:
        LOG.error("Missing TELEGRAM_BOT_TOKEN secret.")
        return 2

    session = make_session()
    # Warm-up NSE cookies before querying its API.
    try:
        session.get("https://www.nseindia.com/", timeout=20)
    except requests.RequestException as exc:
        LOG.warning("NSE homepage warm-up failed; continuing: %s", exc)

    url = NSE_API_URL or os.getenv("NSE_PIT_API_URL", "")
    if not url:
        LOG.error(
            "NSE_PIT_API_URL is not configured. Set it to the NSE PIT disclosure API "
            "endpoint used by your original working script. The public NSE endpoints "
            "are not guaranteed stable, so this bot will not guess a PIT endpoint."
        )
        return 2

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=LOOKBACK_DAYS)
    try:
        payload = fetch_json(session, url)
        rows = normalize_rows(payload)
    except Exception:
        LOG.exception("Unable to retrieve or parse NSE PIT disclosures.")
        return 1

    eligible: list[dict] = []
    for row in rows:
        date_value = first(row, "acqDt", "dateOfAcquisition", "transactionDate",
                           "date", "filingDate", "dateOfDisclosure")
        date = parse_date(date_value)
        if date and not (start <= date <= end):
            continue
        if not is_promoter(row) or not is_market_buy(row):
            continue
        value = transaction_value(row)
        if value >= MIN_VALUE_INR:
            eligible.append(row)

    if not eligible:
        LOG.info("No qualifying promoter open-market purchases of at least ₹%.0f found.", MIN_VALUE_INR)
        return 0

    # Group by company/symbol, but only send rows not previously alerted.
    already = load_state()
    fresh_by_company: dict[tuple[str, str], list[dict]] = {}
    for row in eligible:
        key = record_key(row)
        if key in already:
            continue
        symbol = str(first(row, "symbol", "scSymbol", "companySymbol") or "UNKNOWN").strip()
        company = str(first(row, "company", "companyName", "nameOfCompany") or symbol).strip()
        fresh_by_company.setdefault((symbol, company), []).append(row)

    sent_keys: set[str] = set()
    for (symbol, company), company_rows in fresh_by_company.items():
        total = sum(transaction_value(row) for row in company_rows)
        # Group-level threshold avoids alerting on several smaller rows whose sum
        # crosses the configured threshold only when considered together.
        if total < MIN_VALUE_INR:
            continue
        send_telegram(format_alert(symbol, company, company_rows, total))
        sent_keys.update(record_key(row) for row in company_rows)
        LOG.info("Sent alert for %s (%s), total ₹%.0f", company, symbol, total)

    if sent_keys:
        save_state(already | sent_keys)
    else:
        LOG.info("No new qualifying disclosures to alert.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
