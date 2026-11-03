"""Send eligible BUY SETUP candidates to the private Telegram approval gateway."""
from __future__ import annotations

import logging
import os
from typing import Any

import requests

LOG = logging.getLogger("stock_rama.proposals")


def submit_trade_proposal(symbol: str, result: dict[str, Any], source: str) -> dict[str, Any] | None:
    base_url = os.getenv("APPROVAL_SERVICE_URL", "").strip().rstrip("/")
    token = os.getenv("INTERNAL_API_TOKEN", "").strip()
    if not base_url or not token:
        LOG.info("Approval gateway is not configured; %s remains an alert only.", symbol)
        return None
    if result.get("action") != "BUY SETUP" or not result.get("shares") or not result.get("position_value"):
        return None
    payload = {
        "symbol": symbol.upper(),
        "quantity": int(result["shares"]),
        "entry": round(float(result["entry"]), 2),
        "stop": round(float(result["stop"]), 2),
        "target": round(float(result["target1"]), 2),
        "amount": round(float(result["position_value"]), 2),
        "source": source[:80],
    }
    try:
        response = requests.post(
            f"{base_url}/proposals",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        LOG.exception("Could not submit %s to the approval gateway.", symbol)
        return None
