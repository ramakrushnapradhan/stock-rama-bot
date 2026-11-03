# Stock Rama NSE Insider Alerts

A GitHub Actions Telegram alert bot for NSE insider-trading disclosures.

> **Important — source-data change:** NSE currently displays a separate **Insider Trading (PIT) – Annual** feed. The older detailed API used by the first version of this bot contains fields such as symbol and acquisition mode, but a July 2026 community data-maintainer report says that detailed feed stopped receiving updates after 2 May 2026. The current Annual feed is coarser and does not provide the same symbol/mode detail. Therefore the automatic schedule is intentionally **paused**: do not rely on this repository for current trade alerts until the data-source adapter is updated and tested. See the current [NSE PIT Annual page](https://www.nseindia.com/companies-listing/corporate-filings-pit-annual).

## Configuration

In **Settings → Secrets and variables → Actions**, add:

### Repository secret
- `TELEGRAM_BOT_TOKEN`: your newly generated Telegram bot token. Never commit it to source code.

### Repository variable
- `TELEGRAM_CHAT_ID`: the destination chat ID. Use the numeric ID of your private chat/group, or the @username of a channel where the bot has permission to post. **Do not use the bot's own username as the destination.**

The Telegram token previously pasted into chat should be revoked with **@BotFather → /revoke**. Put only the replacement token in the GitHub secret.

## Running

The GitHub Actions workflow currently supports **manual dispatch only**. Automatic 10-minute scheduling is disabled until the new NSE Annual PIT format can be parsed and its limitations are handled correctly.

Do not manually run the legacy script expecting fresh post-May-2026 disclosures; it uses the old detailed `/api/corporates-pit` feed.

## Intended alert criteria

The original requirements were:
- Promoter or Promoter Group;
- open-market purchase (not sale, pledge, ESOP, or other mode);
- aggregate transaction value of at least ₹50 lakh;
- rolling seven-day lookback;
- one Telegram alert per new disclosure/company group.

The old detailed NSE schema exposed fields including `personCategory`, `tdpTransactionType`, `acqMode`, `symbol`, `secAcq`, and `secVal`. The current PIT Annual presentation has a different, coarser structure; the exact open-market filter and symbol mapping cannot safely be assumed from the old schema.

## Duplicate-alert state

`alerted_transactions.json` stores disclosure keys already sent. GitHub Actions commits changes to this file after a successful run.

## Next compatibility step

Update the source adapter to read the current PIT Annual feed and explicitly label what it can and cannot determine. In particular, if market mode is not supplied, the bot must not describe a disclosure as an open-market purchase. If symbol mapping is unavailable, alerts should identify the company name instead of guessing a ticker.
