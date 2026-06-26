#!/usr/bin/env python3
"""
IWMO Momentum ETF Tracker
=========================
Fetches daily iShares IWMO holdings CSV, stores history in SQLite,
detects new entries/exits, scores 5 mathematical confirmation signals,
generates a monthly rolling HTML report with archive chain, and
optionally calls Groq for a short analyst note on new entries.

Report structure (root of the `report` branch in your GitHub repo):
  index.html        ← current month, always up to date (daily entries appended)
  2026-06.html      ← sealed previous month (renamed from index.html on 1st)
  2026-05.html      ← etc.

Published via GitHub Pages on the `report` branch.
Source code lives on `main`. One repo, two branches.

Usage:
  python3 velocity.py [--no-ai] [--report-only] [--mock-csv PATH]
                          [--db PATH] [--report-dir PATH]

Cron (weekdays 20:00 UTC, after iShares UK update):
  0 20 * * 1-5 $HOME/velocity/velocity.sh >> $HOME/logs/velocity.log 2>&1

Dependencies: pip install requests  (stdlib urllib used for Groq — no extra pkg)
"""

import argparse
import csv
import datetime
import io
import json
import logging
import math
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

import requests

# ── Config ─────────────────────────────────────────────────────────────────────

IWMO_URL = (
    "https://www.ishares.com/uk/individual/en/products/270051/"
    "ishares-msci-world-momentum-factor-ucits-etf/"
    "1506575576011.ajax?fileType=csv&fileName=IWMO_holdings&dataType=fund"
)
FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer":         "https://www.ishares.com/uk/",
}

GROQ_ENDPOINT  = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL     = "llama-3.3-70b-versatile"   # 1,000 RPD free — 1/day is trivial

NEW_ENTRY_LOOKBACK_DAYS = 20
DEFAULT_DB_PATH         = Path(__file__).parent / "velocity.db"
DEFAULT_REPORT_DIR      = Path(__file__).parent / "velocity-report"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Sector colours ──────────────────────────────────────────────────────────────

SECTOR_COLORS = {
    "Information Technology": "#3b82f6",
    "Financials":              "#10b981",
    "Health Care":             "#8b5cf6",
    "Industrials":             "#f59e0b",
    "Energy":                  "#ef4444",
    "Consumer Staples":        "#06b6d4",
    "Consumer Discretionary":  "#ec4899",
    "Communication":           "#6366f1",
    "Materials":               "#84cc16",
    "Utilities":               "#f97316",
    "Real Estate":             "#14b8a6",
}

def sc(sector: str) -> str:
    return SECTOR_COLORS.get(sector or "", "#6b7280")


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════════════

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fetch_date  TEXT UNIQUE NOT NULL,
    fetched_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holdings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date   TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    name            TEXT NOT NULL,
    sector          TEXT,
    weight_pct      REAL,
    market_value    REAL,
    shares          REAL,
    price           REAL,
    currency        TEXT,
    exchange        TEXT,
    location        TEXT,
    UNIQUE(snapshot_date, ticker)
);
CREATE TABLE IF NOT EXISTS weekly_closes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    week_ending TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    name        TEXT NOT NULL,
    sector      TEXT,
    weight_pct  REAL,
    price       REAL,
    currency    TEXT,
    UNIQUE(week_ending, ticker)
);
CREATE INDEX IF NOT EXISTS idx_h_date   ON holdings(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_h_ticker ON holdings(ticker);
CREATE INDEX IF NOT EXISTS idx_wc       ON weekly_closes(ticker);
"""

def get_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ══════════════════════════════════════════════════════════════════════════════
# FETCH & PARSE
# ══════════════════════════════════════════════════════════════════════════════

def fetch_csv() -> tuple[datetime.date, list[dict]]:
    log.info("Fetching IWMO CSV …")
    resp = requests.get(IWMO_URL, headers=FETCH_HEADERS, timeout=30)
    resp.raise_for_status()
    return parse_csv(resp.text)


def parse_csv(raw: str) -> tuple[datetime.date, list[dict]]:
    lines = raw.splitlines()
    holdings_date: Optional[datetime.date] = None

    for line in lines[:5]:
        if "Holdings as of" in line:
            parts = line.split(",")
            if len(parts) >= 2:
                try:
                    holdings_date = datetime.datetime.strptime(
                        parts[1].strip().strip('"'), "%d/%b/%Y"
                    ).date()
                except ValueError:
                    pass
            break

    if not holdings_date:
        holdings_date = datetime.date.today()
        log.warning("Could not parse holdings date; using today")

    header_idx = next((i for i, l in enumerate(lines) if l.startswith("Ticker,")), None)
    if header_idx is None:
        raise ValueError("Ticker header row not found in CSV")

    def to_float(s):
        try:
            return float(str(s).replace(",", "").strip())
        except (ValueError, AttributeError):
            return None

    rows = []
    for row in csv.DictReader(io.StringIO("\n".join(lines[header_idx:]))):
        ticker = row.get("Ticker", "").strip().strip('"')
        if not ticker or row.get("Asset Class", "").strip() != "Equity":
            continue
        rows.append({
            "ticker":       ticker,
            "name":         row.get("Name", "").strip().strip('"'),
            "sector":       row.get("Sector", "").strip().strip('"'),
            "weight_pct":   to_float(row.get("Weight (%)", "")),
            "market_value": to_float(row.get("Market Value", "")),
            "shares":       to_float(row.get("Shares", "")),
            "price":        to_float(row.get("Price", "")),
            "currency":     row.get("Market Currency", "").strip(),
            "exchange":     row.get("Exchange", "").strip().strip('"'),
            "location":     row.get("Location", "").strip().strip('"'),
        })

    log.info("Parsed %d equity rows for %s", len(rows), holdings_date)
    return holdings_date, rows


# ══════════════════════════════════════════════════════════════════════════════
# STORE
# ══════════════════════════════════════════════════════════════════════════════

def store_snapshot(conn, holdings_date: datetime.date, rows: list[dict]) -> bool:
    ds = holdings_date.isoformat()
    if conn.execute("SELECT id FROM snapshots WHERE fetch_date=?", (ds,)).fetchone():
        log.info("Snapshot %s already in DB — skipping", ds)
        return False

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn.execute("INSERT INTO snapshots(fetch_date,fetched_at) VALUES(?,?)", (ds, now))
    conn.executemany(
        """INSERT OR IGNORE INTO holdings
           (snapshot_date,ticker,name,sector,weight_pct,market_value,shares,
            price,currency,exchange,location)
           VALUES(:snapshot_date,:ticker,:name,:sector,:weight_pct,:market_value,
                  :shares,:price,:currency,:exchange,:location)""",
        [{**r, "snapshot_date": ds} for r in rows]
    )
    if holdings_date.weekday() == 4:  # Friday
        conn.executemany(
            """INSERT OR IGNORE INTO weekly_closes
               (week_ending,ticker,name,sector,weight_pct,price,currency)
               VALUES(:week_ending,:ticker,:name,:sector,:weight_pct,:price,:currency)""",
            [{**r, "week_ending": ds} for r in rows]
        )
        log.info("Stored %d weekly close rows (Friday)", len(rows))
    conn.commit()
    log.info("Stored snapshot %s (%d rows)", ds, len(rows))
    return True


# ══════════════════════════════════════════════════════════════════════════════
# MATHEMATICAL ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

def recent_dates(conn, n: int) -> list[str]:
    return [r["fetch_date"] for r in conn.execute(
        "SELECT fetch_date FROM snapshots ORDER BY fetch_date DESC LIMIT ?", (n,)
    )]


def daily_history(conn, ticker: str, n: int = 25) -> list[dict]:
    dates = recent_dates(conn, n)
    if not dates:
        return []
    ph = ",".join("?" * len(dates))
    return [dict(r) for r in conn.execute(
        f"""SELECT snapshot_date,weight_pct,price,currency FROM holdings
            WHERE ticker=? AND snapshot_date IN ({ph}) ORDER BY snapshot_date DESC""",
        [ticker] + dates
    )]


def weekly_history(conn, ticker: str, n: int = 52) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT week_ending,weight_pct,price,currency FROM weekly_closes
           WHERE ticker=? ORDER BY week_ending DESC LIMIT ?""",
        (ticker, n)
    )]


def roc(values: list[float], n: int) -> Optional[float]:
    """Rate of change: (v[0] - v[n]) / v[n]"""
    if len(values) < n + 1 or values[n] == 0:
        return None
    return (values[0] - values[n]) / values[n]


def max_drawdown(prices: list[float]) -> Optional[float]:
    """(current - peak) / peak  — negative = below peak"""
    if not prices:
        return None
    peak = max(prices)
    return (prices[0] - peak) / peak if peak else None


def momentum_consistency(prices: list[float], lag: int = 4) -> Optional[float]:
    """Fraction of weeks where price > price lag-weeks prior"""
    if len(prices) < lag + 1:
        return None
    hits = sum(1 for i in range(len(prices) - lag) if prices[i] > prices[i + lag])
    return hits / (len(prices) - lag)


def weight_percentile(conn, ticker: str, today: str) -> Optional[float]:
    all_w = [r["weight_pct"] for r in conn.execute(
        "SELECT weight_pct FROM holdings WHERE snapshot_date=? AND weight_pct IS NOT NULL", (today,)
    )]
    if len(all_w) < 2:
        return None
    tw = conn.execute(
        "SELECT weight_pct FROM holdings WHERE snapshot_date=? AND ticker=?", (today, ticker)
    ).fetchone()
    if not tw:
        return None
    below = sum(1 for w in all_w if w < tw["weight_pct"])
    return (below / len(all_w)) * 100


def weight_zscore(conn, ticker: str, today: str) -> Optional[float]:
    all_w = [r["weight_pct"] for r in conn.execute(
        "SELECT weight_pct FROM holdings WHERE snapshot_date=? AND weight_pct IS NOT NULL", (today,)
    )]
    if len(all_w) < 2:
        return None
    mu    = sum(all_w) / len(all_w)
    sigma = math.sqrt(sum((w - mu) ** 2 for w in all_w) / len(all_w))
    if sigma == 0:
        return None
    tw = conn.execute(
        "SELECT weight_pct FROM holdings WHERE snapshot_date=? AND ticker=?", (today, ticker)
    ).fetchone()
    return ((tw["weight_pct"] - mu) / sigma) if tw else None


def confirmation_score(m: dict) -> tuple[int, list[str]]:
    score, signals = 0, []

    roc20 = m.get("price_roc_20d")
    if roc20 is not None:
        if roc20 > 0:
            score += 1
            signals.append(f"✅ Price ROC 20d: +{roc20*100:.1f}% — still rising")
        else:
            signals.append(f"❌ Price ROC 20d: {roc20*100:.1f}% — fading")
    else:
        signals.append("⬜ Price ROC 20d: building history")

    wroc = m.get("weight_roc_since_entry")
    if wroc is not None:
        if wroc > 0:
            score += 1
            signals.append(f"✅ Weight since entry: +{wroc*100:.1f}% — ETF adding conviction")
        else:
            signals.append(f"❌ Weight since entry: {wroc*100:.1f}% — not growing")
    else:
        signals.append("⬜ Weight change: first appearance")

    pct = m.get("weight_percentile")
    if pct is not None:
        if pct >= 50:
            score += 1
            signals.append(f"✅ Weight rank: {pct:.0f}th pctile — meaningful position")
        else:
            signals.append(f"❌ Weight rank: {pct:.0f}th pctile — small/token position")
    else:
        signals.append("⬜ Weight rank: unknown")

    entry_roc = m.get("price_roc_since_entry")
    if entry_roc is not None:
        if entry_roc >= 0:
            score += 1
            signals.append(f"✅ vs Entry price: +{entry_roc*100:.1f}% — holding gains")
        else:
            signals.append(f"❌ vs Entry price: {entry_roc*100:.1f}% — fading post-inclusion")
    else:
        signals.append("⬜ vs Entry price: first appearance today")

    dd = m.get("drawdown_from_52w_high")
    if dd is not None:
        if dd >= -0.20:
            score += 1
            signals.append(f"✅ 52w drawdown: {dd*100:.1f}% — not overextended")
        else:
            signals.append(f"❌ 52w drawdown: {dd*100:.1f}% — extended, reversion risk")
    else:
        signals.append("⬜ 52w drawdown: building weekly history")

    return score, signals


def compute_metrics(conn, ticker: str, today: str) -> dict:
    dh = daily_history(conn, ticker, 25)
    wh = weekly_history(conn, ticker, 52)
    dp = [r["price"] for r in dh if r.get("price") is not None]
    wp = [r["price"] for r in wh if r.get("price") is not None]

    _fs = conn.execute(
        "SELECT MIN(snapshot_date) as d FROM holdings WHERE ticker=?", (ticker,)
    ).fetchone()
    first_seen = _fs["d"] if _fs else None

    def fetch_val(date, col):
        row = conn.execute(
            f"SELECT {col} FROM holdings WHERE ticker=? AND snapshot_date=?", (ticker, date)
        ).fetchone()
        return row[col] if row else None

    price_now   = dp[0]        if dp else None
    price_entry = fetch_val(first_seen, "price") if first_seen else None
    weight_now  = fetch_val(today, "weight_pct")
    weight_entry= fetch_val(first_seen, "weight_pct") if first_seen else None

    price_roc_entry  = None
    weight_roc_entry = None
    if first_seen and first_seen != today:
        if price_now and price_entry and price_entry > 0:
            price_roc_entry = (price_now - price_entry) / price_entry
        if weight_now and weight_entry and weight_entry > 0:
            weight_roc_entry = (weight_now - weight_entry) / weight_entry

    # Get currency from today's record
    cr = conn.execute(
        "SELECT currency, name, sector FROM holdings WHERE ticker=? AND snapshot_date=?",
        (ticker, today)
    ).fetchone()

    m = {
        "ticker":                 ticker,
        "name":                   cr["name"]     if cr else ticker,
        "sector":                 cr["sector"]   if cr else "",
        "currency":               cr["currency"] if cr else "",
        "first_seen":             first_seen,
        "days_in_etf":            len(dh),
        "current_price":          price_now,
        "price_at_entry":         price_entry,
        "current_weight":         weight_now,
        "weight_at_entry":        weight_entry,
        "price_roc_since_entry":  price_roc_entry,
        "weight_roc_since_entry": weight_roc_entry,
        "price_roc_5d":           roc(dp, 5)  if len(dp) > 5  else None,
        "price_roc_10d":          roc(dp, 10) if len(dp) > 10 else None,
        "price_roc_20d":          roc(dp, 20) if len(dp) > 20 else None,
        "price_roc_52w":          roc(wp, 51) if len(wp) > 51 else None,
        "weight_percentile":      weight_percentile(conn, ticker, today),
        "weight_zscore":          weight_zscore(conn, ticker, today),
        "drawdown_from_52w_high": max_drawdown(wp) if wp else None,
        "momentum_consistency":   momentum_consistency(wp),
        "daily_history":          dh[:20],
        "weekly_history":         wh[:52],
    }
    m["confirmation_score"], m["confirmation_signals"] = confirmation_score(m)
    return m


def detect_new_entries(conn, today: str) -> list[str]:
    lb = recent_dates(conn, NEW_ENTRY_LOOKBACK_DAYS + 1)
    if len(lb) < 2:
        return []
    prior = lb[1:]
    today_set = {r["ticker"] for r in conn.execute(
        "SELECT ticker FROM holdings WHERE snapshot_date=?", (today,)
    )}
    ph = ",".join("?" * len(prior))
    prior_set = {r["ticker"] for r in conn.execute(
        f"SELECT DISTINCT ticker FROM holdings WHERE snapshot_date IN ({ph})", prior
    )}
    return sorted(today_set - prior_set)


def detect_exits(conn, today: str) -> list[dict]:
    dates = recent_dates(conn, 3)
    if len(dates) < 2:
        return []
    today_set = {r["ticker"] for r in conn.execute(
        "SELECT ticker FROM holdings WHERE snapshot_date=?", (today,)
    )}
    return [dict(r) for r in conn.execute(
        "SELECT ticker,name,sector,weight_pct FROM holdings WHERE snapshot_date=?", (dates[1],)
    ) if r["ticker"] not in today_set]


def detect_weight_movers(conn, today: str, top_n: int = 15) -> list[dict]:
    dates = recent_dates(conn, NEW_ENTRY_LOOKBACK_DAYS)
    if len(dates) < 2:
        return []
    oldest = dates[-1]
    today_map = {r["ticker"]: dict(r) for r in conn.execute(
        "SELECT ticker,name,sector,weight_pct FROM holdings WHERE snapshot_date=?", (today,)
    )}
    old_map = {r["ticker"]: r["weight_pct"] for r in conn.execute(
        "SELECT ticker,weight_pct FROM holdings WHERE snapshot_date=?", (oldest,)
    )}
    movers = []
    for t, row in today_map.items():
        if t in old_map and row["weight_pct"] and old_map[t]:
            delta = row["weight_pct"] - old_map[t]
            if abs(delta) > 0.05:
                movers.append({**row, "weight_then": old_map[t], "weight_delta": delta})
    movers.sort(key=lambda x: abs(x["weight_delta"]), reverse=True)
    return movers[:top_n]


def build_day_data(conn, today: str) -> dict:
    new_tickers = detect_new_entries(conn, today)
    exits       = detect_exits(conn, today)
    movers      = detect_weight_movers(conn, today)

    top30 = [r["ticker"] for r in conn.execute(
        "SELECT ticker FROM holdings WHERE snapshot_date=? ORDER BY weight_pct DESC LIMIT 30",
        (today,)
    )]
    to_score = list(set(new_tickers + top30))
    scored = {t: compute_metrics(conn, t, today) for t in to_score}

    top20 = [dict(r) for r in conn.execute(
        """SELECT ticker,name,sector,weight_pct,price,currency
           FROM holdings WHERE snapshot_date=? ORDER BY weight_pct DESC LIMIT 20""",
        (today,)
    )]
    sectors = [dict(r) for r in conn.execute(
        """SELECT sector, COUNT(*) as count, SUM(weight_pct) as total_weight
           FROM holdings WHERE snapshot_date=?
           GROUP BY sector ORDER BY total_weight DESC""",
        (today,)
    )]

    return {
        "date":         today,
        "new_tickers":  new_tickers,
        "exits":        exits,
        "movers":       movers,
        "scored":       scored,
        "top20":        top20,
        "sectors":      sectors,
        "n_snapshots":  len(recent_dates(conn, 9999)),
        "lookback":     NEW_ENTRY_LOOKBACK_DAYS,
    }


# ══════════════════════════════════════════════════════════════════════════════
# GROQ AI COMMENTARY  (optional — skipped if GROQ_API_KEY not set)
# ══════════════════════════════════════════════════════════════════════════════

def get_groq_commentary(new_tickers: list[str], scored: dict) -> dict[str, str]:
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key or not new_tickers:
        return {}

    commentary = {}
    for ticker in new_tickers:
        m = scored.get(ticker, {})
        prompt = (
            f"A stock just entered the iShares MSCI World Momentum Factor ETF (IWMO).\n"
            f"Ticker: {ticker} | Company: {m.get('name', ticker)} | Sector: {m.get('sector', '')}\n"
            f"Weight: {m.get('current_weight', '?')}% | "
            f"Confirmation score: {m.get('confirmation_score', '?')}/5\n\n"
            f"Write exactly 3 sentences for a private investor:\n"
            f"1. What the company does.\n"
            f"2. Why it may be gaining momentum right now.\n"
            f"3. One specific risk to watch.\n"
            f"Be factual and concise. No buy/sell recommendations."
        )
        payload = json.dumps({
            "model":      GROQ_MODEL,
            "messages":   [{"role": "user", "content": prompt}],
            "max_tokens": 220,
            "temperature": 0.4,
        }).encode()
        req = urllib.request.Request(
            GROQ_ENDPOINT,
            data=payload,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {api_key}",
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.load(resp)
                commentary[ticker] = data["choices"][0]["message"]["content"].strip()
                log.info("Groq commentary OK for %s", ticker)
        except urllib.error.HTTPError as e:
            log.warning("Groq HTTP %s for %s", e.code, ticker)
        except Exception as e:
            log.warning("Groq error for %s: %s", ticker, e)

    return commentary


# ══════════════════════════════════════════════════════════════════════════════
# HTML COMPONENTS
# ══════════════════════════════════════════════════════════════════════════════

SCORE_META = {
    5: ("STRONG",  "#10b981"),
    4: ("GOOD",    "#22c55e"),
    3: ("WATCH",   "#f59e0b"),
    2: ("WEAK",    "#ef4444"),
    1: ("AVOID",   "#ef4444"),
    0: ("AVOID",   "#6b7280"),
}

def score_badge(s: int) -> str:
    lbl, col = SCORE_META.get(s, ("?", "#6b7280"))
    return (f'<span class="sbadge" style="background:{col}22;color:{col};'
            f'border:1px solid {col}44">{s}/5 — {lbl}</span>')


def sparkline(values: list[float], w=180, h=36, colour="#3b82f6") -> str:
    if len(values) < 2:
        return ""
    mn, mx = min(values), max(values)
    rng = mx - mn or 1
    n   = len(values)
    pts = " ".join(
        f"{int(i*w/(n-1))},{int(h - ((v-mn)/rng)*(h-4) - 2)}"
        for i, v in enumerate(values)
    )
    return (f'<svg viewBox="0 0 {w} {h}" style="width:{w}px;height:{h}px;display:block">'
            f'<polyline points="{pts}" fill="none" stroke="{colour}" stroke-width="1.5"/>'
            f'</svg>')


def fmt_pct(v, plus=False) -> str:
    if v is None:
        return "—"
    sign = "+" if plus and v >= 0 else ""
    return f"{sign}{v*100:.1f}%"


def fmt_f(v, dp=2) -> str:
    return f"{v:.{dp}f}" if v is not None else "—"


def entry_card(ticker: str, m: dict, commentary: str = "") -> str:
    col = sc(m.get("sector", ""))
    dp  = [r["price"] for r in reversed(m.get("daily_history", [])) if r.get("price")]
    wp  = [r["price"] for r in reversed(m.get("weekly_history", [])) if r.get("price")]
    sd  = sparkline(dp, colour="#3b82f6")
    sw  = sparkline(wp, colour="#10b981")
    s   = m.get("confirmation_score", 0)
    sigs= "".join(f'<div class="sig">{sg}</div>' for sg in m.get("confirmation_signals", []))
    dd  = m.get("drawdown_from_52w_high")
    mc  = m.get("momentum_consistency")

    roc_items = [
        ("Since entry", m.get("price_roc_since_entry")),
        ("5-day",       m.get("price_roc_5d")),
        ("10-day",      m.get("price_roc_10d")),
        ("20-day",      m.get("price_roc_20d")),
        ("52-week",     m.get("price_roc_52w")),
    ]
    roc_rows = "".join(
        f'<div class="mr"><span>{lbl}</span>'
        f'<span class="{"pos" if (v or 0)>=0 else "neg"}">{fmt_pct(v, True)}</span></div>'
        for lbl, v in roc_items
    )

    return f"""
<div class="ecard" id="{ticker}">
  <div class="ect" style="border-left:4px solid {col}">
    <div class="ectitle">
      <span class="tk">{ticker}</span>
      <span class="cn">{m.get("name","")}</span>
      <span class="badge" style="background:{col}22;color:{col}">{m.get("sector","")}</span>
    </div>
    <div class="ecmeta">
      <span><span class="ml">Weight</span><b>{fmt_f(m.get("current_weight"))}%</b></span>
      <span><span class="ml">Price</span><b>{fmt_f(m.get("current_price"))} {m.get("currency","")}</b></span>
      <span><span class="ml">Days in ETF</span><b>{m.get("days_in_etf","?")}</b></span>
      <span><span class="ml">First seen</span><b>{m.get("first_seen","?")}</b></span>
    </div>
  </div>
  <div class="ecs">
    {score_badge(s)}
    <div class="sigs">{sigs}</div>
  </div>
  <div class="ecg">
    <div class="egcol"><div class="egt">Price ROC</div>{roc_rows}</div>
    <div class="egcol">
      <div class="egt">Weight</div>
      <div class="mr"><span>Current</span><span>{fmt_f(m.get("current_weight"))}%</span></div>
      <div class="mr"><span>At entry</span><span>{fmt_f(m.get("weight_at_entry"))}%</span></div>
      <div class="mr"><span>Change</span><span class="{"pos" if (m.get("weight_roc_since_entry") or 0)>=0 else "neg"}">{fmt_pct(m.get("weight_roc_since_entry"),True)}</span></div>
      <div class="mr"><span>Percentile</span><span>{fmt_f(m.get("weight_percentile"),1)}th</span></div>
      <div class="mr"><span>Z-score</span><span>{fmt_f(m.get("weight_zscore"),2)}</span></div>
    </div>
    <div class="egcol">
      <div class="egt">52-week</div>
      <div class="mr"><span>Drawdown</span><span class="{"pos" if (dd or 0)>=0 else "neg"}">{fmt_pct(dd)}</span></div>
      <div class="mr"><span>Consistency</span><span>{f"{mc*100:.0f}%" if mc is not None else "—"}</span></div>
      <div class="mr"><span>Weekly pts</span><span>{len(wp)}</span></div>
    </div>
  </div>
  {"" if not (sd or sw) else f'<div class="espark"><div><div class="spl">Daily price (20d)</div>{sd}</div><div><div class="spl">Weekly price (52w)</div>{sw}</div></div>'}
  {"" if not commentary else f'<div class="eai"><b>🤖 Groq:</b> {commentary}</div>'}
</div>"""


# ══════════════════════════════════════════════════════════════════════════════
# MONTHLY REPORT LOGIC
# ══════════════════════════════════════════════════════════════════════════════

def month_key(date_str: str) -> str:
    """'2026-06-25' → '2026-06'"""
    return date_str[:7]


def load_month_log(report_dir: Path, ym: str) -> list[dict]:
    """Load the JSON data log for a given YYYY-MM."""
    p = report_dir / f"{ym}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_month_log(report_dir: Path, ym: str, entries: list[dict]) -> None:
    p = report_dir / f"{ym}.json"
    p.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def append_day_to_log(report_dir: Path, day_data: dict, commentary: dict) -> None:
    """Append today's structured summary to the monthly JSON log."""
    ym      = month_key(day_data["date"])
    entries = load_month_log(report_dir, ym)

    # Dedup — replace if same date already exists
    entries = [e for e in entries if e.get("date") != day_data["date"]]

    new_entry_details = []
    for t in day_data["new_tickers"]:
        m = day_data["scored"].get(t, {})
        new_entry_details.append({
            "ticker":   t,
            "name":     m.get("name", t),
            "sector":   m.get("sector", ""),
            "weight":   m.get("current_weight"),
            "score":    m.get("confirmation_score"),
            "signals":  m.get("confirmation_signals", []),
            "price":    m.get("current_price"),
            "currency": m.get("currency", ""),
            "price_roc_20d":    m.get("price_roc_20d"),
            "weight_percentile":m.get("weight_percentile"),
            "drawdown":         m.get("drawdown_from_52w_high"),
            "commentary": commentary.get(t, ""),
        })

    exit_details = [
        {"ticker": e["ticker"], "name": e["name"],
         "sector": e.get("sector",""), "weight": e.get("weight_pct")}
        for e in day_data["exits"]
    ]

    # Top 5 weight movers summary
    mover_summary = [
        {"ticker": m["ticker"], "name": m["name"],
         "delta": m["weight_delta"], "weight": m.get("weight_pct")}
        for m in day_data["movers"][:5]
    ]

    entries.append({
        "date":         day_data["date"],
        "new_entries":  new_entry_details,
        "exits":        exit_details,
        "movers":       mover_summary,
        "n_holdings":   len(day_data["top20"]),
        "n_snapshots":  day_data["n_snapshots"],
    })

    # Sort newest-first for the log
    entries.sort(key=lambda x: x["date"], reverse=True)
    save_month_log(report_dir, ym, entries)
    log.info("Month log %s updated: %d entries", ym, len(entries))


# ══════════════════════════════════════════════════════════════════════════════
# HTML PAGE GENERATION
# ══════════════════════════════════════════════════════════════════════════════

CSS = """
:root{--bg:#0f172a;--sf:#1e293b;--bd:#334155;--tx:#e2e8f0;--mt:#94a3b8;--ac:#3b82f6;--pos:#10b981;--neg:#ef4444}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px;line-height:1.6}
a{color:var(--ac);text-decoration:none}.a:hover{text-decoration:underline}
.wrap{max-width:1100px;margin:0 auto;padding:24px 16px}
h1{font-size:1.5rem;font-weight:700;margin-bottom:4px}
h2{font-size:.9rem;font-weight:600;color:var(--ac);margin:28px 0 10px;border-bottom:1px solid var(--bd);padding-bottom:5px;text-transform:uppercase;letter-spacing:.06em}
.sub{color:var(--mt);font-size:.82rem;margin-bottom:18px}
.nav{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:20px;font-size:.82rem}
.nav a{color:var(--ac)}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px}
.stat{background:var(--sf);border:1px solid var(--bd);border-radius:8px;padding:10px 16px;min-width:120px}
.sv{font-size:1.4rem;font-weight:700;color:var(--ac)}
.sl{font-size:.7rem;color:var(--mt);text-transform:uppercase;letter-spacing:.05em}
.tip{background:#1d2d3e;border:1px solid #3b82f633;border-radius:8px;padding:12px 16px;margin-bottom:18px;font-size:.82rem;color:var(--mt)}
.tip b{color:var(--tx)}
/* day block */
.dayblock{border:1px solid var(--bd);border-radius:10px;margin-bottom:24px;overflow:hidden}
.dayhead{background:var(--sf);padding:12px 16px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.daydate{font-size:1rem;font-weight:700}
.daybadge{font-size:.72rem;padding:2px 8px;border-radius:4px;font-weight:600}
.day-body{padding:16px}
/* entry card */
.ecard{background:var(--sf);border:1px solid var(--bd);border-radius:8px;margin-bottom:12px;overflow:hidden}
.ect{padding:12px 14px 10px}
.ectitle{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:6px}
.tk{font-size:1.1rem;font-weight:700;color:#fff}
.cn{color:var(--mt);font-size:.83rem}
.badge{border-radius:4px;padding:2px 8px;font-size:.7rem;font-weight:500}
.ecmeta{display:flex;gap:16px;flex-wrap:wrap}
.ecmeta span{display:flex;flex-direction:column}
.ml{font-size:.68rem;color:var(--mt);text-transform:uppercase;letter-spacing:.04em}
.ecs{padding:8px 14px;border-top:1px solid var(--bd);background:#ffffff04}
.sbadge{border-radius:6px;padding:3px 10px;font-size:.78rem;font-weight:700;display:inline-block;margin-bottom:6px}
.sigs{display:flex;flex-direction:column;gap:2px}
.sig{font-size:.78rem;color:var(--mt)}
.ecg{display:grid;grid-template-columns:repeat(3,1fr);border-top:1px solid var(--bd)}
.egcol{padding:10px 14px;border-right:1px solid var(--bd)}
.egcol:last-child{border-right:none}
.egt{font-size:.68rem;text-transform:uppercase;letter-spacing:.05em;color:var(--ac);margin-bottom:6px;font-weight:600}
.mr{display:flex;justify-content:space-between;padding:2px 0;font-size:.8rem}
.mr span:first-child{color:var(--mt)}
.espark{display:flex;gap:20px;padding:10px 14px;border-top:1px solid var(--bd);flex-wrap:wrap}
.spl{font-size:.68rem;color:var(--mt);text-transform:uppercase;letter-spacing:.04em;margin-bottom:3px}
.eai{padding:8px 14px 12px;border-top:1px solid var(--bd);font-size:.82rem;color:var(--mt)}
.eai b{color:var(--tx)}
/* exits / movers inline */
.mini-exit{font-size:.82rem;color:var(--mt);padding:4px 0;border-bottom:1px solid var(--bd)}
.mini-exit:last-child{border-bottom:none}
.mini-exit .tk{font-size:.9rem;color:var(--neg)}
.mini-mover{font-size:.82rem;color:var(--mt);padding:4px 0;border-bottom:1px solid var(--bd)}
.mini-mover:last-child{border-bottom:none}
/* table */
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:7px 10px;color:var(--mt);font-size:.7rem;text-transform:uppercase;letter-spacing:.04em;border-bottom:1px solid var(--bd)}
td{padding:6px 10px;border-bottom:1px solid #1e2d3e;vertical-align:middle;font-size:.8rem}
tr:hover td{background:#ffffff05}
.tw{overflow-x:auto}
.pos{color:var(--pos);font-weight:600}
.neg{color:var(--neg);font-weight:600}
.bar{height:3px;border-radius:2px;margin-top:3px}
.none{color:var(--mt);font-style:italic;font-size:.82rem;padding:8px 0}
footer{margin-top:40px;color:var(--mt);font-size:.7rem;border-top:1px solid var(--bd);padding-top:14px}
"""


def render_day_block(entry: dict, is_latest: bool = False) -> str:
    """Render one day's data as a collapsible block within the monthly report."""
    date        = entry["date"]
    new_entries = entry.get("new_entries", [])
    exits_e     = entry.get("exits", [])
    movers_e    = entry.get("movers", [])

    badge_parts = []
    if new_entries:
        badge_parts.append(
            f'<span class="daybadge" style="background:#10b98122;color:#10b981">'
            f'{len(new_entries)} new</span>'
        )
    if exits_e:
        badge_parts.append(
            f'<span class="daybadge" style="background:#ef444422;color:#ef4444">'
            f'{len(exits_e)} exit{"s" if len(exits_e)>1 else ""}</span>'
        )
    if not new_entries and not exits_e:
        badge_parts.append(
            '<span class="daybadge" style="background:#334155;color:#94a3b8">no changes</span>'
        )
    badges = " ".join(badge_parts)

    # New entry mini-cards (full detail)
    cards_html = ""
    for ne in new_entries:
        t   = ne["ticker"]
        col = sc(ne.get("sector", ""))
        s   = ne.get("score", 0) or 0
        lbl, scol = SCORE_META.get(s, ("?", "#6b7280"))
        sigs_html = "".join(f'<div class="sig">{sg}</div>' for sg in ne.get("signals", []))
        commentary = ne.get("commentary", "")

        roc20 = ne.get("price_roc_20d")
        wpct  = ne.get("weight_percentile")
        dd    = ne.get("drawdown")

        cards_html += f"""
<div class="ecard" id="{date}-{t}">
  <div class="ect" style="border-left:4px solid {col}">
    <div class="ectitle">
      <span class="tk">{t}</span>
      <span class="cn">{ne.get("name","")}</span>
      <span class="badge" style="background:{col}22;color:{col}">{ne.get("sector","")}</span>
    </div>
    <div class="ecmeta">
      <span><span class="ml">Weight</span><b>{fmt_f(ne.get("weight"))}%</b></span>
      <span><span class="ml">Price</span><b>{fmt_f(ne.get("price"))} {ne.get("currency","")}</b></span>
      <span><span class="ml">20d ROC</span><b class="{"pos" if (roc20 or 0)>=0 else "neg"}">{fmt_pct(roc20, True)}</b></span>
      <span><span class="ml">Wt pctile</span><b>{fmt_f(wpct,1)}th</b></span>
      <span><span class="ml">52w DD</span><b>{fmt_pct(dd)}</b></span>
    </div>
  </div>
  <div class="ecs">
    <span class="sbadge" style="background:{scol}22;color:{scol};border:1px solid {scol}44">{s}/5 — {lbl}</span>
    <div class="sigs">{sigs_html}</div>
  </div>
  {"" if not commentary else f'<div class="eai"><b>🤖 Groq:</b> {commentary}</div>'}
</div>"""

    # Exits
    exits_html = ""
    if exits_e:
        exits_html = '<h2>Exits</h2>'
        for ex in exits_e:
            col = sc(ex.get("sector", ""))
            exits_html += (
                f'<div class="mini-exit">'
                f'<span class="tk">{ex["ticker"]}</span> '
                f'<span>{ex["name"][:32]}</span> '
                f'<span style="color:{col};font-size:.75rem;margin-left:6px">{ex.get("sector","")}</span>'
                f'</div>'
            )

    # Top movers (compact)
    movers_html = ""
    if movers_e:
        movers_html = '<h2>Top weight movers (20d)</h2>'
        for mv in movers_e:
            delta = mv.get("delta", 0) or 0
            dcls  = "pos" if delta > 0 else "neg"
            dsign = "+" if delta > 0 else ""
            movers_html += (
                f'<div class="mini-mover">'
                f'<b>{mv["ticker"]}</b> {mv["name"][:28]} '
                f'<span class="{dcls}">{dsign}{delta:.3f}%</span>'
                f'</div>'
            )

    body_content = ""
    if new_entries:
        body_content += f'<h2>New entries ({len(new_entries)})</h2>{cards_html}'
    body_content += exits_html
    body_content += movers_html
    if not body_content:
        body_content = '<p class="none">No entries, exits, or significant weight changes today.</p>'

    open_attr = " open" if is_latest else ""
    return f"""
<details class="dayblock"{open_attr}>
  <summary class="dayhead" style="cursor:pointer;list-style:none">
    <span class="daydate">{date}</span>
    {badges}
  </summary>
  <div class="day-body">{body_content}</div>
</details>"""


def generate_monthly_page(
    ym: str,
    entries: list[dict],
    prev_ym: Optional[str],
    next_ym: Optional[str],
    is_current: bool,
    report_dir: Path,
    day_data: Optional[dict] = None,
) -> str:
    """
    Generate the full HTML for a monthly report page.
    entries: list of day-log dicts, newest first.
    """
    month_label = datetime.datetime.strptime(ym + "-01", "%Y-%m-%d").strftime("%B %Y")
    all_new  = sum(len(e.get("new_entries", [])) for e in entries)
    all_exit = sum(len(e.get("exits", []))        for e in entries)

    nav_parts = []
    if prev_ym:
        prev_label = datetime.datetime.strptime(prev_ym + "-01", "%Y-%m-%d").strftime("%b %Y")
        nav_parts.append(f'<a href="{prev_ym}.html">← {prev_label}</a>')
    if next_ym:
        next_label = datetime.datetime.strptime(next_ym + "-01", "%Y-%m-%d").strftime("%b %Y")
        nav_parts.append(f'<a href="{"index" if is_current and not next_ym else next_ym}.html">{next_label} →</a>')
    if is_current:
        nav_parts.append('<a href="index.html">Latest ↑</a>' if not is_current else "")
    nav_html = " · ".join(p for p in nav_parts if p)

    # Day blocks — first entry is always open
    blocks = "".join(
        render_day_block(e, is_latest=(i == 0))
        for i, e in enumerate(entries)
    )
    if not blocks:
        blocks = '<p class="none">No data yet for this month.</p>'

    # Watchlist table from most recent day_data (only on current month)
    watchlist_html = ""
    if day_data and is_current:
        top30  = day_data.get("top20", [])
        scored = day_data.get("scored", {})
        wrows  = ""
        for h in top30:
            t   = h["ticker"]
            col = sc(h.get("sector", ""))
            m   = scored.get(t, {})
            s   = m.get("confirmation_score", 0) or 0
            scol= SCORE_META.get(s, ("?","#6b7280"))[1]
            roc20 = m.get("price_roc_20d")
            rcls  = "pos" if (roc20 or 0) >= 0 else "neg"
            wpct  = m.get("weight_percentile")
            dd    = m.get("drawdown_from_52w_high")
            w     = h.get("weight_pct") or 0
            bw    = min(100, int(w / 10 * 100))
            wrows += (
                f"<tr>"
                f"<td><b>{t}</b></td>"
                f"<td style=\"color:{col}\">{h.get('sector','')[:16]}</td>"
                f"<td>{w:.2f}%<div class='bar' style='width:{bw}px;background:{col}'></div></td>"
                f"<td class='{rcls}'>{fmt_pct(roc20, True)}</td>"
                f"<td>{fmt_f(wpct,1)}th</td>"
                f"<td>{fmt_pct(dd)}</td>"
                f"<td><span style='color:{scol};font-weight:700'>{s}/5</span></td>"
                f"</tr>"
            )
        if wrows:
            watchlist_html = f"""
<h2>Top 20 holdings — {day_data["date"]}</h2>
<div class="tw"><table>
<tr><th>Ticker</th><th>Sector</th><th>Weight</th><th>20d ROC</th><th>Wt pctile</th><th>52w DD</th><th>Score</th></tr>
{wrows}
</table></div>"""

        # Sector breakdown
        sec_rows = ""
        for s_row in day_data.get("sectors", []):
            col = sc(s_row.get("sector", ""))
            tw  = s_row.get("total_weight") or 0
            bw  = min(120, int(tw / 40 * 120))
            sec_rows += (
                f"<tr>"
                f"<td style=\"color:{col}\">{s_row['sector']}</td>"
                f"<td>{s_row['count']}</td>"
                f"<td>{tw:.2f}%<div class='bar' style='width:{bw}px;background:{col}'></div></td>"
                f"</tr>"
            )
        if sec_rows:
            watchlist_html += f"""
<h2>Sector breakdown</h2>
<div class="tw"><table>
<tr><th>Sector</th><th>Holdings</th><th>Total weight</th></tr>
{sec_rows}
</table></div>"""

    n_days = len(entries)
    status = "Live" if is_current else "Archived"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Velocity Tracker — {month_label}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<h1>Velocity Tracker — {month_label}
  <span style="font-size:.75rem;background:#334155;color:#94a3b8;border-radius:4px;padding:2px 8px;margin-left:8px;vertical-align:middle">{status}</span>
</h1>
<p class="sub">iShares MSCI World Momentum Factor ETF · {n_days} trading days · {all_new} new entries · {all_exit} exits</p>

<div class="nav">
  {nav_html if nav_html else '<span style="color:var(--mt)">No other months yet</span>'}
</div>

<div class="tip">
  <b>Score 0–5:</b> ✅ Price ROC (20d) &gt; 0 · ✅ Weight growing since entry ·
  ✅ Weight &gt; 50th pctile · ✅ Price above entry · ✅ Within 20% of 52w high.
  <b>4–5 = act · 3 = watch · 0–2 = wait</b>
</div>

<div class="stats">
  <div class="stat"><div class="sv">{n_days}</div><div class="sl">Trading days</div></div>
  <div class="stat"><div class="sv">{all_new}</div><div class="sl">New entries</div></div>
  <div class="stat"><div class="sv">{all_exit}</div><div class="sl">Exits</div></div>
</div>

{blocks}

{watchlist_html}

<footer>Velocity · Pure maths + Groq (optional) · Source: iShares UK IWMO CSV · {month_label}</footer>
</div>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# MONTH ROLLOVER
# ══════════════════════════════════════════════════════════════════════════════

def get_all_months(report_dir: Path) -> list[str]:
    """Return sorted list of all YYYY-MM months with a .json log file."""
    months = sorted(
        p.stem for p in report_dir.glob("????-??.json")
    )
    return months


def rollover_if_needed(report_dir: Path, today: str) -> None:
    """
    On the 1st of a new month: index.html already contains last month's data.
    We seal it as YYYY-MM.html and the caller will create a fresh index.html.
    This function just checks if a seal is needed and does it.
    """
    index_path = report_dir / "index.html"
    if not index_path.exists():
        return

    # Detect what month index.html currently represents by checking JSON logs
    today_ym = month_key(today)
    # Find most recent json that isn't today's month
    months = get_all_months(report_dir)
    if not months:
        return

    latest_logged_ym = months[-1]
    if latest_logged_ym < today_ym:
        # We've rolled into a new month — seal the old index.html
        sealed = report_dir / f"{latest_logged_ym}.html"
        if not sealed.exists():
            import shutil
            shutil.copy(index_path, sealed)
            log.info("Sealed %s → %s", index_path.name, sealed.name)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(description="Velocity — monthly rolling reports")
    p.add_argument("--db",          default=str(DEFAULT_DB_PATH))
    p.add_argument("--report-dir",  default=str(DEFAULT_REPORT_DIR))
    p.add_argument("--report-only", action="store_true",
                   help="Regenerate HTML from existing DB/logs without fetching")
    p.add_argument("--no-ai",       action="store_true",
                   help="Skip Groq commentary even if GROQ_API_KEY is set")
    p.add_argument("--mock-csv",    help="Use local CSV file instead of fetching")
    args = p.parse_args()

    db_path    = Path(args.db)
    report_dir = Path(args.report_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    conn = get_db(db_path)

    # ── 1. Fetch or load data ──────────────────────────────────────────────
    if args.report_only:
        latest = conn.execute(
            "SELECT fetch_date FROM snapshots ORDER BY fetch_date DESC LIMIT 1"
        ).fetchone()
        if not latest:
            log.error("No data in DB. Run without --report-only first.")
            sys.exit(1)
        today = latest["fetch_date"]
        log.info("Report-only: using %s", today)
    else:
        if args.mock_csv:
            raw = Path(args.mock_csv).read_text(encoding="utf-8-sig")
            hdate, rows = parse_csv(raw)
        else:
            hdate, rows = fetch_csv()
        store_snapshot(conn, hdate, rows)
        today = hdate.isoformat()

    today_ym = month_key(today)

    # ── 2. Rollover check (seal previous month if we've just crossed into new month) ──
    rollover_if_needed(report_dir, today)

    # ── 3. Build today's analysis ──────────────────────────────────────────
    day_data = build_day_data(conn, today)
    log.info(
        "Analysis: %d new, %d exits, %d movers",
        len(day_data["new_tickers"]), len(day_data["exits"]), len(day_data["movers"])
    )

    # ── 4. Groq commentary ─────────────────────────────────────────────────
    commentary = {}
    if not args.no_ai and day_data["new_tickers"]:
        log.info("Requesting Groq commentary for %d tickers …", len(day_data["new_tickers"]))
        commentary = get_groq_commentary(day_data["new_tickers"], day_data["scored"])

    # ── 5. Append to monthly log ───────────────────────────────────────────
    append_day_to_log(report_dir, day_data, commentary)

    # ── 6. Determine prev/next months for nav links ────────────────────────
    all_months = sorted(set(get_all_months(report_dir) + [today_ym]))
    idx        = all_months.index(today_ym)
    prev_ym    = all_months[idx - 1] if idx > 0           else None
    next_ym    = all_months[idx + 1] if idx < len(all_months) - 1 else None

    # ── 7. Regenerate index.html (current month) ───────────────────────────
    current_entries = load_month_log(report_dir, today_ym)
    html = generate_monthly_page(
        ym=today_ym,
        entries=current_entries,
        prev_ym=prev_ym,
        next_ym=next_ym,
        is_current=True,
        report_dir=report_dir,
        day_data=day_data,
    )
    (report_dir / "index.html").write_text(html, encoding="utf-8")
    log.info("index.html updated (%d day entries for %s)", len(current_entries), today_ym)

    # ── 8. Regenerate all archive pages (so nav links stay fresh) ──────────
    for ym in all_months:
        if ym == today_ym:
            continue
        ym_idx   = all_months.index(ym)
        ym_prev  = all_months[ym_idx - 1] if ym_idx > 0                else None
        ym_next  = all_months[ym_idx + 1] if ym_idx < len(all_months)-1 else None
        ym_entries = load_month_log(report_dir, ym)
        arch_html  = generate_monthly_page(
            ym=ym,
            entries=ym_entries,
            prev_ym=ym_prev,
            next_ym=ym_next,
            is_current=False,
            report_dir=report_dir,
        )
        arch_path = report_dir / f"{ym}.html"
        arch_path.write_text(arch_html, encoding="utf-8")
        log.info("Archive %s updated", arch_path.name)

    conn.close()
    log.info("Done.")


if __name__ == "__main__":
    main()
