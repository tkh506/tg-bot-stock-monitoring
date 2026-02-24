# Changelog

---

## 2026-02-24

### Bug Fix — Volume Alert: Phantom Entries on Non-Trading Days (`robo_monitor_multistock.py`)

**Problem:** The monitor used `datetime.now()` as the date key when saving price history. Since the monitor runs 24/7, weekend and holiday runs saved the last trading day's data under new calendar dates (e.g. Friday's data stored again under Saturday, Sunday, and Monday). This created phantom duplicate entries, which contaminated the 7-day volume average — e.g. after a 3-day weekend, the average could include 3 copies of the same Friday's volume.

**Fix:** `get_stock_data()` now returns the actual trading date from the yfinance DataFrame index (`latest.name`). `process_stock()` uses this `trade_date` instead of `datetime.now()`.

```python
# Before
today = datetime.now().strftime('%Y-%m-%d')
self.history.add_price_point(ticker, today, ...)

# After
price, volume, trade_date = self.get_stock_data(ticker)
self.history.add_price_point(ticker, trade_date, ...)
```

---

### Bug Fix — Volume Alert: Partial Intraday Volume in 7-Day Average (`robo_monitor_multistock.py`)

**Problem:** `get_volume_average_7d()` included today's entry (`daily_prices[-7:]`). During market hours, today's entry holds partial intraday volume, which skews the baseline average downward and inflates the computed `vol_ratio`.

**Fix:** The average is now computed from `daily_prices[:-1][-7:]` — the 7 most recent *completed* trading days, excluding today.

---

### New — GitHub Workflow

Replaced the manual file-paste deployment approach with a GitHub-based workflow.

**New files:**
- `.gitignore` — excludes `config.json`, data directories (`user_configs/`, `alert_states/`, `price_history/`), `__pycache__/`, `.DS_Store`
- `config.json.example` — committed template; actual `config.json` is created manually on the VM and never committed

**Update workflow (going forward):**
```bash
# Local: push changes
git add . && git commit -m "..." && git push

# VM: pull and restart
git pull
sudo systemctl restart robo-config-bot.service robo-monitor-bot.service
```

---

### New — `CLAUDE.md`

Created project reference file covering architecture, data formats, alert types, deployment workflow, and key design decisions. Intended for future AI-assisted sessions.

---

### Updated — `MULTISTOCK_DEPLOY_GUIDE.md`

Fully rewrote the deployment guide. Now covers:
- **Part 0:** Local testing with PyCharm (install deps, create local `config.json`, run both scripts, test via Telegram)
- **Part 1:** Push local code to GitHub (git init, PAT note)
- **Part 2:** Create Google Cloud free-tier VM (exact settings table)
- **Part 3:** VM initial setup (install deps, clone repo, create config)
- **Part 4:** Systemd service setup with full `.service` file templates
- **Part 5:** Future update workflow
- **Part 6:** Useful diagnostic commands
- **File Reference table:** what is/isn't in git
