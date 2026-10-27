#!/usr/bin/env python3
"""Crypto day-trade pipeline for trader-crypto: scan, entry, plan, size and
risk check for spot crypto pairs against the US dollar, long side only.
Standard library only. It never places an order.

    python3 crypto.py scan                          # daily setups on the config's pairs -> trader/crypto/watchlist.json
    python3 crypto.py scan --symbols BTC,ETH,SOL,DOGE
    python3 crypto.py entry SOL                     # 15-minute range breakout -> trader/crypto/entry.json
    python3 crypto.py entry SOL --lookback 16 --min-vol-x 1.5
    python3 crypto.py plan                          # from entry.json: 3 ATR stop, 2R target -> plan.json
    python3 crypto.py plan --symbol ETH --entry 3950 --stop pct:1.5 --target 2R
    python3 crypto.py size                          # plan.json + trader/config.json -> orders.json
    python3 crypto.py check                         # orders.json against your limits -> PASS / BLOCK
    python3 crypto.py record --symbol SOL --qty 0.35 --price 192.40 --stop 189.10   # after a fill

Data: Coinbase Exchange public candles (api.exchange.coinbase.com, no key),
one request per pair, granularity 15 minutes or 1 day, newest bar dropped
until it is complete. Prices and qty round to the pair's own increments.

Why this differs from the stock pipeline:
  - Crypto trades 24/7: there is no opening range, no session, no flat-by
    close. The entry is a Donchian breakout: the latest COMPLETED 15-minute
    bar closes above the highest high of the --lookback bars before it.
  - Alpaca crypto has no bracket orders. The size step writes two orders:
    the entry (market, gtc) and, to send right after the fill, a protective
    stop_limit sell. The target is a price alert, not a resting order (two
    resting sells would hold the same coins twice).
  - Fees are real money at this size. Every loss figure includes the fee on
    both sides ("crypto.fee_pct" per side, default 0.25, Alpaca's top taker
    rate), and the stop's limit price (stop minus "crypto.stop_slip_pct"),
    not the stop price, is the worst fill used for sizing.

trader/config.json: needs "allow_crypto": true, and uses budget,
cash_buffer_pct, max_position_pct, max_order_usd, min_order_usd,
max_loss_per_trade_usd, max_daily_loss_usd, min_reward_risk and blocked from
the stock rules, plus an optional "crypto" block (defaults below).

Exit codes: 0 = a hit / a trigger / a plan / an order / every order passed;
1 = nothing found, refused, blocked or an error (the reason is one line).
"""

import argparse
import datetime
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP

UA = "trader-crypto/1.0 (+https://github.com/Gratinallezz77/trader-flux)"
API = "https://api.exchange.coinbase.com"
DIR = os.path.join("trader", "crypto")
CONFIG = os.path.join("trader", "config.json")
STATE = os.path.join("trader", "state", "crypto.json")
CRYPTO_DEFAULTS = {
    "symbols": ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "LTC", "DOT"],
    "fee_pct": 0.25, "stop_slip_pct": 0.5, "lookback_bars": 16, "atr_mult": 3,
    "reward_r": 2, "min_stop_pct": 1.0, "max_trades_per_day": 3, "max_quote_age_min": 15,
}
DEFAULTS = {
    "budget": None, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_usd": 4000,
    "min_order_usd": 5, "max_loss_per_trade_usd": 50, "max_daily_loss_usd": 150,
    "min_reward_risk": 1.0, "blocked": [], "allow_crypto": False, "paper": True,
}
ATR_BARS = 14


class Refused(Exception):
    pass


# ---------------------------------------------------------------- helpers

def D(x, what="a number"):
    try:
        d = Decimal(str(x))
    except (InvalidOperation, ValueError):
        raise Refused("%s is not %s: %r" % (what, "a number", x))
    if not d.is_finite():
        raise Refused("%s is not a number: %r" % (what, x))
    return d


def floor_to(x, inc):
    return (Decimal(x) / inc).to_integral_value(ROUND_FLOOR) * inc


def round_to(x, inc):
    return (Decimal(x) / inc).to_integral_value(ROUND_HALF_UP) * inc


def money(x):
    return "$" + format(Decimal(x).quantize(Decimal("0.01"), ROUND_HALF_UP), ",")


def price_str(x, inc):
    return format(round_to(x, inc).normalize(), "f")


def pair(sym):
    s = sym.upper().replace("/", "-").strip()
    return s if "-" in s else s + "-USD"


def alpaca_symbol(sym):
    return pair(sym).replace("-", "/")


def base(sym):
    return pair(sym).split("-")[0]


def utc_now(text=None):
    if text:
        try:
            return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            raise Refused("--now must look like 2026-10-27 14:30 (UTC), got %r" % text)
    return datetime.datetime.now(datetime.timezone.utc)


def stamp(t):
    return t.strftime("%Y-%m-%d %H:%M UTC")


def read_json(path, what, required=True):
    if not os.path.exists(path):
        if required:
            raise Refused("no %s at %s" % (what, path))
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise Refused("cannot read %s %s: %s" % (what, path, e))


def write_json(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def read_config(path=CONFIG):
    cfg = dict(DEFAULTS)
    cfg.update(read_json(path, "config", required=False) or {})
    c = dict(CRYPTO_DEFAULTS)
    c.update(cfg.get("crypto") or {})
    cfg["crypto"] = c
    return cfg


# ---------------------------------------------------------------- data

def get(path, offline=None):
    if offline:
        doc = read_json(offline, "offline file")
        key = path.split("?")[0]
        if key not in doc:
            raise Refused("offline file has no %s" % key)
        return doc[key]
    url = API + path
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise Refused("%s is not a Coinbase pair" % path.split("/")[2])
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise Refused("Coinbase answered %s for %s" % (e.code, path))
        except (urllib.error.URLError, OSError, ValueError) as e:
            if attempt < 2:
                time.sleep(2)
                continue
            raise Refused("cannot reach Coinbase: %s" % e)


def product(sym, offline=None):
    p = get("/products/%s" % pair(sym), offline)
    if p.get("trading_disabled") or p.get("status") != "online":
        raise Refused("%s is not trading on Coinbase right now" % pair(sym))
    return {"price_inc": D(p["quote_increment"]), "qty_inc": D(p["base_increment"]),
            "min_funds": D(p.get("min_market_funds") or "1")}


def candles(sym, seconds, now, offline=None):
    """Completed bars, oldest first: dicts t (UTC start), o, h, l, c, v."""
    rows = get("/products/%s/candles?granularity=%d" % (pair(sym), seconds), offline)
    bars = []
    for r in rows:
        t = datetime.datetime.fromtimestamp(r[0], datetime.timezone.utc)
        if t + datetime.timedelta(seconds=seconds) > now:
            continue                                    # still forming
        bars.append({"t": t, "l": D(r[1]), "h": D(r[2]), "o": D(r[3]), "c": D(r[4]), "v": D(r[5])})
    bars.sort(key=lambda b: b["t"])
    return bars


def last_price(sym, offline=None):
    t = get("/products/%s/ticker" % pair(sym), offline)
    when = t.get("time")
    at = None
    if when:
        try:
            at = datetime.datetime.fromisoformat(when.replace("Z", "+00:00"))
        except ValueError:
            at = None
    return D(t["price"]), at


# ---------------------------------------------------------------- indicators

def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    out = []
    for i in range(n, len(gains) + 1):
        if i > n:
            ag = (ag * (n - 1) + gains[i - 1]) / n
            al = (al * (n - 1) + losses[i - 1]) / n
        out.append(Decimal(100) if al == 0 else 100 - 100 / (1 + ag / al))
    return out


def atr(bars, n=ATR_BARS):
    trs = []
    for i, b in enumerate(bars):
        prev = bars[i - 1]["c"] if i else b["o"]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - prev), abs(b["l"] - prev)))
    use = trs[-n:]
    return sum(use) / len(use) if use else None


def setups(closes):
    tags = []
    last = closes[-1]
    if len(closes) >= 21 and last > max(closes[-21:-1]):
        tags.append("breakout_20d")
    if len(closes) >= 21:
        ma_now = sum(closes[-20:]) / 20
        ma_prev = sum(closes[-21:-1]) / 20
        if closes[-2] < ma_prev and last > ma_now:
            tags.append("reclaim_ma20")
    if len(closes) >= 16:
        run, flag = closes[-16:-5], closes[-5:]
        if run[0] > 0 and (run[-1] / run[0] - 1) > Decimal("0.10") and (max(flag) / min(flag) - 1) < Decimal("0.05"):
            tags.append("bull_flag")
    r = rsi(closes)
    if r and r[-1] < 30:
        tags.append("oversold")
    if r and len(r) >= 2 and r[-2] < 30 and last > closes[-2]:
        tags.append("oversold_bounce")
    return tags, (r[-1] if r else None)


# ---------------------------------------------------------------- commands

def cmd_scan(a, cfg):
    now = utc_now(a.now)
    syms = [s for s in (a.symbols.split(",") if a.symbols else cfg["crypto"]["symbols"]) if s.strip()]
    hits, failed = [], []
    for s in syms:
        try:
            bars = candles(s, 86400, now, a.offline)
        except Refused as e:
            failed.append("%s (%s)" % (base(s), e))
            continue
        if len(bars) < 21:
            failed.append("%s (only %d days)" % (base(s), len(bars)))
            continue
        closes = [b["c"] for b in bars]
        tags, r = setups(closes)
        usd_vol = bars[-1]["v"] * bars[-1]["c"]
        if usd_vol < D(a.min_usd_volume):
            continue
        if tags:
            hits.append({"symbol": base(s), "pair": pair(s), "close": float(closes[-1]),
                         "change_pct": float(round((closes[-1] / closes[-2] - 1) * 100, 2)),
                         "rsi14": float(round(r, 1)) if r is not None else None,
                         "usd_volume": float(round(usd_vol)), "setups": tags,
                         "day": bars[-1]["t"].strftime("%Y-%m-%d")})
        if not a.offline:
            time.sleep(0.2)
    hits.sort(key=lambda h: -h["usd_volume"])
    print("Crypto daily scan, %s, daily closes through the last completed UTC day (Coinbase)." % stamp(now))
    print("Fetched %d of %d pairs%s." % (len(syms) - len(failed), len(syms),
                                         " (%d failed: %s)" % (len(failed), "; ".join(failed)) if failed else ""))
    if not hits:
        print("No setups.")
    else:
        print("%d pairs matched a setup, most traded first:" % len(hits))
        for h in hits:
            print("  %-5s %14s  %+6.2f%%  RSI %5s  %s" % (h["symbol"], "$" + format(Decimal(str(h["close"])), ","),
                  h["change_pct"], h["rsi14"], ", ".join(h["setups"])))
    if not a.no_write:
        write_json(os.path.join(DIR, "watchlist.json"),
                   {"mode": "crypto_daily", "as_of": stamp(now), "scanned": len(syms) - len(failed),
                    "failed": len(failed), "hits": hits})
    print("A scan is a list of charts that match a rule, not a list of coins to buy.")
    return 0 if hits else 1


def cmd_entry(a, cfg):
    now = utc_now(a.now)
    n = a.lookback or cfg["crypto"]["lookback_bars"]
    bars = candles(a.symbol, 900, now, a.offline)
    if len(bars) < n + 1:
        raise Refused("only %d completed 15-minute bars for %s, need %d" % (len(bars), pair(a.symbol), n + 1))
    last, window = bars[-1], bars[-n - 1:-1]
    hi = max(b["h"] for b in window)
    lo = min(b["l"] for b in window)
    avg_v = sum(b["v"] for b in window) / n
    vol_ok = a.min_vol_x is None or (avg_v > 0 and last["v"] >= avg_v * D(a.min_vol_x))
    closed_at = last["t"] + datetime.timedelta(minutes=15)
    age = (now - closed_at).total_seconds() / 60
    doc = {"symbol": base(a.symbol), "pair": pair(a.symbol), "as_of": stamp(now), "setup": "range_breakout",
           "lookback_bars": n, "range_high": float(hi), "range_low": float(lo),
           "bar_close": float(last["c"]), "bar_time": stamp(closed_at), "volume_x": float(round(last["v"] / avg_v, 2)) if avg_v else None}
    if last["c"] > hi and vol_ok:
        doc.update(status="triggered", entry=float(last["c"]))
        print("TRIGGERED %s: the 15-minute bar that closed at %s closed at %s, above the %d-bar high %s (range low %s)."
              % (pair(a.symbol), stamp(closed_at), last["c"], n, hi, lo))
        if age > 15:
            print("That bar closed %d minutes ago. The price has moved on since: re-run before planning." % age)
    else:
        doc.update(status="waiting")
        why = "volume %.2fx under %sx" % (doc["volume_x"] or 0, a.min_vol_x) if last["c"] > hi else \
              "last close %s is %.2f%% under the %d-bar high %s" % (last["c"], (hi / last["c"] - 1) * 100, n, hi)
        print("Waiting on %s: %s." % (pair(a.symbol), why))
    if not a.no_write:
        write_json(os.path.join(DIR, "entry.json"), doc)
    return 0 if doc["status"] == "triggered" else 1


def parse_rule(text, kinds):
    k, _, v = text.partition(":")
    if k not in kinds:
        raise Refused("unknown rule %r, use one of %s" % (text, ", ".join(kinds)))
    return k, (D(v, text) if v else None)


def cmd_plan(a, cfg):
    now = utc_now(a.now)
    c = cfg["crypto"]
    entry_doc = None
    if a.entry is None:
        entry_doc = read_json(os.path.join(DIR, "entry.json"), "entry (run crypto.py entry first or give --entry)")
        if entry_doc.get("status") != "triggered":
            raise Refused("%s has not triggered: run entry again, or give --entry" % entry_doc.get("pair"))
    sym = a.symbol or (entry_doc or {}).get("symbol")
    if not sym:
        raise Refused("give --symbol with --entry")
    entry = D(a.entry if a.entry is not None else entry_doc["entry"], "the entry")
    prod = product(sym, a.offline)
    kind, val = parse_rule(a.stop or "atr:%s" % c["atr_mult"], ("atr", "pct", "price", "range-low"))
    if kind == "atr":
        bars = candles(sym, 900, now, a.offline)
        v = atr(bars)
        if v is None:
            raise Refused("no bars for an ATR")
        stop = entry - val * v
        how = "%s x the 15-minute ATR(14) %s" % (val, price_str(v, prod["price_inc"]))
    elif kind == "pct":
        stop, how = entry * (1 - val / 100), "%s%% under the entry" % val
    elif kind == "price":
        stop, how = val, "your price"
    else:
        if not entry_doc:
            raise Refused("range-low needs entry.json")
        stop, how = D(entry_doc["range_low"]), "the %d-bar range low" % entry_doc["lookback_bars"]
    stop = floor_to(stop, prod["price_inc"])
    if stop >= entry:
        raise Refused("the stop %s is at or above the entry %s" % (stop, entry))
    risk = entry - stop
    risk_pct = risk / entry * 100
    if risk_pct < D(c["min_stop_pct"]):
        raise Refused("the stop is %.2f%% under the entry, under the %s%% minimum: the fees (%s%% a side) and the "
                      "noise would eat it" % (risk_pct, c["min_stop_pct"], c["fee_pct"]))
    t = a.target or "%sR" % c["reward_r"]
    if t.startswith("price:"):
        target = D(t[6:], "the target price")
    elif t[-1:] in "Rr":
        target = entry + D(t[:-1], "the target") * risk
    else:
        raise Refused("unknown target %r, use 2R, 1.5R or price:X" % t)
    target = round_to(target, prod["price_inc"])
    if target <= entry:
        raise Refused("the target %s is at or under the entry %s" % (target, entry))
    rr = (target - entry) / risk
    plan = {"symbol": base(sym), "pair": pair(sym), "alpaca_symbol": alpaca_symbol(sym), "as_of": stamp(now),
            "setup": (entry_doc or {}).get("setup", "manual"), "entry": float(entry), "stop": float(stop),
            "target": float(target), "risk_per_unit": float(risk), "risk_pct": float(round(risk_pct, 2)),
            "reward_risk": float(round(rr, 2)), "stop_rule": how,
            "price_increment": str(prod["price_inc"]), "qty_increment": str(prod["qty_inc"]),
            "min_funds": str(prod["min_funds"])}
    print("Plan %s: entry %s, stop %s (%s, %.2f%% risk), target %s (%.2fR)."
          % (pair(sym), entry, stop, how, risk_pct, target, rr))
    if rr < D(cfg["min_reward_risk"]):
        print("Warning: reward:risk %.2f is under your minimum %s. The check step will block it." % (rr, cfg["min_reward_risk"]))
    print("A stop is a plan, not a guarantee: crypto moves 24/7 and can gap through it.")
    if not a.no_write:
        write_json(os.path.join(DIR, "plan.json"), plan)
    return 0


def worst_loss(qty, entry, stop, c):
    """Loss per order if the stop_limit fills at its limit price, fees on both sides."""
    fill = stop * (1 - D(c["stop_slip_pct"]) / 100)
    fee = D(c["fee_pct"]) / 100
    return qty * (entry - fill) + qty * entry * fee + qty * fill * fee, fill


def cmd_size(a, cfg):
    plan = read_json(os.path.join(DIR, "plan.json"), "plan (run crypto.py plan first)")
    c = cfg["crypto"]
    if cfg.get("budget") is None:
        raise Refused("no budget in %s: set the rules first" % CONFIG)
    entry, stop = D(plan["entry"]), D(plan["stop"])
    qinc, pinc = D(plan["qty_increment"]), D(plan["price_increment"])
    budget = D(cfg["budget"])
    one_loss, _ = worst_loss(Decimal(1), entry, stop, c)
    usable = budget * (1 - D(cfg["cash_buffer_pct"]) / 100)
    limits = {
        "max loss per trade": D(cfg["max_loss_per_trade_usd"]) / one_loss,
        "position cap": budget * D(cfg["max_position_pct"]) / 100 / entry,
        "max order": D(cfg["max_order_usd"]) / entry,
        "cash after your buffer": usable / entry / (1 + D(c["fee_pct"]) / 100),
    }
    bound = min(limits, key=limits.get)
    qty = floor_to(limits[bound], qinc)
    notional = qty * entry
    floor_usd = max(D(cfg["min_order_usd"]), D(plan["min_funds"]))
    if notional < floor_usd:
        raise Refused("%s %s is %s, under the %s minimum order (bound by %s). The stop is too far for your "
                      "max loss: tighten it or skip the trade." % (qty.normalize(), plan["symbol"], money(notional),
                                                                  money(floor_usd), bound))
    loss, fill = worst_loss(qty, entry, stop, c)
    sym = plan["alpaca_symbol"]
    q = format(qty.normalize(), "f")
    orders = {"as_of": plan["as_of"], "asset_class": "crypto", "paper": cfg.get("paper", True), "orders": [
        {"role": "entry", "symbol": sym, "side": "buy", "qty": q, "type": "market", "time_in_force": "gtc",
         "why": "%s on %s: %s %s (%s), bound by your %s; worst loss %s with fees"
                % (plan["setup"], plan["pair"], q, plan["symbol"], money(notional), bound, money(loss)),
         "plan": {"entry": plan["entry"], "stop": plan["stop"], "target": plan["target"],
                  "stop_fill": float(round_to(fill, pinc)), "max_loss": float(round(loss, 2)),
                  "reward_risk": plan["reward_risk"], "setup": plan["setup"]}},
        {"role": "protective_stop", "send_after": "the entry fills", "symbol": sym, "side": "sell", "qty": q,
         "type": "stop_limit", "time_in_force": "gtc", "stop_price": price_str(stop, pinc),
         "limit_price": price_str(fill, pinc)},
    ], "target_alert": {"symbol": sym, "above": plan["target"],
                        "then": "cancel the protective stop, sell %s at market or limit" % q}}
    print("Size %s: %s %s at %s = %s, bound by your %s." % (plan["pair"], q, plan["symbol"], plan["entry"],
                                                          money(notional), bound))
    print("Worst loss %s: stop %s, stop_limit fill %s, fees %s%% a side included." %
          (money(loss), plan["stop"], price_str(fill, pinc), c["fee_pct"]))
    print("Two orders: buy now; the stop_limit sell right after the fill. Target %s is an alert, not an order." % plan["target"])
    if not a.no_write:
        write_json(os.path.join(DIR, "orders.json"), orders)
    print("Nothing was placed. Next: crypto.py check.")
    return 0


def fingerprint(order):
    keep = {k: order.get(k) for k in ("symbol", "side", "qty", "type", "time_in_force", "stop_price", "limit_price")}
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:16]


def cmd_check(a, cfg):
    now = utc_now(a.now)
    c = cfg["crypto"]
    doc = read_json(os.path.join(DIR, "orders.json"), "orders (run crypto.py size first)")
    state = read_json(STATE, "crypto state", required=False) or {"placed": []}
    today = now.strftime("%Y-%m-%d")
    placed = [p for p in state.get("placed", []) if p.get("day") == today]
    reasons, warns = [], []
    entry_o = next((o for o in doc.get("orders", []) if o.get("role") == "entry"), None)
    stop_o = next((o for o in doc.get("orders", []) if o.get("role") == "protective_stop"), None)
    if not cfg.get("allow_crypto"):
        reasons.append("asset: crypto is off. Set \"allow_crypto\": true in %s to allow it" % CONFIG)
    if not entry_o:
        raise Refused("orders.json has no entry order")
    sym = entry_o.get("symbol", "")
    if base(sym) in [b.upper() for b in cfg.get("blocked", [])]:
        reasons.append("blocked: %s is on your blocked list" % base(sym))
    if entry_o.get("side") != "buy" or entry_o.get("type") not in ("market", "limit"):
        reasons.append("format: the entry must be a market or limit buy")
    if entry_o.get("time_in_force") not in ("gtc", "ioc"):
        reasons.append("format: crypto orders take time_in_force gtc or ioc")
    p = entry_o.get("plan") or {}
    try:
        qty = D(entry_o.get("qty"), "qty")
        entry, stop, target = D(p["entry"]), D(p["stop"]), D(p["target"])
    except (KeyError, Refused) as e:
        raise Refused("the entry order has no usable plan: %s" % e)
    if not stop_o or stop_o.get("qty") != entry_o.get("qty") or D(stop_o.get("stop_price", 0)) != stop:
        reasons.append("stop: no protective stop_limit for the same qty at the plan's stop")
    if stop >= entry:
        reasons.append("stop: the stop %s is not under the entry %s" % (stop, entry))
    fresh, at = None, None
    try:
        fresh, at = last_price(sym, a.offline)
    except Refused as e:
        warns.append("no fresh price (%s): checked against the plan's entry" % e)
    if at and (now - at).total_seconds() > c["max_quote_age_min"] * 60:
        warns.append("the fresh price is %d minutes old" % ((now - at).total_seconds() // 60))
        fresh = None
    used = max(entry, fresh) if fresh else entry
    if fresh and fresh > entry * Decimal("1.005"):
        warns.append("the price %s is %.2f%% over the plan's entry: risk measured from %s" % (fresh, (fresh / entry - 1) * 100, used))
    loss, _ = worst_loss(qty, used, stop, c)
    notional = qty * used
    rr = (target - used) / (used - stop) if used > stop else Decimal(0)
    budget = D(cfg["budget"] or 0)
    if notional < D(cfg["min_order_usd"]):
        reasons.append("size: %s is under your min order %s" % (money(notional), money(cfg["min_order_usd"])))
    if notional > D(cfg["max_order_usd"]):
        reasons.append("size: %s is over your max order %s" % (money(notional), money(cfg["max_order_usd"])))
    if budget and notional > budget * D(cfg["max_position_pct"]) / 100:
        reasons.append("position: %s is over %s%% of your %s budget" % (money(notional), cfg["max_position_pct"], money(budget)))
    if budget and notional * (1 + D(c["fee_pct"]) / 100) > budget * (1 - D(cfg["cash_buffer_pct"]) / 100):
        reasons.append("cash: the buy would dip into your %s%% cash buffer" % cfg["cash_buffer_pct"])
    if loss > D(cfg["max_loss_per_trade_usd"]):
        reasons.append("loss: worst loss %s (fees in) is over your max loss per trade %s" % (money(loss), money(cfg["max_loss_per_trade_usd"])))
    if rr < D(cfg["min_reward_risk"]):
        reasons.append("reward:risk: %.2f is under your minimum %s" % (rr, cfg["min_reward_risk"]))
    day_loss = sum(D(x.get("max_loss", 0)) for x in placed) + loss
    if day_loss > D(cfg["max_daily_loss_usd"]):
        reasons.append("daily loss: today's entries risk %s with this one, over your %s" % (money(day_loss), money(cfg["max_daily_loss_usd"])))
    if len(placed) >= int(c["max_trades_per_day"]):
        reasons.append("trades: %d entries already today (UTC), your max is %s" % (len(placed), c["max_trades_per_day"]))
    if any(x.get("symbol") == sym for x in placed):
        reasons.append("duplicate: %s was already entered today" % sym)
    verdict = "BLOCK" if reasons else "PASS"
    print("%s %s buy %s %s (%s), worst loss %s, %.2fR, paper=%s" % (verdict, sym, entry_o["qty"], base(sym),
          money(notional), money(loss), rr, cfg.get("paper", True)))
    for r in reasons:
        print("  BLOCK  " + r)
    for w in warns:
        print("  warn   " + w)
    if not a.no_write:
        rec = read_json(os.path.join(DIR, "risk.json"), "risk", required=False) or {"verdicts": []}
        rec["verdicts"].append({"day": today, "at": stamp(now), "verdict": verdict, "reasons": reasons,
                                "fingerprints": [fingerprint(o) for o in doc["orders"]]})
        write_json(os.path.join(DIR, "risk.json"), rec)
    print("A PASS is a rules check, not a recommendation. Nothing was placed.")
    return 0 if verdict == "PASS" else 1


def cmd_record(a, cfg):
    now = utc_now(a.now)
    qty, price, stop = D(a.qty, "--qty"), D(a.price, "--price"), D(a.stop, "--stop")
    loss, _ = worst_loss(qty, price, stop, cfg["crypto"])
    state = read_json(STATE, "crypto state", required=False) or {"placed": []}
    state["placed"].append({"day": now.strftime("%Y-%m-%d"), "at": stamp(now), "symbol": alpaca_symbol(a.symbol),
                            "qty": str(qty), "price": str(price), "stop": str(stop), "max_loss": float(round(loss, 2))})
    write_json(STATE, state)
    print("Recorded %s %s at %s, stop %s, max loss %s." % (qty, alpaca_symbol(a.symbol), price, stop, money(loss)))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Crypto scan, entry, plan, size and risk check (long only, no orders).")
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--now", help="pin the clock, UTC: \"2026-10-27 14:30\"")
    ap.add_argument("--offline", help="a JSON file of saved Coinbase answers keyed by API path")
    ap.add_argument("--no-write", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--symbols")
    s.add_argument("--min-usd-volume", default="1000000")
    e = sub.add_parser("entry")
    e.add_argument("symbol")
    e.add_argument("--lookback", type=int)
    e.add_argument("--min-vol-x")
    p = sub.add_parser("plan")
    p.add_argument("--symbol")
    p.add_argument("--entry")
    p.add_argument("--stop", help="atr:K (default 3), pct:X, price:X, range-low")
    p.add_argument("--target", help="2R (default), 1.5R, price:X")
    sub.add_parser("size")
    sub.add_parser("check")
    r = sub.add_parser("record")
    r.add_argument("--symbol", required=True)
    r.add_argument("--qty", required=True)
    r.add_argument("--price", required=True)
    r.add_argument("--stop", required=True)
    a = ap.parse_args(argv)
    try:
        cfg = read_config(a.config)
        return {"scan": cmd_scan, "entry": cmd_entry, "plan": cmd_plan, "size": cmd_size,
                "check": cmd_check, "record": cmd_record}[a.cmd](a, cfg)
    except Refused as e:
        print("Refused: %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
