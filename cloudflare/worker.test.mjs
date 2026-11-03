import test from "node:test";
import assert from "node:assert/strict";
import {
  constantTimeEqual,
  validateProposal,
  tickSizeForPrice,
  roundDownTick,
  isNseTradingWindow,
  approvalStatusForAction,
} from "./worker.mjs";

test("accepts a correctly sized proposal inside the ₹100–₹1,500 limits", () => {
  const value = validateProposal({
    symbol: "INFY", quantity: 2, entry: 500, stop: 480, target: 550, amount: 1000,
  });
  assert.equal(value.symbol, "INFY");
  assert.equal(value.quantity, 2);
  assert.equal(value.amount, 1000);
});

test("rejects proposals below or above the per-trade limit", () => {
  const base = { symbol: "INFY", quantity: 1, entry: 100, stop: 95, target: 110, amount: 100 };
  assert.throws(() => validateProposal({ ...base, amount: 99 }), /between/);
  assert.throws(() => validateProposal({ ...base, entry: 1500, quantity: 2, amount: 3000 }), /between/);
});

test("rejects mismatched value, invalid quantity and incorrect stop/target ordering", () => {
  assert.throws(() => validateProposal({
    symbol: "INFY", quantity: 2, entry: 500, stop: 480, target: 550, amount: 900,
  }), /does not match/);
  assert.throws(() => validateProposal({
    symbol: "INFY", quantity: 0, entry: 500, stop: 480, target: 550, amount: 500,
  }), /Quantity/);
  assert.throws(() => validateProposal({
    symbol: "INFY", quantity: 1, entry: 500, stop: 510, target: 550, amount: 500,
  }), /stop < entry < target/);
});

test("uses the current NSE price-linked tick-size slabs", () => {
  assert.equal(tickSizeForPrice(249.99), 0.01);
  assert.equal(tickSizeForPrice(250), 0.05);
  assert.equal(tickSizeForPrice(1000), 0.05);
  assert.equal(tickSizeForPrice(1000.01), 0.10);
  assert.equal(tickSizeForPrice(5000.01), 0.50);
  assert.equal(tickSizeForPrice(10000.01), 1.00);
  assert.equal(tickSizeForPrice(20000.01), 5.00);
  assert.equal(roundDownTick(500.049, 0.05), 500.00);
});

test("NSE order window is weekdays 09:15–15:25 India time", () => {
  assert.equal(isNseTradingWindow(new Date("2026-11-03T04:00:00Z")), true); // Tue 09:30 IST
  assert.equal(isNseTradingWindow(new Date("2026-11-03T10:00:00Z")), false); // Tue 15:30 IST
  assert.equal(isNseTradingWindow(new Date("2026-11-08T04:30:00Z")), false); // Sunday
});

test("constant-time comparator accepts exact values and rejects different values", () => {
  assert.equal(constantTimeEqual("secret", "secret"), true);
  assert.equal(constantTimeEqual("secret", "secreT"), false);
  assert.equal(constantTimeEqual("", "x"), false);
});

test("Telegram approval queues only; it never executes a broker order in the Worker", () => {
  assert.equal(approvalStatusForAction("approve"), "APPROVED_PENDING_STATIC_IP_EXECUTOR");
  assert.equal(approvalStatusForAction("reject"), "REJECTED");
});
