#!/usr/bin/env python3
"""
Stock Monitor Bot - Multi-Stock Version
Track up to 5 stocks, each with custom alerts
"""

import asyncio
import json
import os
import uuid
from datetime import datetime
import yfinance as yf
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler
)
from ai_advisor import AIAdvisor
from pre_ipo_advisor import PreIpoAdvisor

# Configuration
CONFIG_DIR = "user_configs"
ALERT_STATE_DIR = "alert_states"
PRICE_HISTORY_DIR = "price_history"

# Market session info (mirrors robo_monitor_multistock.py for extended hours support)
MARKET_INFO = {
    'US': {
        'timezone': 'America/New_York',
        'open_hour': 9,  'open_min': 30,
        'close_hour': 16, 'close_min': 0,
        'pre_market_start': (4, 0),    # 4:00 AM ET
        'after_hours_end':  (20, 0),   # 8:00 PM ET
    },
    'HK': {
        'timezone': 'Asia/Hong_Kong',
        'open_hour': 9,  'open_min': 30,
        'close_hour': 16, 'close_min': 0,
        'pre_market_start': None,      # HKEX has no extended hours
        'after_hours_end':  None,
    },
}


def get_market_key(ticker: str) -> str:
    if ticker.upper().endswith('.HK'):
        return 'HK'
    return 'US'


def get_market_session(ticker: str) -> str:
    """Return 'pre_market', 'regular', 'after_hours', or 'closed'."""
    info = MARKET_INFO[get_market_key(ticker)]
    tz = pytz.timezone(info['timezone'])
    now = datetime.now(tz)

    if now.weekday() >= 5:
        return 'closed'

    market_open  = now.replace(hour=info['open_hour'],  minute=info['open_min'],  second=0, microsecond=0)
    market_close = now.replace(hour=info['close_hour'], minute=info['close_min'], second=0, microsecond=0)

    if market_open <= now < market_close:
        return 'regular'

    if info.get('pre_market_start'):
        pre_h, pre_m = info['pre_market_start']
        pre_start = now.replace(hour=pre_h, minute=pre_m, second=0, microsecond=0)
        if pre_start <= now < market_open:
            return 'pre_market'

    if info.get('after_hours_end'):
        ah_h, ah_m = info['after_hours_end']
        after_end = now.replace(hour=ah_h, minute=ah_m, second=0, microsecond=0)
        if market_close <= now < after_end:
            return 'after_hours'

    return 'closed'


def is_market_weekend(ticker: str) -> bool:
    """Return True if the relevant exchange is currently on a weekend."""
    tz = pytz.timezone(MARKET_INFO[get_market_key(ticker)]['timezone'])
    return datetime.now(tz).weekday() >= 5


def get_extended_hours_price(ticker: str, session: str):
    """Fetch the most recent extended-hours price via intraday history (prepost=True).
    Covers standard pre-market (4–9:30 AM ET) and after-hours (4–8 PM ET).
    Note: overnight trading (8 PM–4 AM ET, Blue Ocean ATS) is not available via
    Yahoo Finance's public data feed; the last after-hours close is shown instead.
    Label is 'pre-market' for pre_market session, 'after-hours' for everything else.
    Returns (price: float, label: str) or (None, None) if unavailable."""
    try:
        mkt = MARKET_INFO[get_market_key(ticker)]
        # Market has no extended hours (e.g. HKEX)
        if not mkt.get('pre_market_start') and not mkt.get('after_hours_end'):
            return None, None

        label = 'pre-market' if session == 'pre_market' else 'after-hours'

        hist = yf.Ticker(ticker).history(period='5d', interval='1m', prepost=True)
        if not hist.empty:
            last_price = float(hist['Close'].iloc[-1])
            if last_price > 0:
                tz = pytz.timezone(mkt['timezone'])
                last_ts = hist.index[-1]
                if last_ts.tzinfo is None:
                    last_ts = last_ts.tz_localize('UTC')
                last_local = last_ts.astimezone(tz)
                market_open  = last_local.replace(hour=mkt['open_hour'],  minute=mkt['open_min'],  second=0, microsecond=0)
                market_close = last_local.replace(hour=mkt['close_hour'], minute=mkt['close_min'], second=0, microsecond=0)
                if last_local >= market_close or last_local < market_open:
                    return last_price, label

        return None, None
    except Exception:
        return None, None

# Conversation states
ADD_STOCK_TICKER, ADD_STOCK_NAV = range(2)
ALERT_TYPE, ALERT_OPERATOR, ALERT_THRESHOLD, ALERT_CONFIRM = range(10, 14)
DELETE_ALERT_ID = 20
UPDATE_NAV, UPDATE_HEARTBEAT = 30, 31
AI_SELECT_STOCK, AI_SELECT_SIGNAL, AI_CONFIRM_ALERTS = range(40, 43)
PRE_IPO_SELECT_STOCK, PRE_IPO_SELECT_SIGNAL, PRE_IPO_CONFIRM_ALERTS = range(50, 53)


class UserConfigManager:
    """Manages per-user configuration files"""

    def __init__(self, config_dir):
        self.config_dir = config_dir
        os.makedirs(config_dir, exist_ok=True)

    def _get_config_path(self, user_id):
        return os.path.join(self.config_dir, f"config_user_{user_id}.json")

    def load(self, user_id):
        config_path = self._get_config_path(user_id)
        try:
            with open(config_path, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return self._default_config(user_id)

    def save(self, user_id, config):
        config_path = self._get_config_path(user_id)
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=2)

    def _default_config(self, user_id):
        return {
            "user_id": user_id,
            "heartbeat_frequency": 3,
            "stocks": [],
            "created_at": datetime.now().isoformat()
        }

    def add_stock(self, user_id, ticker, nav):
        """Add a new stock to track"""
        config = self.load(user_id)

        if len(config.get('stocks', [])) >= 5:
            return None, "Maximum 5 stocks reached"

        # Check if ticker already exists
        for stock in config.get('stocks', []):
            if stock['ticker'].upper() == ticker.upper():
                return None, f"{ticker} already being tracked"

        stock_id = f"stock_{uuid.uuid4().hex[:8]}"

        stock = {
            "id": stock_id,
            "ticker": ticker.upper(),
            "nav": nav,
            "alerts": [],
            "added_at": datetime.now().isoformat()
        }

        if 'stocks' not in config:
            config['stocks'] = []

        config['stocks'].append(stock)
        self.save(user_id, config)
        return stock_id, None

    def remove_stock(self, user_id, stock_id):
        """Remove a stock and all its alerts"""
        config = self.load(user_id)

        original_count = len(config.get('stocks', []))
        config['stocks'] = [s for s in config.get('stocks', []) if s['id'] != stock_id]

        if len(config['stocks']) < original_count:
            self.save(user_id, config)
            return True
        return False

    def get_stock(self, user_id, stock_id):
        """Get specific stock"""
        config = self.load(user_id)
        for stock in config.get('stocks', []):
            if stock['id'] == stock_id:
                return stock
        return None

    def update_stock_nav(self, user_id, stock_id, nav):
        """Update NAV for specific stock"""
        config = self.load(user_id)
        for stock in config.get('stocks', []):
            if stock['id'] == stock_id:
                stock['nav'] = nav
                self.save(user_id, config)
                return True
        return False

    def add_alert_to_stock(self, user_id, stock_id, alert_type, operator, threshold):
        """Add alert to specific stock"""
        config = self.load(user_id)

        for stock in config.get('stocks', []):
            if stock['id'] == stock_id:
                alert_id = f"alert_{uuid.uuid4().hex[:8]}"

                alert = {
                    "id": alert_id,
                    "type": alert_type,
                    "operator": operator,
                    "threshold": threshold,
                    "description": self._generate_description(alert_type, operator, threshold),
                    "enabled": True,
                    "created_at": datetime.now().isoformat()
                }

                if 'alerts' not in stock:
                    stock['alerts'] = []

                stock['alerts'].append(alert)
                self.save(user_id, config)
                return alert_id

        return None

    def delete_alert_from_stock(self, user_id, stock_id, alert_id):
        """Delete alert from specific stock"""
        config = self.load(user_id)

        for stock in config.get('stocks', []):
            if stock['id'] == stock_id:
                original_count = len(stock.get('alerts', []))
                stock['alerts'] = [a for a in stock.get('alerts', []) if a['id'] != alert_id]

                if len(stock['alerts']) < original_count:
                    self.save(user_id, config)
                    return True

        return False

    def replace_alerts(self, user_id, stock_id, new_alerts: list) -> bool:
        """Atomically replace ALL alerts for a stock with a new list."""
        config = self.load(user_id)
        for stock in config.get('stocks', []):
            if stock['id'] == stock_id:
                stock['alerts'] = new_alerts
                self.save(user_id, config)
                return True
        return False

    def update_heartbeat_frequency(self, user_id, hours):
        config = self.load(user_id)
        config['heartbeat_frequency'] = hours
        self.save(user_id, config)

    def _generate_description(self, alert_type, operator, threshold):
        op_text = {'>': 'above', '<': 'below'}

        if alert_type == 'premium':
            return f"Premium {op_text.get(operator, operator)} {threshold}%"
        elif alert_type == 'price_1d':
            if operator == '<' and threshold <= 0:
                return f"Drops >{abs(threshold)}% in 1 day"
            elif operator == '>' and threshold >= 0:
                return f"Rises >{threshold}% in 1 day"
            else:
                # Unusual combination — show exact condition so it isn't misleading
                return f"1D change {operator} {threshold}%"
        elif alert_type == 'price_7d':
            if operator == '<' and threshold <= 0:
                return f"Drops >{abs(threshold)}% in 7 days"
            elif operator == '>' and threshold >= 0:
                return f"Rises >{threshold}% in 7 days"
            else:
                return f"7D change {operator} {threshold}%"
        elif alert_type == 'volume':
            return f"Volume {op_text.get(operator, operator)} {threshold}x avg"

        return f"{alert_type} {operator} {threshold}"


class AlertStateManager:
    """Manages alert states"""

    def __init__(self, state_dir):
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)

    def _get_path(self, user_id):
        return os.path.join(self.state_dir, f"state_user_{user_id}.json")

    def load(self, user_id):
        try:
            with open(self._get_path(user_id), 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def get_state(self, user_id, alert_key):
        """Get alert state (True=triggered, False=not)"""
        states = self.load(user_id)
        return states.get(alert_key, False)

    def clear_stock_alert_states(self, user_id, stock_id):
        """Remove all alert state entries for a given stock (call after replacing alerts).

        Old alert IDs become orphaned when alerts are replaced, so we purge their
        state keys to prevent stale triggered/cleared states bleeding into new alerts.
        """
        states = self.load(user_id)
        keys_to_remove = [k for k in states if k.startswith(f"{stock_id}_")]
        if not keys_to_remove:
            return
        for k in keys_to_remove:
            del states[k]
        config_path = self._get_path(user_id)
        with open(config_path, 'w') as f:
            json.dump(states, f, indent=2)


class PriceHistory:
    """Manages price history per ticker"""

    def __init__(self, history_dir):
        self.history_dir = history_dir
        os.makedirs(history_dir, exist_ok=True)

    def _get_path(self, ticker):
        return os.path.join(self.history_dir, f"history_{ticker}.json")

    def load(self, ticker):
        try:
            with open(self._get_path(ticker), 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return {"ticker": ticker, "daily_prices": [], "last_update": None}

    def get_price_change_1d(self, ticker, current_price=None, session=None):
        """Compute 1D % change.
        If current_price is provided (extended hours), compare against the appropriate
        last regular close: [-2] for after_hours (today's close already stored),
        [-1] for pre_market (today hasn't started)."""
        data = self.load(ticker)
        prices = data['daily_prices']
        if current_price is not None:
            if session == 'after_hours':
                if len(prices) < 2:
                    return None
                ref = prices[-2]['price']
            else:  # pre_market
                if len(prices) < 1:
                    return None
                ref = prices[-1]['price']
            return ((current_price - ref) / ref) * 100
        else:
            if len(prices) < 2:
                return None
            today = prices[-1]['price']
            yesterday = prices[-2]['price']
            return ((today - yesterday) / yesterday) * 100


config_manager = UserConfigManager(CONFIG_DIR)
alert_state_manager = AlertStateManager(ALERT_STATE_DIR)
price_history = PriceHistory(PRICE_HISTORY_DIR)

# Populated at startup from config.json → openrouter.api_key
# Set to None if key is missing/placeholder — UI shows "not configured" gracefully
ai_advisor = None

# Pre-IPO Listing Advisor — DXYZ-anchored lifecycle analysis
# Temporary feature; see pre_ipo_advisor.py (standalone, easy to remove)
pre_ipo_advisor = None


def get_user_id(update: Update):
    if update.effective_user:
        return update.effective_user.id
    elif update.effective_chat:
        return update.effective_chat.id
    return None


def get_stock_data(ticker):
    """Fetch current stock price"""
    try:
        stock = yf.Ticker(ticker)
        data = stock.history(period='2d')
        if data.empty:
            return None, None
        latest = data.iloc[-1]
        return float(latest['Close']), int(latest['Volume'])
    except:
        return None, None


async def setup_commands(application):
    commands = [
        BotCommand("start",     "Main menu"),
        BotCommand("stocks",    "View all stocks"),
        BotCommand("addstock",  "Add new stock"),
        BotCommand("status",    "Check all stock prices now"),
        BotCommand("askai",     "Ask AI for stock advice"),
        BotCommand("ipoai",     "IPO Listing Advisor (DXYZ comparison)"),
        BotCommand("heartbeat", "Set heartbeat frequency"),
        BotCommand("help",      "Show help"),
    ]
    await application.bot.set_my_commands(commands)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main menu"""
    user_id = get_user_id(update)
    config = config_manager.load(user_id)

    stock_count = len(config.get('stocks', []))
    heartbeat_freq = config.get('heartbeat_frequency', 3)

    keyboard = [
        [InlineKeyboardButton(f"📊 My Stocks ({stock_count}/5)", callback_data='list_stocks')],
        [InlineKeyboardButton("➕ Add Stock", callback_data='add_stock')],
        [InlineKeyboardButton("🔍 Check Status Now", callback_data='check_status_now')],
        [InlineKeyboardButton("🤖 Ask AI Advice", callback_data='ask_ai')],
        [InlineKeyboardButton("🚀 IPO Listing Advisor", callback_data='ipo_advisor')],
        [InlineKeyboardButton("⏰ Heartbeat Frequency", callback_data='update_heartbeat')],
        [InlineKeyboardButton("ℹ️ Help", callback_data='help')]
    ]

    if stock_count >= 5:
        keyboard[1] = [InlineKeyboardButton("🚫 Max Stocks (5/5)", callback_data='max_stocks')]

    reply_markup = InlineKeyboardMarkup(keyboard)

    message = (
        "📈 *Stock Monitor Bot*\n\n"
        f"Tracking: {stock_count}/5 stocks\n"
        f"Heartbeat: Every {heartbeat_freq}h\n\n"
        "Select an option:"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(
            message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )


async def check_status_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check status of all stocks immediately — works from both callback and /status command."""
    query = update.callback_query
    if query:
        await query.answer()

    user_id = get_user_id(update)
    config = config_manager.load(user_id)
    stocks = config.get('stocks', [])

    async def send(text, reply_markup=None):
        """Send or edit a message depending on whether we came from a callback or a command."""
        if query:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
        else:
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')

    if not stocks:
        message = (
            "📊 *No Stocks Yet*\n\n"
            "You're not tracking any stocks.\n"
            "Add a stock to get started!"
        )
        keyboard = [
            [InlineKeyboardButton("➕ Add Stock", callback_data='add_stock')],
            [InlineKeyboardButton("« Menu", callback_data='menu')]
        ]
        await send(message, InlineKeyboardMarkup(keyboard))
        return

    # Show processing message (callback only — avoids a dangling message on command path)
    if query:
        await query.edit_message_text("🔍 Fetching live prices...", parse_mode='Markdown')

    hk_tz = pytz.timezone('Asia/Hong_Kong')
    now_hk = datetime.now(hk_tz).strftime('%Y-%m-%d %H:%M:%S HKT')

    message = (
        f"🔍 *Status Check*\n\n"
        f"🕐 {now_hk}\n"
        f"📊 Tracking {len(stocks)} stock(s)\n\n"
    )

    for stock in stocks:
        ticker = stock['ticker']
        nav = stock.get('nav', 0)
        stock_id = stock['id']
        alerts = stock.get('alerts', [])

        # Fetch live price (regular session close)
        price, volume = get_stock_data(ticker)

        # Check for extended hours price if outside regular trading hours
        session = get_market_session(ticker)
        display_price = price
        price_label = None
        if price and (session in ('pre_market', 'after_hours') or (session == 'closed' and not is_market_weekend(ticker))):
            ext_price, price_label = get_extended_hours_price(ticker, session)
            if ext_price:
                display_price = ext_price

        message += f"*{ticker}*\n"

        if display_price:
            nav_f = float(nav) if nav else 0
            premium = ((display_price - nav_f) / nav_f) * 100 if nav_f else 0
            label_str = f" _({price_label})_" if price_label else ""
            message += f"• Price: ${display_price:.2f}{label_str} | NAV: ${nav:.2f}\n"

            if nav_f > 0:
                message += f"• Premium: {premium:.1f}%\n"

            # Get 1-day change (uses extended hours price when applicable)
            ext_arg = display_price if price_label else None
            eff_session = 'after_hours' if price_label == 'after-hours' else ('pre_market' if price_label == 'pre-market' else session)
            change_1d = price_history.get_price_change_1d(ticker, current_price=ext_arg, session=eff_session)
            if change_1d:
                sign = '+' if change_1d > 0 else ''
                message += f"• 1D: {sign}{change_1d:.1f}%\n"

            # Show alert statuses
            if alerts:
                for alert in alerts:
                    alert_key = f"{stock_id}_{alert['id']}"
                    triggered = alert_state_manager.get_state(user_id, alert_key)
                    icon = "⚠️" if triggered else "✅"
                    desc = alert['description']
                    if len(desc) > 25:
                        desc = desc[:22] + "..."
                    state = "ACTIVE" if triggered else "Normal"
                    message += f"{icon} {desc}: {state}\n"

        else:
            message += f"• Price: Unable to fetch\n"

        message += "\n"

    message += "🔄 Live status retrieved"

    keyboard = [[InlineKeyboardButton("« Menu", callback_data='menu')]]
    await send(message, InlineKeyboardMarkup(keyboard))


async def list_stocks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all tracked stocks"""
    query = update.callback_query
    if query:
        await query.answer()

    user_id = get_user_id(update)
    config = config_manager.load(user_id)
    stocks = config.get('stocks', [])

    if not stocks:
        message = (
            "📊 *No Stocks Yet*\n\n"
            "You're not tracking any stocks.\n"
            "Add a stock to get started!"
        )
        keyboard = [
            [InlineKeyboardButton("➕ Add Stock", callback_data='add_stock')],
            [InlineKeyboardButton("« Menu", callback_data='menu')]
        ]
    else:
        message = f"📊 *Your Stocks ({len(stocks)}/5)*\n\n"

        keyboard = []
        for i, stock in enumerate(stocks, 1):
            ticker = stock['ticker']
            alert_count = len(stock.get('alerts', []))
            nav = stock.get('nav', 0)

            button_text = f"{i}. {ticker} (${nav:.2f}) - {alert_count} alerts"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=stock['id'])])

        if len(stocks) < 5:
            keyboard.append([InlineKeyboardButton("➕ Add Stock", callback_data='add_stock')])

        keyboard.append([InlineKeyboardButton("« Menu", callback_data='menu')])

    reply_markup = InlineKeyboardMarkup(keyboard)

    if query:
        await query.edit_message_text(
            message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )


async def stock_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show stock detail menu"""
    query = update.callback_query
    await query.answer()

    stock_id = query.data  # ID is already in the format stock_xxxxxxxx
    user_id = get_user_id(update)

    stock = config_manager.get_stock(user_id, stock_id)

    if not stock:
        await query.edit_message_text("❌ Stock not found")
        return

    ticker = stock['ticker']
    nav = stock.get('nav', 0)
    alert_count = len(stock.get('alerts', []))

    # Store current stock in context
    context.user_data['current_stock_id'] = stock_id

    message = (
        f"📈 *{ticker}*\n\n"
        f"💰 NAV: ${nav:.2f}\n"
        f"🔔 Alerts: {alert_count}\n\n"
        "Select an action:"
    )

    keyboard = [
        [InlineKeyboardButton("🔔 Manage Alerts", callback_data=f'alerts_{stock_id}')],
        [InlineKeyboardButton("💰 Update NAV", callback_data=f'nav_{stock_id}')],
        [InlineKeyboardButton("🗑️ Remove Stock", callback_data=f'remove_{stock_id}')],
        [InlineKeyboardButton("« Back to Stocks", callback_data='list_stocks')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        message,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


async def manage_stock_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manage alerts for specific stock"""
    query = update.callback_query
    await query.answer()

    stock_id = query.data.replace('alerts_', '')
    user_id = get_user_id(update)

    stock = config_manager.get_stock(user_id, stock_id)

    if not stock:
        await query.edit_message_text("❌ Stock not found")
        return

    ticker = stock['ticker']
    alerts = stock.get('alerts', [])

    context.user_data['current_stock_id'] = stock_id

    message = f"🔔 *Alerts for {ticker}*\n\n"

    if alerts:
        for i, alert in enumerate(alerts, 1):
            message += f"{i}. {alert['description']}\n   `{alert['id']}`\n\n"
    else:
        message += "No alerts configured yet.\n\n"

    keyboard = [
        [InlineKeyboardButton("➕ Add Alert", callback_data=f'addalert_{stock_id}')],
        [InlineKeyboardButton("🗑️ Delete Alert", callback_data=f'delalert_{stock_id}')],
        [InlineKeyboardButton(f"« Back to {ticker}", callback_data=stock_id)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        message,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


# ========== ADD STOCK HANDLERS ==========

async def add_stock_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start add stock conversation"""
    query = update.callback_query
    if query:
        await query.answer()

    user_id = get_user_id(update)
    config = config_manager.load(user_id)

    if len(config.get('stocks', [])) >= 5:
        message = "🚫 Maximum 5 stocks reached. Remove a stock first."
        keyboard = [[InlineKeyboardButton("« Menu", callback_data='menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if query:
            await query.edit_message_text(message, reply_markup=reply_markup)
        return ConversationHandler.END

    message = (
        "➕ *Add New Stock*\n\n"
        "*Step 1: Enter Ticker*\n\n"
        "Examples: BOT, DXYZ, AAPL, 0700.HK\n\n"
        "💬 Type ticker or /cancel"
    )

    if query:
        await query.message.delete()
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=message,
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(message, parse_mode='Markdown')

    return ADD_STOCK_TICKER


async def add_stock_ticker_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle ticker entry"""
    ticker = update.message.text.strip().upper()

    if not ticker or len(ticker) > 15:
        await update.message.reply_text(
            "❌ Invalid ticker. Enter 1-15 characters or /cancel"
        )
        return ADD_STOCK_TICKER

    context.user_data['new_stock_ticker'] = ticker

    message = (
        f"➕ *Add New Stock*\n\n"
        f"*Ticker:* {ticker}\n\n"
        f"*Step 2: Enter NAV*\n\n"
        f"Enter NAV (or 0 if not applicable)\n"
        f"Example: 10.50\n\n"
        f"💬 Type NAV or /cancel"
    )

    await update.message.reply_text(message, parse_mode='Markdown')

    return ADD_STOCK_NAV


async def add_stock_nav_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle NAV entry and create stock"""
    try:
        nav = float(update.message.text.strip())

        if nav < 0:
            await update.message.reply_text("❌ NAV must be >= 0. Try again or /cancel")
            return ADD_STOCK_NAV

        user_id = get_user_id(update)
        ticker = context.user_data.get('new_stock_ticker')

        stock_id, error = config_manager.add_stock(user_id, ticker, nav)

        if error:
            message = f"❌ {error}"
            keyboard = [[InlineKeyboardButton("« Menu", callback_data='menu')]]
        else:
            message = (
                f"✅ *Stock Added!*\n\n"
                f"Ticker: {ticker}\n"
                f"NAV: ${nav:.2f}\n\n"
                f"Now add alerts for {ticker}!"
            )
            keyboard = [
                [InlineKeyboardButton(f"🔔 Add Alert to {ticker}", callback_data=f'addalert_{stock_id}')],
                [InlineKeyboardButton("📊 My Stocks", callback_data='list_stocks')],
                [InlineKeyboardButton("« Menu", callback_data='menu')]
            ]

        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

        context.user_data.clear()
        return ConversationHandler.END

    except ValueError:
        await update.message.reply_text(
            "❌ Invalid number. Enter NAV (e.g., 10.50) or /cancel"
        )
        return ADD_STOCK_NAV


# ========== ADD ALERT HANDLERS ==========

async def add_alert_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start add alert conversation"""
    query = update.callback_query
    await query.answer()

    stock_id = query.data.replace('addalert_', '')
    user_id = get_user_id(update)

    stock = config_manager.get_stock(user_id, stock_id)

    if not stock:
        await query.edit_message_text("❌ Stock not found")
        return ConversationHandler.END

    context.user_data['current_stock_id'] = stock_id
    ticker = stock['ticker']

    keyboard = [
        [InlineKeyboardButton("📈 Premium to NAV", callback_data='alerttype_premium')],
        [InlineKeyboardButton("📊 1-Day Change", callback_data='alerttype_price_1d')],
        [InlineKeyboardButton("📆 7-Day Change", callback_data='alerttype_price_7d')],
        [InlineKeyboardButton("📊 Volume", callback_data='alerttype_volume')],
        [InlineKeyboardButton("« Cancel", callback_data=f'alerts_{stock_id}')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    message = (
        f"➕ *Add Alert to {ticker}*\n\n"
        "*Step 1: Choose Alert Type*"
    )

    await query.edit_message_text(
        message,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

    return ALERT_TYPE


async def add_alert_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle alert type selection"""
    query = update.callback_query
    await query.answer()

    alert_type = query.data.replace('alerttype_', '')
    context.user_data['alert_type'] = alert_type

    stock_id = context.user_data.get('current_stock_id')
    user_id = get_user_id(update)
    stock = config_manager.get_stock(user_id, stock_id)
    ticker = stock['ticker'] if stock else "Stock"

    type_names = {
        'premium': 'Premium',
        'price_1d': '1-Day Change',
        'price_7d': '7-Day Change',
        'volume': 'Volume'
    }

    keyboard = [
        [InlineKeyboardButton("> Greater than", callback_data='operator_>')],
        [InlineKeyboardButton("< Less than", callback_data='operator_<')],
        [InlineKeyboardButton("« Cancel", callback_data=f'alerts_{stock_id}')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    message = (
        f"➕ *Add Alert to {ticker}*\n\n"
        f"*Type:* {type_names.get(alert_type)}\n\n"
        f"*Step 2: Choose Condition*"
    )

    await query.edit_message_text(
        message,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

    return ALERT_OPERATOR


async def add_alert_operator_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle operator selection"""
    query = update.callback_query
    await query.answer()

    operator = query.data.replace('operator_', '')
    context.user_data['alert_operator'] = operator

    alert_type = context.user_data.get('alert_type')

    stock_id = context.user_data.get('current_stock_id')
    user_id = get_user_id(update)
    stock = config_manager.get_stock(user_id, stock_id)
    ticker = stock['ticker'] if stock else "Stock"

    examples = {
        'premium': '400 (for 400% premium)',
        'price_1d': '-10 (drop 10%) or 15 (rise 15%)',
        'price_7d': '-20 (drop 20%) or 25 (rise 25%)',
        'volume': '3 (for 3x average volume)'
    }

    message = (
        f"➕ *Add Alert to {ticker}*\n\n"
        f"*Type:* {alert_type.replace('_', '-')}\n"
        f"*Condition:* {operator}\n\n"
        f"*Step 3: Enter Threshold*\n\n"
        f"Example: {examples.get(alert_type)}\n\n"
        f"💬 Type number or /cancel"
    )

    await query.message.delete()
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=message,
        parse_mode='Markdown'
    )

    return ALERT_THRESHOLD


async def add_alert_threshold_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle threshold entry"""
    try:
        threshold = float(update.message.text.strip())
        context.user_data['alert_threshold'] = threshold

        alert_type = context.user_data.get('alert_type')
        operator = context.user_data.get('alert_operator')

        stock_id = context.user_data.get('current_stock_id')
        user_id = get_user_id(update)
        stock = config_manager.get_stock(user_id, stock_id)
        ticker = stock['ticker'] if stock else "Stock"

        description = config_manager._generate_description(alert_type, operator, threshold)

        keyboard = [
            [
                InlineKeyboardButton("✅ Confirm", callback_data='confirm_alert_yes'),
                InlineKeyboardButton("❌ Cancel", callback_data='confirm_alert_no')
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        message = (
            f"➕ *Confirm Alert for {ticker}*\n\n"
            f"*Description:*\n{description}\n\n"
            f"*Details:*\n"
            f"• Type: {alert_type.replace('_', '-')}\n"
            f"• Condition: {operator} {threshold}\n\n"
            f"Add this alert?"
        )

        await update.message.reply_text(
            message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

        return ALERT_CONFIRM

    except ValueError:
        await update.message.reply_text(
            "❌ Invalid number. Try again or /cancel"
        )
        return ALERT_THRESHOLD


async def add_alert_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm and save the alert"""
    query = update.callback_query
    await query.answer()

    user_id = get_user_id(update)
    stock_id = context.user_data.get('current_stock_id')

    stock = config_manager.get_stock(user_id, stock_id)
    ticker = stock['ticker'] if stock else "Stock"

    if query.data == 'confirm_alert_yes':
        alert_type = context.user_data.get('alert_type')
        operator = context.user_data.get('alert_operator')
        threshold = context.user_data.get('alert_threshold')

        alert_id = config_manager.add_alert_to_stock(user_id, stock_id, alert_type, operator, threshold)

        message = (
            f"✅ *Alert Added to {ticker}!*\n\n"
            f"Alert ID: `{alert_id}`\n\n"
            f"You'll be notified when state changes."
        )

        keyboard = [
            [InlineKeyboardButton(f"🔔 View {ticker} Alerts", callback_data=f'alerts_{stock_id}')],
            [InlineKeyboardButton("« Menu", callback_data='menu')]
        ]
    else:
        message = "❌ Alert cancelled"
        keyboard = [[InlineKeyboardButton("« Menu", callback_data='menu')]]

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        message,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

    context.user_data.clear()
    return ConversationHandler.END


# ========== DELETE ALERT HANDLERS ==========

async def delete_alert_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start delete alert conversation"""
    query = update.callback_query
    await query.answer()

    stock_id = query.data.replace('delalert_', '')
    user_id = get_user_id(update)

    stock = config_manager.get_stock(user_id, stock_id)

    if not stock:
        await query.edit_message_text("❌ Stock not found")
        return ConversationHandler.END

    alerts = stock.get('alerts', [])
    ticker = stock['ticker']

    if not alerts:
        message = f"❌ No alerts for {ticker}"
        keyboard = [[InlineKeyboardButton("« Back", callback_data=f'alerts_{stock_id}')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(message, reply_markup=reply_markup)
        return ConversationHandler.END

    context.user_data['current_stock_id'] = stock_id

    message = f"🗑️ *Delete Alert from {ticker}*\n\n"

    for i, alert in enumerate(alerts, 1):
        message += f"{i}. `{alert['id']}`\n   {alert['description']}\n\n"

    message += "💬 Tap ID to copy, send it, or /cancel"

    await query.message.delete()
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=message,
        parse_mode='Markdown'
    )

    return DELETE_ALERT_ID


async def delete_alert_id_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle alert ID entry for deletion"""
    user_id = get_user_id(update)
    stock_id = context.user_data.get('current_stock_id')
    alert_id = update.message.text.strip()

    stock = config_manager.get_stock(user_id, stock_id)
    ticker = stock['ticker'] if stock else "Stock"

    if config_manager.delete_alert_from_stock(user_id, stock_id, alert_id):
        message = f"✅ *Alert Deleted from {ticker}!*\n\nAlert `{alert_id}` removed."
    else:
        message = f"❌ *Alert Not Found*\n\nNo alert `{alert_id}` in {ticker}."

    keyboard = [
        [InlineKeyboardButton(f"🔔 {ticker} Alerts", callback_data=f'alerts_{stock_id}')],
        [InlineKeyboardButton("« Menu", callback_data='menu')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        message,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

    context.user_data.clear()
    return ConversationHandler.END


# ========== UPDATE NAV HANDLER ==========

async def update_nav_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start update NAV conversation"""
    query = update.callback_query
    await query.answer()

    stock_id = query.data.replace('nav_', '')
    user_id = get_user_id(update)

    stock = config_manager.get_stock(user_id, stock_id)

    if not stock:
        await query.edit_message_text("❌ Stock not found")
        return ConversationHandler.END

    ticker = stock['ticker']
    current_nav = stock.get('nav', 0)

    context.user_data['current_stock_id'] = stock_id

    message = (
        f"💰 *Update NAV for {ticker}*\n\n"
        f"Current: ${current_nav:.2f}\n\n"
        f"Type new NAV:\n"
        f"Example: 10.50\n\n"
        f"💬 Type number or /cancel"
    )

    await query.message.delete()
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=message,
        parse_mode='Markdown'
    )

    return UPDATE_NAV


async def update_nav_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle NAV entry"""
    try:
        nav = float(update.message.text.strip())

        if nav < 0:
            await update.message.reply_text("❌ NAV must be >= 0 or /cancel")
            return UPDATE_NAV

        user_id = get_user_id(update)
        stock_id = context.user_data.get('current_stock_id')

        stock = config_manager.get_stock(user_id, stock_id)
        ticker = stock['ticker'] if stock else "Stock"

        config_manager.update_stock_nav(user_id, stock_id, nav)

        message = f"✅ *NAV Updated for {ticker}!*\n\nNew NAV: ${nav:.2f}"

        keyboard = [
            [InlineKeyboardButton(f"« Back to {ticker}", callback_data=stock_id)],
            [InlineKeyboardButton("« Menu", callback_data='menu')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

        context.user_data.clear()
        return ConversationHandler.END

    except ValueError:
        await update.message.reply_text("❌ Invalid number or /cancel")
        return UPDATE_NAV


# ========== REMOVE STOCK HANDLER ==========

async def remove_stock_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm stock removal"""
    query = update.callback_query
    await query.answer()

    stock_id = query.data.replace('remove_', '')
    user_id = get_user_id(update)

    stock = config_manager.get_stock(user_id, stock_id)

    if not stock:
        await query.edit_message_text("❌ Stock not found")
        return

    ticker = stock['ticker']
    alert_count = len(stock.get('alerts', []))

    message = (
        f"🗑️ *Remove {ticker}?*\n\n"
        f"This will delete:\n"
        f"• {ticker} from your tracking\n"
        f"• All {alert_count} alerts for {ticker}\n\n"
        f"*This cannot be undone!*"
    )

    keyboard = [
        [InlineKeyboardButton("✅ Yes, Remove", callback_data=f'confirmremove_{stock_id}')],
        [InlineKeyboardButton("❌ Cancel", callback_data=stock_id)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        message,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


async def remove_stock_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Actually remove the stock"""
    query = update.callback_query
    await query.answer()

    stock_id = query.data.replace('confirmremove_', '')
    user_id = get_user_id(update)

    stock = config_manager.get_stock(user_id, stock_id)
    ticker = stock['ticker'] if stock else "Stock"

    if config_manager.remove_stock(user_id, stock_id):
        message = f"✅ *{ticker} Removed!*\n\nAll alerts deleted."
    else:
        message = f"❌ Failed to remove {ticker}"

    keyboard = [
        [InlineKeyboardButton("📊 My Stocks", callback_data='list_stocks')],
        [InlineKeyboardButton("« Menu", callback_data='menu')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        message,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


# ========== HEARTBEAT HANDLER ==========

async def update_heartbeat_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Update heartbeat frequency — works from both callback and /heartbeat command."""
    query = update.callback_query
    if query:
        await query.answer()

    user_id = get_user_id(update)
    config = config_manager.load(user_id)
    current_freq = config.get('heartbeat_frequency', 3)

    message = (
        f"⏰ *Heartbeat Frequency*\n\n"
        f"Current: Every {current_freq}h\n\n"
        f"Range: 1-24 hours\n\n"
        f"Examples: 1, 3, 6, 12, 24\n\n"
        f"💬 Type number or /cancel"
    )

    if query:
        await query.message.delete()
        chat_id = query.message.chat_id
    else:
        chat_id = update.message.chat_id

    await context.bot.send_message(
        chat_id=chat_id,
        text=message,
        parse_mode='Markdown'
    )

    return UPDATE_HEARTBEAT


async def update_heartbeat_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle heartbeat entry"""
    try:
        hours = int(update.message.text.strip())

        if hours < 1 or hours > 24:
            await update.message.reply_text("❌ Range: 1-24 or /cancel")
            return UPDATE_HEARTBEAT

        user_id = get_user_id(update)
        config_manager.update_heartbeat_frequency(user_id, hours)

        message = f"✅ *Heartbeat Updated!*\n\nFrequency: Every {hours}h"

        keyboard = [[InlineKeyboardButton("« Menu", callback_data='menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

        return ConversationHandler.END

    except ValueError:
        await update.message.reply_text("❌ Invalid number or /cancel")
        return UPDATE_HEARTBEAT


# ========== AI ADVISOR HANDLERS ==========

async def ask_ai_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point — show the user's stock list so they can pick one to analyse."""
    query = update.callback_query
    if query:
        await query.answer()

    # Guard: AI not configured
    if ai_advisor is None:
        message = (
            "🤖 *AI Advisor — Not Configured*\n\n"
            "Add your Openrouter API key to `config.json`:\n"
            "```\n\"openrouter\": {\"api_key\": \"sk-or-...\"}\n```\n\n"
            "Restart the bot after saving."
        )
        keyboard = [[InlineKeyboardButton("« Menu", callback_data='menu')]]
        if query:
            await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard),
                                          parse_mode='Markdown')
        else:
            await update.message.reply_text(message, reply_markup=InlineKeyboardMarkup(keyboard),
                                            parse_mode='Markdown')
        return ConversationHandler.END

    user_id = get_user_id(update)
    config = config_manager.load(user_id)
    stocks = config.get('stocks', [])

    if not stocks:
        message = (
            "🤖 *AI Advisor*\n\n"
            "You have no stocks in your monitoring list yet.\n"
            "Add a stock first, then come back here."
        )
        keyboard = [
            [InlineKeyboardButton("➕ Add Stock", callback_data='add_stock')],
            [InlineKeyboardButton("« Menu", callback_data='menu')]
        ]
        if query:
            await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard),
                                          parse_mode='Markdown')
        else:
            await update.message.reply_text(message, reply_markup=InlineKeyboardMarkup(keyboard),
                                            parse_mode='Markdown')
        return ConversationHandler.END

    message = (
        "🤖 *AI Advisor*\n\n"
        "Select the stock you want to analyse:"
    )
    keyboard = [
        [InlineKeyboardButton(f"📈 {s['ticker']}  (NAV: ${s.get('nav', 0):.2f})",
                              callback_data=f"aistock_{s['id']}")]
        for s in stocks
    ]
    keyboard.append([InlineKeyboardButton("« Cancel", callback_data='menu')])

    if query:
        await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard),
                                      parse_mode='Markdown')
    else:
        await update.message.reply_text(message, reply_markup=InlineKeyboardMarkup(keyboard),
                                        parse_mode='Markdown')

    return AI_SELECT_STOCK


async def ask_ai_stock_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User picked a stock — show Buy / Sell signal choice."""
    query = update.callback_query
    await query.answer()

    stock_id = query.data.replace('aistock_', '')
    user_id = get_user_id(update)

    stock = config_manager.get_stock(user_id, stock_id)
    if not stock:
        await query.edit_message_text("❌ Stock not found.")
        return ConversationHandler.END

    context.user_data['ai_stock_id'] = stock_id
    ticker = stock['ticker']

    message = (
        f"🤖 *AI Advisor — {ticker}*\n\n"
        "What kind of signal are you looking for?"
    )
    keyboard = [
        [InlineKeyboardButton("📈 Buy Signal",  callback_data='aisignal_buy')],
        [InlineKeyboardButton("📉 Sell Signal", callback_data='aisignal_sell')],
        [InlineKeyboardButton("« Back",         callback_data='ask_ai')]
    ]
    await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard),
                                  parse_mode='Markdown')

    return AI_SELECT_SIGNAL


async def ask_ai_signal_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    User chose Buy or Sell.
    1. Show loading message immediately (edit in-place).
    2. Call the AI in a thread (non-blocking).
    3. Show result — immediate advice or proposed alerts.
    """
    query = update.callback_query
    await query.answer()

    signal_type = "BUY" if query.data == 'aisignal_buy' else "SELL"
    context.user_data['ai_signal_type'] = signal_type

    stock_id = context.user_data.get('ai_stock_id')
    user_id = get_user_id(update)

    stock = config_manager.get_stock(user_id, stock_id)
    if not stock:
        await query.edit_message_text("❌ Stock not found.")
        return ConversationHandler.END

    ticker = stock['ticker']
    nav = stock.get('nav', 0)

    # ── Show loading message right away ──
    signal_emoji = "📈" if signal_type == "BUY" else "📉"
    await query.edit_message_text(
        f"🤖 *AI Advisor — {ticker}*\n\n"
        f"{signal_emoji} Analysing for *{signal_type}* signal...\n\n"
        f"⏳ Please wait, this may take 20–40 seconds.",
        parse_mode='Markdown'
    )

    # ── Load local price history ──
    history_data = price_history.load(ticker)

    # ── Call AI in a thread (won't block the event loop) ──
    result = await asyncio.to_thread(
        ai_advisor.get_advice, ticker, nav, signal_type, history_data
    )

    # ── Handle failure ──
    if not result.get('ok'):
        error_msg = result.get('error', 'Unknown error')
        keyboard = [[InlineKeyboardButton("« Menu", callback_data='menu')]]
        await query.edit_message_text(
            f"🤖 *AI Advisor — {ticker}*\n\n"
            f"❌ Analysis failed:\n{error_msg}\n\n"
            f"Your existing alerts are unchanged.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        context.user_data.clear()
        return ConversationHandler.END

    rec_type = result['recommendation_type']

    # ── IMMEDIATE recommendation ──
    if rec_type == 'immediate':
        action           = result.get('action', signal_type)
        summary          = result.get('summary', '')
        price_analysis   = result.get('price_analysis', '')
        sector_analysis  = result.get('sector_analysis', '')
        news_analysis    = result.get('news_analysis', '')
        sentiment        = result.get('sentiment_analysis', '')
        macro            = result.get('macro_analysis', '')

        action_emoji = "🟢" if action == "BUY" else "🔴"
        message = (
            f"🤖 *AI Advisor — {ticker}*\n\n"
            f"{action_emoji} *Recommendation: {action} NOW*\n\n"
            f"📋 *Summary:*\n{summary}\n\n"
            f"📈 *Stock Price & Volume:*\n{price_analysis}\n\n"
            f"🏭 *Sector:*\n{sector_analysis}\n\n"
            f"📰 *Company News:*\n{news_analysis}\n\n"
            f"💬 *Market Sentiment:*\n{sentiment}\n\n"
            f"🌐 *Macro:*\n{macro}"
        )

        # Telegram has a 4096 char limit per message — trim reasoning if needed
        if len(message) > 4000:
            message = message[:3980] + "...\n_(analysis truncated)_"

        keyboard = [[InlineKeyboardButton("« Menu", callback_data='menu')]]
        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        context.user_data.clear()
        return ConversationHandler.END

    # ── ALERT SUGGESTIONS ──
    if rec_type == 'alerts':
        proposed         = result.get('alerts', [])
        summary          = result.get('summary', '')
        price_analysis   = result.get('price_analysis', '')
        sector_analysis  = result.get('sector_analysis', '')
        news_analysis    = result.get('news_analysis', '')
        sentiment        = result.get('sentiment_analysis', '')
        macro            = result.get('macro_analysis', '')

        # Store for the confirmation step
        context.user_data['ai_proposed_alerts'] = proposed

        # Count existing alerts
        existing_count = len(stock.get('alerts', []))

        # ── Build alert display ──
        watch_alerts  = [a for a in proposed if a['tier'] == 'watch']
        action_alerts = [a for a in proposed if a['tier'] == 'action']

        def format_alert_line(a):
            desc = config_manager._generate_description(
                a['type'], a['operator'], a['threshold']
            )
            return f"• {desc}\n  _{a['rationale']}_"

        watch_block = (
            "\n".join(format_alert_line(a) for a in watch_alerts)
            if watch_alerts else "  _(none)_"
        )
        action_block = (
            "\n".join(format_alert_line(a) for a in action_alerts)
            if action_alerts else "  _(none)_"
        )

        replace_note = (
            f"⚠️ This will *replace* your current {existing_count} alert(s) for {ticker}."
            if existing_count > 0
            else f"ℹ️ This will add {len(proposed)} new alert(s) to {ticker}."
        )

        message = (
            f"🤖 *AI Advisor — {ticker}* ({signal_type} signal)\n\n"
            f"📋 *Summary:*\n{summary}\n\n"
            f"📈 *Stock Price & Volume:*\n{price_analysis}\n\n"
            f"🏭 *Sector:*\n{sector_analysis}\n\n"
            f"📰 *Company News:*\n{news_analysis}\n\n"
            f"💬 *Market Sentiment:*\n{sentiment}\n\n"
            f"🌐 *Macro:*\n{macro}\n\n"
            f"🔔 *Proposed Alerts:*\n\n"
            f"👁 *Watch (early warning):*\n{watch_block}\n\n"
            f"⚡ *Action (strong signal):*\n{action_block}\n\n"
            f"{replace_note}\n\n"
            f"Apply these alerts?"
        )

        if len(message) > 4000:
            message = message[:3980] + "...\n_(truncated)_"

        keyboard = [
            [
                InlineKeyboardButton("✅ Yes, Apply Alerts", callback_data='aiconfirm_yes'),
                InlineKeyboardButton("❌ Cancel",            callback_data='aiconfirm_no')
            ]
        ]
        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return AI_CONFIRM_ALERTS

    # Fallback (should never reach here)
    await query.edit_message_text("❌ Unexpected AI response type.")
    return ConversationHandler.END


async def ask_ai_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User confirmed or cancelled the proposed alerts."""
    query = update.callback_query
    await query.answer()

    user_id  = get_user_id(update)
    stock_id = context.user_data.get('ai_stock_id')
    stock    = config_manager.get_stock(user_id, stock_id)
    ticker   = stock['ticker'] if stock else "Stock"

    if query.data == 'aiconfirm_yes':
        proposed = context.user_data.get('ai_proposed_alerts', [])

        # Build proper alert dicts (match the format used everywhere else in the bot)
        new_alerts = []
        for a in proposed:
            alert_id = f"alert_{uuid.uuid4().hex[:8]}"
            description = config_manager._generate_description(
                a['type'], a['operator'], a['threshold']
            )
            new_alerts.append({
                "id":          alert_id,
                "type":        a['type'],
                "operator":    a['operator'],
                "threshold":   a['threshold'],
                "description": description,
                "enabled":     True,
                "created_at":  datetime.now().isoformat()
            })

        # Replace all alerts + clear stale alert states
        config_manager.replace_alerts(user_id, stock_id, new_alerts)
        alert_state_manager.clear_stock_alert_states(user_id, stock_id)

        # Confirmation message listing the new alerts
        lines = "\n".join(f"  {i+1}. {a['description']}"
                          for i, a in enumerate(new_alerts))
        message = (
            f"✅ *Alerts Updated for {ticker}!*\n\n"
            f"*New alert set ({len(new_alerts)} alert(s)):*\n"
            f"{lines}\n\n"
            f"The monitor will start checking these on the next cycle."
        )
        keyboard = [
            [InlineKeyboardButton(f"🔔 View {ticker} Alerts", callback_data=f'alerts_{stock_id}')],
            [InlineKeyboardButton("« Menu", callback_data='menu')]
        ]

    else:  # aiconfirm_no
        message = (
            f"❌ *Cancelled*\n\n"
            f"Your existing alerts for {ticker} are unchanged."
        )
        keyboard = [[InlineKeyboardButton("« Menu", callback_data='menu')]]

    # Remove the Yes/No buttons from the analysis message so it stays readable,
    # then send the confirmation as a NEW message so the analysis is preserved.
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass  # If edit fails (e.g. message too old), just proceed

    await query.message.reply_text(
        message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    context.user_data.clear()
    return ConversationHandler.END


# ========== IPO LISTING ADVISOR HANDLERS ==========

async def ipo_ai_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point — guard for missing API key, then show stock list."""
    query = update.callback_query
    if query:
        await query.answer()

    # Guard: pre_ipo_advisor not configured
    if pre_ipo_advisor is None:
        message = (
            "🚀 *IPO Listing Advisor — Not Configured*\n\n"
            "Add your Openrouter API key to `config.json`:\n"
            "```\n\"openrouter\": {\"api_key\": \"sk-or-...\"}\n```\n\n"
            "Restart the bot after saving."
        )
        keyboard = [[InlineKeyboardButton("« Menu", callback_data='menu')]]
        if query:
            await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard),
                                          parse_mode='Markdown')
        else:
            await update.message.reply_text(message, reply_markup=InlineKeyboardMarkup(keyboard),
                                            parse_mode='Markdown')
        return ConversationHandler.END

    user_id = get_user_id(update)
    config = config_manager.load(user_id)
    stocks = config.get('stocks', [])

    if not stocks:
        message = (
            "🚀 *IPO Listing Advisor*\n\n"
            "You have no stocks in your monitoring list yet.\n"
            "Add a stock first, then come back here."
        )
        keyboard = [
            [InlineKeyboardButton("➕ Add Stock", callback_data='add_stock')],
            [InlineKeyboardButton("« Menu", callback_data='menu')]
        ]
        if query:
            await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard),
                                          parse_mode='Markdown')
        else:
            await update.message.reply_text(message, reply_markup=InlineKeyboardMarkup(keyboard),
                                            parse_mode='Markdown')
        return ConversationHandler.END

    message = (
        "🚀 *IPO Listing Advisor*\n\n"
        "Compares a recently-listed stock against DXYZ's post-listing lifecycle "
        "to identify lifecycle stage, calibrate alerts, and recommend position sizing.\n\n"
        "Select the stock you want to analyse:"
    )
    keyboard = [
        [InlineKeyboardButton(f"📈 {s['ticker']}  (NAV: ${s.get('nav', 0):.2f})",
                              callback_data=f"ipostock_{s['id']}")]
        for s in stocks
    ]
    keyboard.append([InlineKeyboardButton("« Cancel", callback_data='menu')])

    if query:
        await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard),
                                      parse_mode='Markdown')
    else:
        await update.message.reply_text(message, reply_markup=InlineKeyboardMarkup(keyboard),
                                        parse_mode='Markdown')

    return PRE_IPO_SELECT_STOCK


async def ipo_ai_stock_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User picked a stock — show Buy / Sell signal choice."""
    query = update.callback_query
    await query.answer()

    stock_id = query.data.replace('ipostock_', '')
    user_id = get_user_id(update)

    stock = config_manager.get_stock(user_id, stock_id)
    if not stock:
        await query.edit_message_text("❌ Stock not found.")
        return ConversationHandler.END

    context.user_data['ipo_stock_id'] = stock_id
    ticker = stock['ticker']

    message = (
        f"🚀 *IPO Listing Advisor — {ticker}*\n\n"
        "What kind of signal are you looking for?"
    )
    keyboard = [
        [InlineKeyboardButton("📈 Buy Signal",  callback_data='iposignal_buy')],
        [InlineKeyboardButton("📉 Sell Signal", callback_data='iposignal_sell')],
        [InlineKeyboardButton("« Back",         callback_data='ipo_advisor')]
    ]
    await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard),
                                  parse_mode='Markdown')

    return PRE_IPO_SELECT_SIGNAL


async def ipo_ai_signal_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    User chose Buy or Sell.
    1. Show loading message immediately.
    2. Call PreIpoAdvisor in a thread (non-blocking).
    3. Display result — immediate recommendation or proposed alerts.
    """
    query = update.callback_query
    await query.answer()

    signal_type = "BUY" if query.data == 'iposignal_buy' else "SELL"
    context.user_data['ipo_signal_type'] = signal_type

    stock_id = context.user_data.get('ipo_stock_id')
    user_id  = get_user_id(update)

    stock = config_manager.get_stock(user_id, stock_id)
    if not stock:
        await query.edit_message_text("❌ Stock not found.")
        return ConversationHandler.END

    ticker = stock['ticker']
    nav    = stock.get('nav', 0)

    signal_emoji = "📈" if signal_type == "BUY" else "📉"
    await query.edit_message_text(
        f"🚀 *IPO Listing Advisor — {ticker}*\n\n"
        f"{signal_emoji} Analysing for *{signal_type}* signal...\n"
        f"📊 Fetching DXYZ lifecycle reference data...\n"
        f"🔍 Comparing post-listing patterns...\n\n"
        f"⏳ Please wait, this may take 20–40 seconds.",
        parse_mode='Markdown'
    )

    history_data = price_history.load(ticker)

    # Call AI in a thread (non-blocking — keeps the event loop responsive)
    result = await asyncio.to_thread(
        pre_ipo_advisor.get_advice, ticker, nav, signal_type, history_data
    )

    # ── Handle failure ──
    if not result.get('ok'):
        error_msg = result.get('error', 'Unknown error')
        keyboard = [[InlineKeyboardButton("« Menu", callback_data='menu')]]
        await query.edit_message_text(
            f"🚀 *IPO Listing Advisor — {ticker}*\n\n"
            f"❌ Analysis failed:\n{error_msg}\n\n"
            f"Your existing alerts are unchanged.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        context.user_data.clear()
        return ConversationHandler.END

    rec_type = result['recommendation_type']

    # ── Common analysis sections (used by both paths) ──
    summary         = result.get('summary', '')
    dxyz_pattern    = result.get('dxyz_pattern_analysis', '')
    lifecycle_stage = result.get('lifecycle_stage', '')
    news_analysis   = result.get('news_analysis', '')
    sentiment       = result.get('sentiment_analysis', '')
    macro           = result.get('macro_analysis', '')

    # ── Position sizing block (SELL required; BUY optional) ──
    position_block = ""
    ps = result.get('position_sizing')
    if ps and isinstance(ps, dict):
        sell_pct  = ps.get('sell_pct_now', '?')
        ps_rat    = ps.get('rationale', '')
        ps_staged = ps.get('staged_approach', '')
        position_block = (
            f"\n💰 *Position Sizing Recommendation:*\n"
            f"Sell *{sell_pct}%* of your position now.\n\n"
            f"{ps_rat}"
            + (f"\n\n_{ps_staged}_" if ps_staged else "")
            + "\n"
        )

    # Split into primary (DXYZ-core) and supplemental sections so we can
    # break into two Telegram messages when the combined text exceeds 4096 chars.
    primary_block = (
        f"📋 *Summary:*\n{summary}\n\n"
        f"📊 *DXYZ Pattern Analysis:*\n{dxyz_pattern}\n\n"
        f"📍 *Lifecycle Stage:*\n{lifecycle_stage}\n"
        + position_block
    )

    supplemental_block = (
        f"\n📰 *Company News:*\n{news_analysis}\n\n"
        f"💬 *Market Sentiment:*\n{sentiment}\n\n"
        f"🌐 *Macro:*\n{macro}"
    )

    analysis_block = primary_block + supplemental_block

    # ── IMMEDIATE recommendation ──
    if rec_type == 'immediate':
        action       = result.get('action', signal_type)
        action_msg   = result.get('message', '')
        action_emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡"}.get(action, "⚪")

        header = (
            f"🚀 *IPO Listing Advisor — {ticker}* ({signal_type} signal)\n\n"
            f"{action_emoji} *Recommendation: {action}*\n"
            f"{action_msg}\n\n"
        )
        keyboard = [[InlineKeyboardButton("« Menu", callback_data='menu')]]
        full_message = header + analysis_block

        if len(full_message) <= 4096:
            # Fits in a single message
            await query.edit_message_text(
                full_message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
        else:
            # Split: part 1 = header + primary analysis (no buttons)
            #        part 2 = supplemental sections + menu button
            msg1 = header + primary_block
            msg2 = supplemental_block.lstrip('\n')
            if len(msg1) > 4096:
                msg1 = msg1[:4080] + "\n_(truncated)_"
            if len(msg2) > 4096:
                msg2 = msg2[:4080] + "\n_(truncated)_"
            await query.edit_message_text(msg1, parse_mode='Markdown')
            await query.message.reply_text(
                msg2,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )

        context.user_data.clear()
        return ConversationHandler.END

    # ── ALERT SUGGESTIONS ──
    if rec_type == 'alerts':
        proposed = result.get('alerts', [])
        context.user_data['ipo_proposed_alerts'] = proposed

        existing_count = len(stock.get('alerts', []))

        watch_alerts  = [a for a in proposed if a['tier'] == 'watch']
        action_alerts = [a for a in proposed if a['tier'] == 'action']

        def format_alert_line(a):
            desc = config_manager._generate_description(
                a['type'], a['operator'], a['threshold']
            )
            return f"• {desc}\n  _{a['rationale']}_"

        watch_block = (
            "\n".join(format_alert_line(a) for a in watch_alerts)
            if watch_alerts else "  _(none)_"
        )
        action_block = (
            "\n".join(format_alert_line(a) for a in action_alerts)
            if action_alerts else "  _(none)_"
        )

        replace_note = (
            f"⚠️ This will *replace* your current {existing_count} alert(s) for {ticker}."
            if existing_count > 0
            else f"ℹ️ This will add {len(proposed)} new alert(s) to {ticker}."
        )

        header = f"🚀 *IPO Listing Advisor — {ticker}* ({signal_type} signal)\n\n"
        alerts_section = (
            f"🔔 *Proposed Alerts:*\n\n"
            f"👁 *Watch (early warning):*\n{watch_block}\n\n"
            f"⚡ *Action (strong signal):*\n{action_block}\n\n"
            f"{replace_note}\n\n"
            f"Apply these alerts?"
        )
        keyboard = [
            [
                InlineKeyboardButton("✅ Yes, Apply Alerts", callback_data='ipoconfirm_yes'),
                InlineKeyboardButton("❌ Cancel",            callback_data='ipoconfirm_no')
            ]
        ]

        full_message = header + analysis_block + "\n\n" + alerts_section

        if len(full_message) <= 4096:
            # Fits in a single message — send with buttons
            await query.edit_message_text(
                full_message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
        else:
            # Too long — split into two messages:
            # Part 1: header + analysis (no buttons, user can scroll and read)
            # Part 2: alerts section with Yes/No buttons
            msg1 = header + analysis_block
            msg2 = alerts_section

            # If even part 1 alone is too long, push supplemental into part 2
            if len(msg1) > 4096:
                msg1 = header + primary_block
                msg2 = supplemental_block.lstrip('\n') + "\n\n" + alerts_section

            # Final safety truncation (should rarely trigger)
            if len(msg1) > 4096:
                msg1 = msg1[:4080] + "\n_(truncated)_"
            if len(msg2) > 4096:
                msg2 = msg2[:4080] + "\n_(truncated)_"

            await query.edit_message_text(msg1, parse_mode='Markdown')
            await query.message.reply_text(
                msg2,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )

        return PRE_IPO_CONFIRM_ALERTS

    # Fallback (should never reach here)
    await query.edit_message_text("❌ Unexpected AI response type.")
    return ConversationHandler.END


async def ipo_ai_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User confirmed or cancelled the proposed alerts from the IPO advisor."""
    query = update.callback_query
    await query.answer()

    user_id  = get_user_id(update)
    stock_id = context.user_data.get('ipo_stock_id')
    stock    = config_manager.get_stock(user_id, stock_id)
    ticker   = stock['ticker'] if stock else "Stock"

    if query.data == 'ipoconfirm_yes':
        proposed = context.user_data.get('ipo_proposed_alerts', [])

        new_alerts = []
        for a in proposed:
            alert_id    = f"alert_{uuid.uuid4().hex[:8]}"
            description = config_manager._generate_description(
                a['type'], a['operator'], a['threshold']
            )
            new_alerts.append({
                "id":          alert_id,
                "type":        a['type'],
                "operator":    a['operator'],
                "threshold":   a['threshold'],
                "description": description,
                "enabled":     True,
                "created_at":  datetime.now().isoformat()
            })

        config_manager.replace_alerts(user_id, stock_id, new_alerts)
        alert_state_manager.clear_stock_alert_states(user_id, stock_id)

        lines = "\n".join(f"  {i+1}. {a['description']}"
                          for i, a in enumerate(new_alerts))
        message = (
            f"✅ *Alerts Updated for {ticker}!*\n\n"
            f"*New alert set ({len(new_alerts)} alert(s)):*\n"
            f"{lines}\n\n"
            f"The monitor will start checking these on the next cycle."
        )
        keyboard = [
            [InlineKeyboardButton(f"🔔 View {ticker} Alerts", callback_data=f'alerts_{stock_id}')],
            [InlineKeyboardButton("« Menu", callback_data='menu')]
        ]

    else:  # ipoconfirm_no
        message = (
            f"❌ *Cancelled*\n\n"
            f"Your existing alerts for {ticker} are unchanged."
        )
        keyboard = [[InlineKeyboardButton("« Menu", callback_data='menu')]]

    # Strip buttons from analysis message so it stays readable in history;
    # send confirmation as a NEW message below (same pattern as ask_ai_confirm)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    await query.message.reply_text(
        message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    context.user_data.clear()
    return ConversationHandler.END


# ========== HELP & MISC HANDLERS ==========

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help"""
    message = (
        "📚 *Stock Monitor Bot Help*\n\n"
        "*Features:*\n"
        "• Track up to 5 stocks\n"
        "• Custom alerts per stock\n"
        "• Premium, price, volume alerts\n"
        "• State-based notifications\n"
        "• Live status check\n\n"
        "*Commands:*\n"
        "/start - Main menu\n"
        "/stocks - View all stocks\n"
        "/addstock - Add new stock\n"
        "/help - This message\n\n"
        "*Alert Types:*\n"
        "• Premium: Price vs NAV\n"
        "• 1-Day: Daily change\n"
        "• 7-Day: Weekly change\n"
        "• Volume: vs 10-day avg"
    )

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        keyboard = [[InlineKeyboardButton("« Menu", callback_data='menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(message, parse_mode='Markdown')


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks"""
    query = update.callback_query
    await query.answer()

    if query.data == 'menu':
        await start(update, context)
    elif query.data == 'list_stocks':
        await list_stocks(update, context)
    elif query.data == 'check_status_now':
        await check_status_now(update, context)
    elif query.data == 'ask_ai':
        await ask_ai_start(update, context)
    elif query.data == 'ipo_advisor':
        await ipo_ai_start(update, context)
    elif query.data.startswith('stock_'):
        await stock_detail(update, context)
    elif query.data.startswith('alerts_'):
        await manage_stock_alerts(update, context)
    elif query.data.startswith('remove_'):
        await remove_stock_confirm(update, context)
    elif query.data.startswith('confirmremove_'):
        await remove_stock_confirmed(update, context)
    elif query.data == 'help':
        await help_command(update, context)
    elif query.data == 'max_stocks':
        await query.answer("Remove a stock to add a new one", show_alert=True)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel operation"""
    await update.message.reply_text("❌ Cancelled")
    context.user_data.clear()
    return ConversationHandler.END


def main():
    """Start the bot"""
    global ai_advisor, pre_ipo_advisor

    try:
        with open('config.json', 'r') as f:
            main_config = json.load(f)
            bot_token = main_config['telegram']['bot_token']
    except:
        print("ERROR: Create config.json with bot token")
        return

    # Initialise AI advisor (optional — bot runs fine without it)
    openrouter_key = (
        main_config.get('openrouter', {}).get('api_key', '') or ''
    ).strip()

    # Optional data-enrichment API keys (all fail silently if absent)
    marketaux_key = (main_config.get('marketaux', {}).get('api_key', '') or '').strip() or None
    finnhub_key   = (main_config.get('finnhub',   {}).get('api_key', '') or '').strip() or None
    adanos_key    = (main_config.get('adanos',    {}).get('api_key', '') or '').strip() or None
    if marketaux_key and marketaux_key == "YOUR_MARKETAUX_API_KEY_HERE":
        marketaux_key = None
    if finnhub_key and finnhub_key == "YOUR_FINNHUB_API_KEY_HERE":
        finnhub_key = None
    if adanos_key and adanos_key == "YOUR_ADANOS_API_KEY_HERE":
        adanos_key = None

    if openrouter_key and openrouter_key != "YOUR_OPENROUTER_API_KEY_HERE":
        ai_advisor = AIAdvisor(openrouter_key,
                               marketaux_key=marketaux_key,
                               finnhub_key=finnhub_key,
                               adanos_key=adanos_key)
        print("🤖 AI Advisor: enabled (Openrouter key loaded)")
        pre_ipo_advisor = PreIpoAdvisor(openrouter_key,
                                        marketaux_key=marketaux_key,
                                        finnhub_key=finnhub_key,
                                        adanos_key=adanos_key)
        print("🚀 IPO Listing Advisor: enabled (DXYZ lifecycle analysis)")
    else:
        ai_advisor = None
        pre_ipo_advisor = None
        print("🤖 AI Advisor: disabled (no Openrouter API key in config.json)")
        print("🚀 IPO Listing Advisor: disabled (no Openrouter API key in config.json)")

    application = Application.builder().token(bot_token).build()
    application.post_init = setup_commands

    # Conversations
    add_stock_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(add_stock_start, pattern='^add_stock$'),
            CommandHandler('addstock', add_stock_start)
        ],
        states={
            ADD_STOCK_TICKER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_stock_ticker_entered)],
            ADD_STOCK_NAV: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_stock_nav_entered)]
        },
        fallbacks=[CommandHandler('cancel', cancel_command)],
        allow_reentry=True
    )

    add_alert_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_alert_start, pattern='^addalert_')],
        states={
            ALERT_TYPE: [CallbackQueryHandler(add_alert_type_selected, pattern='^alerttype_')],
            ALERT_OPERATOR: [CallbackQueryHandler(add_alert_operator_selected, pattern='^operator_')],
            ALERT_THRESHOLD: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_alert_threshold_entered)],
            ALERT_CONFIRM: [CallbackQueryHandler(add_alert_confirmed, pattern='^confirm_alert_')]
        },
        fallbacks=[CommandHandler('cancel', cancel_command)],
        allow_reentry=True
    )

    delete_alert_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(delete_alert_start, pattern='^delalert_')],
        states={DELETE_ALERT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_alert_id_entered)]},
        fallbacks=[CommandHandler('cancel', cancel_command)],
        allow_reentry=True
    )

    nav_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(update_nav_start, pattern='^nav_')],
        states={UPDATE_NAV: [MessageHandler(filters.TEXT & ~filters.COMMAND, update_nav_entered)]},
        fallbacks=[CommandHandler('cancel', cancel_command)],
        allow_reentry=True
    )

    heartbeat_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(update_heartbeat_start, pattern='^update_heartbeat$'),
            CommandHandler('heartbeat', update_heartbeat_start),
        ],
        states={UPDATE_HEARTBEAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, update_heartbeat_entered)]},
        fallbacks=[CommandHandler('cancel', cancel_command)],
        allow_reentry=True
    )

    ask_ai_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(ask_ai_start, pattern='^ask_ai$'),
            CommandHandler('askai', ask_ai_start),
        ],
        states={
            AI_SELECT_STOCK:   [CallbackQueryHandler(ask_ai_stock_selected,  pattern='^aistock_')],
            AI_SELECT_SIGNAL:  [CallbackQueryHandler(ask_ai_signal_selected, pattern='^aisignal_')],
            AI_CONFIRM_ALERTS: [CallbackQueryHandler(ask_ai_confirm,         pattern='^aiconfirm_')]
        },
        fallbacks=[CommandHandler('cancel', cancel_command)],
        allow_reentry=True
    )

    ipo_ai_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(ipo_ai_start, pattern='^ipo_advisor$'),
            CommandHandler('ipoai', ipo_ai_start),
        ],
        states={
            PRE_IPO_SELECT_STOCK:   [CallbackQueryHandler(ipo_ai_stock_selected,  pattern='^ipostock_')],
            PRE_IPO_SELECT_SIGNAL:  [CallbackQueryHandler(ipo_ai_signal_selected, pattern='^iposignal_')],
            PRE_IPO_CONFIRM_ALERTS: [CallbackQueryHandler(ipo_ai_confirm,         pattern='^ipoconfirm_')],
        },
        fallbacks=[CommandHandler('cancel', cancel_command)],
        allow_reentry=True
    )

    # Add handlers
    # ConversationHandlers must be registered before the catch-all button_callback
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('stocks', list_stocks))
    application.add_handler(CommandHandler('status', check_status_now))
    application.add_handler(CommandHandler('help', help_command))

    application.add_handler(add_stock_conv)
    application.add_handler(add_alert_conv)
    application.add_handler(delete_alert_conv)
    application.add_handler(nav_conv)
    application.add_handler(heartbeat_conv)
    application.add_handler(ipo_ai_conv)
    application.add_handler(ask_ai_conv)

    application.add_handler(CallbackQueryHandler(button_callback))

    print("📈 Stock Monitor Bot - Multi-Stock Version")
    print("=" * 60)

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
