#!/usr/bin/env python3
"""Daily NSE PIT Regulation 7(2) XBRL promoter-buy monitor.

Reads the current NSE insider-trading filing list, downloads each XBRL instance,
filters promoter/promoter-group market purchases, and sends Telegram alerts.
Informational only; verify the original NSE filing before acting.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests

from swing_strategy import analyze_symbol, format_swing_analysis

LOG = logging.getLogger("stock_rama_bot")
NSE_PAGE_URL = "https://www.nseindia.com/companies-listing/corporate-filings-insider-trading"
NSE_PIT_API_URL = "https://www.nseindia.com/api/corporates-pit-gg"
NSE_BASE_URL = "https://www.nseindia.com"
STATE_FILE = Path(os.getenv("STATE_FILE", "alerted_transactions.json"))
MIN_VALUE_INR = float(os.getenv("MIN_VALUE_INR", "5000000"))
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "7"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
INDIA_TZ = ZoneInfo("Asia/Kolkata")


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": NSE_PAGE_URL,
        "Connection": "keep-alive",
    })
    # NSE issues cookies from the public filings page before serving API data.
    response = session.get(NSE_PAGE_URL, timeout=30)
    response.raise_for_status()
    return session


def fetch_filing_list(session: requests.Session, start_date, end_date) -> list[dict]:
    params = {
        "index": "equities",
        "from_date": start_date.strftime("%d-%m-%Y"),
        "to_date": end_date.strftime("%d-%m-%Y"),
    }
    last_error = None
    for attempt in range(3):
        try:
            response = session.get(NSE_PIT_API_URL, params=params, timeout=35)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                rows = payload.get("data")
                if not isinstance(rows, list):
                    raise ValueError(
                        "NSE PIT filing-list response did not contain a data array; "
                        f"keys={list(payload)[:30]!r}"
                    )
            elif isinstance(payload, list):
                rows = payload
            else:
                raise ValueError(f"Unexpected NSE filing-list type: {type(payload).__name__}")
            if rows and not all(isinstance(row, dict) for row in rows):
                raise ValueError("NSE filing-list data contained non-object entries.")
            LOG.info(
                "NSE PIT filing list: status=%s url=%s rows=%d fields=%s",
                response.status_code, response.url, len(rows),
                list(rows[0].keys())[:30] if rows else [],
            )
            if rows and not any(row.get("xmlFileName") for row in rows):
                raise ValueError("NSE filing rows did not contain xmlFileName; refusing to treat as no disclosures.")
            return rows
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            LOG.warning("NSE filing-list attempt %d/3 failed: %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Could not retrieve current NSE PIT filing list: {last_error}") from last_error


def fetch_xbrl(session: requests.Session, file_reference: str) -> str:
    url = urljoin(NSE_BASE_URL, file_reference)
    response = session.get(url, timeout=30)
    response.raise_for_status()
    if not response.content:
        raise ValueError(f"NSE returned an empty XBRL document: {url}")
    return response.content.decode("utf-8-sig", errors="replace")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def parse_xbrl_facts(xml_text: str) -> list[dict]:
    """Expand XBRL facts into one row per disclosure context, inheriting MainI facts."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"Could not parse NSE XBRL instance: {exc}") from exc

    facts_by_context: dict[str, dict] = {}
    for element in root.iter():
        context = element.attrib.get("contextRef")
        if not context or len(element):
            continue
        name = local_name(element.tag)
        value = (element.text or "").strip()
        if value:
            facts_by_context.setdefault(context, {})[name] = value

    main = facts_by_context.get("MainI", {})
    rows = []
    for context, facts in facts_by_context.items():
        if context == "MainI":
            continue
        row = {**main, **facts, "_context": context}
        if any(key in row for key in (
            "NameOfThePerson", "CategoryOfPerson",
            "SecuritiesAcquiredOrDisposedNumberOfSecurity",
        )):
            rows.append(row)
    return rows


def to_number(value) -> float:
    if value is None:
        return 0.0
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


def normalized(value) -> str:
    return re.sub(r"[^a-z]", "", str(value or "").lower())


def is_promoter(row: dict) -> bool:
    category = str(first(row, "CategoryOfPerson") or "").lower()
    return "promoter" in category


def is_market_buy(row: dict) -> bool:
    transaction_type = normalized(first(row, "SecuritiesAcquiredOrDisposedTransactionType"))
    mode = normalized(first(row, "ModeOfAcquisitionOrDisposal"))
    return transaction_type in {"buy", "purchase", "acquisition"} and mode in {
        "marketpurchase", "marketbuy", "purchasefrommarket"
    }


def transaction_value(row: dict) -> float:
    return to_number(first(row, "SecuritiesAcquiredOrDisposedValueOfSecurity"))


def transaction_quantity(row: dict) -> float:
    return to_number(first(row, "SecuritiesAcquiredOrDisposedNumberOfSecurity"))


def record_key(row: dict) -> str:
    return "|".join(str(value or "") for value in (
        row.get("_filing_id"),
        row.get("_context"),
        row.get("Symbol") or row.get("_symbol"),
        row.get("NameOfThePerson"),
        row.get("DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate"),
        row.get("DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyToDate"),
        row.get("SecuritiesAcquiredOrDisposedNumberOfSecurity"),
        row.get("SecuritiesAcquiredOrDisposedValueOfSecurity"),
    ))


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
        raise RuntimeError("Missing TELEGRAM_CHAT_ID GitHub Actions variable.")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "disable_web_page_preview": True},
        timeout=20,
    )
    response.raise_for_status()
    if not response.json().get("ok"):
        raise RuntimeError("Telegram API rejected the message.")


def format_alert(symbol: str, company: str, rows: list[dict], total: float, analysis: dict) -> str:
    lines = [
        "📈 NSE PROMOTER OPEN-MARKET BUY ALERT",
        f"Company: {company}",
        f"Symbol: {symbol}",
        f"Aggregate disclosed value: ₹{total:,.0f}",
        f"Transactions: {len(rows)}",
        "",
    ]
    for row in rows[:8]:
        person = first(row, "NameOfThePerson") or "Promoter"
        date_from = first(row, "DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate")
        date_to = first(row, "DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyToDate")
        date = date_from or date_to or "Not listed"
        lines.append(
            f"• {person} | {date} | Qty: {transaction_quantity(row):,.0f} | "
            f"₹{transaction_value(row):,.0f}"
        )
    if len(rows) > 8:
        lines.append(f"…and {len(rows) - 8} more qualifying disclosure(s).")
    lines.extend(format_swing_analysis(analysis))
    lines.extend([
        "",
        f"Source: NSE Regulation 7(2) XBRL disclosures ({NSE_PAGE_URL})",
        "Verify the original filing before acting. Informational only — not investment advice.",
    ])
    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        LOG.error("Missing Telegram configuration: TELEGRAM_BOT_TOKEN secret and TELEGRAM_CHAT_ID variable are required.")
        return 2

    today = datetime.now(INDIA_TZ).date()
    start_date = today - timedelta(days=LOOKBACK_DAYS)
    try:
        session = make_session()
        filings = fetch_filing_list(session, start_date, today)
        all_rows = []
        for index, filing in enumerate(filings, start=1):
            reference = filing.get("xmlFileName")
            if not reference:
                continue
            xml_text = fetch_xbrl(session, reference)
            rows = parse_xbrl_facts(xml_text)
            filing_id = str(filing.get("appId") or reference)
            symbol = str(filing.get("symbol") or "").strip()
            broadcast = str(filing.get("broadcastDateTime") or "")
            for row in rows:
                row["_filing_id"] = filing_id
                row["_symbol"] = symbol
                row["_broadcast"] = broadcast
                all_rows.append(row)
            if index % 10 == 0:
                LOG.info("Downloaded XBRL details for %d/%d filings; disclosure rows=%d", index, len(filings), len(all_rows))
            time.sleep(0.15)  # avoid hammering NSE
    except Exception:
        LOG.exception("Unable to retrieve or parse current NSE PIT XBRL disclosures.")
        return 1

    LOG.info("Parsed %d XBRL disclosure rows from %d NSE filings.", len(all_rows), len(filings))
    eligible = []
    for row in all_rows:
        if not is_promoter(row) or not is_market_buy(row):
            continue
        value = transaction_value(row)
        if value <= 0:
            LOG.warning(
                "Skipping disclosure with missing/non-positive XBRL value: symbol=%s person=%s",
                row.get("_symbol"), row.get("NameOfThePerson"),
            )
            continue
        row["_value"] = value
        eligible.append(row)

    already_alerted = load_state()
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in eligible:
        symbol = str(row.get("_symbol") or row.get("Symbol") or "UNKNOWN").strip()
        company = str(row.get("NameOfTheCompany") or symbol).strip()
        grouped.setdefault((symbol, company), []).append(row)

    sent_keys: set[str] = set()
    for (symbol, company), company_rows in grouped.items():
        # Calculate the threshold across the full rolling window, including
        # disclosures already seen on previous days. Alert only when this
        # company has at least one newly published filing in the current run.
        new_rows = [row for row in company_rows if record_key(row) not in already_alerted]
        if not new_rows:
            continue
        total = sum(row["_value"] for row in company_rows)
        if total < MIN_VALUE_INR:
            continue
        analysis = analyze_symbol(symbol, total, len(company_rows))
        send_telegram(format_alert(symbol, company, company_rows, total, analysis))
        sent_keys.update(record_key(row) for row in company_rows)
        LOG.info(
            "Sent promoter-buy alert: %s (%s), ₹%.0f across %d rolling-window disclosures "
            "(%d new)", company, symbol, total, len(company_rows), len(new_rows)
        )

    if sent_keys:
        save_state(already_alerted | sent_keys)
    else:
        LOG.info("No new promoter open-market buy disclosures ≥ ₹%.0f found in the %d-day window.", MIN_VALUE_INR, LOOKBACK_DAYS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
