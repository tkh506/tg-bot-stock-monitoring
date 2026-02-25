#!/usr/bin/env python3
"""
AI Advisor Module — Stock Monitor Bot
Calls Claude Sonnet via Openrouter to analyse a stock and suggest alerts.

Designed to be called from telegram_bot_multistock.py via asyncio.to_thread()
so it does not block the bot's async event loop.
"""

import json
import math
import requests
import yfinance as yf
from datetime import datetime


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
AI_MODEL       = "openai/gpt-5-mini"          # Openrouter model ID for DeepSeek V3.2
AI_TIMEOUT     = 90  # seconds — generous timeout; analysis can take 20–40 s

# Alert types the bot supports
VALID_ALERT_TYPES     = {"premium", "price_1d", "price_7d", "volume"}
VALID_ALERT_OPERATORS = {">", "<"}


# ─────────────────────────────────────────────
# Helper: derive market benchmark ticker
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

class AIAdvisor:
    """Orchestrates AI-powered stock analysis and alert suggestion."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    # ── Sentiment helpers ───────────────────────

    def _get_stocktwits_sentiment(self, ticker: str) -> dict:
        """
        Fetch retail sentiment from StockTwits.
        Applied to all tickers; HK tickers will often return 0 messages — that
        itself is useful signal (low retail coverage on English-language platforms).
        """
        try:
            # StockTwits uses the ticker without exchange suffix
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
            print(f"[AIAdvisor] StockTwits fetch failed for {ticker}: {e}")
            return {}

    def _get_google_trends(self, ticker: str, company_name: str = "") -> dict:
        """
        Fetch Google search interest trend (3 months, weekly).
        Tries the raw ticker first; falls back to company name if no data.
        Note: pytrends may be blocked on some cloud VMs — fails silently.
        """
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
                        continue  # No data for this keyword — try next

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
                        "keyword_used":    keyword,
                        "current_interest": round(recent_avg, 1),
                        "trend_4w":        trend_str,
                        "peak_3m":         max(values),
                        "weekly_values":   values[-12:],  # last 12 weeks for context
                    }
                except Exception as e:
                    print(f"[AIAdvisor] Google Trends failed for '{keyword}': {e}")
                    continue

            return {}
        except ImportError:
            print("[AIAdvisor] pytrends not installed — run: pip install pytrends")
            return {}
        except Exception as e:
            print(f"[AIAdvisor] Google Trends fetch failed for {ticker}: {e}")
            return {}

    def _get_reddit_sentiment(self, ticker: str) -> dict:
        """
        Fetch Reddit post sentiment using the public .json endpoint — no API key needed.
        Searches ticker-specific posts across r/investing, r/stocks, r/wallstreetbets.
        Skipped entirely for HK tickers (.HK) — Reddit coverage is too thin to be useful.
        Fails silently if Reddit blocks the VM (short 5s timeout per subreddit).
        """
        # HK tickers have near-zero Reddit coverage — skip entirely
        if ticker.upper().endswith(".HK"):
            return {"skipped": True, "reason": "HK ticker — insufficient Reddit coverage"}

        try:
            subreddits = ["investing", "stocks", "wallstreetbets"]
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; StockMonitorBot/1.0; personal use)"
            }
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
                    print(f"[AIAdvisor] Reddit fetch failed for r/{sub}: {e}")
                    continue

            if not all_posts:
                return {"total_posts": 0, "posts": [],
                        "note": "No ticker-specific posts found on Reddit"}

            # Sort by score, keep top 10 across all subreddits
            all_posts.sort(key=lambda x: x["score"], reverse=True)
            return {"total_posts": len(all_posts), "posts": all_posts[:10]}

        except Exception as e:
            print(f"[AIAdvisor] Reddit sentiment fetch failed for {ticker}: {e}")
            return {}

    # ── 1. Context preparation ──────────────────

    def prepare_context(self, ticker: str, nav: float, signal_type: str,
                        history_data: dict) -> dict:
        """
        Gather all data we will send to the AI.

        Primary source  : yfinance — 1 year of daily OHLCV data + derived stats
        Supplementary   : local history_data — NAV/premium records only
                          (yfinance has no knowledge of NAV)
        """
        nav_applicable = nav > 0
        stock_obj = yf.Ticker(ticker)

        # ══ PRIMARY: 1-year daily OHLCV from yfinance ══════════════════════

        hist_1y = None
        try:
            hist_1y = stock_obj.history(period="1y")
            if hist_1y is not None and hist_1y.empty:
                hist_1y = None
        except Exception as e:
            print(f"[AIAdvisor] yfinance 1y fetch failed for {ticker}: {e}")

        # Initialise derived fields
        current_price = None
        week_52_high = week_52_low = None
        ma_20 = ma_50 = ma_200 = None
        change_1d = change_1m = change_3m = change_6m = change_1y_pct = None
        current_vol = avg_vol_30d = vol_ratio = None
        volatility_20d = None
        monthly_summary = []
        recent_60d_daily = []
        data_days = 0

        if hist_1y is not None:
            closes  = hist_1y["Close"]
            volumes = hist_1y["Volume"]
            data_days = len(closes)

            current_price = float(closes.iloc[-1])
            week_52_high  = round(float(closes.max()), 4)
            week_52_low   = round(float(closes.min()), 4)

            # ── Moving averages ──
            if data_days >= 20:
                ma_20  = round(float(closes.tail(20).mean()),  4)
            if data_days >= 50:
                ma_50  = round(float(closes.tail(50).mean()),  4)
            if data_days >= 200:
                ma_200 = round(float(closes.tail(200).mean()), 4)

            # ── Price changes over various horizons ──
            def _pct_chg(n):
                if data_days >= n + 1:
                    return round(((current_price - float(closes.iloc[-n - 1]))
                                  / float(closes.iloc[-n - 1])) * 100, 2)
                return None

            change_1d      = _pct_chg(1)
            change_1m      = _pct_chg(21)    # ~1 trading month
            change_3m      = _pct_chg(63)    # ~3 trading months
            change_6m      = _pct_chg(126)   # ~6 trading months
            change_1y_pct  = round(
                ((current_price - float(closes.iloc[0])) / float(closes.iloc[0])) * 100, 2
            ) if data_days >= 2 else None

            # ── Volume ──
            current_vol = int(volumes.iloc[-1])
            if data_days >= 30:
                avg_vol_30d = int(volumes.tail(30).mean())
                vol_ratio   = round(current_vol / avg_vol_30d, 2) if avg_vol_30d > 0 else None

            # ── Annualised volatility (20-day rolling std of daily returns) ──
            returns = closes.pct_change().dropna()
            if len(returns) >= 20:
                volatility_20d = round(float(returns.tail(20).std()
                                             * math.sqrt(252) * 100), 1)

            # ── Monthly summary (end-of-month close, last 12 months) ──
            try:
                try:
                    monthly_series = closes.resample("ME").last()   # pandas ≥ 2.2
                except Exception:
                    monthly_series = closes.resample("M").last()    # older pandas
                monthly_summary = [
                    {"month": idx.strftime("%Y-%m"), "close": round(float(v), 4)}
                    for idx, v in monthly_series.items()
                    if not math.isnan(float(v))
                ]
            except Exception as e:
                print(f"[AIAdvisor] Monthly resample failed for {ticker}: {e}")

            # ── Last 60 trading days (full OHLCV) ──
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
            # ── FALLBACK: local 30-day history if yfinance unavailable ──
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
                    {"date": d["date"], "close": d["price"],
                     "volume": d.get("volume")}
                    for d in daily_local
                ]
                print(f"[AIAdvisor] yfinance unavailable — "
                      f"using {data_days} days of local history for {ticker}")

        # ── Current premium (computed from live price + NAV) ──
        current_premium = None
        if nav_applicable and current_price is not None:
            current_premium = round(((current_price - nav) / nav) * 100, 2)

        # ── Historical premium from local data (yfinance doesn't know NAV) ──
        premium_history = []
        if nav_applicable:
            local_daily = history_data.get("daily_prices", [])
            premium_history = [
                {"date": d["date"],
                 "price": d["price"],
                 "premium_pct": round(d["premium"], 1)}
                for d in local_daily
                if d.get("premium") is not None
            ][-60:]

        # ── Recent news headlines via yfinance ──
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
            print(f"[AIAdvisor] News fetch failed for {ticker}: {e}")

        # ── Market benchmark 1-year return ──
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
            print(f"[AIAdvisor] Benchmark fetch failed: {e}")

        # ── Basic stock info (sector, beta, etc.) ──
        stock_info = {}
        try:
            info = stock_obj.info or {}
            for field in ("longName", "sector", "industry", "beta",
                          "trailingPE", "marketCap", "currency"):
                if info.get(field) is not None:
                    stock_info[field] = info[field]
        except Exception as e:
            print(f"[AIAdvisor] Stock info fetch failed for {ticker}: {e}")

        # ── Retail sentiment (StockTwits, Google Trends, Reddit) ──
        company_name = stock_info.get("longName", "")
        print(f"[AIAdvisor] Fetching retail sentiment for {ticker}...")
        stocktwits_data = self._get_stocktwits_sentiment(ticker)
        google_trends   = self._get_google_trends(ticker, company_name)
        reddit_data     = self._get_reddit_sentiment(ticker)

        return {
            "ticker":           ticker,
            "nav":              nav,
            "nav_applicable":   nav_applicable,
            "signal_type":      signal_type.upper(),
            "data_days":        data_days,
            "current_price":    current_price,
            "current_premium":  current_premium,
            "week_52_high":     week_52_high,
            "week_52_low":      week_52_low,
            "ma_20":            ma_20,
            "ma_50":            ma_50,
            "ma_200":           ma_200,
            "change_1d_pct":    change_1d,
            "change_1m_pct":    change_1m,
            "change_3m_pct":    change_3m,
            "change_6m_pct":    change_6m,
            "change_1y_pct":    change_1y_pct,
            "current_vol":      current_vol,
            "avg_vol_30d":      avg_vol_30d,
            "vol_ratio":        vol_ratio,
            "volatility_20d":   volatility_20d,
            "monthly_summary":  monthly_summary,
            "recent_60d_daily": recent_60d_daily,
            "premium_history":  premium_history,
            "news_headlines":   news_headlines,
            "benchmark_ticker": benchmark_ticker,
            "benchmark_1y_pct": benchmark_1y_pct,
            "stock_info":       stock_info,
            "stocktwits":       stocktwits_data,
            "google_trends":    google_trends,
            "reddit":           reddit_data,
            "analysis_time":    datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        }

    # ── 2. Prompt construction ──────────────────

    def build_prompt(self, ctx: dict) -> str:
        cp = ctx["current_price"]

        # ── NAV / premium block ──
        if ctx["nav_applicable"]:
            nav_block = (
                f"NAV                : {_fmt_price(ctx['nav'])}\n"
                f"Current premium    : {_fmt_pct(ctx['current_premium'], plus=True)}"
                f"  (formula: (Price − NAV) / NAV × 100)"
            )
        else:
            nav_block = "NAV: N/A — do NOT suggest premium-type alerts"

        # ── 52-week range ──
        if cp and ctx["week_52_high"] and ctx["week_52_low"]:
            pct_from_high = ((cp - ctx["week_52_high"]) / ctx["week_52_high"]) * 100
            pct_from_low  = ((cp - ctx["week_52_low"])  / ctx["week_52_low"])  * 100
            range_block = (
                f"52-week high       : {_fmt_price(ctx['week_52_high'])}"
                f"  (current {_fmt_pct(pct_from_high)} from high)\n"
                f"52-week low        : {_fmt_price(ctx['week_52_low'])}"
                f"  (current {_fmt_pct(pct_from_low, plus=True)} from low)"
            )
        else:
            range_block = "52-week range: N/A"

        # ── Moving averages ──
        ma_block = "\n".join([
            _ma_note(cp, ctx["ma_20"],  "20-day SMA "),
            _ma_note(cp, ctx["ma_50"],  "50-day SMA "),
            _ma_note(cp, ctx["ma_200"], "200-day SMA"),
        ])

        # ── Price changes ──
        changes_block = (
            f"1-day   : {_fmt_pct(ctx['change_1d_pct'])}\n"
            f"1-month : {_fmt_pct(ctx['change_1m_pct'])}\n"
            f"3-month : {_fmt_pct(ctx['change_3m_pct'])}\n"
            f"6-month : {_fmt_pct(ctx['change_6m_pct'])}\n"
            f"1-year  : {_fmt_pct(ctx['change_1y_pct'])}"
        )

        # ── Volume ──
        vol_block = (
            f"Latest session  : {ctx['current_vol']:,}\n"
            f"30-day average  : {ctx['avg_vol_30d']:,}\n"
            f"Ratio vs avg    : {ctx['vol_ratio']}x"
            if ctx["current_vol"] and ctx["avg_vol_30d"]
            else "Volume data: N/A"
        )

        # ── Benchmark ──
        bm_block = (
            f"{ctx['benchmark_ticker']} 1-year return: {_fmt_pct(ctx['benchmark_1y_pct'])}"
            + (
                f"\nStock vs benchmark: "
                f"{_fmt_pct((ctx['change_1y_pct'] or 0) - (ctx['benchmark_1y_pct'] or 0))} "
                f"({'outperforming' if (ctx['change_1y_pct'] or 0) > (ctx['benchmark_1y_pct'] or 0) else 'underperforming'})"
                if ctx["change_1y_pct"] is not None and ctx["benchmark_1y_pct"] is not None
                else ""
            )
        )

        # ── News ──
        news_block = (
            "\n".join(f"  - {h}" for h in ctx["news_headlines"])
            if ctx["news_headlines"] else "  (No recent news available)"
        )

        # ── Stock info ──
        info_lines = "\n".join(f"  {k}: {v}"
                               for k, v in ctx["stock_info"].items()) or "  (N/A)"

        # ── Retail sentiment ──
        # StockTwits
        st = ctx["stocktwits"]
        if st:
            st_line = (f"Messages fetched: {st['total_messages']} | "
                       f"Sentiment: {st['sentiment_ratio']}")
            samples_text = "\n".join(f"    {s}" for s in st.get("samples", []))
            stocktwits_block = (f"StockTwits ({ctx['ticker'].replace('.HK','')})\n"
                                f"  {st_line}"
                                + (f"\n  Sample messages:\n{samples_text}" if samples_text else ""))
        else:
            stocktwits_block = "StockTwits: fetch failed or unavailable"

        # Google Trends
        gt = ctx["google_trends"]
        if gt:
            gt_block = (
                f"Google Trends (keyword: '{gt['keyword_used']}'):\n"
                f"  Current interest: {gt['current_interest']}/100  |  "
                f"4-week trend: {gt['trend_4w']}  |  "
                f"3-month peak: {gt['peak_3m']}/100"
            )
        else:
            gt_block = "Google Trends: no data (pytrends unavailable or blocked)"

        # Reddit
        rd = ctx.get("reddit", {})
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
                f"Reddit (r/investing + r/stocks + r/wallstreetbets) — "
                f"{rd['total_posts']} recent posts found, top {len(rd['posts'])} shown:\n"
                + post_lines
            )

        # ── Premium history (only if NAV applicable) ──
        premium_section = ""
        if ctx["nav_applicable"] and ctx["premium_history"]:
            premium_section = (
                f"\n════════════════════════════════\n"
                f"HISTORICAL PREMIUM TO NAV (last {len(ctx['premium_history'])} entries from local monitor)\n"
                f"════════════════════════════════\n"
                + json.dumps(ctx["premium_history"], indent=2)
            )

        # ── Monthly summary ──
        monthly_json = (
            json.dumps(ctx["monthly_summary"], indent=2)
            if ctx["monthly_summary"] else "  (Not available)"
        )

        # ── Recent 60-day OHLCV ──
        recent_json = (
            json.dumps(ctx["recent_60d_daily"], indent=2)
            if ctx["recent_60d_daily"] else "  (Not available)"
        )

        return f"""You are a financial analysis assistant for a stock monitoring bot.

Today: {ctx['analysis_time']}

════════════════════════════════
STOCK UNDER ANALYSIS
════════════════════════════════
Ticker             : {ctx['ticker']}
{nav_block}
Current price      : {_fmt_price(cp)}

{range_block}

MOVING AVERAGES
{ma_block}

PRICE CHANGES
{changes_block}

VOLUME
{vol_block}

Annualised volatility (20-day): {f"{ctx['volatility_20d']}%" if ctx['volatility_20d'] else 'N/A'}

ADDITIONAL STOCK INFO
{info_lines}

════════════════════════════════
MARKET CONTEXT
════════════════════════════════
{bm_block}

Recent news (up to 15 headlines):
{news_block}

════════════════════════════════
RETAIL SENTIMENT
════════════════════════════════
{stocktwits_block}

{gt_block}

{reddit_block}

════════════════════════════════
12-MONTH PRICE SUMMARY (end-of-month close, {ctx['data_days']} trading days of data)
════════════════════════════════
{monthly_json}

════════════════════════════════
RECENT 60-DAY DAILY DATA (OHLCV)
════════════════════════════════
{recent_json}
{premium_section}
════════════════════════════════
BOT ALERT SYSTEM — AVAILABLE ALERT TYPES
════════════════════════════════
The bot supports exactly 4 alert types. Each alert fires when the live value
crosses the threshold (state-based: only triggers once on transition).

1. "premium"  — (Price − NAV) / NAV × 100  {{operator}}  {{threshold}}%
   • Only valid when NAV is applicable (nav > 0)
   • Useful for closed-end funds / ETFs trading at a premium/discount

2. "price_1d" — 1-day price change %  {{operator}}  {{threshold}}%
   • Example: operator "<", threshold -5  → fires when stock drops > 5% in a day
   • Example: operator ">", threshold 3   → fires when stock rises > 3% in a day

3. "price_7d" — 7-day price change %  {{operator}}  {{threshold}}%
   • Example: operator "<", threshold -15 → fires when stock drops > 15% in 7 days
   • Example: operator ">", threshold 10  → fires when stock rises > 10% in 7 days

4. "volume"   — today's volume / 7-day average volume  {{operator}}  {{threshold}}
   • Threshold is a multiplier (e.g. 2.0 = 2× average daily volume)
   • Useful for detecting unusual trading activity

════════════════════════════════
YOUR TASK
════════════════════════════════
The user wants {ctx['signal_type']} signal alerts for {ctx['ticker']}.

Analyse the stock using all data above and choose one output format:

FORMAT A — Conditions already clearly met (act now):
{{
  "recommendation_type": "immediate",
  "action": "{ctx['signal_type']}",
  "summary": "2–3 sentences: current situation and why immediate action",
  "price_analysis": "Price movement, volume, SMA cross, 52-week range, momentum — what the chart is saying",
  "sector_analysis": "Sector/industry context, ETF composition or peer comparison if relevant. Write 'N/A' if no useful sector data available.",
  "news_analysis": "2–3 sentences on the most relevant recent news headlines for this specific stock and their likely impact",
  "sentiment_analysis": "2–3 sentences covering StockTwits bullish/bearish ratio, Reddit post tone, and Google Trends interest level and direction",
  "macro_analysis": "Macro factors affecting this stock: economy, interest rates, geopolitics, government policy, currency"
}}

FORMAT B — Set up monitoring alerts (wait for the signal):
{{
  "recommendation_type": "alerts",
  "summary": "2–3 sentences: current situation and what to watch for",
  "price_analysis": "Price movement, volume, SMA cross, 52-week range, momentum — what the chart is saying",
  "sector_analysis": "Sector/industry context, ETF composition or peer comparison if relevant. Write 'N/A' if no useful sector data available.",
  "news_analysis": "2–3 sentences on the most relevant recent news headlines for this specific stock and their likely impact",
  "sentiment_analysis": "2–3 sentences covering StockTwits bullish/bearish ratio, Reddit post tone, and Google Trends interest level and direction",
  "macro_analysis": "Macro factors affecting this stock: economy, interest rates, geopolitics, government policy, currency",
  "alerts": [
    {{
      "type": "price_1d | price_7d | premium | volume",
      "operator": "> or <",
      "threshold": <number>,
      "tier": "watch or action",
      "rationale": "One sentence: why this threshold is appropriate given recent volatility"
    }}
  ]
}}

Alert tier definitions:
  "watch"  — closer threshold, early warning, fires first
  "action" — stronger threshold, high-confidence signal, fires on conviction

Rules for alert design:
  - Provide 2 to 6 alerts total; use a mix of tiers
  - Calibrate thresholds to this stock's actual recent volatility (check the data!)
  - Prefer diverse types (e.g. mix 1 price + 1 volume rather than all price)
  - Do NOT suggest "premium" alerts if NAV is not applicable
  - For a {ctx['signal_type']} signal: alerts should fire when conditions favour {ctx['signal_type']}ing

════════════════════════════════
IMPORTANT
════════════════════════════════
Return ONLY valid JSON — no markdown fences, no comments, no text outside the JSON object.
"""

    # ── 3. Openrouter API call ──────────────────

    def call_openrouter(self, prompt: str) -> dict:
        """POST to Openrouter and return the parsed API response."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
            "HTTP-Referer":  "https://github.com/tg-bot-stock-monitoring",
            "X-Title":       "Stock Monitor Bot",
        }
        payload = {
            "model": AI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 4000,
        }
        response = requests.post(OPENROUTER_URL, headers=headers,
                                 json=payload, timeout=AI_TIMEOUT)
        response.raise_for_status()
        return response.json()

    # ── 4. Response parsing & validation ───────

    def parse_and_validate(self, raw_response: dict) -> dict:
        """
        Extract the AI's JSON from the Openrouter response wrapper, parse it,
        and validate the schema.  Raises ValueError with a readable message on any problem.
        """
        try:
            content = raw_response["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise ValueError(f"Unexpected API response structure: {e}")

        # Guard: response truncated by token limit
        finish_reason = raw_response.get("choices", [{}])[0].get("finish_reason", "")
        if finish_reason == "length":
            print(f"[AIAdvisor] Response truncated (finish_reason=length). "
                  f"Increase max_tokens. Partial content:\n{content}")
            raise ValueError("AI response was cut off (token limit reached). Please try again.")

        # Guard: empty content (can happen if model refused or returned nothing)
        if not content or not content.strip():
            print(f"[AIAdvisor] Empty content in API response. "
                  f"Full response: {json.dumps(raw_response)}")
            raise ValueError("AI returned an empty response. Please try again.")

        # Strip markdown fences if the model wrapped the JSON anyway
        # e.g.  ```json\n{...}\n```  or  ```\n{...}\n```
        stripped = content.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            # Drop first line (```json / ```) and last line (```)
            inner_lines = lines[1:] if lines[-1].strip() == "```" else lines[1:-1]
            stripped = "\n".join(inner_lines).strip()

        # Find the start of the JSON object (skip any leading non-JSON text)
        json_start = stripped.find("{")
        if json_start == -1:
            print(f"[AIAdvisor] No JSON object found. Raw content was:\n{content}")
            raise ValueError("AI response contained no JSON object.")
        stripped = stripped[json_start:]

        try:
            # raw_decode parses the first complete JSON object and ignores
            # any trailing text (e.g. extra explanation after the closing })
            result, _ = json.JSONDecoder().raw_decode(stripped)
        except json.JSONDecodeError as e:
            print(f"[AIAdvisor] JSON parse failed. Raw content was:\n{content}")
            raise ValueError(f"AI returned invalid JSON: {e}")

        rec_type = result.get("recommendation_type")
        if rec_type not in ("immediate", "alerts"):
            raise ValueError(
                f"Unknown recommendation_type: '{rec_type}'. "
                "Expected 'immediate' or 'alerts'."
            )

        analysis_fields = ("price_analysis", "sector_analysis", "news_analysis",
                            "sentiment_analysis", "macro_analysis")

        if rec_type == "immediate":
            for field in ("action", "summary") + analysis_fields:
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

        return result

    # ── 5. Public entry point ───────────────────

    def get_advice(self, ticker: str, nav: float, signal_type: str,
                   history_data: dict) -> dict:
        """
        Full pipeline: gather data → build prompt → call AI → parse response.

        Always returns a dict (never raises):
          {"ok": True,  "recommendation_type": "immediate", ...}
          {"ok": True,  "recommendation_type": "alerts",    ...}
          {"ok": False, "error": "human-readable message"}
        """
        try:
            print(f"[AIAdvisor] Preparing context for {ticker} ({signal_type})...")
            ctx = self.prepare_context(ticker, nav, signal_type, history_data)

            print(f"[AIAdvisor] Building prompt "
                  f"({ctx['data_days']} days of price data)...")
            prompt = self.build_prompt(ctx)

            print(f"[AIAdvisor] Calling {AI_MODEL} via Openrouter...")
            raw = self.call_openrouter(prompt)

            print(f"[AIAdvisor] Parsing response...")
            result = self.parse_and_validate(raw)

            result["ok"] = True
            print(f"[AIAdvisor] Done — "
                  f"recommendation_type={result['recommendation_type']}")
            return result

        except requests.exceptions.Timeout:
            msg = "AI request timed out. Please try again."
            print(f"[AIAdvisor] Timeout for {ticker}")
            return {"ok": False, "error": msg}

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else "?"
            msg = f"AI API error (HTTP {status}). Check your Openrouter API key."
            print(f"[AIAdvisor] HTTPError {status} for {ticker}: {e}")
            return {"ok": False, "error": msg}

        except requests.exceptions.RequestException as e:
            msg = f"Network error contacting AI: {e}"
            print(f"[AIAdvisor] RequestException for {ticker}: {e}")
            return {"ok": False, "error": msg}

        except ValueError as e:
            msg = f"AI response was malformed: {e}"
            print(f"[AIAdvisor] Validation error for {ticker}: {e}")
            return {"ok": False, "error": msg}

        except Exception as e:
            msg = f"Unexpected error: {e}"
            print(f"[AIAdvisor] Unexpected error for {ticker}: {e}")
            return {"ok": False, "error": msg}
