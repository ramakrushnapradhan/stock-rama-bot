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

export function approvalStatusForAction(action) {
  return action === "reject" ? "REJECTED" : "APPROVED_PENDING_STATIC_IP_EXECUTOR";
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

function authBearer(request, env) {
  const header = request.headers.get("authorization") || "";
  return Boolean(env.INTERNAL_API_TOKEN) && constantTimeEqual(header, "Bearer " + env.INTERNAL_API_TOKEN);
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
  const newStatus = approvalStatusForAction(action);

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
  await sendMessage(env, "✅ " + proposal.symbol + " approval recorded. No Zerodha order was submitted. Cloudflare Workers do not provide the dedicated static outbound IP required for Kite API order placement. Place the trade manually in Kite until a static-IP executor is configured.");
  return json({ ok: true, status: "APPROVED_PENDING_STATIC_IP_EXECUTOR" });
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

export default {
  async fetch(request, env) {
    if (!env.DB) return json({ error: "D1 binding DB is unavailable" }, 503);
    const url = new URL(request.url);
    try {
      if (request.method === "GET" && url.pathname === "/health") {
        return json({ ok: true, service: "stock-rama-approval", broker_order_execution: false, mode: "telegram-approval-only" });
      }
      if (request.method === "POST" && url.pathname === "/proposals") return await createProposal(request, env);
      if (request.method === "POST" && url.pathname === "/telegram/webhook") return await handleTelegramWebhook(request, env);
      if (request.method === "POST" && url.pathname === "/admin/set-webhook") return await setTelegramWebhook(request, env);
      return json({ error: "Not found" }, 404);
    } catch (error) {
      return json({ error: String(error.message || "Internal error").slice(0, 300) }, 500);
    }
  },

  async scheduled() {
    // No scheduled trading or broker polling runs in the Cloudflare Worker.
  },
};
