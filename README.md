# Stock Rama NSE Insider Alerts

A GitHub Actions Telegram alert bot for NSE insider-trading disclosures.

> **Source-data limitation:** The bot now targets NSE's **Insider Trading (PIT) – Annual** feed. NSE's May 4, 2026 circular moved new Regulation 7(2)/(7(3)) filings to an XBRL-based process; previously filed disclosures remain in the archive. The Annual feed's displayed columns do not always expose execution mode or a ticker symbol. The bot therefore fails closed: it skips promoter BUY rows unless the response explicitly identifies a Market acquisition mode. It must not describe an unverified BUY as an open-market purchase. See the [NSE PIT Annual page](https://www.nseindia.com/companies-listing/corporate-filings-pit-annual) and [NSE circular](https://www.nseindia.com/companies-listing/corporate-filings-insider-trading-archive-data).

## Configuration

In **Settings → Secrets and variables → Actions**, add:

### Repository secret
- `TELEGRAM_BOT_TOKEN`: your newly generated Telegram bot token. Never commit it to source code.

### Repository variable
- `TELEGRAM_CHAT_ID`: the destination chat ID. Use the numeric ID of your private chat/group, or the @username of a channel where the bot has permission to post. **Do not use the bot's own username as the destination.**

The Telegram token previously pasted into chat should be revoked with **@BotFather → /revoke**. Put only the replacement token in the GitHub secret.

## Running

The GitHub Actions workflow currently supports **manual dispatch only**. Automatic 10-minute scheduling remains disabled until the new endpoint and response schema have been verified with a successful live run.

The script now requests `/api/corporate-IT-PIT-Annual`. It accepts common NSE response envelopes and field-name variants, but only sends an open-market alert when the source explicitly provides a Market acquisition mode. Rows without that field are skipped rather than guessed.

## Intended alert criteria

The original requirements were:
- Promoter or Promoter Group;
- open-market purchase (not sale, pledge, ESOP, or other mode);
- aggregate transaction value of at least ₹50 lakh;
- rolling seven-day lookback;
- one Telegram alert per new disclosure/company group.

The Annual PIT schema is not guaranteed to expose the old detailed fields. If a disclosure has no explicit execution-mode field, it is not eligible for the strict open-market alert. If a ticker is absent, the alert uses the company name and does not invent a symbol.

## Duplicate-alert state

`alerted_transactions.json` stores disclosure keys already sent. GitHub Actions commits changes to this file after a successful run.

## Next compatibility step

After the first successful live run, inspect the returned NSE field names and add only verified aliases. Do not enable the 10-minute schedule until a sample disclosure has been checked against the NSE filing page and the alert filter is confirmed.
