"""
ob_fvg_confluence.py — OB + FVG Confluence Detector
=====================================================
Finds Order Blocks that have a Fair Value Gap appearing immediately
after them (within a configurable candle window).

This is a high-probability confluence zone:
  - OB = institutional order origin (demand/supply zone)
  - FVG right after = the impulse that created the OB also left an
    imbalance, confirming the strength of the move

Logic:
  For each OB, check if any FVG exists such that:
    1. FVG appeared AFTER the OB candle (fvg.time1 >= ob.ob_time)
    2. FVG appeared within `max_candles_after` bars of the OB
    3. FVG direction matches OB direction
       (Bullish OB → Bullish FVG, Bearish OB → Bearish FVG)

Result:
  ConfluenceZone — contains the OB, the matching FVG(s), and a
  combined zone (widest span of both zones).

Chart drawing:
  Uses same command-file bridge.
  Prefix: "OBFVG_"
  Bullish confluence → Gold/Yellow border rectangle
  Bearish confluence → Purple border rectangle
  fill=0 (outline only, so it overlays OB and FVG rects cleanly)
"""

import MetaTrader5 as mt5
import os
import time as _time
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from core.ob_detector import OrderBlock, OB_PREFIX
from core.fvg_detector import FVG, FVG_PREFIX

log = logging.getLogger("ob_fvg_confluence")

CONFLUENCE_PREFIX = "OBFVG_"
RECT_EXTEND_BARS  = 80

# MQL5 BGR colors
# Gold   (bullish confluence) = RGB(255, 215, 0)  → BGR: B=0   G=215 R=255 → 0x0000D7FF
# Purple (bearish confluence) = RGB(148, 0,   211) → BGR: B=211 G=0   R=148 → 0x00D30094
COLOR_BULL_CONF = 0x0000D7FF   # Gold / Yellow
COLOR_BEAR_CONF = 0x00D30094   # Purple


@dataclass
class ConfluenceZone:
    ob:           OrderBlock
    fvgs:         List[FVG]          # all matching FVGs (usually 1)

    @property
    def kind(self) -> str:
        return self.ob.kind

    @property
    def name(self) -> str:
        return f"{CONFLUENCE_PREFIX}{'B' if self.kind == 'BULL' else 'S'}_{self.ob.ob_time}"

    @property
    def ob_zone_top(self) -> float:
        return self.ob.top

    @property
    def ob_zone_bottom(self) -> float:
        return self.ob.bottom

    @property
    def fvg_zone_top(self) -> float:
        return max(f.top for f in self.fvgs)

    @property
    def fvg_zone_bottom(self) -> float:
        return min(f.bottom for f in self.fvgs)

    @property
    def combined_top(self) -> float:
        """Widest span covering both OB and FVG zones."""
        return max(self.ob_zone_top, self.fvg_zone_top)

    @property
    def combined_bottom(self) -> float:
        return min(self.ob_zone_bottom, self.fvg_zone_bottom)

    @property
    def mitigated(self) -> bool:
        return self.ob.mitigated

    @property
    def impulse_pips(self) -> float:
        return self.ob.impulse_pips

    @property
    def fvg_gap_pips(self) -> float:
        return max(f.gap_pips for f in self.fvgs)

    @property
    def score(self) -> float:
        """Simple quality score: OB impulse + FVG gap (larger = better)."""
        return self.impulse_pips + self.fvg_gap_pips

    def summary(self) -> str:
        icon = "🟡" if self.kind == "BULL" else "🟣"
        return (
            f"{icon} {self.kind} OB+FVG | "
            f"OB {self.ob_zone_bottom:.5f}–{self.ob_zone_top:.5f} "
            f"[{self.ob.method}] {self.ob.impulse_pips:.1f}p | "
            f"FVG {self.fvg_zone_bottom:.5f}–{self.fvg_zone_top:.5f} "
            f"{self.fvg_gap_pips:.1f}p | "
            f"score={self.score:.1f}"
        )


# ── Main confluence detection ─────────────────────────────────────────────────

def find_confluences(
    obs:                List[OrderBlock],
    fvgs:               List[FVG],
    pip_size:           float,
    max_candles_after:  int   = 10,
    timeframe_sec:      int   = 60,     # seconds per candle (M1 = 60)
    require_direction:  bool  = True,   # FVG direction must match OB direction
) -> List[ConfluenceZone]:
    """
    Cross-reference OBs and FVGs to find confluence zones.

    Args:
        obs:               Active (non-mitigated) OrderBlocks
        fvgs:              All detected FVGs
        pip_size:          Pip size for the symbol
        max_candles_after: Maximum candles after OB candle for the FVG to count
        timeframe_sec:     Candle duration in seconds
        require_direction: If True, FVG kind must match OB kind

    Returns:
        List of ConfluenceZone sorted by score descending
    """
    if not obs or not fvgs:
        return []

    max_time_gap = max_candles_after * timeframe_sec
    zones        = []

    for ob in obs:
        if ob.mitigated:
            continue

        matching_fvgs = []
        for fvg in fvgs:
            # FVG must appear after OB candle
            if fvg.time1 < ob.ob_time:
                continue

            # FVG must be within the candle window
            time_gap = fvg.time1 - ob.ob_time
            if time_gap > max_time_gap:
                continue

            # Direction match
            if require_direction:
                if ob.kind == "BULL" and fvg.kind != "BULL":
                    continue
                if ob.kind == "BEAR" and fvg.kind != "BEAR":
                    continue

            matching_fvgs.append(fvg)

        if matching_fvgs:
            # Sort matching FVGs by proximity to OB (closest first)
            matching_fvgs.sort(key=lambda f: f.time1 - ob.ob_time)
            zones.append(ConfluenceZone(ob=ob, fvgs=matching_fvgs))

    # Sort by score descending (strongest confluence first)
    zones.sort(key=lambda z: z.score, reverse=True)
    log.info(
        "Confluence: %d OBs × %d FVGs → %d confluence zones",
        len(obs), len(fvgs), len(zones)
    )
    return zones


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


def draw_confluences_on_chart(
    symbol:    str,
    zones:     List[ConfluenceZone],
    max_draw:  int  = 20,
    timeframe=None,
    also_draw_ob:  bool = False,   # suppress OB rects when confluence mode is on
    also_draw_fvg: bool = False,   # suppress FVG rects when confluence mode is on
):
    """
    Draw confluence zone rectangles on chart.
    Optionally suppress individual OB and FVG rectangles so only
    the combined confluence zones are visible.

    Rectangle style: outline only (fill=0), thicker border (width=2)
    so it visually wraps the underlying OB+FVG zones.
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

    commands = [f"DELETE_PREFIX|{CONFLUENCE_PREFIX}"]

    # Optionally clear individual OB and FVG rects
    if not also_draw_ob:
        commands.append(f"DELETE_PREFIX|{OB_PREFIX}")
    if not also_draw_fvg:
        commands.append(f"DELETE_PREFIX|{FVG_PREFIX}")

    drawn = 0
    for zone in zones:
        if drawn >= max_draw:
            break

        color  = COLOR_BULL_CONF if zone.kind == "BULL" else COLOR_BEAR_CONF
        # Left edge = OB candle, right edge = latest FVG right edge + extension
        t_left  = zone.ob.ob_time
        t_right = max(f.time2 for f in zone.fvgs) + bar_sec * RECT_EXTEND_BARS

        # Draw outer rectangle spanning both OB and FVG zones
        commands.append(
            f"DRAW_RECT|{zone.name}|{t_left}|{zone.combined_top}|"
            f"{t_right}|{zone.combined_bottom}|{color}|2|0"
        )
        drawn += 1

    _write_commands(symbol, commands)
    log.info("Confluence: drew %d rectangles", drawn)


def clear_confluences_on_chart(symbol: str, restore_ob: bool = True, restore_fvg: bool = True):
    """
    Remove confluence rectangles from chart.
    restore_ob / restore_fvg: send DELETE+redraw signal so individual
    rects reappear — caller handles actual redraw by triggering their watchers.
    """
    commands = [f"DELETE_PREFIX|{CONFLUENCE_PREFIX}"]
    _write_commands(symbol, commands)