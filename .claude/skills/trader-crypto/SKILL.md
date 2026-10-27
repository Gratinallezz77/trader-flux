---
name: trader-crypto
description: >-
  Crypto Day Trader. The stock pipeline's scan, entry, plan, size and risk
  check for spot crypto against the US dollar (BTC, ETH, SOL, DOGE...), long
  side only, sized for small accounts. Reads free Coinbase candles, scans daily
  setups (20-day closing-high breakout, reclaim of the 20-day average, bull
  flag, oversold, oversold bounce), calls a 15-minute range breakout on the
  first completed bar that closes above the last 16 bars' high, sets an ATR
  stop and a 2R target, sizes fractional coins inside your max loss WITH fees
  on both sides, and writes Alpaca's crypto order shape (a market buy plus a
  protective stop_limit, since Alpaca crypto has no brackets). Then checks it
  against your limits: PASS or BLOCK. Nothing is placed. Use when the user says
  "crypto", "bitcoin", "trade BTC", "scan crypto", "SOL breakout", "how much
  ETH can I buy", "small account crypto", or wants to test with a few dollars.
---

# trader-crypto

The stock pack is built around the NYSE session; crypto trades 24/7. This
skill runs the same steps for crypto in one script and writes to its own
folder, `trader/crypto/`, so it never mixes with the stock files. A scan is a
list of charts that match a rule, a PASS is a rules check, and most people who
day trade lose money. At $100 the fees are a real part of every loss, so every
loss figure here includes them.

```bash
python3 "${CLAUDE_SKILL_DIR}/crypto.py" scan                       # daily setups -> trader/crypto/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/crypto.py" entry SOL                  # 15-min range breakout -> entry.json
python3 "${CLAUDE_SKILL_DIR}/crypto.py" plan                       # 3 ATR stop, 2R target -> plan.json
python3 "${CLAUDE_SKILL_DIR}/crypto.py" plan --symbol ETH --entry 2310 --stop pct:2
python3 "${CLAUDE_SKILL_DIR}/crypto.py" size                       # -> orders.json (entry + protective stop)
python3 "${CLAUDE_SKILL_DIR}/crypto.py" check                      # PASS / BLOCK -> risk.json
python3 "${CLAUDE_SKILL_DIR}/crypto.py" record --symbol SOL --qty 0.5 --price 99.9 --stop 96.86   # after a fill
```

Global flags (before the command): `--now "2026-10-27 14:30"` (UTC),
`--offline FILE` (saved Coinbase answers keyed by API path), `--no-write`,
`--config PATH`.

## Setup

`trader/config.json` (the stock rules, see trader-budget) plus:

```json
"allow_crypto": true,
"crypto": {
  "symbols": ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "LTC", "DOT"],
  "fee_pct": 0.25, "stop_slip_pct": 0.5, "lookback_bars": 16, "atr_mult": 3,
  "reward_r": 2, "min_stop_pct": 1.0, "max_trades_per_day": 3, "max_quote_age_min": 15
}
```

The `crypto` block is optional (those are the defaults). From the stock rules
it uses budget, cash_buffer_pct, max_position_pct, max_order_usd,
min_order_usd, max_loss_per_trade_usd, max_daily_loss_usd, min_reward_risk
and blocked. The entry window, flat_by and the NYSE calendar do not apply.

## Steps

1. `scan`: tell the user how many pairs came back and which matched a rule.
2. The user picks one. `entry SYM`: triggered (with the bar time) or waiting
   (how far under the range high). Re-run; never pick for them.
3. `plan`: stop and target. A stop under `min_stop_pct` (1%) is refused: the
   round-trip fee alone is 0.5%.
4. `size`: the fractional qty and which limit decided it. A stop too far for
   the max loss gives an order under the minimum: that is a refusal, not a
   smaller trade.
5. `check`: PASS or BLOCK with the reasons. The risk is re-measured from the
   higher of the plan's entry and a fresh price.
6. Only after a PASS, and only with the user's explicit yes, the order goes to
   the broker (Alpaca's `place_crypto_order`, paper first). Send the
   protective stop right after the fill, then `record` the fill so the day
   limits count it. Set a price alert at the target (trader-alerts).

## How it differs from the stock pipeline

| | stocks | crypto |
| --- | --- | --- |
| data | Yahoo 5-minute bars | Coinbase 15-minute and daily candles |
| entry | opening-range breakout after 9:45 ET | close above the last 16 bars' high, any hour |
| exit | bracket order, flat by 15:55 | market buy + stop_limit sell; target is an alert |
| day | New York trading day | UTC day |
| loss | qty x (entry - stop) | qty x (entry - stop_limit fill) + fees both sides |

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | the rules (read only) |
| `trader/crypto/watchlist.json` | `scan` |
| `trader/crypto/entry.json` | `entry`: status, range high and low, bar close |
| `trader/crypto/plan.json` | `plan`: entry, stop, target, R, the pair's increments |
| `trader/crypto/orders.json` | `size`: the entry and protective_stop orders, target_alert |
| `trader/crypto/risk.json` | `check`: verdicts with order fingerprints |
| `trader/state/crypto.json` | `record`: today's entries for the day limits |

## Rules

- Say "matched a rule", never "a buy". Never print a P&L or a return.
- Say the time of the data, in UTC. A trigger goes stale once price moves.
- Never widen a stop to make a size or a check pass.

## What it never does

It never places, cancels or changes an order, never picks a coin, never trades
short, on margin or with leverage, and never calls Coinbase data Alpaca's
price (the fill can differ).

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Crypto is off unless `"allow_crypto": true` is in the user's own config.
- An order goes to a broker only when `check` passed that exact order, the user typed an explicit yes
  for that one order in the chat, and the user clicked Allow once on Claude's own card because the
  order tool is set to "Needs approval". Never set an order tool to "Always allow".
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
