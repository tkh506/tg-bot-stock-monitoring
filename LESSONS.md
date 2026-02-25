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

---

### 11. Python stdout is buffered by default — add `PYTHONUNBUFFERED=1` to systemd services

When running Python scripts as systemd services, `print()` output is buffered and does not appear in `journalctl` until the buffer flushes (or the process exits). This makes live debugging impossible.

Fix: add `Environment="PYTHONUNBUFFERED=1"` to the `[Service]` block of every Python `.service` file:

```ini
[Service]
Environment="PYTHONUNBUFFERED=1"
ExecStart=/usr/bin/python3 /path/to/script.py
```

---

### 12. Seed price history on first run — don't wait for organic data accumulation

Alert conditions that require multiple days of history (e.g. 1-day price change, 7-day volume average) silently never fire until the monitor has run across enough separate trading days. On a fresh install this means alerts may be broken for days before the user notices.

Fix: detect sparse history (fewer than 2 entries) and seed it upfront using `yf.Ticker(ticker).history(period='15d')`. Alerts work correctly from the very first monitor cycle.

---

### 13. Alert description logic must factor in both operator and threshold sign

When generating a natural-language description for an alert (e.g. "Drops >2.5% in 1 day"), it is tempting to key only on the threshold sign. But the operator matters too. `operator='>'` with `threshold=-2.5` means "triggers when change > -2.5%" — which fires almost always and is not a drop alert at all.

Always derive descriptions from the *combination* of operator and threshold. Fall back to showing the explicit condition (`1D change > -2.5%`) for unusual combinations rather than generating a misleading natural-language label.

---

### 14. Never push to git or deploy to the VM before the user has dry-run locally

After making code changes, **always stop at the local commit step**. Do not run `git push` or SSH to the VM to restart services until the user has:
1. Tested the change locally (e.g. via a test bot token)
2. Confirmed it works as expected
3. Explicitly asked to push and deploy

Pushing broken code to the VM takes down the production bot for all users. The correct workflow is:

```
Code change → git commit (local only) → user dry-runs locally → user confirms → git push → VM git pull + systemctl restart
```

---

### 15. Openrouter + Claude: do not use `response_format: json_object`

`"response_format": {"type": "json_object"}` is an **OpenAI-specific** API parameter. When sent to Claude models via Openrouter, it causes the `content` field in the response to be an empty string — no error is raised, the HTTP status is 200, but `content` is `""`. `json.loads("")` then fails with `Expecting value: line 1 column 1 (char 0)`.

Fix: remove `response_format` from the payload entirely. Instead, instruct the model in the prompt to return only valid JSON (e.g. `"Return ONLY valid JSON — no markdown fences, no comments, no text outside the JSON object."`). Claude reliably follows this instruction.

---

### 17. Verbose LLMs need higher `max_tokens` — and a `finish_reason` guard

Different models have very different output verbosity. GPT-5 mini and DeepSeek V3.x generate significantly more tokens per response than older models like Claude Haiku or GPT-4o mini. A `max_tokens` value that worked fine for one model may silently truncate another mid-JSON, producing `Unterminated string` parse errors.

Rules:
- Set `max_tokens` generously (4000+) for modern frontier models
- Always check `finish_reason` before parsing — if it equals `"length"`, the response was cut off and JSON parsing will fail; raise a clear error and log the partial content

```python
finish_reason = raw_response.get("choices", [{}])[0].get("finish_reason", "")
if finish_reason == "length":
    raise ValueError("AI response was cut off (token limit reached). Please try again.")
```

---

### 18. Reddit `.json` trick: free sentiment data, but with real limitations

Every Reddit page can be accessed as raw JSON by appending `.json` to the URL. For ticker-specific sentiment, use the search endpoint:

```
https://www.reddit.com/r/stocks/search.json?q=TICKER&sort=new&limit=5&restrict_sr=1
```

No API key or account needed. Key limitations:
- **HK tickers**: near-zero coverage — skip entirely for `.HK` tickers
- **Cloud VMs**: Reddit may rate-limit or block requests from cloud IP ranges; always wrap in `try/except` with a short timeout (5s)
- **Relevance**: only popular US stocks have meaningful post volume; ETFs and small caps may return 0 results
- **User-Agent**: include a descriptive `User-Agent` header to reduce blocking risk

---

### 19. Structured output sections force the AI to engage with each data source

A single `"reasoning"` field in the AI JSON response gives the model freedom to ignore inconvenient data (e.g. skipping sentiment if it seems noisy). Splitting the response into explicit named sections (`price_analysis`, `sector_analysis`, `news_analysis`, `sentiment_analysis`, `macro_analysis`) forces the model to populate each one, ensuring it actively processes every data category.

Side benefit: the user sees clearly organised output in Telegram, making it easier to spot which factor drove the recommendation.

---

### 16. Use `raw_decode()` instead of `json.loads()` for AI-generated JSON

`json.loads()` is strict — it rejects any characters after the closing `}`, including trailing newlines, notes, or explanations that an LLM might append. This raises `Extra data: line N column 1`.

Use `json.JSONDecoder().raw_decode(text)` instead. It parses the first complete JSON object and returns `(result, end_index)`, silently ignoring everything after the object closes. Also defensively skip any leading non-JSON text with `text[text.find("{"):]` before calling `raw_decode`.

```python
# Fragile — breaks on any trailing text
result = json.loads(content)

# Robust — ignores trailing text after the JSON object
json_start = content.find("{")
result, _ = json.JSONDecoder().raw_decode(content[json_start:])
```
