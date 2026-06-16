"""
ob_detector.py — Order Block (OB) Detector
===========================================
Detects Bullish and Bearish Order Blocks using two complementary methods:

METHOD 1 — Classic OB (last opposite candle before impulse):
  BULLISH OB : last bearish candle before a strong bullish impulse
               Zone: OB candle's low (bottom) → OB candle's high (top)
  BEARISH OB : last bullish candle before a strong bearish impulse
               Zone: OB candle's low (bottom) → OB candle's high (top)

METHOD 2 — BOS-based OB (candle that caused structure break):
  Tracks swing highs/lows. When price closes beyond a prior swing,
  the last opposite-direction candle before that break = OB.

Confirmation requires BOTH:
  1. Next candle closes beyond the OB candle's high (bull) or low (bear)
  2. That close is at least min_impulse_pips away from OB boundary

Mitigation:
  An OB is mitigated when price re-enters the zone (bid touches it).
  Mitigated OBs are flagged and removed from chart automatically.

Chart drawing:
  Uses same command-file bridge as fvg_detector.py.
  Bullish OB  → Aqua      (0x00D7D7D7 → actually 0x00D7D700 = aqua in MQL BGR)
  Bearish OB  → Magenta   (0x00FF00FF in BGR = magenta)
  Rectangles extend RECT_EXTEND_BARS bars to the right.
"""

import MetaTrader5 as mt5
import os
import time as _time
import logging
from dataclasses import dataclass, field
from typing import List, Optional

log = logging.getLogger("ob_detector")

OB_PREFIX        = "OB_"
RECT_EXTEND_BARS = 80

# MQL5 color format: 0x00BBGGRR
# Aqua    = R=0xFF, G=0xFF, B=0x00  →  0x00FFFF00  (but MQL BGR: B=0x00, G=0xFF, R=0xFF → 0x00FFFF)
# Actually MQL5 uses BGR so:
#   Aqua    (cyan)    = RGB(0, 255, 255) → BGR bytes: B=255 G=255 R=0   → 0x00FFFF00
#   Magenta           = RGB(255, 0, 255) → BGR bytes: B=255 G=0   R=255 → 0x00FF00FF
COLOR_BULL_OB = 0x00FFFF00   # Aqua
COLOR_BEAR_OB = 0x00FF00FF   # Magenta


def get_pip_size(symbol: str) -> float:
    mt5.symbol_select(symbol, True)
    info = mt5.symbol_info(symbol)
    if info is None:
        sym = symbol.upper()
        if "JPY" in sym: return 0.01
        if "XAU" in sym: return 0.10
        if "XAG" in sym: return 0.01
        if any(x in sym for x in ["US30", "NAS", "DAX", "FTSE", "SPX"]): return 1.0
        return 0.0001
    if info.digits in (0, 1): return 1.0
    if info.digits in (2, 3): return info.point * 10
    return info.point * 10


@dataclass
class OrderBlock:
    kind:           str     # "BULL" or "BEAR"
    top:            float   # upper price of OB zone
    bottom:         float   # lower price of OB zone
    ob_time:        int     # unix time of the OB candle itself
    impulse_time:   int     # unix time of the confirming impulse candle
    impulse_pips:   float   # size of confirming impulse in pips
    method:         str     # "CLASSIC" or "BOS"
    mitigated:      bool    = False
    name:           str     = ""

    def __post_init__(self):
        if not self.name:
            tag = "B" if self.kind == "BULL" else "S"
            self.name = f"{OB_PREFIX}{tag}_{self.ob_time}"

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2

    def is_touched_by(self, bid: float, ask: float) -> bool:
        """Price has re-entered the OB zone → mitigated."""
        if self.kind == "BULL":
            # Price came back down into bullish OB
            return bid <= self.top and ask >= self.bottom
        else:
            # Price came back up into bearish OB
            return ask >= self.bottom and bid <= self.top


# ── Detection ─────────────────────────────────────────────────────────────────

def detect_order_blocks(
    symbol:           str,
    timeframe=None,
    lookback:         int   = 200,
    min_impulse_pips: float = 3.0,
    swing_lookback:   int   = 5,      # bars each side to define a swing high/low
) -> List[OrderBlock]:
    """
    Scan last `lookback` candles for Order Block patterns (both methods).
    Returns list sorted newest first, deduped by ob_time.
    """
    if timeframe is None:
        timeframe = mt5.TIMEFRAME_M1

    pip           = get_pip_size(symbol)
    min_impulse_p = min_impulse_pips * pip

    bars = mt5.copy_rates_from_pos(symbol, timeframe, 0, lookback + swing_lookback + 3)
    if bars is None or len(bars) < 5:
        log.warning("Not enough candle data for OB on %s", symbol)
        return []

    obs      = {}   # ob_time → OrderBlock (dedup by OB candle time)
    n        = len(bars)

    # ── Helper: is candle bearish / bullish ───────────────────────
    def is_bear(b): return float(b["close"]) < float(b["open"])
    def is_bull(b): return float(b["close"]) > float(b["open"])

    # ─────────────────────────────────────────────────────────────
    # METHOD 1 — Classic: last opposite candle before impulse
    # We look at every candle i as a potential OB, and check if
    # bar[i+1] or bar[i+2] is a strong confirming impulse.
    # ─────────────────────────────────────────────────────────────
    for i in range(n - 2):
        ob_bar  = bars[i]
        ob_high = float(ob_bar["high"])
        ob_low  = float(ob_bar["low"])
        ob_t    = int(ob_bar["time"])

        # ── Bullish OB: bearish candle before bullish impulse ─────
        if is_bear(ob_bar):
            # Look at next 1-3 candles for confirming bullish impulse
            for j in range(i + 1, min(i + 4, n)):
                imp = bars[j]
                imp_close = float(imp["close"])
                imp_t     = int(imp["time"])
                # Confirmation: closes above OB high by at least min_impulse_pips
                if imp_close > ob_high and (imp_close - ob_high) >= min_impulse_p:
                    impulse_pips = round((imp_close - ob_high) / pip, 1)
                    if ob_t not in obs:
                        obs[ob_t] = OrderBlock(
                            kind         = "BULL",
                            top          = ob_high,
                            bottom       = ob_low,
                            ob_time      = ob_t,
                            impulse_time = imp_t,
                            impulse_pips = impulse_pips,
                            method       = "CLASSIC",
                        )
                    break

        # ── Bearish OB: bullish candle before bearish impulse ─────
        elif is_bull(ob_bar):
            for j in range(i + 1, min(i + 4, n)):
                imp = bars[j]
                imp_close = float(imp["close"])
                imp_t     = int(imp["time"])
                # Confirmation: closes below OB low by at least min_impulse_pips
                if imp_close < ob_low and (ob_low - imp_close) >= min_impulse_p:
                    impulse_pips = round((ob_low - imp_close) / pip, 1)
                    if ob_t not in obs:
                        obs[ob_t] = OrderBlock(
                            kind         = "BEAR",
                            top          = ob_high,
                            bottom       = ob_low,
                            ob_time      = ob_t,
                            impulse_time = imp_t,
                            impulse_pips = impulse_pips,
                            method       = "CLASSIC",
                        )
                    break

    # ─────────────────────────────────────────────────────────────
    # METHOD 2 — BOS-based: find swing highs/lows, detect when
    # price breaks them, then mark the last opposite candle before
    # the break as the OB.
    # ─────────────────────────────────────────────────────────────
    sw = swing_lookback  # bars each side

    # Identify swing highs and lows
    swing_highs = []  # (index, price, time)
    swing_lows  = []

    for i in range(sw, n - sw):
        h = float(bars[i]["high"])
        l = float(bars[i]["low"])
        t = int(bars[i]["time"])

        if all(h >= float(bars[i - k]["high"]) for k in range(1, sw + 1)) and \
           all(h >= float(bars[i + k]["high"]) for k in range(1, sw + 1)):
            swing_highs.append((i, h, t))

        if all(l <= float(bars[i - k]["low"]) for k in range(1, sw + 1)) and \
           all(l <= float(bars[i + k]["low"]) for k in range(1, sw + 1)):
            swing_lows.append((i, l, t))

    # For each swing high: look for a BOS (close above it) → bearish OB
    for (sh_idx, sh_price, sh_time) in swing_highs:
        for j in range(sh_idx + 1, n):
            close_j = float(bars[j]["close"])
            if close_j > sh_price and (close_j - sh_price) >= min_impulse_p:
                # BOS confirmed — find last bearish (bullish OB context is inverse here:
                # breaking a swing HIGH with a bullish close → the OB is the last
                # bearish candle before this breakout candle)
                ob_idx = None
                for k in range(j - 1, sh_idx - 1, -1):
                    if is_bear(bars[k]):
                        ob_idx = k
                        break
                if ob_idx is not None:
                    ob_bar       = bars[ob_idx]
                    ob_t         = int(ob_bar["time"])
                    ob_high      = float(ob_bar["high"])
                    ob_low       = float(ob_bar["low"])
                    imp_close    = float(bars[j]["close"])
                    impulse_pips = round((imp_close - sh_price) / pip, 1)
                    if ob_t not in obs:
                        obs[ob_t] = OrderBlock(
                            kind         = "BULL",
                            top          = ob_high,
                            bottom       = ob_low,
                            ob_time      = ob_t,
                            impulse_time = int(bars[j]["time"]),
                            impulse_pips = impulse_pips,
                            method       = "BOS",
                        )
                break  # only first BOS per swing high

    # For each swing low: look for a BOS (close below it) → bullish OB
    for (sl_idx, sl_price, sl_time) in swing_lows:
        for j in range(sl_idx + 1, n):
            close_j = float(bars[j]["close"])
            if close_j < sl_price and (sl_price - close_j) >= min_impulse_p:
                # BOS confirmed — last bullish candle before breakout = bearish OB
                ob_idx = None
                for k in range(j - 1, sl_idx - 1, -1):
                    if is_bull(bars[k]):
                        ob_idx = k
                        break
                if ob_idx is not None:
                    ob_bar       = bars[ob_idx]
                    ob_t         = int(ob_bar["time"])
                    ob_high      = float(ob_bar["high"])
                    ob_low       = float(ob_bar["low"])
                    imp_close    = float(bars[j]["close"])
                    impulse_pips = round((sl_price - imp_close) / pip, 1)
                    if ob_t not in obs:
                        obs[ob_t] = OrderBlock(
                            kind         = "BEAR",
                            top          = ob_high,
                            bottom       = ob_low,
                            ob_time      = ob_t,
                            impulse_time = int(bars[j]["time"]),
                            impulse_pips = impulse_pips,
                            method       = "BOS",
                        )
                break  # only first BOS per swing low

    result = sorted(obs.values(), key=lambda o: o.ob_time, reverse=True)
    log.info("OB scan: %d candles → %d OBs (min_impulse=%.1fpips)", n, len(result), min_impulse_pips)
    return result


# ── Mitigation check ──────────────────────────────────────────────────────────

def check_mitigation(obs: List[OrderBlock], symbol: str) -> List[OrderBlock]:
    """
    Check which OBs have been mitigated by current price.
    Marks ob.mitigated = True for each touched zone.
    Returns the updated list (mitigated ones flagged, not removed — caller decides).
    """
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return obs
    bid = tick.bid
    ask = tick.ask
    for ob in obs:
        if not ob.mitigated and ob.is_touched_by(bid, ask):
            ob.mitigated = True
            log.info("OB mitigated: %s %s zone %.5f-%.5f", ob.kind, ob.method, ob.bottom, ob.top)
    return obs


# ── Chart drawing ─────────────────────────────────────────────────────────────

def _command_file(symbol: str) -> str:
    appdata = os.environ.get("APPDATA", "")
    return os.path.join(
        appdata, "MetaQuotes", "Terminal", "Common", "Files",
        f"trader_commands_{symbol}.txt"
    )


def _write_commands(symbol: str, commands: list):
    path = _command_file(symbol)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for attempt in range(5):
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(commands) + "\n")
            return
        except PermissionError:
            _time.sleep(0.05)


def draw_obs_on_chart(symbol: str, obs: List[OrderBlock],
                      max_draw: int = 30, timeframe=None):
    """
    Clear old OB rectangles and draw fresh ones (non-mitigated only).
    Format mirrors fvg_detector.draw_fvgs_on_chart exactly.
    """
    if timeframe is None:
        timeframe = mt5.TIMEFRAME_M1

    period_map = {
        mt5.TIMEFRAME_M1:  60,
        mt5.TIMEFRAME_M5:  300,
        mt5.TIMEFRAME_M15: 900,
        mt5.TIMEFRAME_M30: 1800,
        mt5.TIMEFRAME_H1:  3600,
        mt5.TIMEFRAME_H4:  14400,
        mt5.TIMEFRAME_D1:  86400,
    }
    bar_sec = period_map.get(timeframe, 60)

    commands = [f"DELETE_PREFIX|{OB_PREFIX}"]

    drawn = 0
    for ob in obs:
        if ob.mitigated:
            continue
        if drawn >= max_draw:
            break

        color  = COLOR_BULL_OB if ob.kind == "BULL" else COLOR_BEAR_OB
        t2_ext = ob.impulse_time + bar_sec * RECT_EXTEND_BARS

        # Format: DRAW_RECT|name|t1|top|t2|bottom|color|border_width|fill(0/1)
        commands.append(
            f"DRAW_RECT|{ob.name}|{ob.ob_time}|{ob.top}|"
            f"{t2_ext}|{ob.bottom}|{color}|1|0"
        )
        drawn += 1

    _write_commands(symbol, commands)
    log.info("OB: drew %d rectangles", drawn)


def clear_obs_on_chart(symbol: str):
    _write_commands(symbol, [f"DELETE_PREFIX|{OB_PREFIX}"])