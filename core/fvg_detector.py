"""
fvg_detector.py — Fair Value Gap (FVG) Detector
================================================
3-candle FVG pattern detection + MT5 chart drawing.

FVG Definition:
  BULLISH FVG: right.low > left.high
               Zone: left.high (bottom) → right.low (top)
               Middle candle is the strong impulse candle.

  BEARISH FVG: right.high < left.low
               Zone: right.high (bottom) → left.low (top)

  left   = bars[i]     (oldest of 3)
  middle = bars[i+1]   (impulse)
  right  = bars[i+2]   (newest of 3)

Quality Filter — min_gap_pips:
  Higher → fewer, more significant FVGs only
  Lower  → more FVGs including small noise
"""
import MetaTrader5 as mt5
import os
import time as _time
import logging
from dataclasses import dataclass
from typing import List

log = logging.getLogger("fvg")

FVG_PREFIX = "FVG_"

# MQL5 color format is 0x00BBGGRR
# Bullish = green tint, Bearish = red tint
# Using bright colors that show clearly as thin-bordered rectangles
COLOR_BULL = 0x0032CD32   # lime green  (R=0x32, G=0xCD, B=0x32)
COLOR_BEAR = 0x000000CD   # medium red  (R=0xCD, G=0x00, B=0x00)

# How many bars to extend the rectangle to the right of the FVG
RECT_EXTEND_BARS = 50


@dataclass
class FVG:
    kind:      str    # "BULL" or "BEAR"
    top:       float  # upper price of gap zone
    bottom:    float  # lower price of gap zone
    time1:     int    # unix time of left candle (bar[i])
    time2:     int    # unix time of right candle (bar[i+2])
    gap_pips:  float
    name:      str


def get_pip_size(symbol: str) -> float:
    mt5.symbol_select(symbol, True)
    info = mt5.symbol_info(symbol)
    if info is None:
        sym = symbol.upper()
        if "JPY" in sym: return 0.01
        if "XAU" in sym: return 0.10
        if "XAG" in sym: return 0.01
        if any(x in sym for x in ["US30","NAS","DAX","FTSE","SPX"]): return 1.0
        return 0.0001
    if info.digits in (0, 1): return 1.0
    if info.digits in (2, 3): return info.point * 10
    return info.point * 10


def detect_fvgs(symbol: str,
                timeframe=None,
                lookback: int = 200,
                min_gap_pips: float = 3.0) -> List[FVG]:
    """
    Scan last `lookback` candles for FVG patterns.
    Returns list sorted newest first.
    """
    if timeframe is None:
        timeframe = mt5.TIMEFRAME_M1

    pip           = get_pip_size(symbol)
    min_gap_price = min_gap_pips * pip

    # Fetch bars — copy_rates_from_pos returns oldest first (index 0 = oldest)
    bars = mt5.copy_rates_from_pos(symbol, timeframe, 0, lookback + 3)
    if bars is None or len(bars) < 3:
        log.warning("Not enough candle data for FVG on %s", symbol)
        return []

    fvgs = []
    seen = set()

    for i in range(len(bars) - 2):
        left  = bars[i]       # oldest
        # middle = bars[i+1]  # impulse (not directly used in gap check)
        right = bars[i + 2]   # newest

        l_high = float(left["high"])
        l_low  = float(left["low"])
        r_high = float(right["high"])
        r_low  = float(right["low"])
        t1     = int(left["time"])
        t2     = int(right["time"])

        if t1 in seen:
            continue

        # ── Bullish FVG: gap between left.high and right.low ─────
        if r_low > l_high:
            gap = r_low - l_high
            if gap >= min_gap_price:
                seen.add(t1)
                fvgs.append(FVG(
                    kind     = "BULL",
                    top      = r_low,
                    bottom   = l_high,
                    time1    = t1,
                    time2    = t2,
                    gap_pips = round(gap / pip, 1),
                    name     = f"{FVG_PREFIX}B_{t1}",
                ))

        # ── Bearish FVG: gap between right.high and left.low ─────
        elif r_high < l_low:
            gap = l_low - r_high
            if gap >= min_gap_price:
                seen.add(t1)
                fvgs.append(FVG(
                    kind     = "BEAR",
                    top      = l_low,
                    bottom   = r_high,
                    time1    = t1,
                    time2    = t2,
                    gap_pips = round(gap / pip, 1),
                    name     = f"{FVG_PREFIX}S_{t1}",
                ))

    fvgs.sort(key=lambda f: f.time1, reverse=True)
    log.info("FVG scan: %d candles → %d FVGs (min=%.1fpips)", len(bars), len(fvgs), min_gap_pips)
    return fvgs


# ── Chart drawing ─────────────────────────────────────────────────────

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


def draw_fvgs_on_chart(symbol: str, fvgs: List[FVG], max_draw: int = 50,
                       timeframe=None):
    """
    Clear old FVG rectangles and draw new ones.

    Each rectangle:
    - Left edge  = bar[i] open time (left candle of the 3)
    - Right edge = bar[i+2] open time + RECT_EXTEND_BARS bar-periods
    - Top/Bottom = gap zone prices
    - Fill = transparent background style
    - No selection handles
    """
    if timeframe is None:
        timeframe = mt5.TIMEFRAME_M1

    # Get the bar period in seconds for right-edge extension
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

    commands = [f"DELETE_PREFIX|{FVG_PREFIX}"]

    drawn = 0
    for fvg in fvgs:
        if drawn >= max_draw:
            break

        color     = COLOR_BULL if fvg.kind == "BULL" else COLOR_BEAR
        # Extend right edge far enough to be visible on chart
        t2_ext    = fvg.time2 + bar_sec * RECT_EXTEND_BARS

        # Format: DRAW_RECT|name|t1|top|t2|bottom|color|border_width|fill(0/1)
        commands.append(
            f"DRAW_RECT|{fvg.name}|{fvg.time1}|{fvg.top}|"
            f"{t2_ext}|{fvg.bottom}|{color}|1|1"
        )
        drawn += 1

    _write_commands(symbol, commands)
    log.info("FVG: drew %d rectangles", drawn)


def clear_fvgs_on_chart(symbol: str):
    _write_commands(symbol, [f"DELETE_PREFIX|{FVG_PREFIX}"])