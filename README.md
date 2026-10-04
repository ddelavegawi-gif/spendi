# 💸 Spendi — WhatsApp expense tracker for two

Text Spendi what you spent; it figures out the category, saves it, and tells you what's left.

```
Diego:  100 mxn in whole foods
Spendi: ✅ Expense saved under Groceries.
        $100 · Whole Foods
        $9,900 available out of $10,000 this month.

Romi:   200 mxn in sephora
Spendi: ✅ Expense saved under Personal Expenses Romi.

Diego:  show current budget
Spendi: 📊 October budget · day 4 of 31
        🛒 Groceries
        $9,900 available out of $10,000
        ▓░░░░░░░░░ 1% used
        ...
```

## How it works

```
WhatsApp ──► Twilio ──► app.py (/whatsapp) ──► bot.py ──► SQLite
                                     │
                         interpreter.py → Claude (understands the message)
```

- **Shared categories** (Groceries, Restaurants, Transport…): one household budget, whoever pays.
- **Personal Expenses**: a separate budget per person. Spendi knows who wrote by their phone number,
  so Romi's Sephora goes to *Personal Expenses Romi* and Diego's Nike to *Personal Expenses Diego*.
  Say "for Romi" to log something on the other person's budget.
- **Weekly budgets** are derived from monthly ones (monthly × 12 ÷ 52). Weeks start Monday.
- Claude reads free text in English or Spanish ("350 uber ayer", "50 oxxo y 120 starbucks").
  If the API is down or no key is set, a keyword parser keeps basic logging working.

## Things you can say

| Message | What happens |
|---|---|
| `100 mxn whole foods` | Logs an expense |
| `350 uber yesterday` / `ayer` | Logs it with yesterday's date |
| `20 usd uber` | Converts with `fx_rates` in config |
| `200 sephora for Romi` | Logs to Romi's personal budget |
| `show budget` / `show weekly budget` | Budget overview |
| `how much is left in groceries?` | One category |
| `last expenses` | Latest 15 this month |
| `undo` | Deletes *your* last expense |
| `set groceries budget to 12000` | Changes a monthly budget |
| `help` | Instructions |

## 1. Configure

Edit **`config.yaml`**: your two WhatsApp numbers (international format, e.g. `+5255…`), categories,
monthly budgets and merchant keywords. Copy `.env.example` to `.env` and add your
[Anthropic API key](https://console.anthropic.com/).

## 2. Try it locally (no WhatsApp needed)

```bash
pip install -r requirements.txt
python chat_sim.py          # type as Diego, switch with /Romi
```

## 3. Deploy

Any host that runs Python and gives you a persistent disk for the SQLite file works
(Railway, Render, Fly.io, a small VPS). Start command:

```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```

Set `SPENDI_DB` to a path on the persistent volume (e.g. `/data/spendi.db`), otherwise expenses
are lost on redeploy. For local testing you can expose your machine with `ngrok http 8000`.

## 4. Connect WhatsApp (Twilio)

**Quick start — Sandbox (free, ~10 min)**
1. Create a Twilio account → Messaging → *Try it out* → *Send a WhatsApp message*.
2. You and Romi each send the `join <code>` message to the sandbox number from your phones.
3. In *Sandbox settings*, set **When a message comes in** to `https://<your-domain>/whatsapp` (POST).
4. Save the sandbox number in your contacts as **Spendi**. Done.

The sandbox is meant for testing: it shows Twilio's name, and you may need to re-send the join
message from time to time.

**Permanent — your own "Spendi" number**
Register a WhatsApp sender in Twilio (needs a phone number and a Meta Business account; approval
usually takes a few days). You get a real business profile named Spendi. Point its webhook to the
same `/whatsapp` URL — no code changes.

Since you always message Spendi first and it replies instantly, you stay inside WhatsApp's
24-hour reply window and don't need message templates.

Set `TWILIO_AUTH_TOKEN` and `PUBLIC_BASE_URL` in production so Spendi rejects requests that
don't come from Twilio. Messages from numbers not in `config.yaml` are ignored.

## Costs (rough)
- Twilio: a fraction of a US cent per message plus WhatsApp's per-conversation fee (user-initiated
  service conversations are currently free or very cheap — check Twilio's pricing page).
- Claude Haiku: well under a cent per message.
- Hosting: free tier to ~$5/month.

## Files
| File | Purpose |
|---|---|
| `config.yaml` | People, categories, budgets, keywords |
| `app.py` | Twilio webhook (FastAPI) |
| `bot.py` | Logic and reply formatting |
| `interpreter.py` | Claude parsing + offline fallback |
| `storage.py` | SQLite tables |
| `chat_sim.py` | Terminal simulator |

## Ideas for later
- Weekly summary every Sunday (needs an approved WhatsApp template since Spendi would message first)
- Photo of a receipt → expense (Claude can read images; Twilio sends them as `MediaUrl0`)
- Export to Google Sheets / CSV
- Category budgets that roll over unspent money
