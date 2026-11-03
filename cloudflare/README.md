# Stock Rama — Cloudflare approval gateway

This Worker is the free-tier alternative to the Render approval service. GitHub Actions continues to run the NSE insider and NIFTY 500 scanners. The Worker handles Telegram Approve/Reject callbacks, Kite login, CNC LIMIT buy placement, and post-fill OCO GTT exits.

## Safety defaults

- `LIVE_TRADING_ENABLED` is `false` in `wrangler.toml`. An approval in this mode records `APPROVED_DISABLED` and sends no Zerodha order.
- ₹100 minimum and ₹1,500 maximum are enforced by the Worker.
- Every proposal expires after 5 minutes.
- Only `AUTHORIZED_TELEGRAM_USER_ID` can approve, and the callback must come from `TELEGRAM_CHAT_ID`.
- A successful approval submits a CNC LIMIT buy only during the weekday NSE order window (09:15–15:25 IST).
- After a confirmed buy fill, the Worker creates a Kite two-leg OCO GTT with stop and target. It checks GTT status every minute.
- A stop-limit/GTT trigger does not guarantee execution. If the buy fills but the exit GTT cannot be created, Telegram is sent an urgent manual-action alert. Always verify orders, GTTs, and net positions in Kite.

## 1. Create the free Cloudflare resources

1. Create/sign in to a Cloudflare account.
2. Open **Workers & Pages → D1 SQL Database → Create**.
3. Create a database named `stock-rama-db`. Choose Asia Pacific as the primary location if offered.
4. Copy its database UUID. This is not a password.
5. In Cloudflare, create an API token with account-level **Workers Scripts: Edit** and **D1: Edit** permissions.

Cloudflare currently lists a Workers Free tier and a D1 Free tier. Limits can change; check the official pricing and limits before relying on them:
- https://developers.cloudflare.com/workers/platform/limits/
- https://developers.cloudflare.com/d1/platform/pricing/

## 2. Add GitHub repository variables

Open **Settings → Secrets and variables → Actions → Variables** in this repository and add:

| Variable | Value |
|---|---|
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare account ID |
| `CLOUDFLARE_D1_DATABASE_ID` | UUID copied from the D1 database |
| `AUTHORIZED_TELEGRAM_USER_ID` | Your numeric Telegram user ID |
| `APPROVAL_SERVICE_URL` | Worker URL, e.g. `https://stock-rama-approval.<your-workers-subdomain>.workers.dev` |

Keep the existing `TELEGRAM_CHAT_ID` variable. It must be the private chat ID where Stock Rama alerts arrive. For a private one-to-one chat, it is commonly the same number as the user's Telegram ID, but confirm rather than assuming.

## 3. Add GitHub repository secrets

Open **Settings → Secrets and variables → Actions → Secrets** and add:

| Secret | Purpose |
|---|---|
| `CLOUDFLARE_API_TOKEN` | Cloudflare API token with Workers Scripts: Edit and D1: Edit |
| `TELEGRAM_WEBHOOK_SECRET` | New random secret for Telegram webhook verification |
| `KITE_API_KEY` | Zerodha Kite Connect API key |
| `KITE_API_SECRET` | Zerodha Kite Connect API secret |
| `KITE_TOKEN_ENCRYPTION_KEY` | Random 32-byte key encoded as Base64 |
| `INTERNAL_API_TOKEN` | Existing scanner-to-service bearer token |
| `TELEGRAM_BOT_TOKEN` | Existing BotFather token |

The existing `TELEGRAM_BOT_TOKEN` and `INTERNAL_API_TOKEN` should remain in GitHub Secrets. The deploy workflow copies them into Cloudflare as encrypted Worker secrets without printing their values.

Generate new random secrets on your own computer; do not paste them into chat:
- Webhook secret: `openssl rand -hex 32`
- Kite token encryption key: `openssl rand -base64 32`

The Telegram webhook secret is separate from your bot token. Do not reuse the bot token as the webhook secret.

## 4. Deploy

1. Open **Actions → Deploy Stock Rama approval Worker → Run workflow**.
2. The workflow applies the D1 migration, deploys the Worker, and copies the configured GitHub secrets into Cloudflare.
3. The first deploy prints the `workers.dev` URL. Set the repository variable `APPROVAL_SERVICE_URL` to that exact base URL.
4. Run the deploy workflow again. It will configure the Telegram webhook.
5. Verify `https://<your-worker-url>/health` returns `ok: true`.
6. Register `https://<your-worker-url>/kite/callback` as the redirect URL in the Zerodha Kite developer console.

The existing scanner workflow uses the repository variable `APPROVAL_SERVICE_URL` and the existing secret `INTERNAL_API_TOKEN`. Once the variable is set, eligible scanner proposals will be sent to this Worker.

## 5. Daily Kite login

Kite access tokens expire at 6:00 AM the next day. Each trading day, open:

`https://<your-worker-url>/kite/login`

Sign in to Zerodha. The callback exchanges the temporary request token and stores the resulting daily access token encrypted in D1 using `KITE_TOKEN_ENCRYPTION_KEY`.

Check the session with the authenticated `GET /kite/status` endpoint using the `INTERNAL_API_TOKEN` bearer header.

## 6. Enable live trading only after testing

First test the Telegram flow while `LIVE_TRADING_ENABLED = "false"`:
- Run the existing scanner workflow or submit a test proposal.
- Confirm the Telegram card arrives.
- Approve it and confirm the bot says TEST MODE and no Zerodha order was sent.
- Reject another proposal and confirm no order was sent.
- Authenticate Kite and confirm `/kite/status` reports a valid session.

Only after reviewing the code, testing with tiny simulated/test proposals, and verifying the Zerodha account setup should you deliberately change `LIVE_TRADING_ENABLED` to `"true"` in `wrangler.toml` and redeploy. Do not enable it just to test Telegram callbacks.

## Important operating limits

- GitHub Actions is still the scanner; it is not the Telegram webhook server. Cloudflare Worker provides the persistent HTTPS webhook endpoint.
- Kite requires a fresh daily login. A stale/expired session blocks new orders and monitoring API calls.
- The Worker sends orders only after an authorized Telegram approval. It never auto-approves a proposal.
- A buy limit order can remain unfilled or partially filled. The Worker cancels the unfilled remainder after a partial fill before preparing exits.
- The Worker creates OCO GTT exits only after the final buy quantity is confirmed. If the current price is already outside the stop/target corridor, or GTT placement fails, it alerts you to manage the position manually.
- GTT and stop-limit orders can be rejected, disabled, triggered without execution, or fail due to price bands, funds, holdings, or exchange conditions. A protective order is not a guaranteed exit. Check Kite after every fill and on every alert.
- The target GTT is an exit target, not a guarantee of profit. Brokerage, taxes, slippage, liquidity, and price gaps can change actual results.
