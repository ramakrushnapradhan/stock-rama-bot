# Stock Rama NSE Insider Alerts

A GitHub Actions bot that checks NSE Prohibition of Insider Trading (PIT) disclosures and sends Telegram alerts for qualifying promoter/promoter-group open-market purchases.

> **Important:** Informational monitoring only — not investment advice. Verify every disclosure against the original NSE filing. NSE can change its endpoints, response schema, access controls, or availability. GitHub scheduled jobs can be delayed and are not guaranteed to execute at the exact scheduled minute.

## 1. Configure GitHub Actions

Open **Settings → Secrets and variables → Actions** in this repository.

Add this repository **secret**:

- `TELEGRAM_BOT_TOKEN`: your newly generated Telegram bot token. Never commit the token to source code.

Add these repository **variables**:

- `TELEGRAM_CHAT_ID`: the destination chat ID. Use the numeric ID of your private chat/group, or the @username of a channel where the bot has permission to post. **Do not use the bot's own username as the destination.**
- `NSE_PIT_API_URL` (optional): leave unset to use the default NSE PIT API endpoint, `https://www.nseindia.com/api/corporates-pit`. Set this only if NSE changes the endpoint.

The Telegram token previously pasted into chat should be revoked with **@BotFather → /revoke**. Put only the replacement token in the GitHub secret.

## 2. Run the bot

The workflow runs on weekdays approximately every 10 minutes during the NSE cash-market session (09:15–15:30 India time). You can also start it manually:

**Actions → Stock Rama NSE Insider Alerts → Run workflow**

The workflow checks the latest seven calendar days by default.

## 3. Alert threshold and matching

The default threshold is ₹50,00,000 (₹50 lakh). It can be changed through the `MIN_VALUE_INR` environment variable in `.github/workflows/auto_run.yml`. The lookback window is controlled by `LOOKBACK_DAYS`.

The bot filters disclosures where the person category is Promoters or Promoter Group, the transaction is a buy, and the acquisition mode is a market transaction. It aggregates new qualifying rows by company and symbol before applying the threshold. It uses the NSE PIT fields such as `personCategory`, `tdpTransactionType`, `acqMode`, `secAcq`, and `secVal`.

## 4. Duplicate alerts

The file `alerted_transactions.json` stores keys for disclosures already sent. GitHub Actions commits this state file after a successful alert, reducing repeat notifications on later runs.

## 5. Troubleshooting

- **Missing token:** check that the repository secret is named exactly `TELEGRAM_BOT_TOKEN`.
- **Missing destination:** set `TELEGRAM_CHAT_ID` to the user/group/channel that should receive alerts. The bot must have been started by a private-chat recipient or have permission to post in the channel/group.
- **NSE 403/429 or HTML response:** NSE may be rate-limiting automated requests or changing its anti-bot checks. Review the Actions log.
- **Unexpected response shape:** inspect the latest NSE PIT API response and update the field mapping in `nse_insider_bot.py`.

