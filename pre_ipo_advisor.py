#!/usr/bin/env python3
"""
Pre-IPO Listing Advisor Module — Stock Monitor Bot
Compares a newly-listed (or soon-to-list) stock against DXYZ (Destiny Tech100)
post-listing price behaviour to identify lifecycle stage, calibrate alerts,
and recommend position sizing for sell signals.

TEMPORARY MODULE — designed to be standalone and easily deleted once the
target stock's post-listing lifecycle is no longer relevant.

Designed to be called from telegram_bot_multistock.py via asyncio.to_thread()
so it does not block the bot's async event loop.
"""

import json
import math
import requests
import yfinance as yf
from datetime import datetime, timedelta


OPENROUTER_URL     = "https://openrouter.ai/api/v1/chat/completions"
DXYZ_TICKER        = "DXYZ"
PRE_IPO_AI_MODEL   = "openai/gpt-5-mini"
PRE_IPO_AI_TIMEOUT = 120   # seconds — generous; DXYZ dataset is larger than a single stock

# Alert types the bot supports (must stay in sync with the main bot)
VALID_ALERT_TYPES     = {"premium", "price_1d", "price_7d", "volume"}
VALID_ALERT_OPERATORS = {">", "<"}


# ─────────────────────────────────────────────
# Standalone helpers (copied from ai_advisor.py so this module is self-contained)
# ─────────────────────────────────────────────

def _get_benchmark(ticker: str) -> str:
    return "^HSI" if ticker.upper().endswith(".HK") else "SPY"


def _fmt_pct(val, plus=True) -> str:
    """Format a percentage value, or return 'N/A'."""
    if val is None:
        return "N/A"
    sign = "+" if plus and val > 0 else ""
    return f"{sign}{val:.2f}%"


def _fmt_price(val, decimals=4) -> str:
    """Format a price, or return 'N/A'."""
    if val is None:
        return "N/A"
    return f"${val:.{decimals}f}"


def _ma_note(price, ma, label) -> str:
    """Return a one-line moving-average summary with a directional note."""
    if price is None or ma is None:
        return f"{label}: N/A"
    diff_pct = ((price - ma) / ma) * 100
    direction = "ABOVE ↑" if diff_pct > 0 else "BELOW ↓"
    return f"{label}: {_fmt_price(ma)}  (price {_fmt_pct(diff_pct)} {direction})"


# ─────────────────────────────────────────────
# Main class
# ─────────────────────────────────────────────

class PreIpoAdvisor:
    """
    Orchestrates DXYZ-anchored post-listing lifecycle analysis.
    Primary emphasis: compare target stock against DXYZ's full post-listing history.
    Supplemental: news, retail sentiment, macro.
    """

    def __init__(self, api_key: str):
        self.api_key = api_key

    # ── Sentiment helpers (identical to AIAdvisor; prefix changed for log clarity) ──

    def _get_stocktwits_sentiment(self, ticker: str) -> dict:
        try:
            st_ticker = ticker.replace(".HK", "").upper()
            url = f"https://api.stocktwits.com/api/2/streams/symbol/{st_ticker}.json"
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                return {}
            messages = resp.json().get("messages", [])
            if not messages:
                return {"total_messages": 0, "bullish": 0, "bearish": 0,
                        "sentiment_ratio": "no messages found", "samples": []}
            bullish, bearish = 0, 0
            samples = []
            for msg in messages[:30]:
                sentiment = (msg.get("entities") or {}).get("sentiment") or {}
                basic = sentiment.get("basic", "")
                if basic == "Bullish":
                    bullish += 1
                elif basic == "Bearish":
                    bearish += 1
                body = (msg.get("body") or "").strip()
                if body and len(samples) < 5:
                    label = f" [{basic}]" if basic else ""
                    samples.append(f'"{body[:120]}"{label}')
            total_with_sentiment = bullish + bearish
            ratio = (f"{bullish}/{total_with_sentiment} bullish"
                     if total_with_sentiment else "no sentiment labels")
            return {
                "total_messages": len(messages),
                "bullish": bullish,
                "bearish": bearish,
                "sentiment_ratio": ratio,
                "samples": samples,
            }
        except Exception as e:
            print(f"[PreIpoAdvisor] StockTwits fetch failed for {ticker}: {e}")
            return {}

    def _get_google_trends(self, ticker: str, company_name: str = "") -> dict:
        try:
            from pytrends.request import TrendReq
            pt = TrendReq(hl="en-US", tz=0, timeout=(10, 25))
            keywords_to_try = [ticker]
            if company_name and company_name != ticker:
                keywords_to_try.append(company_name[:50])
            for keyword in keywords_to_try:
                try:
                    pt.build_payload([keyword], timeframe="today 3-m", geo="")
                    df = pt.interest_over_time()
                    if df is None or df.empty or keyword not in df.columns:
                        continue
                    values = [int(v) for v in df[keyword].tolist()]
                    if sum(values) == 0:
                        continue
                    recent_avg = sum(values[-4:]) / 4 if len(values) >= 4 else sum(values) / len(values)
                    older_avg  = sum(values[-8:-4]) / 4 if len(values) >= 8 else None
                    if older_avg and older_avg > 0:
                        trend_pct = ((recent_avg - older_avg) / older_avg) * 100
                        trend_dir = ("rising ↑" if trend_pct > 10
                                     else "falling ↓" if trend_pct < -10
                                     else "stable →")
                        trend_str = f"{trend_pct:+.0f}% ({trend_dir})"
                    else:
                        trend_str = "insufficient history"
                    return {
                        "keyword_used":     keyword,
                        "current_interest": round(recent_avg, 1),
                        "trend_4w":         trend_str,
                        "peak_3m":          max(values),
                        "weekly_values":    values[-12:],
                    }
                except Exception as e:
                    print(f"[PreIpoAdvisor] Google Trends failed for '{keyword}': {e}")
                    continue
            return {}
        except ImportError:
            print("[PreIpoAdvisor] pytrends not installed — run: pip install pytrends")
            return {}
        except Exception as e:
            print(f"[PreIpoAdvisor] Google Trends fetch failed for {ticker}: {e}")
            return {}

    def _get_reddit_sentiment(self, ticker: str) -> dict:
        if ticker.upper().endswith(".HK"):
            return {"skipped": True, "reason": "HK ticker — insufficient Reddit coverage"}
        try:
            subreddits = ["investing", "stocks", "wallstreetbets"]
            headers = {"User-Agent": "Mozilla/5.0 (compatible; StockMonitorBot/1.0; personal use)"}
            all_posts = []
            for sub in subreddits:
                try:
                    url = (f"https://www.reddit.com/r/{sub}/search.json"
                           f"?q={ticker}&sort=new&limit=5&restrict_sr=1")
                    resp = requests.get(url, headers=headers, timeout=5)
                    if resp.status_code != 200:
                        continue
                    children = resp.json().get("data", {}).get("children", [])
                    for post in children:
                        d = post.get("data", {})
                        title = (d.get("title") or "").strip()
                        if not title:
                            continue
                        all_posts.append({
                            "subreddit":    sub,
                            "title":        title[:150],
                            "score":        d.get("score", 0),
                            "upvote_ratio": round(d.get("upvote_ratio", 0), 2),
                            "num_comments": d.get("num_comments", 0),
                        })
                except Exception as e:
                    print(f"[PreIpoAdvisor] Reddit fetch failed for r/{sub}: {e}")
                    continue
            if not all_posts:
                return {"total_posts": 0, "posts": [],
                        "note": "No ticker-specific posts found on Reddit"}
            all_posts.sort(key=lambda x: x["score"], reverse=True)
            return {"total_posts": len(all_posts), "posts": all_posts[:10]}
        except Exception as e:
            print(f"[PreIpoAdvisor] Reddit sentiment fetch failed for {ticker}: {e}")
            return {}

    # ── 1. DXYZ reference data ──────────────────

    def _fetch_dxyz_data(self) -> dict:
        """
        Fetch DXYZ's full post-listing history from yfinance.
        DXYZ (Destiny Tech100) listed ~March 26 2024 and is used as a lifecycle
        reference for newly-listed closed-end / pre-IPO vehicle stocks.
        """
        try:
            stock_obj = yf.Ticker(DXYZ_TICKER)
            hist = stock_obj.history(period="max")

            if hist is None or hist.empty:
                print(f"[PreIpoAdvisor] yfinance returned no data for {DXYZ_TICKER}")
                return {}

            closes  = hist["Close"]
            volumes = hist["Volume"]

            # ── Listing metadata ──
            listing_ts          = hist.index[0]
            listing_date        = listing_ts.strftime("%Y-%m-%d")
            listing_open_price  = round(float(hist.iloc[0]["Open"]), 4)
            listing_close_price = round(float(hist.iloc[0]["Close"]), 4)
            today               = datetime.now().date()
            days_since_listing  = (today - listing_ts.date()).days
            trading_days        = len(hist)

            # ── Current price ──
            current_price = round(float(closes.iloc[-1]), 4)

            # ── ATH ──
            ath_idx      = closes.idxmax()
            ath_price    = round(float(closes[ath_idx]), 4)
            ath_date     = ath_idx.strftime("%Y-%m-%d")
            days_to_ath  = (ath_idx.date() - listing_ts.date()).days
            drawdown_pct = round(((current_price - ath_price) / ath_price) * 100, 2)

            # ── Price at calendar-day checkpoints after listing ──
            def price_at_day(n):
                target_date = listing_ts + timedelta(days=n)
                future = hist[hist.index >= target_date]
                if future.empty:
                    return None
                return round(float(future.iloc[0]["Close"]), 4)

            price_day_7   = price_at_day(7)
            price_day_30  = price_at_day(30)
            price_day_60  = price_at_day(60)
            price_day_90  = price_at_day(90)
            price_day_180 = price_at_day(180)
            price_day_365 = price_at_day(365)

            # ── Volume stats ──
            listing_day_vol    = int(volumes.iloc[0])
            avg_vol_10d_post   = (int(volumes.iloc[1:11].mean())
                                  if len(volumes) > 10 else None)
            avg_vol_30d_curr   = (int(volumes.tail(30).mean())
                                  if len(volumes) >= 30 else None)

            # ── Monthly summary ──
            monthly_summary = []
            try:
                try:
                    monthly_series = closes.resample("ME").last()
                except Exception:
                    monthly_series = closes.resample("M").last()
                monthly_summary = [
                    {"month": idx.strftime("%Y-%m"), "close": round(float(v), 4)}
                    for idx, v in monthly_series.items()
                    if not math.isnan(float(v))
                ]
            except Exception as e:
                print(f"[PreIpoAdvisor] DXYZ monthly resample failed: {e}")

            # ── Full OHLCV since listing ──
            full_daily_ohlcv = [
                {
                    "date":   idx.strftime("%Y-%m-%d"),
                    "open":   round(float(row["Open"]),  4),
                    "high":   round(float(row["High"]),  4),
                    "low":    round(float(row["Low"]),   4),
                    "close":  round(float(row["Close"]), 4),
                    "volume": int(row["Volume"]),
                }
                for idx, row in hist.iterrows()
            ]

            print(f"[PreIpoAdvisor] DXYZ data fetched: {trading_days} trading days "
                  f"since listing ({listing_date})")

            return {
                "ticker":                    DXYZ_TICKER,
                "listing_date":              listing_date,
                "listing_open_price":        listing_open_price,
                "listing_close_price":       listing_close_price,
                "days_since_listing":        days_since_listing,
                "trading_days_since_listing": trading_days,
                "current_price":             current_price,
                "ath_price":                 ath_price,
                "ath_date":                  ath_date,
                "days_to_ath":               days_to_ath,
                "drawdown_from_ath_pct":     drawdown_pct,
                "price_day_7":               price_day_7,
                "price_day_30":              price_day_30,
                "price_day_60":              price_day_60,
                "price_day_90":              price_day_90,
                "price_day_180":             price_day_180,
                "price_day_365":             price_day_365,
                "listing_day_volume":        listing_day_vol,
                "avg_vol_10d_post_listing":  avg_vol_10d_post,
                "avg_vol_30d_current":       avg_vol_30d_curr,
                "monthly_summary":           monthly_summary,
                "full_daily_ohlcv":          full_daily_ohlcv,
                "fetch_time":                datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            }

        except Exception as e:
            print(f"[PreIpoAdvisor] DXYZ data fetch failed: {e}")
            return {}

    # ── 2. Target stock data ────────────────────

    def _fetch_target_data(self, ticker: str, nav: float, signal_type: str,
                           history_data: dict) -> dict:
        """
        Gather target stock data — streamlined version of AIAdvisor.prepare_context().
        Drops signal_type from the returned dict fields but passes it through the context.
        Adds days_since_own_listing derived from the earliest date in yfinance history.
        """
        nav_applicable = nav > 0
        stock_obj = yf.Ticker(ticker)

        # ── Primary: 1-year daily OHLCV ──
        hist_1y = None
        try:
            hist_1y = stock_obj.history(period="1y")
            if hist_1y is not None and hist_1y.empty:
                hist_1y = None
        except Exception as e:
            print(f"[PreIpoAdvisor] yfinance 1y fetch failed for {ticker}: {e}")

        current_price = current_vol = avg_vol_30d = vol_ratio = None
        week_52_high = week_52_low = None
        ma_20 = ma_50 = ma_200 = None
        change_1d = change_1m = change_3m = change_6m = change_1y_pct = None
        volatility_20d = None
        monthly_summary = []
        recent_60d_daily = []
        data_days = 0
        days_since_own_listing = None

        if hist_1y is not None:
            closes  = hist_1y["Close"]
            volumes = hist_1y["Volume"]
            data_days = len(closes)

            current_price = float(closes.iloc[-1])
            week_52_high  = round(float(closes.max()), 4)
            week_52_low   = round(float(closes.min()), 4)

            # Days since own listing (first date in the 1y window — proxy for listing age)
            try:
                days_since_own_listing = (
                    datetime.now().date() - hist_1y.index[0].date()
                ).days
            except Exception:
                pass

            # ── Moving averages ──
            if data_days >= 20:
                ma_20  = round(float(closes.tail(20).mean()),  4)
            if data_days >= 50:
                ma_50  = round(float(closes.tail(50).mean()),  4)
            if data_days >= 200:
                ma_200 = round(float(closes.tail(200).mean()), 4)

            # ── Price changes ──
            def _pct_chg(n):
                if data_days >= n + 1:
                    return round(((current_price - float(closes.iloc[-n - 1]))
                                  / float(closes.iloc[-n - 1])) * 100, 2)
                return None

            change_1d     = _pct_chg(1)
            change_1m     = _pct_chg(21)
            change_3m     = _pct_chg(63)
            change_6m     = _pct_chg(126)
            change_1y_pct = round(
                ((current_price - float(closes.iloc[0])) / float(closes.iloc[0])) * 100, 2
            ) if data_days >= 2 else None

            # ── Volume ──
            current_vol = int(volumes.iloc[-1])
            if data_days >= 30:
                avg_vol_30d = int(volumes.tail(30).mean())
                vol_ratio   = round(current_vol / avg_vol_30d, 2) if avg_vol_30d > 0 else None

            # ── Annualised volatility ──
            returns = closes.pct_change().dropna()
            if len(returns) >= 20:
                volatility_20d = round(float(returns.tail(20).std()
                                             * math.sqrt(252) * 100), 1)

            # ── Monthly summary ──
            try:
                try:
                    monthly_series = closes.resample("ME").last()
                except Exception:
                    monthly_series = closes.resample("M").last()
                monthly_summary = [
                    {"month": idx.strftime("%Y-%m"), "close": round(float(v), 4)}
                    for idx, v in monthly_series.items()
                    if not math.isnan(float(v))
                ]
            except Exception as e:
                print(f"[PreIpoAdvisor] Monthly resample failed for {ticker}: {e}")

            # ── Last 60 trading days (OHLCV) ──
            recent = hist_1y.tail(60)
            recent_60d_daily = [
                {
                    "date":   idx.strftime("%Y-%m-%d"),
                    "open":   round(float(row["Open"]),  4),
                    "high":   round(float(row["High"]),  4),
                    "low":    round(float(row["Low"]),   4),
                    "close":  round(float(row["Close"]), 4),
                    "volume": int(row["Volume"]),
                }
                for idx, row in recent.iterrows()
            ]

        else:
            # Fallback: local 30-day history
            daily_local = history_data.get("daily_prices", [])[-30:]
            if daily_local:
                current_price = daily_local[-1]["price"]
                data_days     = len(daily_local)
                if data_days >= 2:
                    change_1d = round(
                        ((current_price - daily_local[-2]["price"])
                         / daily_local[-2]["price"]) * 100, 2
                    )
                recent_60d_daily = [
                    {"date": d["date"], "close": d["price"], "volume": d.get("volume")}
                    for d in daily_local
                ]
                print(f"[PreIpoAdvisor] yfinance unavailable — "
                      f"using {data_days} days of local history for {ticker}")

        # ── Current premium ──
        current_premium = None
        if nav_applicable and current_price is not None:
            current_premium = round(((current_price - nav) / nav) * 100, 2)

        # ── Historical premium from local data ──
        premium_history = []
        if nav_applicable:
            local_daily = history_data.get("daily_prices", [])
            premium_history = [
                {"date": d["date"], "price": d["price"],
                 "premium_pct": round(d["premium"], 1)}
                for d in local_daily
                if d.get("premium") is not None
            ][-60:]

        # ── News headlines ──
        news_headlines = []
        try:
            raw_news = getattr(stock_obj, "news", None) or []
            for item in raw_news[:15]:
                title = ((item.get("content", {}) or {}).get("title")
                         or item.get("title", ""))
                pub   = (item.get("content", {}) or {}).get("provider", {})
                publisher = pub.get("displayName", "") if isinstance(pub, dict) else ""
                if title:
                    news_headlines.append(
                        title + (f" ({publisher})" if publisher else "")
                    )
        except Exception as e:
            print(f"[PreIpoAdvisor] News fetch failed for {ticker}: {e}")

        # ── Benchmark ──
        benchmark_ticker = _get_benchmark(ticker)
        benchmark_1y_pct = None
        try:
            bm = yf.Ticker(benchmark_ticker).history(period="1y")
            if not bm.empty and len(bm) >= 2:
                benchmark_1y_pct = round(
                    ((float(bm.iloc[-1]["Close"]) - float(bm.iloc[0]["Close"]))
                     / float(bm.iloc[0]["Close"])) * 100, 2
                )
        except Exception as e:
            print(f"[PreIpoAdvisor] Benchmark fetch failed: {e}")

        # ── Stock info ──
        stock_info = {}
        try:
            info = stock_obj.info or {}
            for field in ("longName", "sector", "industry", "beta",
                          "trailingPE", "marketCap", "currency"):
                if info.get(field) is not None:
                    stock_info[field] = info[field]
        except Exception as e:
            print(f"[PreIpoAdvisor] Stock info fetch failed for {ticker}: {e}")

        # ── Retail sentiment ──
        company_name = stock_info.get("longName", "")
        print(f"[PreIpoAdvisor] Fetching retail sentiment for {ticker}...")
        stocktwits_data = self._get_stocktwits_sentiment(ticker)
        google_trends   = self._get_google_trends(ticker, company_name)
        reddit_data     = self._get_reddit_sentiment(ticker)

        return {
            "ticker":               ticker,
            "nav":                  nav,
            "nav_applicable":       nav_applicable,
            "signal_type":          signal_type.upper(),
            "data_days":            data_days,
            "days_since_own_listing": days_since_own_listing,
            "current_price":        current_price,
            "current_premium":      current_premium,
            "week_52_high":         week_52_high,
            "week_52_low":          week_52_low,
            "ma_20":                ma_20,
            "ma_50":                ma_50,
            "ma_200":               ma_200,
            "change_1d_pct":        change_1d,
            "change_1m_pct":        change_1m,
            "change_3m_pct":        change_3m,
            "change_6m_pct":        change_6m,
            "change_1y_pct":        change_1y_pct,
            "current_vol":          current_vol,
            "avg_vol_30d":          avg_vol_30d,
            "vol_ratio":            vol_ratio,
            "volatility_20d":       volatility_20d,
            "monthly_summary":      monthly_summary,
            "recent_60d_daily":     recent_60d_daily,
            "premium_history":      premium_history,
            "news_headlines":       news_headlines,
            "benchmark_ticker":     benchmark_ticker,
            "benchmark_1y_pct":     benchmark_1y_pct,
            "stock_info":           stock_info,
            "stocktwits":           stocktwits_data,
            "google_trends":        google_trends,
            "reddit":               reddit_data,
            "analysis_time":        datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        }

    # ── 3. Prompt construction ──────────────────

    def build_prompt(self, dxyz_ctx: dict, target_ctx: dict) -> str:
        cp  = target_ctx["current_price"]
        sig = target_ctx["signal_type"]

        # ── NAV / premium block ──
        if target_ctx["nav_applicable"]:
            nav_block = (
                f"NAV                : {_fmt_price(target_ctx['nav'])}\n"
                f"Current premium    : {_fmt_pct(target_ctx['current_premium'], plus=True)}"
                f"  (formula: (Price − NAV) / NAV × 100)"
            )
        else:
            nav_block = "NAV: N/A — do NOT suggest premium-type alerts"

        # ── 52-week range ──
        if cp and target_ctx["week_52_high"] and target_ctx["week_52_low"]:
            pct_from_high = ((cp - target_ctx["week_52_high"]) / target_ctx["week_52_high"]) * 100
            pct_from_low  = ((cp - target_ctx["week_52_low"])  / target_ctx["week_52_low"])  * 100
            range_block = (
                f"52-week high       : {_fmt_price(target_ctx['week_52_high'])}"
                f"  (current {_fmt_pct(pct_from_high)} from high)\n"
                f"52-week low        : {_fmt_price(target_ctx['week_52_low'])}"
                f"  (current {_fmt_pct(pct_from_low, plus=True)} from low)"
            )
        else:
            range_block = "52-week range: N/A"

        # ── Moving averages ──
        ma_block = "\n".join([
            _ma_note(cp, target_ctx["ma_20"],  "20-day SMA "),
            _ma_note(cp, target_ctx["ma_50"],  "50-day SMA "),
            _ma_note(cp, target_ctx["ma_200"], "200-day SMA"),
        ])

        # ── Price changes ──
        changes_block = (
            f"1-day   : {_fmt_pct(target_ctx['change_1d_pct'])}\n"
            f"1-month : {_fmt_pct(target_ctx['change_1m_pct'])}\n"
            f"3-month : {_fmt_pct(target_ctx['change_3m_pct'])}\n"
            f"6-month : {_fmt_pct(target_ctx['change_6m_pct'])}\n"
            f"1-year  : {_fmt_pct(target_ctx['change_1y_pct'])}"
        )

        # ── Volume ──
        vol_block = (
            f"Latest session  : {target_ctx['current_vol']:,}\n"
            f"30-day average  : {target_ctx['avg_vol_30d']:,}\n"
            f"Ratio vs avg    : {target_ctx['vol_ratio']}x"
            if target_ctx["current_vol"] and target_ctx["avg_vol_30d"]
            else "Volume data: N/A"
        )

        # ── Benchmark ──
        bm_block = (
            f"{target_ctx['benchmark_ticker']} 1-year return: "
            f"{_fmt_pct(target_ctx['benchmark_1y_pct'])}"
        )

        # ── News ──
        news_block = (
            "\n".join(f"  - {h}" for h in target_ctx["news_headlines"])
            if target_ctx["news_headlines"] else "  (No recent news available)"
        )

        # ── Stock info ──
        info_lines = "\n".join(f"  {k}: {v}"
                               for k, v in target_ctx["stock_info"].items()) or "  (N/A)"

        # ── Retail sentiment ──
        st = target_ctx["stocktwits"]
        if st:
            st_line = (f"Messages fetched: {st['total_messages']} | "
                       f"Sentiment: {st['sentiment_ratio']}")
            samples_text = "\n".join(f"    {s}" for s in st.get("samples", []))
            stocktwits_block = (
                f"StockTwits ({target_ctx['ticker'].replace('.HK','')})\n"
                f"  {st_line}"
                + (f"\n  Sample messages:\n{samples_text}" if samples_text else ""))
        else:
            stocktwits_block = "StockTwits: fetch failed or unavailable"

        gt = target_ctx["google_trends"]
        gt_block = (
            f"Google Trends (keyword: '{gt['keyword_used']}'):\n"
            f"  Current interest: {gt['current_interest']}/100  |  "
            f"4-week trend: {gt['trend_4w']}  |  "
            f"3-month peak: {gt['peak_3m']}/100"
        ) if gt else "Google Trends: no data (pytrends unavailable or blocked)"

        rd = target_ctx.get("reddit", {})
        if rd.get("skipped"):
            reddit_block = f"Reddit: skipped ({rd.get('reason', 'HK ticker')})"
        elif not rd or rd.get("total_posts", 0) == 0:
            reddit_block = "Reddit: no ticker-specific posts found (or fetch blocked)"
        else:
            post_lines = "\n".join(
                f"  [{p['subreddit']}] {p['title']}  "
                f"(score: {p['score']}, upvotes: {int(p['upvote_ratio']*100)}%, "
                f"comments: {p['num_comments']})"
                for p in rd["posts"]
            )
            reddit_block = (
                f"Reddit — {rd['total_posts']} recent posts found, "
                f"top {len(rd['posts'])} shown:\n" + post_lines
            )

        # ── Premium history ──
        premium_section = ""
        if target_ctx["nav_applicable"] and target_ctx["premium_history"]:
            premium_section = (
                f"\n════════════════════════════════\n"
                f"TARGET STOCK — HISTORICAL PREMIUM TO NAV "
                f"(last {len(target_ctx['premium_history'])} monitor entries)\n"
                f"════════════════════════════════\n"
                + json.dumps(target_ctx["premium_history"], indent=2)
            )

        # ── DXYZ monthly / full OHLCV ──
        dxyz_monthly_json = (
            json.dumps(dxyz_ctx.get("monthly_summary", []), indent=2)
            or "  (Not available)"
        )
        dxyz_ohlcv_json = (
            json.dumps(dxyz_ctx.get("full_daily_ohlcv", []), indent=2)
            or "  (Not available)"
        )

        # ── Target stock monthly / 60d OHLCV ──
        target_monthly_json = (
            json.dumps(target_ctx["monthly_summary"], indent=2)
            if target_ctx["monthly_summary"] else "  (Not available)"
        )
        target_recent_json = (
            json.dumps(target_ctx["recent_60d_daily"], indent=2)
            if target_ctx["recent_60d_daily"] else "  (Not available)"
        )

        # ── Days since own listing ──
        own_listing_str = (
            f"{target_ctx['days_since_own_listing']} calendar days"
            if target_ctx.get("days_since_own_listing") is not None
            else "Unknown (yfinance data may predate listing)"
        )

        # ── DXYZ price timeline block ──
        def fmt_checkpoint(label, price, reference=None):
            if price is None:
                return f"  {label}: N/A"
            chg = ""
            if reference:
                chg = f"  ({_fmt_pct(((price - reference) / reference) * 100, plus=True)} vs listing open)"
            return f"  {label}: {_fmt_price(price)}{chg}"

        ref = dxyz_ctx.get("listing_open_price")
        dxyz_timeline = "\n".join([
            fmt_checkpoint("Listing open  (Day 0)", ref),
            fmt_checkpoint("Listing close (Day 0)", dxyz_ctx.get("listing_close_price"), ref),
            fmt_checkpoint("Day 7",   dxyz_ctx.get("price_day_7"),   ref),
            fmt_checkpoint("Day 30",  dxyz_ctx.get("price_day_30"),  ref),
            fmt_checkpoint("Day 60",  dxyz_ctx.get("price_day_60"),  ref),
            fmt_checkpoint("Day 90",  dxyz_ctx.get("price_day_90"),  ref),
            fmt_checkpoint("Day 180", dxyz_ctx.get("price_day_180"), ref),
            fmt_checkpoint("Day 365", dxyz_ctx.get("price_day_365"), ref),
            fmt_checkpoint(f"Current ({dxyz_ctx.get('days_since_listing', '?')} calendar days post-listing)",
                           dxyz_ctx.get("current_price"), ref),
        ])

        # ── Position sizing instruction (SELL only) ──
        if sig == "SELL":
            position_sizing_instruction = """
POSITION SIZING (required for SELL signal):
You MUST include a "position_sizing" object in your response with:
  - "sell_pct_now" : integer 0–100 — what % of the position to sell immediately
  - "rationale"    : string — derive this from DXYZ's actual post-listing phases.
                     Identify which DXYZ lifecycle phase the target stock is currently in,
                     what DXYZ did next from that equivalent stage, and why the
                     recommended % is appropriate given that precedent.
  - "staged_approach" : string — when/how to sell the remaining position
                         (e.g. "Sell remaining 50% if premium exceeds X% or 7d change > Y%")
                         Tie thresholds to DXYZ's actual turning-point levels.

Think of this as a staged exit strategy anchored to DXYZ's lifecycle data.
"""
        else:
            position_sizing_instruction = (
                '(BUY signal — "position_sizing" is optional. '
                'You may include it to suggest how much to accumulate if you see '
                'a DXYZ-pattern-supported dip buy opportunity.)'
            )

        return f"""You are a financial analysis assistant specialising in post-IPO / post-listing
closed-end fund dynamics, with deep knowledge of DXYZ (Destiny Tech100) as a lifecycle reference.

Today: {target_ctx['analysis_time']}
User's signal intent: {sig}

════════════════════════════════
SECTION 1 — DXYZ REFERENCE DATA  ★ PRIMARY ANALYSIS EMPHASIS ★
This is your main analytical framework. Use DXYZ's actual post-listing history
to identify phases and derive calibrated thresholds for the target stock.
════════════════════════════════
Ticker         : {DXYZ_TICKER} (Destiny Tech100 — closed-end fund, listed pre-IPO tech holdings)
Listing date   : {dxyz_ctx.get('listing_date', 'N/A')}
Calendar days since listing : {dxyz_ctx.get('days_since_listing', 'N/A')}
Trading days of data        : {dxyz_ctx.get('trading_days_since_listing', 'N/A')}

POST-LISTING PRICE TIMELINE
{dxyz_timeline}

ALL-TIME HIGH (post-listing)
  ATH price    : {_fmt_price(dxyz_ctx.get('ath_price'))}
  ATH date     : {dxyz_ctx.get('ath_date', 'N/A')}
  Days to ATH from listing : {dxyz_ctx.get('days_to_ath', 'N/A')}
  Current drawdown from ATH: {_fmt_pct(dxyz_ctx.get('drawdown_from_ath_pct'))}

VOLUME PATTERNS
  Listing day volume        : {f"{dxyz_ctx.get('listing_day_volume', 0):,}" if dxyz_ctx.get('listing_day_volume') else 'N/A'}
  Avg volume days 1–10      : {f"{dxyz_ctx.get('avg_vol_10d_post_listing', 0):,}" if dxyz_ctx.get('avg_vol_10d_post_listing') else 'N/A'}
  Avg volume last 30d       : {f"{dxyz_ctx.get('avg_vol_30d_current', 0):,}" if dxyz_ctx.get('avg_vol_30d_current') else 'N/A'}

DXYZ MONTHLY CLOSE SUMMARY
{dxyz_monthly_json}

DXYZ FULL DAILY OHLCV (since listing — use this to identify key phases, turning points, and volume signals)
{dxyz_ohlcv_json}

════════════════════════════════
SECTION 2 — TARGET STOCK DATA
════════════════════════════════
Ticker             : {target_ctx['ticker']}
Days since own listing (approx): {own_listing_str}
{nav_block}
Current price      : {_fmt_price(cp)}

{range_block}

MOVING AVERAGES
{ma_block}

PRICE CHANGES
{changes_block}

VOLUME
{vol_block}

Annualised volatility (20-day): {f"{target_ctx['volatility_20d']}%" if target_ctx['volatility_20d'] else 'N/A'}

ADDITIONAL STOCK INFO
{info_lines}

TARGET STOCK — 12-MONTH PRICE SUMMARY
{target_monthly_json}

TARGET STOCK — RECENT 60-DAY DAILY DATA (OHLCV)
{target_recent_json}
{premium_section}

════════════════════════════════
SECTION 3 — SUPPLEMENTAL DATA (supporting context only)
════════════════════════════════
MARKET BENCHMARK
{bm_block}

RECENT NEWS (up to 15 headlines)
{news_block}

RETAIL SENTIMENT
{stocktwits_block}

{gt_block}

{reddit_block}

════════════════════════════════
SECTION 4 — BOT ALERT TYPES AVAILABLE
════════════════════════════════
1. "premium"  — (Price − NAV) / NAV × 100  {{operator}}  {{threshold}}%
   • Only valid when NAV is applicable (nav > 0)

2. "price_1d" — 1-day price change %  {{operator}}  {{threshold}}%

3. "price_7d" — 7-day price change %  {{operator}}  {{threshold}}%

4. "volume"   — today's volume / 7-day average volume  {{operator}}  {{threshold}}
   • Threshold is a multiplier (e.g. 2.5 = 2.5× average daily volume)

════════════════════════════════
SECTION 5 — YOUR ANALYTICAL TASK
════════════════════════════════
Step 1 — Analyse DXYZ lifecycle phases from the OHLCV data above:
  • Phase 1: Listing euphoria — how many days, how far above listing open?
  • Phase 2: ATH and premium peak — timing, price level, volume characteristics
  • Phase 3: Selloff — pace, magnitude, duration, key support levels
  • Phase 4: Current state — stabilisation, renewed decline, or recovery?

Step 2 — Map the target stock ({target_ctx['ticker']}) onto the DXYZ lifecycle:
  • Based on days since own listing and current price/volume/premium, which DXYZ
    phase does the target most closely resemble?
  • How many DXYZ-equivalent days into that phase does it appear to be?

Step 3 — Derive DXYZ-calibrated alert thresholds:
  • Use DXYZ's actual turning-point prices, premiums, and volume ratios as anchors
  • Alerts should fire at levels that would have been meaningful signals in DXYZ

{position_sizing_instruction}

Choose ONE output format:

FORMAT A — Conditions already clearly met (act now):
{{
  "recommendation_type": "immediate",
  "action": "BUY" | "SELL" | "HOLD",
  "message": "2–3 sentences: current situation and why immediate action",
  "summary": "2–3 sentences: which DXYZ lifecycle phase target resembles and key signal",
  "dxyz_pattern_analysis": "DXYZ lifecycle phases with dates, price levels, premium context, and volume patterns",
  "lifecycle_stage": "Which DXYZ phase target is in, how far along, key parallels and differences",
  "news_analysis": "2–3 sentences on relevant recent news",
  "sentiment_analysis": "2–3 sentences on StockTwits/Reddit/Google Trends",
  "macro_analysis": "Macro factors relevant to this stock",
  "position_sizing": {{   ← include if SELL (required) or BUY (optional)
    "sell_pct_now": <integer 0-100>,
    "rationale": "DXYZ-anchored justification for this percentage",
    "staged_approach": "When and how to sell/buy the remainder"
  }}
}}

FORMAT B — Set up monitoring alerts:
{{
  "recommendation_type": "alerts",
  "summary": "2–3 sentences: which DXYZ lifecycle phase target resembles and what to watch for",
  "dxyz_pattern_analysis": "DXYZ lifecycle phases with dates, price levels, premium context, and volume patterns",
  "lifecycle_stage": "Which DXYZ phase target is in, how far along, key parallels and differences",
  "news_analysis": "2–3 sentences on relevant recent news",
  "sentiment_analysis": "2–3 sentences on StockTwits/Reddit/Google Trends",
  "macro_analysis": "Macro factors relevant to this stock",
  "position_sizing": {{   ← include if SELL (required) or BUY (optional)
    "sell_pct_now": <integer 0-100>,
    "rationale": "DXYZ-anchored justification for this percentage",
    "staged_approach": "When and how to sell/buy the remainder (ties to the alert thresholds below)"
  }},
  "alerts": [
    {{
      "type": "price_1d | price_7d | premium | volume",
      "operator": "> or <",
      "threshold": <number>,
      "tier": "watch or action",
      "rationale": "One sentence: DXYZ-derived justification for this threshold"
    }}
  ]
}}

Alert rules:
  - Provide 2 to 6 alerts total; use a mix of watch and action tiers
  - Thresholds MUST be anchored to DXYZ's actual data (not generic percentages)
  - Do NOT suggest "premium" alerts if NAV is not applicable
  - For a {sig} signal: alerts should fire when conditions favour {sig.lower()}ing
  - The staged_approach in position_sizing should logically align with the action-tier alerts

════════════════════════════════
IMPORTANT
════════════════════════════════
Return ONLY valid JSON — no markdown fences, no comments, no text outside the JSON object.
"""

    # ── 4. Openrouter API call ──────────────────

    def call_openrouter(self, prompt: str) -> dict:
        """POST to Openrouter and return the parsed API response."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
            "HTTP-Referer":  "https://github.com/tg-bot-stock-monitoring",
            "X-Title":       "Stock Monitor Bot",
        }
        payload = {
            "model":       PRE_IPO_AI_MODEL,
            "messages":    [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens":  8000,
        }
        response = requests.post(OPENROUTER_URL, headers=headers,
                                 json=payload, timeout=PRE_IPO_AI_TIMEOUT)
        response.raise_for_status()
        return response.json()

    # ── 5. Response parsing & validation ───────

    def parse_and_validate(self, raw_response: dict, signal_type: str = "") -> dict:
        """
        Extract, parse, and validate the AI's JSON from the Openrouter response.
        For SELL signals, validates that position_sizing is present and well-formed.
        Raises ValueError with a readable message on any problem.
        """
        try:
            content = raw_response["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise ValueError(f"Unexpected API response structure: {e}")

        # Guard: token limit truncation
        finish_reason = raw_response.get("choices", [{}])[0].get("finish_reason", "")
        if finish_reason == "length":
            print(f"[PreIpoAdvisor] Response truncated (finish_reason=length). "
                  f"Partial content:\n{content}")
            raise ValueError("AI response was cut off (token limit reached). Please try again.")

        # Guard: empty content
        if not content or not content.strip():
            print(f"[PreIpoAdvisor] Empty content. Full response: {json.dumps(raw_response)}")
            raise ValueError("AI returned an empty response. Please try again.")

        # Strip markdown fences if present
        stripped = content.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            inner_lines = lines[1:] if lines[-1].strip() == "```" else lines[1:-1]
            stripped = "\n".join(inner_lines).strip()

        # Find JSON start
        json_start = stripped.find("{")
        if json_start == -1:
            print(f"[PreIpoAdvisor] No JSON object found. Raw content:\n{content}")
            raise ValueError("AI response contained no JSON object.")
        stripped = stripped[json_start:]

        try:
            result, _ = json.JSONDecoder().raw_decode(stripped)
        except json.JSONDecodeError as e:
            print(f"[PreIpoAdvisor] JSON parse failed. Raw content:\n{content}")
            raise ValueError(f"AI returned invalid JSON: {e}")

        # Validate recommendation_type
        rec_type = result.get("recommendation_type")
        if rec_type not in ("immediate", "alerts"):
            raise ValueError(
                f"Unknown recommendation_type: '{rec_type}'. "
                "Expected 'immediate' or 'alerts'."
            )

        # Required analysis fields for this advisor
        analysis_fields = ("dxyz_pattern_analysis", "lifecycle_stage",
                           "news_analysis", "sentiment_analysis", "macro_analysis")

        if rec_type == "immediate":
            for field in ("action", "message", "summary") + analysis_fields:
                if not result.get(field):
                    raise ValueError(f"Missing field '{field}' in immediate response")

        elif rec_type == "alerts":
            for field in ("summary", "alerts") + analysis_fields:
                if not result.get(field):
                    raise ValueError(f"Missing field '{field}' in alerts response")

            alerts = result["alerts"]
            if not isinstance(alerts, list) or len(alerts) == 0:
                raise ValueError("'alerts' must be a non-empty list")

            validated = []
            for i, a in enumerate(alerts):
                a_type = a.get("type")
                a_op   = a.get("operator")
                a_thr  = a.get("threshold")
                a_tier = a.get("tier")
                a_rat  = a.get("rationale", "")

                if a_type not in VALID_ALERT_TYPES:
                    raise ValueError(f"Alert {i+1}: invalid type '{a_type}'")
                if a_op not in VALID_ALERT_OPERATORS:
                    raise ValueError(f"Alert {i+1}: invalid operator '{a_op}'")
                if not isinstance(a_thr, (int, float)):
                    raise ValueError(f"Alert {i+1}: threshold must be a number, got '{a_thr}'")
                if a_tier not in ("watch", "action"):
                    raise ValueError(f"Alert {i+1}: tier must be 'watch' or 'action', got '{a_tier}'")

                validated.append({
                    "type":      a_type,
                    "operator":  a_op,
                    "threshold": float(a_thr),
                    "tier":      a_tier,
                    "rationale": str(a_rat),
                })
            result["alerts"] = validated

        # Validate position_sizing for SELL signal
        if signal_type.upper() == "SELL":
            ps = result.get("position_sizing")
            if not ps or not isinstance(ps, dict):
                raise ValueError("Missing 'position_sizing' object for SELL signal")
            sell_pct = ps.get("sell_pct_now")
            if not isinstance(sell_pct, (int, float)):
                raise ValueError(
                    f"'position_sizing.sell_pct_now' must be a number, got '{sell_pct}'"
                )
            if not ps.get("rationale"):
                raise ValueError("Missing 'position_sizing.rationale' for SELL signal")
            # Clamp to valid range
            result["position_sizing"]["sell_pct_now"] = max(0, min(100, int(sell_pct)))

        return result

    # ── 6. Public entry point ───────────────────

    def get_advice(self, ticker: str, nav: float, signal_type: str,
                   history_data: dict) -> dict:
        """
        Full pipeline: fetch DXYZ data → fetch target data → build prompt →
        call AI → parse response.

        Always returns a dict (never raises):
          {"ok": True,  "recommendation_type": "immediate", ...}
          {"ok": True,  "recommendation_type": "alerts",    ...}
          {"ok": False, "error": "human-readable message"}
        """
        try:
            print(f"[PreIpoAdvisor] Fetching DXYZ reference data...")
            dxyz_ctx = self._fetch_dxyz_data()
            if not dxyz_ctx:
                return {"ok": False,
                        "error": "Failed to fetch DXYZ reference data from yfinance. "
                                 "Check your internet connection and try again."}

            print(f"[PreIpoAdvisor] Fetching target data for {ticker} ({signal_type})...")
            target_ctx = self._fetch_target_data(ticker, nav, signal_type, history_data)

            print(f"[PreIpoAdvisor] Building prompt "
                  f"({dxyz_ctx['trading_days_since_listing']} DXYZ days, "
                  f"{target_ctx['data_days']} target days)...")
            prompt = self.build_prompt(dxyz_ctx, target_ctx)

            print(f"[PreIpoAdvisor] Calling {PRE_IPO_AI_MODEL} via Openrouter...")
            raw = self.call_openrouter(prompt)

            print(f"[PreIpoAdvisor] Parsing response...")
            result = self.parse_and_validate(raw, signal_type)

            result["ok"] = True
            print(f"[PreIpoAdvisor] Done — "
                  f"recommendation_type={result['recommendation_type']}")
            return result

        except requests.exceptions.Timeout:
            msg = "AI request timed out. Please try again."
            print(f"[PreIpoAdvisor] Timeout for {ticker}")
            return {"ok": False, "error": msg}

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else "?"
            msg = f"AI API error (HTTP {status}). Check your Openrouter API key."
            print(f"[PreIpoAdvisor] HTTPError {status} for {ticker}: {e}")
            return {"ok": False, "error": msg}

        except requests.exceptions.RequestException as e:
            msg = f"Network error contacting AI: {e}"
            print(f"[PreIpoAdvisor] RequestException for {ticker}: {e}")
            return {"ok": False, "error": msg}

        except ValueError as e:
            msg = f"AI response was malformed: {e}"
            print(f"[PreIpoAdvisor] Validation error for {ticker}: {e}")
            return {"ok": False, "error": msg}

        except Exception as e:
            msg = f"Unexpected error: {e}"
            print(f"[PreIpoAdvisor] Unexpected error for {ticker}: {e}")
            return {"ok": False, "error": msg}
