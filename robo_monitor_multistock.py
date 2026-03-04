#!/usr/bin/env python3
"""
Stock Monitor Bot - Multi-Stock Monitor
Monitors up to 5 stocks per user with state-tracked alerts
"""

import json
import time
import requests
import yfinance as yf
from datetime import datetime
import pytz
import os
import glob


CONFIG_DIR = "user_configs"
PRICE_HISTORY_DIR = "price_history"
ALERT_STATE_DIR = "alert_states"
MAIN_CONFIG_FILE = "config.json"
CHECK_INTERVAL = 900  # 15 minutes

VOLUME_GRACE_PERIOD_HOURS = 2  # Skip volume check for first N hours after market open

# Market session definitions (add more as needed)
MARKET_INFO = {
    'US': {
        'timezone': 'America/New_York',
        'open_hour': 9,  'open_min': 30,
        'close_hour': 16, 'close_min': 0,
        'total_trading_hours': 6.5,   # 9:30-16:00
        'lunch_break': None,
        'pre_market_start': (4, 0),    # 4:00 AM ET – pre-market opens
        'after_hours_end':  (20, 0),   # 8:00 PM ET – after-hours closes
    },
    'HK': {
        'timezone': 'Asia/Hong_Kong',
        'open_hour': 9,  'open_min': 30,
        'close_hour': 16, 'close_min': 0,
        'total_trading_hours': 5.5,   # 9:30-16:00 minus 1 h lunch
        'lunch_break': (12, 0, 13, 0),  # (start_h, start_m, end_h, end_m)
        'pre_market_start': None,      # HKEX has no extended hours
        'after_hours_end':  None,
    },
}


def get_market_key(ticker):
    """Return the MARKET_INFO key that matches this ticker's exchange."""
    if ticker.upper().endswith('.HK'):
        return 'HK'
    return 'US'  # Default: assume US market


def is_market_weekend(ticker: str) -> bool:
    """Return True if today is Saturday or Sunday in the stock's market timezone.
    Used to suppress volume alerts on weekends when no trading occurs."""
    tz = pytz.timezone(MARKET_INFO[get_market_key(ticker)]['timezone'])
    return datetime.now(tz).weekday() >= 5  # 5=Saturday, 6=Sunday


def get_market_session(ticker: str) -> str:
    """Return the current trading session for a ticker:
      'pre_market'  – extended pre-market hours (US only, 4:00–9:30 AM ET)
      'regular'     – regular trading hours
      'after_hours' – extended after-hours (US only, 4:00–8:00 PM ET)
      'closed'      – outside all trading windows, or weekend
    """
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
    except Exception as e:
        print(f"      Extended hours price unavailable for {ticker}: {e}")
        return None, None


def get_market_elapsed_info(ticker):
    """
    Work out how far into the current trading session we are.

    Returns a tuple:
        is_open        (bool)  – True if the market is currently in session
        elapsed_hours  (float) – trading hours elapsed since open
                                 (lunch break is excluded for HK)
        total_hours    (float) – full trading hours in a normal session
        prorata_factor (float) – elapsed / total, capped at 1.0
    """
    info = MARKET_INFO[get_market_key(ticker)]
    tz = pytz.timezone(info['timezone'])
    now = datetime.now(tz)

    market_open = now.replace(
        hour=info['open_hour'], minute=info['open_min'], second=0, microsecond=0
    )
    market_close = now.replace(
        hour=info['close_hour'], minute=info['close_min'], second=0, microsecond=0
    )

    if now < market_open or now >= market_close:
        # Market is closed – caller should use full-day volume as-is
        return False, 0.0, info['total_trading_hours'], 0.0

    # Calendar time since open
    elapsed = (now - market_open).total_seconds() / 3600.0

    # Subtract any lunch break that has already passed
    lb = info['lunch_break']
    if lb:
        lh_start, lm_start, lh_end, lm_end = lb
        lunch_start = now.replace(hour=lh_start, minute=lm_start, second=0, microsecond=0)
        lunch_end   = now.replace(hour=lh_end,   minute=lm_end,   second=0, microsecond=0)
        if now >= lunch_end:
            elapsed -= (lunch_end - lunch_start).total_seconds() / 3600.0
        elif now >= lunch_start:
            elapsed -= (now - lunch_start).total_seconds() / 3600.0

    prorata_factor = min(elapsed / info['total_trading_hours'], 1.0)
    return True, elapsed, info['total_trading_hours'], prorata_factor


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

    def save(self, ticker, data):
        with open(self._get_path(ticker), 'w') as f:
            json.dump(data, f, indent=2)

    def add_price_point(self, ticker, date_str, price, volume, nav, premium):
        data = self.load(ticker)

        if data['daily_prices'] and data['daily_prices'][-1]['date'] == date_str:
            data['daily_prices'][-1] = {
                'date': date_str, 'price': price, 'volume': volume,
                'nav': nav, 'premium': premium
            }
        else:
            data['daily_prices'].append({
                'date': date_str, 'price': price, 'volume': volume,
                'nav': nav, 'premium': premium
            })

        if len(data['daily_prices']) > 30:
            data['daily_prices'] = data['daily_prices'][-30:]

        data['last_update'] = datetime.now().isoformat()
        self.save(ticker, data)

    def get_price_change_1d(self, ticker):
        data = self.load(ticker)
        if len(data['daily_prices']) < 2:
            return None
        today = data['daily_prices'][-1]['price']
        yesterday = data['daily_prices'][-2]['price']
        return ((today - yesterday) / yesterday) * 100

    def get_price_change_7d(self, ticker):
        data = self.load(ticker)
        if len(data['daily_prices']) < 8:
            return None
        today = data['daily_prices'][-1]['price']
        seven_ago = data['daily_prices'][-8]['price']
        return ((today - seven_ago) / seven_ago) * 100

    def get_volume_average_7d(self, ticker):
        data = self.load(ticker)
        if not data['daily_prices']:
            return None
        # Exclude the most recent (today's) entry — it may have partial intraday volume.
        # Using only completed trading days gives a clean baseline for comparison.
        history = data['daily_prices'][:-1] if len(data['daily_prices']) > 1 else data['daily_prices']
        recent = [d['volume'] for d in history[-7:] if d.get('volume')]
        return sum(recent) / len(recent) if recent else None


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

    def save(self, user_id, states):
        with open(self._get_path(user_id), 'w') as f:
            json.dump(states, f, indent=2)

    def get_state(self, user_id, alert_key):
        """Get alert state (True=triggered, False=not)"""
        states = self.load(user_id)
        return states.get(alert_key, False)

    def check_state_change(self, user_id, alert_key, currently_triggered):
        """Check if state changed"""
        old_state = self.get_state(user_id, alert_key)

        if old_state != currently_triggered:
            states = self.load(user_id)
            states[alert_key] = currently_triggered
            self.save(user_id, states)
            return True, old_state, currently_triggered

        return False, old_state, currently_triggered


class MultiStockMonitor:
    """Main multi-stock monitoring bot"""

    def __init__(self, config_dir, history_dir, state_dir, main_config):
        self.config_dir = config_dir
        self.history = PriceHistory(history_dir)
        self.alert_states = AlertStateManager(state_dir)
        self.main_config_file = main_config
        self.last_heartbeat = {}

    def load_main_config(self):
        try:
            with open(self.main_config_file, 'r') as f:
                return json.load(f)
        except:
            return None

    def get_all_users(self):
        pattern = os.path.join(self.config_dir, "config_user_*.json")
        users = []
        for path in glob.glob(pattern):
            try:
                with open(path, 'r') as f:
                    users.append(json.load(f))
            except:
                pass
        return users

    def send_telegram(self, user_id, message):
        main_config = self.load_main_config()
        if not main_config:
            return False

        bot_token = main_config['telegram']['bot_token']
        if not bot_token or bot_token == "YOUR_BOT_TOKEN_HERE":
            return False

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {'chat_id': user_id, 'text': message, 'parse_mode': 'Markdown'}

        try:
            response = requests.post(url, json=payload, timeout=10)
            return response.status_code == 200
        except:
            return False

    def get_stock_data(self, ticker):
        try:
            stock = yf.Ticker(ticker)
            data = stock.history(period='2d')
            if data.empty:
                return None, None, None
            latest = data.iloc[-1]
            # Use the actual trading date from yfinance (not datetime.now()) so that
            # weekend/holiday runs don't create phantom entries for non-trading days.
            trade_date = latest.name.strftime('%Y-%m-%d')
            return float(latest['Close']), int(latest['Volume']), trade_date
        except:
            return None, None, None

    def check_alert(self, alert, value):
        if value is None:
            return False
        operator = alert['operator']
        threshold = alert['threshold']

        if operator == '>':
            return value > threshold
        elif operator == '<':
            return value < threshold
        return False

    def check_stock_alerts(self, user_id, stock, price, nav, premium,
                          change_1d, change_7d, vol_ratio):
        """Check alerts for one stock"""
        stock_id = stock['id']
        ticker = stock['ticker']
        alerts = stock.get('alerts', [])

        state_changes = []

        for alert in alerts:
            if not alert.get('enabled', True):
                continue

            alert_id = alert['id']
            alert_type = alert['type']

            # Get current value
            value = None
            if alert_type == 'premium':
                value = premium
            elif alert_type == 'price_1d':
                value = change_1d
            elif alert_type == 'price_7d':
                value = change_7d
            elif alert_type == 'volume':
                if vol_ratio is None:
                    # Grace period or no data – leave state unchanged to avoid
                    # spurious "triggered / cleared" messages at market open.
                    continue
                value = vol_ratio

            # Check condition
            triggered = self.check_alert(alert, value)

            # Check state change
            alert_key = f"{stock_id}_{alert_id}"
            changed, old, new = self.alert_states.check_state_change(
                user_id, alert_key, triggered
            )

            if changed:
                state_changes.append({
                    'alert': alert,
                    'old_state': old,
                    'new_state': new,
                    'value': value,
                    'ticker': ticker
                })

        return state_changes

    def send_alert_triggered(self, user_id, ticker, alert, value, price, nav, price_label=None):
        """Send triggered alert"""
        types = {'premium': '📈', 'price_1d': '📊', 'price_7d': '📆', 'volume': '📊'}
        emoji = types.get(alert['type'], '🔔')

        label_str = f" _{price_label}_" if price_label else ""
        msg = (
            f"{emoji} *ALERT TRIGGERED!*\n\n"
            f"*Ticker:* {ticker}\n"
            f"*Alert:* {alert['description']}\n\n"
            f"*Current:*\n"
            f"• Price: ${price:.2f}{label_str}\n"
            f"• NAV: ${nav:.2f}\n"
        )

        if alert['type'] == 'premium':
            msg += f"• Premium: {value:.1f}%\n"
        elif alert['type'] in ['price_1d', 'price_7d']:
            sign = '+' if value > 0 else ''
            msg += f"• Change: {sign}{value:.1f}%\n"
        elif alert['type'] == 'volume':
            msg += f"• Volume: {value:.2f}x\n"

        msg += f"\n*Alert ID:* `{alert['id']}`\n⚠️ **Review position**"

        self.send_telegram(user_id, msg)

    def send_alert_cleared(self, user_id, ticker, alert, value):
        """Send cleared alert"""
        msg = (
            f"✅ *ALERT CLEARED*\n\n"
            f"*Ticker:* {ticker}\n"
            f"*Alert:* {alert['description']}\n\n"
            f"Condition no longer met.\n"
            f"*Alert ID:* `{alert['id']}`"
        )
        self.send_telegram(user_id, msg)

    def should_send_heartbeat(self, user_id, frequency):
        now = datetime.now()
        if user_id not in self.last_heartbeat:
            self.last_heartbeat[user_id] = now
            return True

        hours_since = (now - self.last_heartbeat[user_id]).total_seconds() / 3600
        if hours_since >= frequency:
            self.last_heartbeat[user_id] = now
            return True
        return False

    def send_heartbeat(self, user_id, stock_summaries, config):
        """Send heartbeat for all stocks"""
        hk_tz = pytz.timezone('Asia/Hong_Kong')
        now_hk = datetime.now(hk_tz).strftime('%Y-%m-%d %H:%M:%S HKT')

        msg = (
            f"✅ *Heartbeat - System Active*\n\n"
            f"🕐 {now_hk}\n"
            f"📊 Tracking {len(stock_summaries)} stock(s)\n\n"
        )

        for summary in stock_summaries:
            ticker = summary['ticker']
            price = summary['price']
            nav = summary['nav']
            premium = summary['premium']
            alerts = summary['alerts']

            price_label = summary.get('price_label')
            label_str = f" _({price_label})_" if price_label else ""
            msg += f"*{ticker}*\n"
            msg += f"• Price: ${price:.2f}{label_str} | NAV: ${nav:.2f}\n"
            if premium is not None:
                msg += f"• Premium: {premium:.1f}%\n"

            if summary.get('change_1d'):
                sign = '+' if summary['change_1d'] > 0 else ''
                msg += f"• 1D: {sign}{summary['change_1d']:.1f}%\n"

            # Show alert states
            if alerts:
                for alert in alerts:
                    triggered = alert['triggered']
                    icon = "⚠️" if triggered else "✅"
                    desc = alert['description']
                    if len(desc) > 25:
                        desc = desc[:22] + "..."
                    state = "ACTIVE" if triggered else "Normal"
                    msg += f"{icon} {desc}: {state}\n"

            msg += "\n"

        msg += "🟢 Monitoring continues..."

        self.send_telegram(user_id, msg)

    def _seed_history(self, ticker, nav):
        """Seed price history with ~15 days of data on first run.

        Without this, change/volume alerts return None on a fresh install and
        never trigger until the monitor has accumulated data across multiple days.
        """
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period='15d')
            if hist.empty:
                return
            print(f"      Seeding {len(hist)} day(s) of history for {ticker}")
            for ts, row in hist.iterrows():
                date_str = ts.strftime('%Y-%m-%d')
                p = float(row['Close'])
                v = int(row['Volume'])
                prem = ((p - nav) / nav) * 100 if nav else None
                self.history.add_price_point(ticker, date_str, p, v, nav, prem)
        except Exception as e:
            print(f"      Could not seed history for {ticker}: {e}")

    def get_avg_volume_10d(self, ticker: str):
        """Fetch yfinance's native 10-day average daily volume from .info.
        Field name varies slightly across yfinance versions — tries both.
        Returns an int or None on failure (caller should fall back to local 7-day avg)."""
        try:
            info = yf.Ticker(ticker).info
            avg = info.get('averageDailyVolume10Day') or info.get('averageVolume10days')
            return int(avg) if avg and avg > 0 else None
        except Exception as e:
            print(f"      Could not fetch 10d avg volume for {ticker}: {e}")
            return None

    def process_stock(self, user_id, stock):
        """Process one stock for one user"""
        ticker = stock['ticker']
        stock_id = stock['id']
        nav = stock.get('nav', 0)

        print(f"    Checking {ticker}...")

        price, volume, trade_date = self.get_stock_data(ticker)
        if not price:
            print(f"      Failed to fetch")
            return None

        premium = ((price - nav) / nav) * 100 if nav else None

        # Seed history on first run so that change/volume alerts work immediately.
        # Without this, a fresh install has only 1 day of data and all change
        # calculations return None until the monitor has run on multiple trading days.
        history_data = self.history.load(ticker)
        if len(history_data.get('daily_prices', [])) < 2:
            self._seed_history(ticker, nav)

        # Save history using the actual trade date from yfinance, not datetime.now().
        # This prevents phantom entries on weekends/holidays where the monitor keeps
        # running but yfinance returns the last trading day's data.
        self.history.add_price_point(ticker, trade_date, price, volume, nav, premium)

        # --- Extended hours price (pre-market / after-hours) ---
        session = get_market_session(ticker)
        display_price = price
        price_label = None
        if session in ('pre_market', 'after_hours') or (session == 'closed' and not is_market_weekend(ticker)):
            ext_price, price_label = get_extended_hours_price(ticker, session)
            if ext_price:
                display_price = ext_price
                print(f"      Extended ({price_label}): ${ext_price:.2f}")

        # Compute premium using display_price
        display_premium = ((display_price - nav) / nav) * 100 if nav else None

        # Compute 1D/7D changes relative to extended hours price when applicable
        if display_price != price:
            prices = self.history.load(ticker).get('daily_prices', [])
            if price_label == 'after-hours':
                # history[-1] = today's regular close (just stored), [-2] = yesterday
                ref_1d = prices[-2]['price'] if len(prices) >= 2 else None
                ref_7d = prices[-8]['price'] if len(prices) >= 8 else None
            else:  # pre-market — history[-1] = yesterday's close (today hasn't opened)
                ref_1d = prices[-1]['price'] if len(prices) >= 1 else None
                ref_7d = prices[-8]['price'] if len(prices) >= 8 else None
            change_1d = ((display_price - ref_1d) / ref_1d * 100) if ref_1d else None
            change_7d = ((display_price - ref_7d) / ref_7d * 100) if ref_7d else None
        else:
            change_1d = self.history.get_price_change_1d(ticker)
            change_7d = self.history.get_price_change_7d(ticker)

        # Use yfinance 10-day average volume directly; fall back to self-computed 7-day
        vol_avg = self.get_avg_volume_10d(ticker)
        if vol_avg is None:
            vol_avg = self.history.get_volume_average_7d(ticker)
            if vol_avg:
                print(f"      10d avg unavailable — using local 7d avg: {vol_avg:,}")

        # --- Smart volume ratio (pro-rata aware) ---
        is_market_open, elapsed_hours, total_hours, prorata_factor = \
            get_market_elapsed_info(ticker)

        if is_market_weekend(ticker):
            # No trading on weekends — skip volume check to avoid spurious alerts
            vol_ratio = None
            print(f"      Volume check skipped: weekend in {get_market_key(ticker)} timezone")
        elif is_market_open and elapsed_hours < VOLUME_GRACE_PERIOD_HOURS:
            # Too early in the session – don't compare yet; avoid false low-volume alerts
            vol_ratio = None
            print(f"      Volume check skipped: market open {elapsed_hours:.1f}h "
                  f"(grace period = {VOLUME_GRACE_PERIOD_HOURS}h)")
        elif is_market_open and vol_avg and vol_avg > 0 and prorata_factor > 0:
            # Scale the 10-day average to how much of the day has elapsed
            prorata_avg = vol_avg * prorata_factor
            vol_ratio = (volume / prorata_avg) if volume else None
            print(f"      Volume: {volume:,} vs pro-rata 10d avg {prorata_avg:,.0f} "
                  f"({elapsed_hours:.1f}h / {total_hours}h = {prorata_factor:.2f}x)")
        else:
            # Market closed on a weekday – yfinance has the full-day volume; compare normally
            vol_ratio = (volume / vol_avg) if (volume and vol_avg and vol_avg > 0) else None

        premium_str = f"{display_premium:.1f}%" if display_premium is not None else "N/A"
        label_log = f" ({price_label})" if price_label else ""
        print(f"      ${display_price:.2f}{label_log} | Premium: {premium_str}")

        # Check alerts (use display_price/display_premium so extended hours price drives alerts)
        state_changes = self.check_stock_alerts(
            user_id, stock, display_price, nav, display_premium,
            change_1d, change_7d, vol_ratio
        )

        if state_changes:
            print(f"      🔔 {len(state_changes)} state change(s)")
            for change in state_changes:
                alert = change['alert']
                if change['new_state']:
                    print(f"        ⚠️ TRIGGERED: {alert['description']}")
                    self.send_alert_triggered(
                        user_id, ticker, alert, change['value'], display_price, nav, price_label
                    )
                else:
                    print(f"        ✅ CLEARED: {alert['description']}")
                    self.send_alert_cleared(
                        user_id, ticker, alert, change['value']
                    )

        # Return summary for heartbeat
        alerts_with_states = []
        for alert in stock.get('alerts', []):
            if alert.get('enabled', True):
                alert_key = f"{stock_id}_{alert['id']}"
                triggered = self.alert_states.get_state(user_id, alert_key)
                alerts_with_states.append({
                    'description': alert['description'],
                    'triggered': triggered
                })

        return {
            'ticker': ticker,
            'price': display_price,
            'price_label': price_label,
            'nav': nav,
            'premium': display_premium,
            'change_1d': change_1d,
            'change_7d': change_7d,
            'vol_ratio': vol_ratio,
            'alerts': alerts_with_states
        }

    def process_user(self, user_config):
        """Process one user and all their stocks"""
        try:
            user_id = user_config.get('user_id')
            stocks = user_config.get('stocks', [])
            heartbeat_freq = user_config.get('heartbeat_frequency', 3)

            if not stocks:
                print(f"\n[User {user_id}] No stocks")
                return

            print(f"\n[User {user_id}] {len(stocks)} stock(s)")

            summaries = []
            for stock in stocks:
                summary = self.process_stock(user_id, stock)
                if summary:
                    summaries.append(summary)

            # Heartbeat
            if summaries and self.should_send_heartbeat(user_id, heartbeat_freq):
                print(f"  💓 Sending heartbeat (every {heartbeat_freq}h)")
                self.send_heartbeat(user_id, summaries, user_config)

        except Exception as e:
            print(f"Error processing user {user_id}: {e}")

    def run(self):
        """Main loop"""
        print("📈 Stock Monitor Bot - Multi-Stock Version")
        print("=" * 60)

        while True:
            try:
                print(f"\n[{datetime.now()}] Monitoring...")

                users = self.get_all_users()

                if not users:
                    print("No users yet")
                else:
                    print(f"Monitoring {len(users)} user(s)")

                    for user_config in users:
                        self.process_user(user_config)

                print(f"\nNext check in {CHECK_INTERVAL}s...")
                time.sleep(CHECK_INTERVAL)

            except KeyboardInterrupt:
                print("\n\n🛑 Stopped")
                break
            except Exception as e:
                print(f"\n❌ Error: {e}")
                print("Retrying in 5 min...")
                time.sleep(300)


if __name__ == "__main__":
    monitor = MultiStockMonitor(
        CONFIG_DIR, PRICE_HISTORY_DIR, ALERT_STATE_DIR, MAIN_CONFIG_FILE
    )
    monitor.run()
