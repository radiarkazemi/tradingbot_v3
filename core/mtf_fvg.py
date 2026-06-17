"""
mtf_fvg.py — Multi-Timeframe FVG Confluence Detector
=====================================================
Triggered on every completed 1M candle.

Logic:
  1. Scan 15M timeframe for active (non-mitigated) FVGs
  2. Scan 5M  timeframe for active (non-mitigated) FVGs
  3. Scan 1M  timeframe for active (non-mitigated) FVGs
  4. Find all triplets (15M, 5M, 1M) where:
     - All three are the same direction (all BULL or all BEAR)
     - All three are still "active" — i.e. price has not yet
       traded through them (mitigated FVGs are excluded before
       matching, not just marked afterward)
     - 15M ∩ 5M overlaps in price
     - (15M ∩ 5M) ∩ 1M overlaps in price
  5. The final intersection zone = entry zone

IMPORTANT — recency constraint:
  A 15M FVG and 5M FVG are only considered "confluent" if the 5M
  FVG's anchor candle falls within RECENCY_WINDOW_15M candles of
  the 15M FVG's anchor (in 5M-candle terms), and similarly the 1M
  FVG must fall within RECENCY_WINDOW_5M 1M-candles of the 5M
  anchor. Without this, any old/stale FVG that still overlaps in
  price gets paired with unrelated fresh FVGs purely by price
  coincidence, producing many near-duplicate zones from a single
  real 15M+5M overlap. This was the cause of zones with identical
  15M/5M gap sizes appearing repeatedly with only the 1M leg
  differing.

Mitigation:
  A zone is mitigated when price enters it (bid/ask touches).
  Mitigated zones are removed from chart automatically.
  Active mitigation is also applied to each individual FVG BEFORE
  matching, so a stale/already-traded-through FVG can never be
  used to build a new zone.

Drawing:
  Prefix: "MTFFVG_"
  Bullish confluence → Gold   (0x0000D7FF)
  Bearish confluence → Purple (0x00D30094)
"""

import MetaTrader5 as mt5
import os
import time as _time
import logging
from dataclasses import dataclass
from typing import List, Optional

log = logging.getLogger("mtf_fvg")

MTFFVG_PREFIX    = "MTFFVG_"
RECT_EXTEND_BARS = 50    # extend rectangle this many 1M bars to the right

# How close in time the anchors of each timeframe pair must be,
# expressed in "bars of the smaller timeframe in the pair".
# e.g. a 5M FVG must be within 6 5M-bars (=30min) of the 15M FVG
# it's being matched against.
RECENCY_BARS_15M_5M = 6     # 6 x 5M = 30 minutes
RECENCY_BARS_5M_1M  = 10    # 10 x 1M = 10 minutes

# MQL5 BGR colors
COLOR_BULL_ZONE = 0x0000D7FF   # Gold   — bullish entry zone
COLOR_BEAR_ZONE = 0x00D30094   # Purple — bearish entry zone


@dataclass
class SingleFVG:
    """One FVG from a single timeframe."""
    kind:      str     # "BULL" or "BEAR"
    top:       float
    bottom:    float
    time1:     int     # unix time of left candle (anchor)
    time2:     int     # unix time of right candle
    timeframe: int     # mt5.TIMEFRAME_*
    gap_pips:  float

    def overlaps_price(self, bid: float, ask: float) -> bool:
        return bid <= self.top and ask >= self.bottom


@dataclass
class MTFZone:
    """
    A confirmed multi-timeframe FVG confluence zone.
    Price range is the intersection of all three FVGs.
    """
    kind:        str
    top:         float
    bottom:      float
    fvg_15m:     SingleFVG
    fvg_5m:      SingleFVG
    fvg_1m:      SingleFVG
    created_at:  int
    mitigated:   bool = False

    @property
    def name(self) -> str:
        tag = "B" if self.kind == "BULL" else "S"
        return f"{MTFFVG_PREFIX}{tag}_{self.fvg_1m.time1}"

    @property
    def color(self) -> int:
        return COLOR_BULL_ZONE if self.kind == "BULL" else COLOR_BEAR_ZONE

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2

    def is_touched_by(self, bid: float, ask: float) -> bool:
        return bid <= self.top and ask >= self.bottom

    def height_pips(self, pip_size: float) -> float:
        return round((self.top - self.bottom) / pip_size, 1)


# ── FVG scanning ──────────────────────────────────────────────────

def _scan_fvgs(symbol: str, timeframe: int,
               lookback: int, min_gap_pips: float,
               pip_size: float, bid: float, ask: float) -> List[SingleFVG]:
    """
    Scan `lookback` bars on `timeframe` for FVG patterns.
    Only returns FVGs that have NOT yet been mitigated by current price
    (i.e. price hasn't traded back through the gap). This keeps stale
    historical gaps out of the confluence matching entirely.
    Returns list sorted newest first.
    """
    min_gap = min_gap_pips * pip_size
    bars    = mt5.copy_rates_from_pos(symbol, timeframe, 0, lookback + 3)
    if bars is None or len(bars) < 3:
        return []

    fvgs = []
    seen = set()

    for i in range(len(bars) - 2):
        left  = bars[i]
        right = bars[i + 2]

        l_high = float(left["high"])
        l_low  = float(left["low"])
        r_high = float(right["high"])
        r_low  = float(right["low"])
        t1     = int(left["time"])
        t2     = int(right["time"])

        if t1 in seen:
            continue

        fvg = None

        if r_low > l_high and (r_low - l_high) >= min_gap:
            fvg = SingleFVG(kind="BULL", top=r_low, bottom=l_high,
                            time1=t1, time2=t2, timeframe=timeframe,
                            gap_pips=round((r_low - l_high) / pip_size, 1))
        elif r_high < l_low and (l_low - r_high) >= min_gap:
            fvg = SingleFVG(kind="BEAR", top=l_low, bottom=r_high,
                            time1=t1, time2=t2, timeframe=timeframe,
                            gap_pips=round((l_low - r_high) / pip_size, 1))

        if fvg is None:
            continue

        # Skip FVGs already mitigated by current price — a gap that
        # price has already traded back through is not a valid,
        # still-open imbalance and must not be used for matching.
        if fvg.overlaps_price(bid, ask):
            seen.add(t1)
            continue

        seen.add(t1)
        fvgs.append(fvg)

    fvgs.sort(key=lambda f: f.time1, reverse=True)
    return fvgs


# ── Overlap logic ─────────────────────────────────────────────────

def _overlap(a_bot: float, a_top: float, b_bot: float, b_top: float):
    i_bot = max(a_bot, b_bot)
    i_top = min(a_top, b_top)
    if i_bot < i_top:
        return i_bot, i_top
    return None


def _within_recency(anchor_a: int, anchor_b: int,
                     bar_seconds: int, max_bars: int) -> bool:
    """
    True if the two anchor timestamps are within max_bars worth of
    bar_seconds of each other. Prevents pairing a fresh FVG with a
    stale one purely because their price ranges happen to overlap.
    """
    return abs(anchor_a - anchor_b) <= (bar_seconds * max_bars)


# ── Main detection ──────────────────────────────────────────────

def find_mtf_zones(
    symbol:        str,
    pip_size:      float,
    min_gap_pips:  float = 1.0,
    lookback_15m:  int   = 50,
    lookback_5m:   int   = 100,
    lookback_1m:   int   = 200,
) -> List[MTFZone]:
    """
    Find all price zones where a 15M, 5M, and 1M FVG all overlap
    in the same direction AND are temporally close to one another
    (not just coincidentally overlapping in price).

    Returns list of MTFZone sorted by zone height descending.
    """
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return []
    bid, ask = tick.bid, tick.ask

    fvgs_15m = _scan_fvgs(symbol, mt5.TIMEFRAME_M15,
                          lookback_15m, min_gap_pips, pip_size, bid, ask)
    fvgs_5m  = _scan_fvgs(symbol, mt5.TIMEFRAME_M5,
                          lookback_5m,  min_gap_pips, pip_size, bid, ask)
    fvgs_1m  = _scan_fvgs(symbol, mt5.TIMEFRAME_M1,
                          lookback_1m,  min_gap_pips, pip_size, bid, ask)

    if not fvgs_15m or not fvgs_5m or not fvgs_1m:
        log.debug("MTF FVG: insufficient active FVGs on one or more timeframes")
        return []

    zones = {}

    for f15 in fvgs_15m:
        for f5 in fvgs_5m:
            if f5.kind != f15.kind:
                continue

            # Recency check: the 5M FVG must have formed reasonably
            # close in time to the 15M FVG it's being matched with.
            if not _within_recency(f15.time1, f5.time1,
                                   bar_seconds=300,  # 5M = 300s
                                   max_bars=RECENCY_BARS_15M_5M):
                continue

            inter1 = _overlap(f15.bottom, f15.top, f5.bottom, f5.top)
            if inter1 is None:
                continue
            i1_bot, i1_top = inter1

            for f1 in fvgs_1m:
                if f1.kind != f15.kind:
                    continue

                # Recency check: the 1M FVG must be close in time to
                # the 5M FVG.
                if not _within_recency(f5.time1, f1.time1,
                                       bar_seconds=60,  # 1M = 60s
                                       max_bars=RECENCY_BARS_5M_1M):
                    continue

                inter2 = _overlap(i1_bot, i1_top, f1.bottom, f1.top)
                if inter2 is None:
                    continue

                final_bot, final_top = inter2

                zone = MTFZone(
                    kind       = f15.kind,
                    top        = final_top,
                    bottom     = final_bot,
                    fvg_15m    = f15,
                    fvg_5m     = f5,
                    fvg_1m     = f1,
                    created_at = f1.time1,
                    mitigated  = False,
                )

                if zone.name not in zones:
                    zones[zone.name] = zone

    result = sorted(zones.values(),
                    key=lambda z: z.top - z.bottom,
                    reverse=True)

    log.info(
        "MTF FVG: 15M=%d 5M=%d 1M=%d (active, post-recency) → %d confluence zones",
        len(fvgs_15m), len(fvgs_5m), len(fvgs_1m), len(result)
    )
    return result


def check_mitigation(zones: List[MTFZone], symbol: str) -> List[MTFZone]:
    """Mark zones as mitigated when price touches them."""
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return zones
    bid, ask = tick.bid, tick.ask
    for z in zones:
        if not z.mitigated and z.is_touched_by(bid, ask):
            z.mitigated = True
            log.info("MTF FVG mitigated: %s zone %.5f–%.5f", z.kind, z.bottom, z.top)
    return zones


# ── Chart drawing ─────────────────────────────────────────────────

def _command_file(symbol: str) -> str:
    appdata = os.environ.get("APPDATA", "")
    return os.path.join(
        appdata, "MetaQuotes", "Terminal", "Common", "Files",
        f"trader_commands_{symbol}.txt"
    )


def _write_commands(symbol: str, commands: list):
    path = _command_file(symbol)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for _ in range(5):
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(commands) + "\n")
            return
        except PermissionError:
            _time.sleep(0.05)


def draw_mtf_zones(symbol: str, zones: List[MTFZone], max_draw: int = 20):
    commands = [f"DELETE_PREFIX|{MTFFVG_PREFIX}"]
    drawn = 0
    for zone in zones:
        if zone.mitigated:
            continue
        if drawn >= max_draw:
            break
        t_left  = zone.fvg_1m.time1
        t_right = t_left + RECT_EXTEND_BARS * 60
        commands.append(
            f"DRAW_RECT|{zone.name}|{t_left}|{zone.top}|"
            f"{t_right}|{zone.bottom}|{zone.color}|2|1"
        )
        drawn += 1
    _write_commands(symbol, commands)
    log.info("MTF FVG: drew %d zones", drawn)


def clear_mtf_zones(symbol: str):
    _write_commands(symbol, [f"DELETE_PREFIX|{MTFFVG_PREFIX}"])