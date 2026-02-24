# Lessons Learned

Lessons from development sessions — for reference in future work on this project.

---

## Technical

### 1. Always use data-source dates, not wall-clock dates, for time-series keys

When storing time-series data from an external API (yfinance, or any market data provider), use the **date returned by the API** as the record key — not `datetime.now()`.

If the consumer process runs 24/7 but the data source only updates on trading days, using `datetime.now()` creates phantom entries on weekends and holidays. Each new calendar day appends a new record with stale data, silently corrupting rolling averages and comparisons.

```python
# Wrong — creates phantom entries on non-trading days
today = datetime.now().strftime('%Y-%m-%d')

# Correct — uses the actual date the data represents
trade_date = latest.name.strftime('%Y-%m-%d')  # yfinance DataFrame index
```

---

### 2. Exclude today's partial data from historical baselines

When computing a rolling average (e.g. 7-day volume average) during an active trading session, today's entry holds **intraday (partial) data** — not a completed day's figure. Including it in the historical baseline skews the average.

The pro-rata adjustment handles the *comparison side* (scaling today's volume to an expected run-rate), but the *baseline side* should use only completed days:

```python
# Exclude today (last entry) from the historical average
history = data['daily_prices'][:-1] if len(data['daily_prices']) > 1 else data['daily_prices']
recent = [d['volume'] for d in history[-7:] if d.get('volume')]
```

---

### 3. yfinance: accessing the actual trading date

The yfinance `history()` DataFrame is indexed by timestamp. The actual trading date of the latest bar is:

```python
data = yf.Ticker(ticker).history(period='2d')
latest = data.iloc[-1]
trade_date = latest.name.strftime('%Y-%m-%d')
```

`latest.name` is the pandas Timestamp index of that row.

---

### 4. State-based alerting requires persistent state across restarts

Alert state (triggered / not triggered) is stored in `alert_states/state_user_{id}.json`. This is critical — without it, every restart would re-fire all currently-triggered alerts. Always persist state to disk, not only in memory.

---

### 5. Two-process architecture: shared flat-file storage needs care

The Telegram bot (`telegram_bot_multistock.py`) and the monitor (`robo_monitor_multistock.py`) both read and write the same JSON files. There is no locking. This works in practice because:
- The bot writes user config files; the monitor reads them (mostly non-overlapping)
- The monitor writes price history and alert states; the bot reads alert states for display

If either process is ever made to write more aggressively, file locking or an atomic write pattern (write to temp → rename) should be added.

---

## Architecture & Design

### 6. Keep secrets out of source code from day one

Even for a single-user or small project, store credentials in a separate `config.json` excluded from git. The cost is near zero; the benefit is that the code can be safely shared, open-sourced, or reviewed without credential exposure.

Template pattern used here:
- `config.json.example` — committed, shows structure, placeholder values
- `config.json` — in `.gitignore`, created manually on each deployment target, never committed

---

### 7. Separate deployment from code

The VM's `config.json` and data directories survive `git pull` updates because they are gitignored. This means:
- Code updates never accidentally overwrite user data
- Credentials never touch the repo
- `git pull` + `systemctl restart` is a safe, repeatable update pattern

---

## Process

### 8. Deployment guides for non-technical users need exact values, not descriptions

Vague instructions like "choose a small machine type" are not actionable. Exact tables work better:

| Setting | Value |
|---------|-------|
| Machine type | `e2-micro` |
| Region | `us-east1`, `us-central1`, or `us-west1` |

Similarly, include full copy-pasteable systemd service file content rather than describing what fields to change.

---

### 9. Local PyCharm testing works out of the box

Both scripts can run locally without any VM or special setup:
1. Install packages via pip
2. Create a local `config.json` (gitignored, safe)
3. Run each script in a separate terminal

This is the fastest way to verify logic changes before pushing and redeploying. Prefer testing locally first.

---

### 10. Warn users when local and VM bots share the same token

If the local dev instance and the VM instance both run with the same bot token simultaneously, **both will process incoming messages and both will send outgoing alerts**. Users see duplicate messages. Stop the VM services before doing a local test run, or use a separate test bot token for development.
