# TraderBot v2 — 2-Order Martingale Bot

A simplified, focused MT5 bot. The trader draws a horizontal line; when the candle **close price touches** the line, the bot places exactly **1 BUY-STOP + 1 SELL-STOP**. When one activates, the other is cancelled. If the active position's SL is hit, the bot re-enters the same direction with the lot increased by 20%, up to 9 rounds.

---

## Architecture

```
traderbot_v2/
│
├── core/
│   ├── __init__.py
│   ├── watcher.py           ← Reads MT5 objects, detects candle touches
│   ├── order_manager.py     ← Builds & sends orders, lot calculation
│   └── position_monitor.py  ← Per-line state machine (IDLE→PENDING→ACTIVE→EXHAUSTED)
│
├── gui.py                   ← PyQt5 GUI (entry point)
├── config.py                ← All settings
└── requirements.txt
```

---

## Order Logic

```
Trader draws line at price P
          │
          ▼  (candle close touches P)
  ┌───────────────────────────┐
  │  BUY-STOP  @ P + dist     │  SL = P - dist
  │  SELL-STOP @ P - dist     │  SL = P + dist
  └───────────────────────────┘
          │
          ▼  (one fills, e.g. BUY-STOP)
  Cancel SELL-STOP
  Position is now ACTIVE (BUY)
          │
          ▼  (SL hit)
  Re-place BUY-STOP @ same levels
  Lot = prev_lot × 1.20  (Round 2)
          │
          ▼  (repeat up to 9 rounds)
  After round 9 → EXHAUSTED, no more re-entries
```

### Martingale Lot Schedule (default base=0.01, ×1.20):
| Round | Lot    |
|-------|--------|
| 1     | 0.01   |
| 2     | 0.01   |
| 3     | 0.01   |
| 4     | 0.02   |
| 5     | 0.02   |
| 6     | 0.02   |
| 7     | 0.03   |
| 8     | 0.04   |
| 9     | 0.04   |

*(actual values shown in GUI's lot schedule preview)*

---

## Quick Start

### 1. Prerequisites
- MetaTrader 5 (Windows)
- Python 3.11+

### 2. Install
```bash
pip install -r requirements.txt
```

### 3. Configure
Edit `config.py`:
```python
MT5_LOGIN           = 123456789
MT5_PASSWORD        = "yourpassword"
MT5_SERVER          = "Broker-Demo"
WATCH_SYMBOL        = "EURUSD"
ORDER_DISTANCE_PIPS = 1.5      # distance above/below line for the stop orders
LOT_SIZE            = 0.01     # base lot (round 1)
LOT_MULTIPLIER      = 1.20     # +20% each SL hit
MAX_ROUNDS          = 9
TP_RR_RATIO         = 2.0      # TP = SL distance × 2
```

### 4. Run
```bash
python gui.py
```

### 5. Use
1. Click **▶ Start Watcher**
2. Draw a **horizontal line** on your MT5 chart
3. The bot detects it, waits for candle to touch the line
4. Automatically places BUY-STOP + SELL-STOP
5. Manages the martingale sequence automatically

---

## Key Differences from v1

| Feature          | v1                              | v2                                   |
|------------------|---------------------------------|--------------------------------------|
| Orders placed    | 6 (3 buy + 3 sell levels)       | 2 (1 buy-stop + 1 sell-stop)        |
| Trigger          | Candle touches line             | Candle **close** touches line        |
| Re-entry logic   | Cascade (L2/L3 spawn new round) | Martingale (SL hit → same direction) |
| Lot management   | Fixed lot                       | ×1.20 per SL hit, up to 9 rounds    |
| No EA needed     | Requires ObjectExporter EA      | ✅ Reads MT5 directly via Python API |

---

## Notes
- No MT5 EA required — the bot reads objects and candles directly via the MT5 Python API
- All settings can be adjusted in the GUI before starting
- "Follow moved lines" option: if you drag a line, the bot resets and re-watches from the new position
- Position history is checked via `mt5.history_deals_get()` to distinguish SL vs TP closes
