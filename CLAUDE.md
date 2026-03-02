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
| `volume` | Current volume vs 10-day avg (from yfinance `.info`), compared with `>` or `<` |

### Alert Logic
- **State-based**: notifications fire only on state *change* (triggered → cleared, or cleared → triggered)
- Alert state persisted in `alert_states/` so restarts don't re-fire old alerts
- Volume alerts are skipped on weekends (no trading) and during a 2-hour grace period after market open (avoids false low-volume alerts)

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
- `/ipoai` → IPO Listing Advisor (same entry as "🚀 IPO Listing Advisor" button)
- All stock/alert management via inline keyboard buttons

### AI Advisor Feature (`ask_ai` flow)
- Entry: "🤖 Ask AI Advice" button on main menu, or `/askai` command
- User selects stock → Buy or Sell signal
- Bot calls LLM via Openrouter API (in `asyncio.to_thread` — non-blocking)
- Model configured via `AI_MODEL` constant in `ai_advisor.py` (currently `deepseek/deepseek-v3.2`; swap freely)
- `max_tokens = 4000` — needed for verbose models (GPT-5 mini, DeepSeek); lower models may truncate at 1500
- AI analyses: **1-year daily OHLCV from yfinance** (52w range, 20/50/200-day SMA, price changes 1d–1y, volume stats, annualised volatility, monthly summary, recent 60-day OHLCV) + yfinance news (up to 15 headlines) + benchmark return (SPY/^HSI) + stock info + local premium history (if NAV applicable) + retail sentiment (StockTwits, Google Trends, Reddit)
- **News sources** (fail silently; Marketaux preferred over yfinance when key present):
  - Marketaux: up to 10 news articles with source + description; configured via `config.json` → `marketaux.api_key`
  - Finnhub news: last 7 days of company news; configured via `config.json` → `finnhub.api_key`
  - yfinance `.news`: fallback if Marketaux key absent
- **Fundamental metrics**: Finnhub `/stock/metric` — P/E, EPS growth, revenue growth, net margin, current ratio, beta; shown as a new prompt section
- **Retail sentiment sources** (all fail silently):
  - StockTwits: `api.stocktwits.com` — bullish/bearish counts + sample messages
  - Google Trends: via `pytrends` — search interest level + 4-week trend
  - Adanos Finance (`api.adanos.org`): Reddit, X/Twitter, Polymarket sentiment (buzz score, bullish/bearish %, trend); configured via `config.json` → `adanos.api_key`; replaces public Reddit scrape when key present
  - Reddit: public `.json` endpoint — fallback when Adanos key absent; skipped for HK tickers
- AI returns either:
  - `immediate` → display recommendation message, no alert changes
  - `alerts` → display two-tier (watch/action) alert proposals (2–6 alerts), ask for confirmation
- **Response JSON has 5 analysis sections**: `price_analysis`, `sector_analysis`, `news_analysis`, `sentiment_analysis`, `macro_analysis` — plus `summary` and optionally `alerts`
- On confirm/decline: **analysis message is preserved** in chat; confirmation result sent as a new message below
- On confirm: `replace_alerts()` atomically replaces all alerts; `clear_stock_alert_states()` purges stale states
- Module: `ai_advisor.py` — `AIAdvisor` class, all AI logic isolated here
- Configured via `config.json` → `openrouter.api_key`; bot works normally if key is absent
- **API note**: do NOT use `response_format: json_object` with Openrouter + Claude — it is OpenAI-specific and causes empty content responses; rely on prompt instruction instead
- **Truncation guard**: `parse_and_validate()` checks `finish_reason == "length"` and raises a clear error if response was cut off

### IPO Listing Advisor Feature (`ipo_advisor` flow)
- Entry: "🚀 IPO Listing Advisor" button on main menu, or `/ipoai` command
- **Standalone module**: `pre_ipo_advisor.py` — `PreIpoAdvisor` class; fully isolated from `ai_advisor.py` for easy future deletion
- User selects stock → Buy or Sell signal
- Bot calls LLM via Openrouter API (in `asyncio.to_thread` — non-blocking)
- Model configured via `PRE_IPO_AI_MODEL` constant in `pre_ipo_advisor.py` (currently `openai/gpt-5-mini`)
- `max_tokens = 8000` — higher than standard advisor (4000) because the DXYZ full OHLCV prompt (~240 trading days) is substantially larger
- **Primary data source**: DXYZ (Destiny Tech100) full post-listing OHLCV via `yf.Ticker("DXYZ").history(period="max")`
  - Listing date, open/close price, ATH (price, date, days-to-ATH), current drawdown
  - Price at Day 7 / 30 / 60 / 90 / 180 / 365 checkpoints (via `timedelta` forward-search)
  - Volume: listing day, 10-day post-listing avg, current 30d avg
  - Monthly summary + full daily OHLCV table
- **Secondary data**: target stock 1y OHLCV + news + sentiment (same pipeline as `ai_advisor.py`)
- **AI response schema** (different from standard advisor):
  - `dxyz_pattern_analysis` — DXYZ lifecycle phases with actual dates/prices
  - `lifecycle_stage` — where target stock sits on the DXYZ curve
  - `position_sizing` (SELL signal only) — `sell_pct_now` (0–100, clamped), `rationale`, `staged_approach`
  - `news_analysis`, `sentiment_analysis`, `macro_analysis`, `summary` — same as standard advisor
- **Telegram display**: two-message split if combined content exceeds 4096 chars
  - Message 1: Summary → DXYZ Pattern → Lifecycle Stage → Position Sizing (SELL)
  - Message 2: News → Sentiment → Macro → Proposed Alerts (if any)
- **Conversation states**: 50–52 (`PRE_IPO_SELECT_STOCK`, `PRE_IPO_SELECT_SIGNAL`, `PRE_IPO_CONFIRM_ALERTS`)
- **Callback data namespacing**: `ipo_advisor`, `ipostock_`, `iposignal_`, `ipoconfirm_` — no collision with standard advisor
- On confirm: same `replace_alerts()` + `clear_stock_alert_states()` flow as standard advisor
- **Note**: Only `robo-config-bot.service` needs restart after changes to this file — the monitor never calls it

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
  },
  "marketaux": {
    "api_key": "YOUR_MARKETAUX_API_KEY"
  },
  "finnhub": {
    "api_key": "YOUR_FINNHUB_API_KEY"
  },
  "adanos": {
    "api_key": "YOUR_ADANOS_API_KEY"
  }
}
```
The `openrouter` block is optional — if absent or placeholder, the bot starts normally but the "Ask AI Advice" button shows a "not configured" message. The `marketaux`, `finnhub`, and `adanos` blocks are also optional — each fails silently if absent; existing sources (yfinance news, Reddit scrape) are used as fallback.

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
