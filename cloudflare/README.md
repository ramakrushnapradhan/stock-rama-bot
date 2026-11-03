# Stock Rama — Cloudflare Telegram approval gateway

This Cloudflare Worker provides the HTTPS Telegram Approve/Reject webhook and stores proposal decisions in Cloudflare D1. It is **approval/notification only**. It does not place Zerodha orders.

## Why live API orders are disabled

Zerodha requires a whitelisted static IP for API order placement from 1 April 2026. Cloudflare Workers and standard GitHub-hosted runners do not provide this project with a dedicated fixed outbound IP. A separate executor running from a whitelisted static IP must be implemented before API orders can be enabled.

Zerodha also confirms that Kite Connect has no sandbox environment. Treat all Kite API credentials as real trading credentials. Never commit them to this repository or paste them into chat.

## Beginner setup

Follow the complete step-by-step guide:

**[cloudflare/BEGINNER_SETUP.md](BEGINNER_SETUP.md)**

It explains:
- How to invalidate API credentials that have been exposed.
- How to create the Cloudflare D1 database and API token.
- Which GitHub Actions Secrets and Variables to add.
- How to deploy this Worker from GitHub Actions.
- How to register the Telegram webhook.
- How to send a TESTONLY Telegram Approve/Reject card that cannot place a broker order.

## Worker safety behavior

- ₹100 minimum and ₹1,500 maximum trade proposal amount.
- Five-minute approval expiry.
- Approvals accepted only from the configured Telegram numeric user ID and private chat ID.
- Duplicate Telegram updates are blocked.
- Reject records `REJECTED`; Approve records `APPROVED_PENDING_STATIC_IP_EXECUTOR`.
- Both outcomes send Telegram status messages.
- No Kite API credentials are uploaded to Cloudflare by the deployment workflow.
- No Zerodha order is sent by this Worker.

## Current files

- `worker.mjs`: Cloudflare Worker webhook and proposal handling.
- `migrations/0001_schema.sql`: D1 database schema.
- `wrangler.toml`: Wrangler deployment configuration. Replace account/database placeholders only through the deployment workflow.
- `worker.test.mjs`: unit tests.
- `.github/workflows/deploy-cloudflare.yml`: manual Cloudflare deployment.
- `.github/workflows/test-cloudflare-worker.yml`: syntax and unit tests.
- `.github/workflows/test-telegram-approval.yml`: manual TESTONLY Telegram approval card.

Until a static-IP executor is built and whitelisted, execute trades manually in Kite.
