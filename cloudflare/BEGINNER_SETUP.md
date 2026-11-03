# Stock Rama — beginner setup guide

This guide is written for someone who does not work in software. Do the steps in order. **Do not paste any API keys, API secrets, Telegram bot tokens, Cloudflare tokens, or passwords into Git files, comments, or chat.**

## First: protect the Zerodha credentials that were shared

The API key and API secret shared in the chat must be treated as exposed. Zerodha says it does not provide a sandbox account for Kite Connect; API credentials belong to an app linked to a real trading account. An API order can therefore be a real order.

1. Open the [Kite Connect Developer Console](https://developers.kite.trade/).
2. Open **My Apps** and select the app whose credentials were shared.
3. If that app is only for Stock Rama and is not used by another system, delete it. Zerodha's documented delete flow is **Delete → type `I UNDERSTAND` → OK**. Deleting the app invalidates its API key, API secret, and access tokens.
4. Do not create or share replacement credentials yet. First complete the static-IP decision below. Never reuse the exposed secret.

If the app is used by anything else, stop before deleting it and contact Zerodha support for the safest way to invalidate the exposed credentials.

Official references:
- [Does Zerodha offer a sandbox?](https://support.zerodha.com/category/trading-and-markets/general-kite/kite-api/articles/kite-connect-api-faqs)
- [Delete a Kite Connect app](https://support.zerodha.com/category/trading-and-markets/general-kite/kite-api/articles/unsubscribe-unlink-trading-account-kite-api)

## Important: why Telegram approval does not place a live order yet

Zerodha's current guidance says API order placement requires a whitelisted static IP from 1 April 2026. A normal Cloudflare Worker or GitHub-hosted Actions runner does not give this project a dedicated, fixed outbound IP that you can whitelist.

So this version deliberately works in safe approval mode:
- NSE scanners continue to run in GitHub Actions.
- Eligible proposals can be sent to Telegram.
- You can press Approve or Reject.
- The Worker records your decision and notifies you.
- **It does not place a Zerodha order.** Place any trade manually in Kite until a separate executor is set up on a whitelisted static IP.

Do not change `LIVE_TRADING_ENABLED` to true. The Cloudflare Worker has been restricted to approval/notification duties and does not contain an active broker-order route.

For automated order placement later, you need:
1. A fixed public IP from your internet provider or a cloud/VPS provider.
2. That IP registered in the Kite Connect Developer Console.
3. A separate executor running from that same IP. That executor is not included in this approval-only deployment yet.

If you do not already have a static IP, ask your internet provider whether they can provide one and what it costs. Do not buy a VPS or API subscription until you have decided whether you want to pay for this.

## Part A — create the free Cloudflare approval service

1. Open [Cloudflare](https://dash.cloudflare.com/) and sign in or create an account.
2. In the dashboard, open **Workers & Pages**.
3. Open **D1 SQL Database** and choose **Create database**.
4. Name the database exactly `stock-rama-db`.
5. After creation, copy the database UUID. It is a long ID, not a password.
6. Open your Cloudflare account overview and copy your **Account ID**.
7. Open **My Profile → API Tokens → Create Token**.
8. Create a custom token with only these permissions for the account that owns this Worker:
   - **Workers Scripts: Edit**
   - **D1: Edit**
9. Copy the Cloudflare API token once. Keep it private.

Cloudflare free-tier limits and features can change. Check [Workers limits](https://developers.cloudflare.com/workers/platform/limits/) and [D1 pricing/limits](https://developers.cloudflare.com/d1/platform/pricing/).

## Part B — add GitHub Secrets

Open the [Stock Rama repository](https://github.com/ramakrushnapradhan/stock-rama-bot), then:

**Settings → Secrets and variables → Actions → Secrets → New repository secret**

Create or confirm these secrets:

| Secret name | What to put there |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your existing BotFather token (keep the existing secret; do not paste it here) |
| `INTERNAL_API_TOKEN` | Existing long random token used by the scanner; if missing, create a new strong random value |
| `CLOUDFLARE_API_TOKEN` | The Cloudflare API token created in Part A |
| `TELEGRAM_WEBHOOK_SECRET` | A new random secret; do not reuse your Telegram bot token |

### Generate the two new random values on Windows

Open **PowerShell** on your Windows computer. Paste this command to generate a webhook secret:

```powershell
$bytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
[BitConverter]::ToString($bytes).Replace("-", "").ToLower()
```

Copy the displayed value directly into the GitHub secret named `TELEGRAM_WEBHOOK_SECRET`.

If you need a new `INTERNAL_API_TOKEN`, run the same command again and copy its output into that secret. Do not send either value to me.

**Do not add `KITE_API_KEY` or `KITE_API_SECRET` for this approval-only Worker.** Those credentials are not needed for the safe Telegram approval test, and must never be committed to the repository.

## Part C — add GitHub Variables

In the same GitHub page, click the **Variables** tab, then **New repository variable**.

Add:

| Variable name | Value |
|---|---|
| `CLOUDFLARE_ACCOUNT_ID` | Account ID copied in Part A |
| `CLOUDFLARE_D1_DATABASE_ID` | Database UUID copied in Part A |
| `AUTHORIZED_TELEGRAM_USER_ID` | Your personal numeric Telegram user ID |
| `TELEGRAM_CHAT_ID` | Keep your existing chat ID variable |
| `APPROVAL_SERVICE_URL` | Leave blank for the moment; add it after the first deployment |

To find your Telegram user ID, open Telegram, search for `@userinfobot`, press Start, and copy the numeric ID it returns. Use your own numeric user ID, not your bot's username.

## Part D — deploy the safe approval Worker

1. Open [GitHub Actions](https://github.com/ramakrushnapradhan/stock-rama-bot/actions).
2. Select **Deploy Stock Rama approval Worker** on the left.
3. Click **Run workflow** and confirm **Run workflow**.
4. Open the run that starts. When it completes successfully, open its logs and find the `workers.dev` URL printed by Wrangler. It looks like `https://stock-rama-approval.<your-subdomain>.workers.dev`.
5. Go to the repository **Settings → Secrets and variables → Actions → Variables**.
6. Create or edit `APPROVAL_SERVICE_URL` and paste only that base URL (no trailing slash).
7. Run **Deploy Stock Rama approval Worker** one more time. This second run registers the Telegram webhook.
8. Open `<your-worker-url>/health` in a browser. You should see JSON with `"ok":true`.

The Worker uses Telegram's webhook for button callbacks. If the same Telegram bot is already connected to another webhook-based application, setting this webhook will replace that other webhook. The existing scanner's simple send-message alerts do not require a webhook.

## Part E — test Telegram buttons without a broker order

1. Open [GitHub Actions](https://github.com/ramakrushnapradhan/stock-rama-bot/actions).
2. Select **Send Stock Rama Telegram approval test**.
3. Click **Run workflow**.
4. A test-only `TESTONLY` proposal for ₹100 should appear in your Telegram chat.
5. Tap **Approve BUY**. The bot should confirm that approval was recorded and clearly say **no Zerodha order was submitted**.
6. Run the test workflow again and tap **Reject**. The bot should confirm rejection and no order.

This confirms that GitHub → Cloudflare → Telegram callbacks work. It is not a broker order test.

## After setup

- The existing NSE scanner runs remain in GitHub Actions.
- The Cloudflare Worker only receives proposals and records Approve/Reject decisions.
- Use Kite manually for actual orders until a static-IP executor is implemented and tested.
- Keep the existing ₹100 minimum and ₹1,500 maximum per trade.
- Never enable unattended trading. A limit buy may not fill, and a stop-loss or GTT trigger does not guarantee an exit price.

## Zerodha API plan note

Zerodha's current Personal (Free) plan supports order/GTT/portfolio APIs but does not include live market data or historical candle data. Its paid Connect plan is listed at ₹500 per month per app and includes live/historical data. The current approval-only Worker does not need either plan. Check the latest official pricing before subscribing:
https://support.zerodha.com/category/trading-and-markets/general-kite/kite-api/articles/what-are-the-charges-for-kite-apis

## Current project state

| Component | State |
|---|---|
| NSE insider and NIFTY 500 scanners | In GitHub |
| Telegram message alerts | Uses existing GitHub Actions configuration |
| Cloudflare approval Worker code | In `main` |
| Cloudflare D1 database | Must be created in your Cloudflare account |
| Telegram webhook | Must be registered by running the deploy workflow |
| Approve/Reject test | Available as a manual GitHub Actions workflow |
| Zerodha API credentials | Do not add yet; the exposed pair must be invalidated |
| Live Zerodha order execution | Disabled/not available until a static-IP executor is built and whitelisted |

