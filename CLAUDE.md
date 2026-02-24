# Telegram Stock Monitor Bot - Project Reference

## Overview
A two-process Telegram bot system for monitoring up to 5 stocks per user. Users configure stocks and alerts via Telegram; a background monitor checks prices every 15 minutes and sends notifications on alert state changes.

## Architecture

### Two-Process Design
| File | Role | Systemd Service |
|------|------|-----------------|
| `telegram_bot_multistock.py` | Interactive UI bot (ConversationHandlers) | `robo-config-bot.service` |
| `robo_monitor_multistock.py` | Background monitor loop | `robo-monitor-bot.service` |

Both processes share the same JSON file-based storage — no database.

### Deployment
- **VM**: Google Cloud e2-micro (free tier), SSH as `jason_tamkh@robostrategy-exit-reminder-bot`
- **Working dir**: `~/tg-bot-stock-monitoring/`
- **Source control**: GitHub repo `tg-bot-stock-monitoring`; VM pulls via `git pull`
- **Secrets**: stored in `config.json` on the VM (gitignored, never committed)

### Data Storage (JSON files)
```
config.json                          # Bot token and main config
user_configs/config_user_{id}.json   # Per-user stocks + alerts config
alert_states/state_user_{id}.json    # Per-user alert trigger states
price_history/history_{ticker}.json  # 30-day price history per ticker
```

## Key Functionality

### Alert Types (per stock)
| Type | Description |
|------|-------------|
| `premium` | (Price - NAV) / NAV × 100, compared with `>` or `<` operator |
| `price_1d` | 1-day price change %, compared with `>` or `<` |
| `price_7d` | 7-day price change %, compared with `>` or `<` |
| `volume` | Current volume vs 7-day avg (ratio), compared with `>` or `<` |

### Alert Logic
- **State-based**: notifications fire only on state *change* (triggered → cleared, or cleared → triggered)
- Alert state persisted in `alert_states/` so restarts don't re-fire old alerts
- Volume alerts are skipped during a 2-hour grace period after market open (avoids false low-volume alerts)

### Volume: Pro-Rata Aware
- During market hours, the 7-day average is scaled by `elapsed_trading_hours / total_trading_hours`
- Supports US (NYSE: 9:30–16:00 ET) and HK (HKEX: 9:30–16:00 HKT, minus 1h lunch)
- Market detection: ticker ending in `.HK` → HK market; otherwise → US market

### Heartbeat
- Each user sets a frequency (1–24h); monitor sends a summary message at that interval
- Heartbeat shows price, NAV, premium, 1D change, and each alert's current state

### Bot Commands / UI Flow
- `/start` → main menu (inline keyboard)
- `/stocks` → list all tracked stocks
- `/addstock` → add stock (ticker → NAV → done)
- `/help` → help text
- `/cancel` → cancel active conversation
- All stock/alert management via inline keyboard buttons

### AI Advisor Feature (`ask_ai` flow)
- Entry: "🤖 Ask AI Advice" button on main menu
- User selects stock → Buy or Sell signal
- Bot calls Claude Sonnet via Openrouter API (in `asyncio.to_thread` — non-blocking)
- AI analyses: 30-day local price history + yfinance news + benchmark (SPY/^HSI) + stock info
- AI returns either:
  - `immediate` → display recommendation message, no alert changes
  - `alerts` → display two-tier (watch/action) alert proposals, ask for confirmation
- On confirm: `replace_alerts()` atomically replaces all alerts; `clear_stock_alert_states()` purges stale states
- Module: `ai_advisor.py` — `AIAdvisor` class, all AI logic isolated here
- Configured via `config.json` → `openrouter.api_key`; bot works normally if key is absent

## Dependencies
```
python-telegram-bot   # Telegram bot framework (telegram_bot_multistock.py)
yfinance              # Stock price data
pytz                  # Timezone handling
requests              # Direct Telegram API calls (robo_monitor_multistock.py) + Openrouter AI calls (ai_advisor.py)
```

## Config File Format (`config.json`)
```json
{
  "telegram": {
    "bot_token": "YOUR_BOT_TOKEN"
  },
  "openrouter": {
    "api_key": "YOUR_OPENROUTER_API_KEY"
  }
}
```
The `openrouter` block is optional — if absent or placeholder, the bot starts normally but the "Ask AI Advice" button shows a "not configured" message.

## User Config Format (`user_configs/config_user_{id}.json`)
```json
{
  "user_id": 123456789,
  "heartbeat_frequency": 3,
  "stocks": [
    {
      "id": "stock_abc12345",
      "ticker": "BOT",
      "nav": 10.00,
      "alerts": [
        {
          "id": "alert_xyz98765",
          "type": "premium",
          "operator": ">",
          "threshold": 400,
          "description": "Premium above 400%",
          "enabled": true,
          "created_at": "2026-01-01T00:00:00"
        }
      ],
      "added_at": "2026-01-01T00:00:00"
    }
  ],
  "created_at": "2026-01-01T00:00:00"
}
```

## Monitor Loop Constants
```python
CHECK_INTERVAL = 900           # seconds (15 min) between monitoring cycles
VOLUME_GRACE_PERIOD_HOURS = 2  # skip volume checks for first 2h after market open
```

## Deployment

### Source Control
- Managed via **GitHub** (repo on github.com)
- `config.json`, data dirs, `__pycache__`, `.DS_Store` are all excluded via `.gitignore`
- `config.json.example` is committed as a template; actual `config.json` lives only on the VM

### Updating the Bot (normal flow)
```bash
ssh jason_tamkh@robostrategy-exit-reminder-bot
cd ~/tg-bot-stock-monitoring
git pull
sudo systemctl restart robo-config-bot.service
sudo systemctl restart robo-monitor-bot.service
```

### VM Commands
```bash
# Check service status
sudo systemctl status robo-config-bot.service
sudo systemctl status robo-monitor-bot.service

# View logs (live)
sudo journalctl -u robo-monitor-bot.service -f
sudo journalctl -u robo-config-bot.service -f

# View last 100 lines
sudo journalctl -u robo-monitor-bot.service -n 100
sudo journalctl -u robo-config-bot.service -n 100
```

> **Note:** Both `.service` files include `Environment="PYTHONUNBUFFERED=1"` — this is required for Python `print()` output to appear in `journalctl` in real time.

## Key Design Decisions
- **No database**: JSON files per user keep things simple and portable
- **Two separate processes**: bot and monitor run independently; crash in one doesn't affect the other
- **State-based alerts**: avoids alert spam — only notifies on transitions
- **Pro-rata volume**: prevents false alarms from comparing full-day average against early-session volume
- **Multi-user**: each user has isolated config and alert state files
