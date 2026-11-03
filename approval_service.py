"""Telegram approval gateway and guarded Zerodha Kite execution.

The service is intentionally inert until LIVE_TRADING_ENABLED=true is set.
Keep this service on a persistent HTTPS host; never run it as a short GitHub Action.
"""
from __future__ import annotations

import json
import logging
import math
import os
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

LOG = logging.getLogger("stock_rama.approvals")
DB_PATH = Path(os.getenv("APPROVAL_DB_PATH", "/var/data/stock_rama.sqlite3"))
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
INTERNAL_TOKEN = os.getenv("INTERNAL_API_TOKEN", "")
AUTHORIZED_USER = os.getenv("AUTHORIZED_TELEGRAM_USER_ID", "")
KITE_API_KEY = os.getenv("KITE_API_KEY", "")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
LIVE_TRADING = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
MIN_AMOUNT = float(os.getenv("MIN_TRADE_AMOUNT_INR", "100"))
MAX_AMOUNT = float(os.getenv("MAX_TRADE_AMOUNT_INR", "1500"))
APPROVAL_TTL_SECONDS = 300
TICK_SIZE = 0.05

app = FastAPI(title="Stock Rama Approval Gateway", version="1.0.0")
_worker_started = False
_worker_lock = threading.Lock()


class TradeProposal(BaseModel):
    symbol: str = Field(min_length=1, max_length=30)
    quantity: int = Field(gt=0)
    entry: float = Field(gt=0)
    stop: float = Field(gt=0)
    target: float = Field(gt=0)
    amount: float = Field(gt=0)
    source: str = Field(default="NSE scanner", max_length=80)


@contextmanager
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS trades (
            trade_id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
            created_at REAL NOT NULL, expires_at REAL NOT NULL,
            buy_order_id TEXT, stop_covered_qty INTEGER NOT NULL DEFAULT 0,
            filled_qty INTEGER NOT NULL DEFAULT 0, last_error TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS updates (
            update_id TEXT PRIMARY KEY, received_at REAL NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        )""")


def telegram(method: str, payload: dict[str, Any]) -> dict:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        json=payload, timeout=15,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error in {method}")
    return result.get("result", {})


def send_message(text: str, keyboard: dict | None = None):
    payload: dict[str, Any] = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = keyboard
    return telegram("sendMessage", payload)


def get_access_token() -> str | None:
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='kite_access_token'").fetchone()
        return row["value"] if row else None


def set_access_token(token: str):
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('kite_access_token',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (token,)
        )


def kite_client():
    if not KITE_API_KEY:
        raise RuntimeError("KITE_API_KEY is not configured")
    token = get_access_token()
    if not token:
        raise RuntimeError("Zerodha session missing; open /kite/login and authenticate")
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=KITE_API_KEY)
    kite.set_access_token(token)
    return kite


def round_down_tick(value: float) -> float:
    return math.floor((value + 1e-9) / TICK_SIZE) * TICK_SIZE


def validate_proposal(p: TradeProposal):
    if not (MIN_AMOUNT <= p.amount <= MAX_AMOUNT):
        raise HTTPException(422, f"Trade value must be ₹{MIN_AMOUNT:.0f}–₹{MAX_AMOUNT:.0f}")
    if abs(p.amount - p.entry * p.quantity) > max(1.0, p.amount * 0.002):
        raise HTTPException(422, "Trade amount does not match entry × quantity")
    if not (p.stop < p.entry < p.target):
        raise HTTPException(422, "Expected stop < entry < target")
    if p.symbol.upper() != p.symbol or not p.symbol.replace("&", "").replace("-", "").isalnum():
        raise HTTPException(422, "Invalid NSE trading symbol")


def execute_approved_trade(trade_id: str, payload: dict):
    """Submit one LIMIT CNC buy; background reconciliation protects actual fills."""
    if not LIVE_TRADING:
        with db() as conn:
            conn.execute("UPDATE trades SET status='APPROVED_DISABLED' WHERE trade_id=? AND status='APPROVED_PENDING_EXECUTION'", (trade_id,))
        send_message(f"⚠️ {payload['symbol']} approved, but live trading is disabled. No order was sent.")
        return
    try:
        kite = kite_client()
        margins = kite.margins("equity")
        available = float((margins.get("available") or {}).get("live_balance", 0) or 0)
        if available < float(payload["amount"]):
            raise RuntimeError(f"Insufficient available equity margin (₹{available:.2f})")
        with db() as conn:
            conn.execute("UPDATE trades SET status='SUBMITTING' WHERE trade_id=? AND status='APPROVED_PENDING_EXECUTION'", (trade_id,))
            row = conn.execute("SELECT status FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
            if not row or row["status"] != "SUBMITTING":
                return
        buy_tag = ("SR" + trade_id.replace("-", "")[:10])[:20]
        # Reconcile by deterministic broker tag before submitting. This prevents
        # duplicate buys if the process died after Zerodha accepted the request.
        prior = next((o for o in kite.orders() if o.get("tag") == buy_tag), None)
        if prior:
            order_id = str(prior["order_id"])
        else:
            try:
                order_id = kite.place_order(
                    variety=kite.VARIETY_REGULAR,
                    exchange=kite.EXCHANGE_NSE,
                    tradingsymbol=payload["symbol"],
                    transaction_type=kite.TRANSACTION_TYPE_BUY,
                    quantity=int(payload["quantity"]),
                    product=kite.PRODUCT_CNC,
                    order_type=kite.ORDER_TYPE_LIMIT,
                    price=round_down_tick(float(payload["entry"])),
                    validity=kite.VALIDITY_DAY,
                    tag=buy_tag,
                )
            except Exception:
                # The HTTP response may be lost after the broker accepted it.
                # Check the broker book before ever considering a retry.
                recovered = next((o for o in kite.orders() if o.get("tag") == buy_tag), None)
                if not recovered:
                    raise
                order_id = str(recovered["order_id"])
        with db() as conn:
            conn.execute("UPDATE trades SET status='BUY_SUBMITTED', buy_order_id=? WHERE trade_id=? AND status='SUBMITTING'", (str(order_id), trade_id))
        send_message(f"📨 BUY LIMIT submitted: {payload['symbol']} × {payload['quantity']} at ₹{payload['entry']:.2f}. Order ID: {order_id}. Stop-loss protection will be placed only for confirmed fills.")
    except Exception as exc:
        LOG.exception("Execution failed for %s", trade_id)
        with db() as conn:
            conn.execute("UPDATE trades SET status='EXECUTION_ERROR', last_error=? WHERE trade_id=?", (str(exc)[:500], trade_id))
        send_message(f"❌ Order execution failed for {payload['symbol']}: {str(exc)[:300]}")


def reconcile_fills():
    """Attach SELL SL orders to actual BUY fills; never assume requested qty filled."""
    if not LIVE_TRADING or not get_access_token():
        return
    kite = kite_client()
    orders = kite.orders()
    order_map = {str(o.get("order_id")): o for o in orders}
    with db() as conn:
        rows = conn.execute("SELECT * FROM trades WHERE status IN ('BUY_SUBMITTED','PARTIALLY_PROTECTED','PROTECTED')").fetchall()
    for row in rows:
        buy = order_map.get(str(row["buy_order_id"]))
        if not buy:
            continue
        history = kite.order_history(str(row["buy_order_id"]))
        latest = history[-1] if history else buy
        filled = int(latest.get("filled_quantity") or 0)
        if filled > int(row["stop_covered_qty"]):
            delta = filled - int(row["stop_covered_qty"])
            payload = json.loads(row["payload"])
            trigger = round_down_tick(float(payload["stop"]))
            limit_price = round_down_tick(trigger * 0.995)
            sl_tag = ("SRS" + row["trade_id"].replace("-", "")[:9])[:20]
            # Recover from a crash after placing an SL but before updating SQLite.
            existing = [o for o in orders if o.get("tag") == sl_tag and o.get("transaction_type") == "SELL"]
            already = sum(int(o.get("quantity") or 0) for o in existing)
            if already < filled:
                kite.place_order(
                    variety=kite.VARIETY_REGULAR,
                    exchange=kite.EXCHANGE_NSE,
                    tradingsymbol=payload["symbol"],
                    transaction_type=kite.TRANSACTION_TYPE_SELL,
                    quantity=filled - already,
                    product=kite.PRODUCT_CNC,
                    order_type=kite.ORDER_TYPE_SL,
                    price=limit_price,
                    trigger_price=trigger,
                    validity=kite.VALIDITY_DAY,
                    tag=sl_tag,
                )
            with db() as conn:
                conn.execute(
                    "UPDATE trades SET stop_covered_qty=?, filled_qty=?, status=? WHERE trade_id=?",
                    (filled, filled, "PROTECTED" if latest.get("status") == "COMPLETE" else "PARTIALLY_PROTECTED", row["trade_id"])
                )
            send_message(f"🛡 Stop-loss order placed for {payload['symbol']}: {filled} filled share(s), trigger ₹{trigger:.2f}.")
        elif latest.get("status") in ("REJECTED", "CANCELLED") and filled == 0:
            with db() as conn:
                conn.execute("UPDATE trades SET status=? WHERE trade_id=?", (latest.get("status"), row["trade_id"]))


def worker_loop():
    while True:
        try:
            reconcile_fills()
        except Exception:
            LOG.exception("Order reconciliation cycle failed")
        time.sleep(5)


@app.on_event("startup")
def startup():
    global _worker_started
    init_db()
    if not _worker_started:
        with _worker_lock:
            if not _worker_started:
                threading.Thread(target=worker_loop, daemon=True, name="order-reconciler").start()
                _worker_started = True


@app.get("/health")
def health():
    return {"ok": True, "live_trading_enabled": LIVE_TRADING, "kite_session_present": bool(get_access_token())}


@app.get("/kite/login")
def kite_login():
    if not KITE_API_KEY or not KITE_API_SECRET or not PUBLIC_BASE_URL:
        raise HTTPException(503, "Kite API key/secret and PUBLIC_BASE_URL must be configured")
    from kiteconnect import KiteConnect
    return RedirectResponse(KiteConnect(api_key=KITE_API_KEY).login_url())


@app.get("/kite/callback")
def kite_callback(request_token: str, status: str = "success"):
    if status != "success":
        raise HTTPException(400, "Zerodha login was not successful")
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=KITE_API_KEY)
    session = kite.generate_session(request_token, api_secret=KITE_API_SECRET)
    set_access_token(session["access_token"])
    send_message("✅ Zerodha Kite session authenticated. Daily session is ready.")
    return {"ok": True, "message": "Kite session saved. You may close this page."}


@app.post("/proposals")
def create_proposal(proposal: TradeProposal, authorization: str | None = Header(default=None)):
    if not INTERNAL_TOKEN or not secrets.compare_digest(authorization or "", f"Bearer {INTERNAL_TOKEN}"):
        raise HTTPException(401, "Unauthorized")
    validate_proposal(proposal)
    now = time.time()
    trade_id = str(uuid.uuid4())
    data = proposal.model_dump()
    with db() as conn:
        conn.execute(
            "INSERT INTO trades(trade_id,payload,status,created_at,expires_at) VALUES(?,?,?,?,?)",
            (trade_id, json.dumps(data), "PENDING", now, now + APPROVAL_TTL_SECONDS),
        )
    text = (
        f"📊 STOCK RAMA — TRADE APPROVAL\n\n"
        f"Symbol: {data['symbol']} (NSE)\nQuantity: {data['quantity']}\n"
        f"Limit buy: ₹{data['entry']:.2f}\nInvestment: ₹{data['amount']:.2f}\n"
        f"Stop-loss trigger: ₹{data['stop']:.2f}\nTarget: ₹{data['target']:.2f}\n"
        f"Source: {data['source']}\n\n"
        f"Approval expires in 5 minutes. Approve only if you want this exact LIMIT CNC order."
    )
    send_message(text, {"inline_keyboard": [[
        {"text": "✅ Approve BUY", "callback_data": f"approve:{trade_id}"},
        {"text": "❌ Reject", "callback_data": f"reject:{trade_id}"},
    ]]})
    return {"trade_id": trade_id, "status": "PENDING", "expires_in_seconds": APPROVAL_TTL_SECONDS}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    if not WEBHOOK_SECRET or not secrets.compare_digest(x_telegram_bot_api_secret_token or "", WEBHOOK_SECRET):
        raise HTTPException(401, "Invalid Telegram webhook secret")
    update = await request.json()
    update_id = str(update.get("update_id", ""))
    callback = update.get("callback_query")
    if not callback:
        return {"ok": True}
    user_id = str((callback.get("from") or {}).get("id", ""))
    if not AUTHORIZED_USER or user_id != AUTHORIZED_USER:
        telegram("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Not authorized", "show_alert": True})
        return {"ok": True}
    action, _, trade_id = str(callback.get("data", "")).partition(":")
    if action not in ("approve", "reject") or not trade_id:
        telegram("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Invalid action", "show_alert": True})
        return {"ok": True}
    now = time.time()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if update_id and conn.execute("SELECT 1 FROM updates WHERE update_id=?", (update_id,)).fetchone():
            conn.execute("COMMIT")
            return {"ok": True}
        if update_id:
            conn.execute("INSERT INTO updates(update_id,received_at) VALUES(?,?)", (update_id, now))
        row = conn.execute("SELECT * FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
        if not row or row["status"] != "PENDING":
            conn.execute("COMMIT")
            telegram("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Already handled or unknown", "show_alert": True})
            return {"ok": True}
        if now > row["expires_at"]:
            conn.execute("UPDATE trades SET status='EXPIRED' WHERE trade_id=?", (trade_id,))
            conn.execute("COMMIT")
            telegram("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Approval expired", "show_alert": True})
            return {"ok": True}
        new_status = "APPROVED_PENDING_EXECUTION" if action == "approve" else "REJECTED"
        conn.execute("UPDATE trades SET status=? WHERE trade_id=? AND status='PENDING'", (new_status, trade_id))
        conn.execute("COMMIT")
    telegram("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Approved" if action == "approve" else "Rejected"})
    try:
        telegram("editMessageReplyMarkup", {
            "chat_id": callback["message"]["chat"]["id"],
            "message_id": callback["message"]["message_id"],
            "reply_markup": {"inline_keyboard": []},
        })
    except Exception:
        LOG.warning("Could not remove handled approval buttons")
    payload = json.loads(row["payload"])
    if action == "approve":
        execute_approved_trade(trade_id, payload)
    else:
        send_message(f"Trade rejected: {payload['symbol']}. No order was placed.")
    return {"ok": True}


@app.post("/admin/set-webhook")
def set_webhook(authorization: str | None = Header(default=None)):
    if not INTERNAL_TOKEN or not secrets.compare_digest(authorization or "", f"Bearer {INTERNAL_TOKEN}"):
        raise HTTPException(401, "Unauthorized")
    if not PUBLIC_BASE_URL or not WEBHOOK_SECRET:
        raise HTTPException(503, "PUBLIC_BASE_URL and TELEGRAM_WEBHOOK_SECRET are required")
    return telegram("setWebhook", {
        "url": f"{PUBLIC_BASE_URL}/telegram/webhook",
        "secret_token": WEBHOOK_SECRET,
        "allowed_updates": ["callback_query"],
        "drop_pending_updates": True,
    })
