#!/usr/bin/env python3
"""Monitor NSE PIT disclosures and send qualifying promoter open-market buy alerts.

Informational only, not investment advice. NSE may change its website/API at any time.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

LOG = logging.getLogger("stock_rama_bot")
NSE_PAGE_URL = "https://www.nseindia.com/companies-listing/corporate-filings-pit-annual"
NSE_API_URL = (
    os.getenv("NSE_PIT_API_URL", "").strip()
    or "https://www.nseindia.com/api/corporate-IT-PIT-Annual"
)
STATE_FILE = Path(os.getenv("STATE_FILE", "alerted_transactions.json"))
MIN_VALUE_INR = float(os.getenv("MIN_VALUE_INR", "5000000"))  # ₹50 lakh
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "7"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
INDIA_TZ = ZoneInfo("Asia/Kolkata")


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": NSE_PAGE_URL,
        "Connection": "keep-alive",
    })
    return session


def fetch_json(
    session: requests.Session, url: str, *, params: dict | None = None
):
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = session.get(url, params=params, timeout=25)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            LOG.warning("NSE request attempt %d/3 failed: %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Could not fetch NSE PIT data: {last_error}") from last_error


def parse_date(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10] if fmt == "%Y-%m-%d" else text[:11], fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return None


def to_number(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[^0-9.-]", "", str(value).replace(",", ""))
    try:
        return float(text) if text else 0.0
    except ValueError:
        return 0.0


def first(row: dict, *keys: str):
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def normalize_rows(payload) -> list[dict]:
    """Unwrap the current NSE PIT Annual response and common nested table envelopes."""
    if isinstance(payload, list):
        rows = [item for item in payload if isinstance(item, dict)]
        if rows:
            return rows
    if isinstance(payload, dict):
        # Current NSE responses have used several envelope names. Search only
        # structured collections; never treat arbitrary metadata as disclosures.
        for key in (
            "data", "records", "results", "tableData", "dataList",
            "corporateITPITAnnual", "pitAnnual", "rows", "aaData",
        ):
            value = payload.get(key)
            if isinstance(value, (dict, list)):
                try:
                    return normalize_rows(value)
                except ValueError:
                    continue
        for value in payload.values():
            if isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
                return value
    raise ValueError(
        "NSE PIT Annual response did not contain a disclosure row collection. "
        "The upstream API may have changed or returned an error page."
    )


def is_promoter(row: dict) -> bool:
    category = str(first(
        row, "personCategory", "categoryOfPerson", "category",
        "acquirerCategory", "promoterCategory", "person_category",
        "category_of_person", "categoryOfPersonName"
    ) or "").lower()
    return "promoter" in category


def is_market_buy(row: dict) -> bool:
    mode = str(first(
        row, "acqMode", "acqmMode", "modeOfAcquisition",
        "acquisitionMode", "transactionMode", "mode",
        "mode_of_acquisition", "marketMode"
    ) or "").lower()
    transaction_type = str(first(
        row, "tdpTransactionType", "transactionType", "buySell",
        "transaction_type", "typeOfTransaction", "transaction"
    ) or "").lower()
    is_buy = any(word in transaction_type for word in ("buy", "purchase", "acquisition"))
    is_market = "market" in mode
    return is_buy and is_market


def transaction_value(row: dict) -> float:
    direct = first(
        row, "secVal", "securitiesValue", "transactionValue", "totalValue", "value",
        "valueOfSecurities", "value_of_securities", "buyValue", "buy_value"
    )
    value = to_number(direct)
    if value > 0:
        return value
    quantity = to_number(first(
        row, "secAcq", "securitiesAcquired", "quantity", "noOfShares", "shares",
        "noOfSharesAcquired", "no_of_shares", "buyQuantity", "buy_quantity"
    ))
    price = to_number(first(
        row, "secPrice", "price", "avgPrice", "acquisitionPrice"
    ))
    return quantity * price


def record_key(row: dict) -> str:
    identifier = first(row, "seqNumber", "id", "transactionId", "acqId", "filingNo")
    parts = [
        identifier,
        first(row, "symbol", "scSymbol"),
        first(row, "acqName", "personName", "acquirerName", "nameOfPerson", "person_name"),
        first(row, "acqtoDt", "acqDt", "dateOfAcquisition", "transactionDate", "date"),
        first(row, "secAcq", "quantity", "noOfShares"),
        first(row, "secVal", "transactionValue", "value"),
    ]
    return "|".join(str(value or "") for value in parts)


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
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN GitHub Actions secret.")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Missing TELEGRAM_CHAT_ID repository variable. Set it to the destination "
            "user/group/channel ID, not the bot's own username."
        )
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError("Telegram API rejected the message.")


def format_alert(symbol: str, company: str, rows: list[dict], total: float) -> str:
    lines = [
        "📈 NSE PROMOTER OPEN-MARKET BUY ALERT",
        f"Company: {company}",
        f"Symbol: {symbol}",
        f"Aggregate disclosed value: ₹{total:,.0f}",
        f"Transactions: {len(rows)}",
        "",
    ]
    for row in rows[:8]:
        name = first(row, "acqName", "personName", "acquirerName") or "Promoter"
        date = first(row, "acqtoDt", "acqfromDt", "acqDt", "dateOfAcquisition", "date",
        "transactionDate", "transaction_to", "transaction_from") or "Not listed"
        quantity = first(row, "secAcq", "securitiesAcquired", "quantity", "noOfShares")
        lines.append(
            f"• {name} | {date} | Qty: {quantity or 'N/A'} | "
            f"₹{transaction_value(row):,.0f}"
        )
    if len(rows) > 8:
        lines.append(f"…and {len(rows) - 8} more qualifying disclosure(s).")
    lines.extend([
        "",
        "Source: NSE PIT disclosures. Verify the original filing before acting.",
        "Informational only — not investment advice.",
    ])
    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not TELEGRAM_TOKEN:
        LOG.error("Missing TELEGRAM_BOT_TOKEN secret.")
        return 2
    if not TELEGRAM_CHAT_ID:
        LOG.error("Missing TELEGRAM_CHAT_ID repository variable.")
        return 2

    # NSE expects browser-like headers and a cookie warm-up before its JSON API call.
    session = make_session()
    try:
        landing = session.get(NSE_PAGE_URL, timeout=20)
        landing.raise_for_status()
        today = datetime.now(INDIA_TZ).date()
        from_date = today - timedelta(days=LOOKBACK_DAYS)
        params = {
            "index": "equities",
            "from_date": from_date.strftime("%d-%m-%Y"),
            "to_date": today.strftime("%d-%m-%Y"),
        }
        payload = fetch_json(session, NSE_API_URL, params=params)
        rows = normalize_rows(payload)
    except Exception:
        LOG.exception("Unable to retrieve or parse NSE PIT disclosures.")
        return 1

    # Treat NSE date-only fields as Indian calendar dates.
    start_date = from_date
    end_date = today
    eligible: list[dict] = []
    for row in rows:
        date_value = first(
            row, "acqtoDt", "acqfromDt", "acqDt",
            "dateOfAcquisition", "transactionDate", "date", "intimDt",
            "transaction_to", "transaction_from", "dateOfTransaction"
        )
        parsed = parse_date(date_value)
        if parsed and not (start_date <= parsed.date() <= end_date):
            continue
        if not is_promoter(row):
            continue
        if not is_market_buy(row):
            # The Annual PIT feed may identify a BUY without exposing execution
            # mode. Never treat a missing mode as proof of an open-market trade.
            LOG.debug("Skipping disclosure without an explicit BUY + Market mode.")
            continue
        value = transaction_value(row)
        if value >= MIN_VALUE_INR:
            eligible.append(row)

    if not eligible:
        LOG.info(
            "No confirmed promoter open-market buy disclosures ≥ ₹%.0f found. "
            "Rows without an explicit Market acquisition mode are intentionally skipped.",
            MIN_VALUE_INR,
        )
        return 0

    already_alerted = load_state()
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in eligible:
        if record_key(row) in already_alerted:
            continue
        symbol = str(first(row, "symbol", "scSymbol", "companySymbol", "ticker") or "UNKNOWN").strip()
        company = str(first(row, "company", "companyName", "nameOfCompany", "company_name", "issuerName") or symbol).strip()
        grouped.setdefault((symbol, company), []).append(row)

    sent_keys: set[str] = set()
    for (symbol, company), company_rows in grouped.items():
        total = sum(transaction_value(row) for row in company_rows)
        if total < MIN_VALUE_INR:
            continue
        send_telegram(format_alert(symbol, company, company_rows, total))
        sent_keys.update(record_key(row) for row in company_rows)
        LOG.info("Sent alert for %s (%s), total ₹%.0f", company, symbol, total)

    if sent_keys:
        save_state(already_alerted | sent_keys)
    else:
        LOG.info("No new qualifying disclosures to alert.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
