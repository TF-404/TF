#!/usr/bin/env python3
"""Recompute HyperGrok daily P&L (Asia/Taipei calendar days) and write data/daily-pnl.json.

Daily P&L = sum(closedPnl) - USDC fill fees + funding.
Deposits, withdrawals and their transfer fees are not P&L.
A trade is a unique oid that had at least one fill in that Taipei day.
% = day's P&L / account value at 00:00 Taipei; if that value is 0
(for example the account was funded later that day), use the last
account value before the first fill of the day.

Idempotent: safe to run repeatedly. The days payload is compared without
updatedAt, and the file is rewritten only when a day actually changed.
Set HL_PNL_NO_PUSH=1 (the GitHub Action does this) to skip the built-in
git commit; the workflow commits data/daily-pnl.json itself.
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ACCOUNT = "0xc70E03319051E7C4CE2556c064022090869012e4"
API = "https://api.hyperliquid.xyz/info"
TZ = timezone(timedelta(hours=8), name="Asia/Taipei")


def _default_site_dir() -> Path:
    here = Path(__file__).resolve().parent
    # scripts/daily_pnl.py sits one level below the Pages site root.
    if here.name == "scripts":
        return here.parent
    return here


SITE_DIR = Path(os.environ.get("HL_SITE_DIR", str(_default_site_dir())))
OUT_REL = Path("data/daily-pnl.json")
OUT_PATH = SITE_DIR / OUT_REL
REPO_DIR = Path(os.environ.get("HL_PNL_REPO", str(SITE_DIR)))

# Rounding used for a stable, comparable JSON payload.
PNL_DEC = 6
PCT_DEC = 4
VAL_DEC = 6


def r(v: float, n: int) -> float:
    return round(float(v) + 0.0, n)


def post(payload: dict, retries: int = 6, timeout: int = 40) -> Any:
    data = json.dumps(payload).encode("utf-8")
    last: Exception | None = None
    ctx = ssl.create_default_context()
    for i in range(retries):
        try:
            req = urllib.request.Request(
                API,
                data=data,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            last = e
            code = getattr(e, "code", None)
            # Retry timeouts, 502/503/429, and short network blips.
            if code not in (None, 408, 425, 429, 500, 502, 503, 504) and not isinstance(
                e, (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError)
            ):
                raise
            time.sleep(min(12.0, 1.2 * (2 ** i)))
    raise RuntimeError(f"Hyperliquid info API failed after {retries} retries ({payload.get('type')}): {last}")


def taipei_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%Y-%m-%d")


def midnight_ms(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=TZ)
    return int(dt.timestamp() * 1000)


def today_date(now_ms: int | None = None) -> str:
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return taipei_date(now_ms)


def paginate_time(kind: str, start_time: int = 0, page_size_hint: int = 500) -> list[dict]:
    """Walk a time-ranged info endpoint, deduping overlap on the last timestamp."""
    out: list[dict] = []
    seen: set[str] = set()
    cur = int(start_time)
    empty_pages = 0
    while True:
        batch = post({"type": kind, "user": ACCOUNT, "startTime": cur})
        if not isinstance(batch, list) or not batch:
            break
        new = 0
        max_t = cur
        for row in batch:
            if not isinstance(row, dict):
                continue
            t = row.get("time")
            key = json.dumps(row, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
            new += 1
            if isinstance(t, (int, float)) and t > max_t:
                max_t = int(t)
        if new == 0:
            empty_pages += 1
            if empty_pages >= 2:
                break
            # Advance 1ms to escape a stuck inclusive cursor.
            cur = cur + 1
            continue
        empty_pages = 0
        if kind == "userFillsByTime":
            done = len(batch) < 2000
        else:
            done = len(batch) < page_size_hint
        if done:
            break
        if max_t <= cur:
            break
        cur = max_t
        if len(out) > 50000:
            break
    return out


def n0(v: Any) -> float:
    try:
        if v is None or v == "":
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def load_existing(path: Path) -> dict:
    if not path.is_file():
        return {"days": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"days": []}
        days = data.get("days")
        if not isinstance(days, list):
            data["days"] = []
        return data
    except (OSError, json.JSONDecodeError):
        return {"days": []}


def comparable_days(days: list[dict]) -> list[dict]:
    keys = ("date", "pnl", "pct", "startValue", "closedPnl", "fees", "funding", "trades", "fills", "complete")
    out = []
    for d in days:
        out.append({k: d.get(k) for k in keys})
    return out


def snapshots_from_portfolio(port: Any) -> list[tuple[int, float]]:
    points: dict[int, float] = {}
    if not isinstance(port, list):
        return []
    for item in port:
        if not (isinstance(item, list) and len(item) == 2 and isinstance(item[1], dict)):
            continue
        name = str(item[0] or "")
        # perpDay / perpWeek / perpMonth / perpAllTime are the perp sub-account.
        # They share timestamps with the account series and would replace the
        # real account value, often with 0, so the start-of-day % would be wrong.
        if name.lower().startswith("perp"):
            continue
        hist = item[1].get("accountValueHistory")
        if not isinstance(hist, list):
            continue
        for row in hist:
            if not (isinstance(row, list) and len(row) >= 2):
                continue
            t, v = row[0], row[1]
            if not isinstance(t, (int, float)):
                continue
            points[int(t)] = n0(v)
    return sorted(points.items())


def value_at_or_before(snaps: list[tuple[int, float]], t: int) -> float | None:
    last = None
    for ts, v in snaps:
        if ts <= t:
            last = v
        else:
            break
    return last


def last_value_in(snaps: list[tuple[int, float]], start: int, end: int) -> float | None:
    last = None
    for ts, v in snaps:
        if start <= ts < end:
            last = v
        elif ts >= end:
            break
    return last


def current_equity() -> tuple[float, float]:
    """Return (equity, unrealizedPnl) for the unified or standard account."""
    ch = post({"type": "clearinghouseState", "user": ACCOUNT}) or {}
    spot = post({"type": "spotClearinghouseState", "user": ACCOUNT}) or {}
    abstr = post({"type": "userAbstraction", "user": ACCOUNT})
    mode = abstr if isinstance(abstr, str) else ""
    positions = ch.get("assetPositions") or []
    upnl = 0.0
    for row in positions:
        p = (row or {}).get("position") or row or {}
        upnl += n0(p.get("unrealizedPnl"))
    margin = ch.get("marginSummary") or {}
    perp_value = n0(margin.get("accountValue"))
    usdc_total = 0.0
    for b in (spot.get("balances") or []):
        if str(b.get("coin") or "").upper() == "USDC":
            usdc_total = n0(b.get("total"))
            break
    unified = mode in ("unifiedAccount", "portfolioMargin")
    equity = (usdc_total + upnl) if unified else (perp_value + usdc_total)
    return equity, upnl


def reconstruct_equity_series(
    fills: list[dict],
    funding: list[dict],
    ledger: list[dict],
) -> list[tuple[int, float]]:
    """Realized equity after each non-mark event (excludes unrealized)."""
    events: list[tuple[int, float, str]] = []
    acc = ACCOUNT.lower()
    for row in ledger:
        t = int(n0(row.get("time")))
        d = row.get("delta") or {}
        typ = str(d.get("type") or "")
        if typ != "send":
            # Also handle explicit deposit / withdraw shapes if they appear.
            usdc = n0(d.get("usdc") or d.get("usdcValue") or d.get("amount"))
            if typ in ("deposit", "spotDeposit", "accountActivation"):
                events.append((t, abs(usdc), "deposit"))
            elif typ in ("withdraw", "withdrawal", "spotWithdrawal"):
                events.append((t, -abs(usdc) - n0(d.get("fee")), "withdraw"))
            continue
        token = str(d.get("token") or "").upper()
        amount = n0(d.get("usdcValue") if d.get("usdcValue") is not None else d.get("amount"))
        fee = n0(d.get("fee")) if str(d.get("feeToken") or "USDC").upper() in ("", "USDC") else 0.0
        src = str(d.get("user") or "").lower()
        dest = str(d.get("destination") or "").lower()
        if dest == acc and src != acc:
            events.append((t, amount, "deposit"))
        elif src == acc and dest != acc:
            events.append((t, -(amount + fee), "withdraw"))
    for f in fills:
        t = int(n0(f.get("time")))
        pnl = n0(f.get("closedPnl"))
        fee = n0(f.get("fee")) if str(f.get("feeToken") or "USDC").upper() == "USDC" else 0.0
        events.append((t, pnl - fee, "fill"))
    for row in funding:
        t = int(n0(row.get("time")))
        events.append((t, n0((row.get("delta") or {}).get("usdc")), "funding"))
    events.sort(key=lambda x: x[0])
    series: list[tuple[int, float]] = []
    eq = 0.0
    for t, delta, _kind in events:
        eq += delta
        series.append((t, eq))
    return series


def start_value_for_day(
    date_str: str,
    snaps: list[tuple[int, float]],
    reconstructed: list[tuple[int, float]],
    first_fill_ms: int | None,
) -> float | None:
    start = midnight_ms(date_str)
    end = midnight_ms((datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d"))
    cutoff = first_fill_ms if first_fill_ms is not None else end

    mid = value_at_or_before(snaps, start)
    if mid is None:
        mid = value_at_or_before(reconstructed, start)
    if mid is not None and mid > 1e-9:
        return mid

    # Account was empty at 00:00 (funded later). Use the last known
    # value before the first fill so the % reflects trading capital.
    later = last_value_in(snaps, start, cutoff)
    if later is None or later <= 1e-9:
        later = last_value_in(reconstructed, start, cutoff)
    if later is not None and later > 1e-9:
        return later
    return mid if mid is not None else later


def bucket_days(
    fills: list[dict],
    funding: list[dict],
    snaps: list[tuple[int, float]],
    reconstructed: list[tuple[int, float]],
    now_ms: int,
) -> list[dict]:
    today = today_date(now_ms)
    buckets: dict[str, dict] = defaultdict(lambda: {
        "closedPnl": 0.0,
        "fees": 0.0,
        "funding": 0.0,
        "oids": set(),
        "fills": 0,
        "first_fill": None,
    })
    for f in fills:
        t = int(n0(f.get("time")))
        if t <= 0:
            continue
        day = taipei_date(t)
        b = buckets[day]
        b["closedPnl"] += n0(f.get("closedPnl"))
        if str(f.get("feeToken") or "USDC").upper() == "USDC":
            b["fees"] += n0(f.get("fee"))
        oid = f.get("oid")
        if oid is not None:
            b["oids"].add(oid)
        b["fills"] += 1
        if b["first_fill"] is None or t < b["first_fill"]:
            b["first_fill"] = t
    for row in funding:
        t = int(n0(row.get("time")))
        if t <= 0:
            continue
        day = taipei_date(t)
        buckets[day]["funding"] += n0((row.get("delta") or {}).get("usdc"))

    days = []
    for date_str in sorted(buckets):
        b = buckets[date_str]
        pnl = b["closedPnl"] - b["fees"] + b["funding"]
        start_v = start_value_for_day(date_str, snaps, reconstructed, b["first_fill"])
        pct = None
        if start_v is not None and start_v > 1e-9:
            pct = r(pnl / start_v * 100.0, PCT_DEC)
        complete = date_str < today
        days.append({
            "date": date_str,
            "pnl": r(pnl, PNL_DEC),
            "pct": pct,
            "startValue": r(start_v, VAL_DEC) if start_v is not None else None,
            "closedPnl": r(b["closedPnl"], PNL_DEC),
            "fees": r(b["fees"], PNL_DEC),
            "funding": r(b["funding"], PNL_DEC),
            "trades": int(len(b["oids"])),
            "fills": int(b["fills"]),
            "complete": complete,
        })
    return days


def merge_days(existing: list[dict], fresh: list[dict], earliest_api_day: str | None) -> list[dict]:
    """Keep stored days that the API can no longer see; replace overlapping dates."""
    by_date: dict[str, dict] = {}
    for d in existing:
        if not isinstance(d, dict) or not d.get("date"):
            continue
        by_date[str(d["date"])] = d
    for d in fresh:
        by_date[d["date"]] = d
    if earliest_api_day:
        # Drop incomplete stale rows that fall inside the API window but
        # were not returned this run only if we successfully recomputed
        # them. Days older than the window stay as they were.
        pass
    return [by_date[k] for k in sorted(by_date)]


def build_payload(days: list[dict], now_ms: int) -> dict:
    iso = datetime.fromtimestamp(now_ms / 1000, TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    return {
        "version": 1,
        "timezone": "Asia/Taipei",
        "cutoff": "00:00",
        "account": ACCOUNT,
        "method": "sum(closedPnl) - USDC fill fees + funding; deposits and withdrawals excluded",
        "updatedAt": iso,
        "updatedAtMs": now_ms,
        "days": days,
    }


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def git_repo_root(start: Path) -> Path | None:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return Path(out) if out else None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def pushes_enabled() -> bool:
    flag = os.environ.get("HL_PNL_NO_PUSH", "").strip().lower()
    if flag in {"1", "true", "yes"}:
        return False
    # GitHub Actions commits with GITHUB_TOKEN in the workflow. A second
    # push from this process would run before git identity is configured.
    if os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true":
        return False
    return True


def maybe_push(path: Path, changed: bool) -> str:
    if not changed:
        return "no change in daily P&L days; skipped push"
    repo = git_repo_root(REPO_DIR)
    if repo is None:
        return f"wrote {path}; no git repo at {REPO_DIR}, skipped push"
    rel = os.path.relpath(path, repo)
    try:
        subprocess.check_call(["git", "-C", str(repo), "add", "--", rel], stdout=subprocess.DEVNULL)
        status = subprocess.check_output(
            ["git", "-C", str(repo), "status", "--porcelain", "--", rel], text=True
        ).strip()
        if not status:
            return "no change in daily P&L days; skipped push"
        subprocess.check_call(
            [
                "git", "-C", str(repo),
                "commit", "-m", "Update daily P&L record",
                "--", rel,
            ],
            stdout=subprocess.DEVNULL,
        )
        subprocess.check_call(["git", "-C", str(repo), "push"], stdout=subprocess.DEVNULL)
        sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        return f"updated {rel} and pushed {sha[:8]}"
    except subprocess.CalledProcessError as e:
        return f"wrote {path}; git push failed ({e})"


def main() -> int:
    now_ms = int(time.time() * 1000)
    existing = load_existing(OUT_PATH)
    existing_days = existing.get("days") if isinstance(existing.get("days"), list) else []

    fills = paginate_time("userFillsByTime", 0, page_size_hint=2000)
    funding = paginate_time("userFunding", 0, page_size_hint=500)
    ledger = paginate_time("userNonFundingLedgerUpdates", 0, page_size_hint=500)
    port = post({"type": "portfolio", "user": ACCOUNT})
    snaps = snapshots_from_portfolio(port)
    try:
        equity, _upnl = current_equity()
        snaps = sorted(set(snaps + [(now_ms, equity)]))
    except Exception:
        pass
    reconstructed = reconstruct_equity_series(fills, funding, ledger)

    fresh = bucket_days(fills, funding, snaps, reconstructed, now_ms)
    earliest = min((d["date"] for d in fresh), default=None)
    days = merge_days(existing_days, fresh, earliest)
    payload = build_payload(days, now_ms)

    prev_cmp = comparable_days(
        [d for d in existing_days if isinstance(d, dict)]
    )
    new_cmp = comparable_days(days)
    changed = prev_cmp != new_cmp

    # Rewriting on an unchanged day would only move updatedAt and produce
    # an empty commit. Leave the previous file bytes alone.
    if changed or not OUT_PATH.is_file():
        write_json(OUT_PATH, payload)
    n_trade_days = sum(1 for d in days if int(d.get("trades") or 0) > 0)
    if not changed and OUT_PATH.is_file():
        push_note = "no change in daily P&L days; skipped push"
    elif not pushes_enabled():
        push_note = "git push left to the caller"
    else:
        push_note = maybe_push(OUT_PATH, True)
    summary = (
        f"wrote {OUT_PATH} with {len(days)} day(s), {n_trade_days} trading day(s); "
        + push_note
    )
    print(summary)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"daily_pnl.py failed: {e}", file=sys.stderr)
        print(f"daily_pnl.py failed: {e}")
        sys.exit(1)
