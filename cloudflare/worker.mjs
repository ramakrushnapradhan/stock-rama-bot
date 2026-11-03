/**
 * Stock Rama Telegram approval gateway for Cloudflare Workers + D1.
 *
 * Live order execution is deliberately disabled by default in wrangler.toml.
 * This Worker accepts authenticated scanner proposals, routes Telegram
 * callbacks, exchanges the daily Kite request token, places CNC LIMIT buys,
 * and attaches an OCO GTT (stop + target) after the buy is confirmed filled.
 */
const KITE_BASE = "https://api.kite.trade";
const TELEGRAM_BASE = "https://api.telegram.org";
const ACTIVE_ORDER_STATES = new Set([
  "OPEN", "OPEN PENDING", "VALIDATION PENDING", "PUT ORDER REQ RECEIVED",
  "MODIFY VALIDATION PENDING", "MODIFY PENDING", "TRIGGER PENDING",
]);

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });

export function constantTimeEqual(left, right) {
  const a = String(left ?? "");
  const b = String(right ?? "");
  let diff = a.length ^ b.length;
  const len = Math.max(a.length, b.length);
  for (let i = 0; i < len; i += 1) {
    diff |= (a.charCodeAt(i % (a.length || 1)) || 0) ^ (b.charCodeAt(i % (b.length || 1)) || 0);
  }
  return diff === 0;
}

export function validateProposal(input, minAmount = 100, maxAmount = 1500) {
  const symbol = String(input?.symbol ?? "").trim().toUpperCase();
  const quantity = Number(input?.quantity);
  const entry = Number(input?.entry);
  const stop = Number(input?.stop);
  const target = Number(input?.target);
  const amount = Number(input?.amount);
  if (!/^[A-Z0-9&.-]{1,30}$/.test(symbol)) throw new Error("Invalid NSE trading symbol");
  if (!Number.isInteger(quantity) || quantity <= 0) throw new Error("Quantity must be a positive integer");
  if (![entry, stop, target, amount].every(Number.isFinite) || [entry, stop, target, amount].some((v) => v <= 0)) {
    throw new Error("Entry, stop, target and amount must be positive numbers");
  }
  if (amount < minAmount || amount > maxAmount) {
    throw new Error("Trade value must be between ₹" + minAmount + " and ₹" + maxAmount);
  }
  if (Math.abs(amount - entry * quantity) > Math.max(1, amount * 0.002)) {
    throw new Error("Trade amount does not match entry × quantity");
  }
  if (!(stop < entry && entry < target)) throw new Error("Expected stop < entry < target");
  return {
    symbol, quantity, entry: round2(entry), stop: round2(stop), target: round2(target),
    amount: round2(amount), source: String(input?.source ?? "NSE scanner").slice(0, 80),
  };
}

export function tickSizeForPrice(price) {
  // NSE cash-market price-linked slabs. The exchange reviews the applicable
  // slab monthly; proposals still need broker/exchange validation at placement.
  if (price < 250) return 0.01;
  if (price <= 1000) return 0.05;
  if (price <= 5000) return 0.10;
  if (price <= 10000) return 0.50;
  if (price <= 20000) return 1.00;
  return 5.00;
}

export function roundDownTick(price, tick = tickSizeForPrice(price)) {
  const units = Math.floor((price + 1e-9) / tick);
  return round2(units * tick);
}

const round2 = (value) => Math.round((Number(value) + Number.EPSILON) * 100) / 100;

export function isNseTradingWindow(date = new Date()) {
  const parts = new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata", weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(date);
  const part = (type) => parts.find((p) => p.type === type)?.value;
  const weekday = part("weekday");
  const minutes = Number(part("hour")) * 60 + Number(part("minute"));
  return ["Mon", "Tue", "Wed", "Thu", "Fri"].includes(weekday) && minutes >= 555 && minutes <= 925;
}

function requireEnv(env, keys) {
  const missing = keys.filter((key) => !env[key]);
  if (missing.length) throw new Error("Missing Worker configuration: " + missing.join(", "));
}

async function telegram(env, method, payload) {
  requireEnv(env, ["TELEGRAM_BOT_TOKEN"]);
  const response = await fetch(TELEGRAM_BASE + "/bot" + env.TELEGRAM_BOT_TOKEN + "/" + method, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok || !result.ok) throw new Error("Telegram " + method + " failed: " + (result.description || response.status));
  return result.result;
}

async function sendMessage(env, text, replyMarkup = undefined) {
  requireEnv(env, ["TELEGRAM_CHAT_ID"]);
  const payload = { chat_id: env.TELEGRAM_CHAT_ID, text, disable_web_page_preview: true };
  if (replyMarkup) payload.reply_markup = replyMarkup;
  return telegram(env, "sendMessage", payload);
}

async function getSetting(env, key) {
  const row = await env.DB.prepare("SELECT value FROM settings WHERE key = ?").bind(key).first();
  return row?.value ?? null;
}

async function setSetting(env, key, value) {
  await env.DB.prepare(
    "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value"
  ).bind(key, String(value)).run();
}

function toBase64(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function fromBase64(value) {
  return Uint8Array.from(atob(value), (character) => character.charCodeAt(0));
}

async function encryptionKey(env) {
  requireEnv(env, ["KITE_TOKEN_ENCRYPTION_KEY"]);
  const raw = fromBase64(env.KITE_TOKEN_ENCRYPTION_KEY);
  if (raw.byteLength !== 32) throw new Error("KITE_TOKEN_ENCRYPTION_KEY must be a base64-encoded 32-byte key");
  return crypto.subtle.importKey("raw", raw, { name: "AES-GCM" }, false, ["encrypt", "decrypt"]);
}

async function setKiteAccessToken(env, token) {
  const key = await encryptionKey(env);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const encrypted = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, new TextEncoder().encode(token));
  await setSetting(env, "kite_access_token", "v1." + toBase64(iv) + "." + toBase64(new Uint8Array(encrypted)));
}

async function getKiteAccessToken(env) {
  const stored = await getSetting(env, "kite_access_token");
  if (!stored) return null;
  const parts = stored.split(".");
  if (parts.length !== 3 || parts[0] !== "v1") {
    throw new Error("Stored Kite token format is invalid; authenticate again after configuring KITE_TOKEN_ENCRYPTION_KEY");
  }
  const key = await encryptionKey(env);
  const decrypted = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: fromBase64(parts[1]) },
    key,
    fromBase64(parts[2])
  );
  return new TextDecoder().decode(decrypted);
}

function authBearer(request, env) {
  const header = request.headers.get("authorization") || "";
  return Boolean(env.INTERNAL_API_TOKEN) && constantTimeEqual(header, "Bearer " + env.INTERNAL_API_TOKEN);
}

async function kiteRequest(env, path, { method = "GET", form = null } = {}) {
  requireEnv(env, ["KITE_API_KEY"]);
  const accessToken = await getKiteAccessToken(env);
  if (!accessToken) throw new Error("Zerodha session is missing or expired. Open /kite/login and sign in again.");
  const headers = {
    "X-Kite-Version": "3",
    "Authorization": "token " + env.KITE_API_KEY + ":" + accessToken,
  };
  const options = { method, headers };
  if (form) {
    headers["content-type"] = "application/x-www-form-urlencoded";
    options.body = new URLSearchParams(form).toString();
  }
  const response = await fetch(KITE_BASE + path, options);
  const result = await response.json();
  if (!response.ok || result.status !== "success") {
    throw new Error(result.message || result.error_type || ("Kite API returned HTTP " + response.status));
  }
  return result.data;
}

async function kiteGet(env, path, query = {}) {
  const qs = new URLSearchParams(query).toString();
  return kiteRequest(env, path + (qs ? "?" + qs : ""));
}

async function exchangeRequestToken(env, requestToken) {
  requireEnv(env, ["KITE_API_KEY", "KITE_API_SECRET"]);
  const source = env.KITE_API_KEY + requestToken + env.KITE_API_SECRET;
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(source));
  const checksum = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
  const response = await fetch(KITE_BASE + "/session/token", {
    method: "POST",
    headers: { "X-Kite-Version": "3", "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ api_key: env.KITE_API_KEY, request_token: requestToken, checksum }).toString(),
  });
  const result = await response.json();
  if (!response.ok || result.status !== "success" || !result.data?.access_token) {
    throw new Error(result.message || "Kite login token exchange failed");
  }
  await setKiteAccessToken(env, result.data.access_token);
  await setSetting(env, "kite_login_date_utc", new Date().toISOString().slice(0, 10));
  return result.data;
}

async function sendProposal(env, proposal, tradeId, expiresAt) {
  const rupees = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 }).format(proposal.amount);
  const message =
    "📊 STOCK RAMA — TRADE APPROVAL\n\n" +
    "Symbol: " + proposal.symbol + " (NSE)\n" +
    "Quantity: " + proposal.quantity + "\n" +
    "LIMIT buy: ₹" + proposal.entry.toFixed(2) + "\n" +
    "Investment: ₹" + rupees + "\n" +
    "Stop-loss: ₹" + proposal.stop.toFixed(2) + "\n" +
    "Target: ₹" + proposal.target.toFixed(2) + "\n" +
    "Source: " + proposal.source + "\n\n" +
    "Expires in 5 minutes. Approval submits this exact CNC LIMIT buy only.";
  return telegram(env, "sendMessage", {
    chat_id: env.TELEGRAM_CHAT_ID,
    text: message,
    disable_web_page_preview: true,
    reply_markup: { inline_keyboard: [[
      { text: "✅ Approve BUY", callback_data: "approve:" + tradeId },
      { text: "❌ Reject", callback_data: "reject:" + tradeId },
    ]] },
  });
}

async function createProposal(request, env) {
  if (!authBearer(request, env)) return json({ error: "Unauthorized" }, 401);
  requireEnv(env, ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "INTERNAL_API_TOKEN"]);
  const input = await request.json();
  let proposal;
  try {
    proposal = validateProposal(input, Number(env.MIN_TRADE_AMOUNT_INR || 100), Number(env.MAX_TRADE_AMOUNT_INR || 1500));
  } catch (error) {
    return json({ error: error.message }, 422);
  }
  const now = Math.floor(Date.now() / 1000);
  const ttl = Number(env.APPROVAL_TTL_SECONDS || 300);
  const tradeId = crypto.randomUUID();
  const buyTag = ("SR" + tradeId.replaceAll("-", "")).slice(0, 20);
  await env.DB.prepare(
    "INSERT INTO trades(trade_id,payload,status,created_at,expires_at,buy_tag) VALUES(?,?,?,?,?,?)"
  ).bind(tradeId, JSON.stringify(proposal), "PENDING", now, now + ttl, buyTag).run();

  try {
    await sendProposal(env, proposal, tradeId, now + ttl);
  } catch (error) {
    await env.DB.prepare("UPDATE trades SET status='ALERT_FAILED',last_error=?,updated_at=? WHERE trade_id=?")
      .bind(String(error.message).slice(0, 400), now, tradeId).run();
    return json({ error: "Proposal saved but Telegram alert failed" }, 502);
  }
  return json({ trade_id: tradeId, status: "PENDING", expires_in_seconds: ttl });
}

async function clearApprovalButtons(env, callback) {
  try {
    await telegram(env, "editMessageReplyMarkup", {
      chat_id: callback.message.chat.id,
      message_id: callback.message.message_id,
      reply_markup: { inline_keyboard: [] },
    });
  } catch {
    // Approval state is authoritative in D1; button cleanup is cosmetic.
  }
}

async function answerCallback(env, callbackId, text, showAlert = false) {
  try {
    await telegram(env, "answerCallbackQuery", {
      callback_query_id: callbackId, text, show_alert: showAlert,
    });
  } catch {
    // Telegram may have already expired the callback query.
  }
}

async function handleTelegramWebhook(request, env) {
  if (!env.TELEGRAM_WEBHOOK_SECRET ||
      !constantTimeEqual(request.headers.get("X-Telegram-Bot-Api-Secret-Token"), env.TELEGRAM_WEBHOOK_SECRET)) {
    return json({ error: "Invalid webhook secret" }, 401);
  }
  const update = await request.json();
  const callback = update.callback_query;
  if (!callback) return json({ ok: true });

  const userId = String(callback.from?.id ?? "");
  const chatId = String(callback.message?.chat?.id ?? "");
  if (!env.AUTHORIZED_TELEGRAM_USER_ID ||
      userId !== String(env.AUTHORIZED_TELEGRAM_USER_ID) ||
      chatId !== String(env.TELEGRAM_CHAT_ID ?? "")) {
    await answerCallback(env, callback.id, "Not authorized", true);
    return json({ ok: true });
  }

  const [action, tradeId] = String(callback.data ?? "").split(":");
  if (!["approve", "reject"].includes(action) || !tradeId) {
    await answerCallback(env, callback.id, "Invalid approval action", true);
    return json({ ok: true });
  }

  const now = Math.floor(Date.now() / 1000);
  const updateId = String(update.update_id ?? callback.id);
  const newStatus = action === "reject" ? "REJECTED" :
    (String(env.LIVE_TRADING_ENABLED).toLowerCase() === "true" ? "EXECUTING" : "APPROVED_DISABLED");

  // D1 batch is transactional. Duplicate Telegram update IDs and already-handled
  // trade IDs cannot win this conditional state transition twice.
  const results = await env.DB.batch([
    env.DB.prepare("INSERT OR IGNORE INTO processed_updates(update_id,processed_at) VALUES(?,?)").bind(updateId, now),
    env.DB.prepare(
      "UPDATE trades SET status=?,updated_at=? WHERE trade_id=? AND status='PENDING' AND expires_at>? RETURNING trade_id,payload,buy_tag"
    ).bind(newStatus, now, tradeId, now),
  ]);
  if (Number(results[0]?.meta?.changes || 0) === 0) {
    await answerCallback(env, callback.id, "Already processed", true);
    return json({ ok: true });
  }
  const accepted = results[1]?.results?.[0];
  if (!accepted) {
    const existing = await env.DB.prepare("SELECT status,expires_at FROM trades WHERE trade_id=?")
      .bind(tradeId).first();
    if (existing?.status === "PENDING" && Number(existing.expires_at) <= now) {
      await env.DB.prepare("UPDATE trades SET status='EXPIRED',updated_at=? WHERE trade_id=? AND status='PENDING'")
        .bind(now, tradeId).run();
    }
    await answerCallback(env, callback.id, "Expired, already handled or unknown", true);
    return json({ ok: true });
  }

  await answerCallback(env, callback.id, action === "approve" ? "Approval received" : "Trade rejected");
  await clearApprovalButtons(env, callback);
  const proposal = JSON.parse(accepted.payload);
  if (action === "reject") {
    await sendMessage(env, "❌ " + proposal.symbol + " rejected. No Zerodha order was sent.");
    return json({ ok: true });
  }
  if (newStatus === "APPROVED_DISABLED") {
    await sendMessage(env, "🟡 " + proposal.symbol + " approved in TEST MODE. Live trading is disabled; no Zerodha order was sent.");
    return json({ ok: true });
  }
  await executeApprovedBuy(env, accepted.trade_id, accepted.buy_tag, proposal);
  return json({ ok: true });
}

async function executeApprovedBuy(env, tradeId, buyTag, proposal) {
  try {
    if (!isNseTradingWindow()) {
      await env.DB.prepare("UPDATE trades SET status='MARKET_CLOSED',updated_at=? WHERE trade_id=? AND status='EXECUTING'")
        .bind(Math.floor(Date.now() / 1000), tradeId).run();
      await sendMessage(env, "⏸ " + proposal.symbol + " was approved outside the NSE order window (09:15–15:25 IST, weekdays). No order was sent. Request a fresh proposal during market hours.");
      return;
    }
    const margin = await kiteGet(env, "/user/margins/equity");
    const available = Number(margin?.available?.live_balance ?? 0);
    if (available < proposal.amount) throw new Error("Insufficient available equity balance for this trade");

    // Reconcile by our deterministic Kite tag before placing anything.
    const currentOrders = await kiteGet(env, "/orders");
    let order = currentOrders.find((item) => item.tag === buyTag);
    if (!order) {
      const price = roundDownTick(proposal.entry);
      try {
        const data = await kiteRequest(env, "/orders/regular", {
          method: "POST",
          form: {
            exchange: "NSE", tradingsymbol: proposal.symbol,
            transaction_type: "BUY", quantity: String(proposal.quantity),
            product: "CNC", order_type: "LIMIT", price: String(price),
            validity: "DAY", tag: buyTag,
          },
        });
        const orderId = String(data.order_id);
        await env.DB.prepare("UPDATE trades SET status='BUY_SUBMITTED',buy_order_id=?,updated_at=? WHERE trade_id=?")
          .bind(orderId, Math.floor(Date.now() / 1000), tradeId).run();
        await sendMessage(env, "📨 Zerodha BUY LIMIT submitted: " + proposal.symbol + " × " + proposal.quantity +
          " at ₹" + price.toFixed(2) + ". Order ID: " + orderId + ". Waiting for fill confirmation before attaching OCO stop-loss/target.");
        return;
      } catch (submitError) {
        // If the HTTP response was lost after broker acceptance, query the order
        // book again. Never blindly repeat a potentially accepted order.
        const afterError = await kiteGet(env, "/orders");
        order = afterError.find((item) => item.tag === buyTag);
        if (!order) throw new Error("Order submission outcome is uncertain; check Zerodha order book before retrying. " + submitError.message);
      }
    }
    await env.DB.prepare("UPDATE trades SET status='BUY_SUBMITTED',buy_order_id=?,updated_at=? WHERE trade_id=?")
      .bind(String(order.order_id), Math.floor(Date.now() / 1000), tradeId).run();
    await sendMessage(env, "🔎 Existing tagged Zerodha order reconciled for " + proposal.symbol + ". Order ID: " + order.order_id);
  } catch (error) {
    await env.DB.prepare("UPDATE trades SET status='EXECUTION_ERROR',last_error=?,updated_at=? WHERE trade_id=?")
      .bind(String(error.message).slice(0, 400), Math.floor(Date.now() / 1000), tradeId).run();
    await sendMessage(env, "🚨 " + proposal.symbol + " approval did not complete safely: " + String(error.message).slice(0, 250) +
      ". Check Zerodha order book; do not retry until order status is verified.");
  }
}

async function orderHistory(env, orderId) {
  const data = await kiteGet(env, "/orders/" + encodeURIComponent(orderId));
  return Array.isArray(data) ? data[data.length - 1] : data;
}

async function currentLtp(env, symbol) {
  const data = await kiteGet(env, "/quote/ltp", { i: "NSE:" + symbol });
  const quote = data["NSE:" + symbol];
  if (!quote || !Number.isFinite(Number(quote.last_price))) throw new Error("Could not retrieve current NSE price for " + symbol);
  return Number(quote.last_price);
}

async function createExitOco(env, trade, proposal, quantity) {
  const ltp = await currentLtp(env, proposal.symbol);
  const instrumentTick = tickSizeForPrice(proposal.entry);
  const stopTrigger = roundDownTick(proposal.stop, instrumentTick);
  const stopLimit = roundDownTick(Math.max(stopTick, stopTrigger - stopTick * 2), stopTick);
  const targetTrigger = roundDownTick(proposal.target, instrumentTick);
  const targetLimit = targetTrigger;
  if (!(stopTrigger < ltp && ltp < targetTrigger)) {
    throw new Error("Current price ₹" + ltp.toFixed(2) +
      " is outside the stop/target corridor. Buy is filled but an OCO GTT cannot be safely created; place/check the exit manually now.");
  }
  const condition = {
    exchange: "NSE", tradingsymbol: proposal.symbol,
    trigger_values: [stopTrigger, targetTrigger], last_price: ltp,
  };
  const orders = [
    { exchange: "NSE", tradingsymbol: proposal.symbol, transaction_type: "SELL",
      quantity, order_type: "LIMIT", product: "CNC", price: stopLimit },
    { exchange: "NSE", tradingsymbol: proposal.symbol, transaction_type: "SELL",
      quantity, order_type: "LIMIT", product: "CNC", price: targetLimit },
  ];
  const data = await kiteRequest(env, "/gtt/triggers", {
    method: "POST",
    form: { type: "two-leg", condition: JSON.stringify(condition), orders: JSON.stringify(orders) },
  });
  return { id: String(data.trigger_id), ltp, stopTrigger, stopLimit, targetTrigger, targetLimit };
}

async function protectFilledBuys(env) {
  const rowsResult = await env.DB.prepare(
    "SELECT * FROM trades WHERE status IN ('BUY_SUBMITTED','GTT_SUBMITTING') ORDER BY created_at LIMIT 25"
  ).all();
  const rows = rowsResult.results || [];
  if (!rows.length) return;
  const orders = await kiteGet(env, "/orders");
  for (const trade of rows) {
    const proposal = JSON.parse(trade.payload);
    if (trade.status === "GTT_SUBMITTING") {
      const triggers = await kiteGet(env, "/gtt/triggers");
      const candidate = (triggers || []).find((item) =>
        item.type === "two-leg" && item.condition?.tradingsymbol === proposal.symbol &&
        item.orders?.[0]?.quantity === Number(trade.filled_qty) &&
        item.condition?.trigger_values?.length === 2 &&
        Math.abs(Number(item.condition.trigger_values[0]) - roundDownTick(proposal.stop, tickSizeForPrice(proposal.entry))) < 0.001 &&
        Math.abs(Number(item.condition.trigger_values[1]) - roundDownTick(proposal.target, tickSizeForPrice(proposal.entry))) < 0.001 &&
        ["active", "triggered"].includes(item.status)
      );
      if (candidate) {
        await env.DB.prepare("UPDATE trades SET status='PROTECTED',gtt_id=?,gtt_status=?,updated_at=? WHERE trade_id=?")
          .bind(String(candidate.id), candidate.status, Math.floor(Date.now() / 1000), trade.trade_id).run();
        await sendMessage(env, "🛡 Reconciled exit OCO GTT for " + proposal.symbol + ". GTT ID: " + candidate.id);
      } else {
        await env.DB.prepare("UPDATE trades SET status='GTT_UNKNOWN',last_error=?,updated_at=? WHERE trade_id=?")
          .bind("GTT API response was lost; inspect Kite GTT book manually before any retry.", Math.floor(Date.now() / 1000), trade.trade_id).run();
        await sendMessage(env, "🚨 GTT submission status is unknown for " + proposal.symbol +
          ". Check Kite GTT book and positions manually. The system will not create a duplicate GTT automatically.");
      }
      continue;
    }

    let order = orders.find((item) => String(item.order_id) === String(trade.buy_order_id));
    if (!order && trade.buy_tag) order = orders.find((item) => item.tag === trade.buy_tag);
    if (!order) {
      await env.DB.prepare("UPDATE trades SET status='BUY_ORDER_NOT_FOUND',last_error=?,updated_at=? WHERE trade_id=?")
        .bind("Buy order is absent from today's Kite order book; verify it manually.", Math.floor(Date.now() / 1000), trade.trade_id).run();
      await sendMessage(env, "🚨 Cannot find today's buy order for " + proposal.symbol + ". Verify your Zerodha order book and holdings manually.");
      continue;
    }

    if (ACTIVE_ORDER_STATES.has(order.status) && Number(order.filled_quantity || 0) > 0) {
      try {
        await kiteRequest(env, "/orders/regular/" + encodeURIComponent(order.order_id), { method: "DELETE" });
        order = await orderHistory(env, order.order_id);
      } catch (error) {
        await sendMessage(env, "🚨 " + proposal.symbol + " has a partial buy fill and the unfilled balance could not be cancelled: " +
          String(error.message).slice(0, 180) + ". Check Kite immediately.");
        continue;
      }
    } else if (ACTIVE_ORDER_STATES.has(order.status)) {
      continue;
    }

    const stable = ["COMPLETE", "CANCELLED", "REJECTED"].includes(order.status);
    if (!stable) continue;
    const filled = Number(order.filled_quantity || 0);
    if (filled <= 0) {
      await env.DB.prepare("UPDATE trades SET status='BUY_NO_FILL',updated_at=? WHERE trade_id=?")
        .bind(Math.floor(Date.now() / 1000), trade.trade_id).run();
      await sendMessage(env, "ℹ️ " + proposal.symbol + " buy order ended without a fill. No exit GTT was required.");
      continue;
    }

    await env.DB.prepare("UPDATE trades SET status='GTT_SUBMITTING',filled_qty=?,updated_at=? WHERE trade_id=?")
      .bind(filled, Math.floor(Date.now() / 1000), trade.trade_id).run();
    try {
      const gtt = await createExitOco(env, trade, proposal, filled);
      await env.DB.prepare("UPDATE trades SET status='PROTECTED',gtt_id=?,gtt_status='active',last_error=NULL,updated_at=? WHERE trade_id=?")
        .bind(gtt.id, Math.floor(Date.now() / 1000), trade.trade_id).run();
      await sendMessage(env, "🛡 OCO GTT created for " + proposal.symbol + " × " + filled +
        ". Stop trigger ₹" + gtt.stopTrigger.toFixed(2) + " (limit ₹" + gtt.stopLimit.toFixed(2) +
        "), target trigger ₹" + gtt.targetTrigger.toFixed(2) + ". GTT ID: " + gtt.id +
        ". Verify it in Kite; stop-limit execution is not guaranteed.");
    } catch (error) {
      await env.DB.prepare("UPDATE trades SET status='PROTECTION_ERROR',last_error=?,updated_at=? WHERE trade_id=?")
        .bind(String(error.message).slice(0, 400), Math.floor(Date.now() / 1000), trade.trade_id).run();
      await sendMessage(env, "🚨 BUY FILLED BUT EXIT GTT FAILED for " + proposal.symbol + " × " + filled +
        ": " + String(error.message).slice(0, 220) + ". Place/verify your stop-loss in Kite immediately.");
    }
  }
}

async function monitorGtts(env) {
  const rowsResult = await env.DB.prepare(
    "SELECT trade_id,payload,gtt_id,gtt_status FROM trades WHERE status='PROTECTED' AND gtt_id IS NOT NULL LIMIT 50"
  ).all();
  const rows = rowsResult.results || [];
  if (!rows.length) return;
  const triggers = await kiteGet(env, "/gtt/triggers");
  const byId = new Map((triggers || []).map((item) => [String(item.id), item]));
  for (const trade of rows) {
    const current = byId.get(String(trade.gtt_id));
    if (!current) continue;
    if (current.status !== trade.gtt_status) {
      await env.DB.prepare("UPDATE trades SET gtt_status=?,updated_at=? WHERE trade_id=?")
        .bind(String(current.status), Math.floor(Date.now() / 1000), trade.trade_id).run();
      if (current.status !== "active") {
        const detail = (current.orders || []).map((o) => {
          const result = o.result?.order_result;
          return result ? (result.status + (result.rejection_reason ? ": " + result.rejection_reason : "")) : "pending";
        }).join(" | ");
        const severity = ["disabled", "expired", "cancelled", "deleted", "rejected"].includes(current.status) ? "🚨" : "🔔";
        await sendMessage(env, severity + " GTT status changed for " + JSON.parse(trade.payload).symbol +
          ": " + current.status + (detail ? "\n" + detail : "") +
          ". Check Kite order book and net position.");
      }
    }
  }
}

async function scheduledHandler(env) {
  if (!await getKiteAccessToken(env)) return;
  try {
    await protectFilledBuys(env);
    await monitorGtts(env);
  } catch (error) {
    const today = new Date().toISOString().slice(0, 10);
    const lastNotice = await getSetting(env, "reconcile_error_notice_date");
    if (lastNotice !== today) {
      await setSetting(env, "reconcile_error_notice_date", today);
      await sendMessage(env, "⚠️ Stock Rama order/GTT monitoring failed: " + String(error.message).slice(0, 220) +
        ". Check Zerodha order book and GTTs manually.");
    }
    throw error;
  }
}

async function setTelegramWebhook(request, env) {
  if (!authBearer(request, env)) return json({ error: "Unauthorized" }, 401);
  requireEnv(env, ["TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET"]);
  const base = new URL(request.url).origin;
  const result = await telegram(env, "setWebhook", {
    url: base + "/telegram/webhook",
    secret_token: env.TELEGRAM_WEBHOOK_SECRET,
    allowed_updates: ["callback_query"],
    drop_pending_updates: false,
  });
  return json({ ok: true, webhook_url: base + "/telegram/webhook", telegram_result: result });
}

async function kiteStatus(request, env) {
  if (!authBearer(request, env)) return json({ error: "Unauthorized" }, 401);
  const token = await getKiteAccessToken(env);
  if (!token) return json({ kite_session: "missing", live_trading_enabled: String(env.LIVE_TRADING_ENABLED) === "true" });
  try {
    const profile = await kiteGet(env, "/user/profile");
    return json({
      kite_session: "valid", user_id: profile.user_id,
      live_trading_enabled: String(env.LIVE_TRADING_ENABLED) === "true",
    });
  } catch {
    return json({ kite_session: "expired_or_invalid", live_trading_enabled: String(env.LIVE_TRADING_ENABLED) === "true" }, 401);
  }
}

export default {
  async fetch(request, env) {
    if (!env.DB) return json({ error: "D1 binding DB is unavailable" }, 503);
    const url = new URL(request.url);
    try {
      if (request.method === "GET" && url.pathname === "/health") {
        return json({ ok: true, service: "stock-rama-approval", live_trading_enabled: String(env.LIVE_TRADING_ENABLED) === "true" });
      }
      if (request.method === "GET" && url.pathname === "/kite/login") {
        requireEnv(env, ["KITE_API_KEY", "KITE_API_SECRET", "KITE_TOKEN_ENCRYPTION_KEY"]);
        return Response.redirect("https://kite.zerodha.com/connect/login?v=3&api_key=" + encodeURIComponent(env.KITE_API_KEY), 302);
      }
      if (request.method === "GET" && url.pathname === "/kite/callback") {
        if (url.searchParams.get("status") !== "success" || !url.searchParams.get("request_token")) {
          return json({ error: "Kite login was not successful" }, 400);
        }
        const session = await exchangeRequestToken(env, url.searchParams.get("request_token"));
        await sendMessage(env, "✅ Zerodha Kite session authenticated for " + (session.user_id || "your account") +
          ". The access token expires at 6:00 AM IST; sign in again each trading day.");
        return new Response("<!doctype html><title>Stock Rama</title><h2>Kite session connected</h2><p>You may close this page. Live order execution remains controlled by the Worker setting.</p>", {
          headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" },
        });
      }
      if (request.method === "GET" && url.pathname === "/kite/status") return kiteStatus(request, env);
      if (request.method === "POST" && url.pathname === "/proposals") return await createProposal(request, env);
      if (request.method === "POST" && url.pathname === "/telegram/webhook") return await handleTelegramWebhook(request, env);
      if (request.method === "POST" && url.pathname === "/admin/set-webhook") return await setTelegramWebhook(request, env);
      return json({ error: "Not found" }, 404);
    } catch (error) {
      return json({ error: String(error.message || "Internal error").slice(0, 300) }, 500);
    }
  },

  async scheduled(_controller, env) {
    await scheduledHandler(env);
  },
};
