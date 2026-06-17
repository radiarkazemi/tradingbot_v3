"""
mtf_fvg.py — Multi-Timeframe FVG Confluence Detector
=====================================================
Triggered on every completed 1M candle.

GENERALIZED N-TIMEFRAME CASCADE
--------------------------------
The trader selects any 2 or 3 of {15M, 5M, 1M} via checkboxes, plus an
"entry timeframe" (one of the selected ones) that becomes the
tradeable zone. Examples:

  15M + 5M + 1M  (entry=1M)  → classic triple confluence (default)
  5M  + 1M       (entry=1M)  → just 5M∩1M, skip 15M entirely
  15M + 1M       (entry=1M)  → just 15M∩1M, skip 5M entirely
  15M + 5M       (entry=5M)  → 15M∩5M, no 1M leg at all

The cascade always processes timeframes largest → smallest, pairwise
intersecting as it goes:
  result = TF[0]
  for next_tf in TF[1:]:
      result = overlap(result, next_tf), with a recency check between
               next_tf's anchor and the anchor of the level it's being
               matched against

Mitigation:
  A zone is mitigated when price enters it. Individual FVGs are also
  pre-filtered for mitigation BEFORE matching (see _scan_fvgs), so a
  stale/already-traded-through gap can never seed a new zone.

RECENCY WINDOW — why it exists and how it scales
--------------------------------------------------
Without a time constraint, a 15M FVG (which can span a wide price
range) will price-overlap with many unrelated 5M/1M FVGs purely by
coincidence, even ones that formed hours apart. This produces zones
that all share the same outer-timeframe gap size paired against many
unrelated inner-timeframe gaps — confluence everywhere, but none of
it real. The recency window forces each pair's anchors to actually be
close in time. The window for a given pair scales with the ratio of
their bar sizes: a 15M→5M pair (ratio 3) gets a longer window than a
5M→1M pair (ratio 5), proportional to how many of the SMALLER bars
fit inside one of the LARGER bars, with a sane minimum floor.

Drawing:
  Prefix: "MTFFVG_"     — the narrow final-intersection box
  Prefix: "MTFFVG5M_"   — the entry-timeframe FVG (tradeable zone;
                          prefix name kept for backward compatibility
                          even when the entry timeframe isn't 5M)
  Bullish confluence → Gold   (0x0000D7FF)
  Bearish confluence → Purple (0x00D30094)
"""

import MetaTrader5 as mt5
import os
import time as _time
import logging
from dataclasses import dataclass
from typing import List, Optional, Dict

log = logging.getLogger("mtf_fvg")

MTFFVG_PREFIX    = "MTFFVG_"
MTFFVG_5M_PREFIX = "MTFFVG5M_"
RECT_EXTEND_BARS = 50

# MQL5 BGR colors
COLOR_BULL_ZONE = 0x0000D7FF
COLOR_BEAR_ZONE = 0x00D30094

# ── Timeframe registry ──────────────────────────────────────────
# bar_seconds is used for recency-window scaling and rectangle
# extension. Order here is largest → smallest (used for sorting
# whatever subset the user selects).
TIMEFRAME_SPECS = {
    "15M": {"mt5_tf": mt5.TIMEFRAME_M15, "bar_seconds": 900, "default_lookback": 50},
    "5M":  {"mt5_tf": mt5.TIMEFRAME_M5,  "bar_seconds": 300, "default_lookback": 100},
    "1M":  {"mt5_tf": mt5.TIMEFRAME_M1,  "bar_seconds": 60,  "default_lookback": 200},
}
TIMEFRAME_ORDER = ["15M", "5M", "1M"]   # largest → smallest

# Minimum recency window floor, in seconds, regardless of computed scale.
MIN_RECENCY_SECONDS = 300   # 5 minutes


@dataclass
class SingleFVG:
    kind:      str
    top:       float
    bottom:    float
    time1:     int
    time2:     int
    timeframe: str     # e.g. "15M", "5M", "1M" (registry key, not mt5 const)
    gap_pips:  float

    def overlaps_price(self, bid: float, ask: float) -> bool:
        return bid <= self.top and ask >= self.bottom


@dataclass
class MTFZone:
    """A confirmed multi-timeframe FVG confluence zone."""
    kind:         str
    top:          float
    bottom:       float
    legs:         Dict[str, SingleFVG]   # {"15M": fvg, "5M": fvg, "1M": fvg} — only selected tfs
    entry_tf:     str                     # which key in `legs` is the tradeable zone
    created_at:   int
    mitigated:    bool = False

    @property
    def entry_fvg(self) -> SingleFVG:
        return self.legs[self.entry_tf]

    # Backward-compat accessors used by older callers (fvg_5m, fvg_15m,
    # fvg_1m) — return None if that leg wasn't part of this zone's
    # selected timeframe set.
    @property
    def fvg_15m(self): return self.legs.get("15M")
    @property
    def fvg_5m(self):  return self.legs.get("5M")
    @property
    def fvg_1m(self):  return self.legs.get("1M")

    @property
    def name(self) -> str:
        tag = "B" if self.kind == "BULL" else "S"
        anchor = self.entry_fvg.time1
        return f"{MTFFVG_PREFIX}{tag}_{anchor}"

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

    def legs_summary(self) -> str:
        return "  ".join(f"{tf}:{fvg.gap_pips}p" for tf, fvg in self.legs.items())


# ── FVG scanning ──────────────────────────────────────────────────

def _scan_fvgs(symbol: str, tf_key: str, lookback: int, min_gap_pips: float,
               pip_size: float, bid: float, ask: float) -> List[SingleFVG]:
    """Scan one timeframe for active (non-mitigated) FVGs."""
    spec      = TIMEFRAME_SPECS[tf_key]
    min_gap   = min_gap_pips * pip_size
    bars      = mt5.copy_rates_from_pos(symbol, spec["mt5_tf"], 0, lookback + 3)
    if bars is None or len(bars) < 3:
        return []

    fvgs = []
    seen = set()

    for i in range(len(bars) - 2):
        left  = bars[i]
        right = bars[i + 2]

        l_high = float(left["high"]); l_low = float(left["low"])
        r_high = float(right["high"]); r_low = float(right["low"])
        t1 = int(left["time"]); t2 = int(right["time"])

        if t1 in seen:
            continue

        fvg = None
        if r_low > l_high and (r_low - l_high) >= min_gap:
            fvg = SingleFVG(kind="BULL", top=r_low, bottom=l_high,
                            time1=t1, time2=t2, timeframe=tf_key,
                            gap_pips=round((r_low - l_high) / pip_size, 1))
        elif r_high < l_low and (l_low - r_high) >= min_gap:
            fvg = SingleFVG(kind="BEAR", top=l_low, bottom=r_high,
                            time1=t1, time2=t2, timeframe=tf_key,
                            gap_pips=round((l_low - r_high) / pip_size, 1))

        if fvg is None:
            continue

        seen.add(t1)

        # Pre-filter mitigated gaps — never let a stale, already-
        # traded-through FVG seed a new zone.
        if fvg.overlaps_price(bid, ask):
            continue

        fvgs.append(fvg)

    fvgs.sort(key=lambda f: f.time1, reverse=True)
    return fvgs


# ── Overlap + recency logic ─────────────────────────────────────

def _overlap(a_bot: float, a_top: float, b_bot: float, b_top: float):
    i_bot = max(a_bot, b_bot)
    i_top = min(a_top, b_top)
    if i_bot < i_top:
        return i_bot, i_top
    return None


def _recency_window_seconds(outer_tf: str, inner_tf: str) -> int:
    """
    Scales the recency window by how many inner-timeframe bars fit
    inside one outer-timeframe bar, with a sane floor. e.g.:
      15M→5M:  900/300=3   → window = max(3, 3) bars of 5M  × 300s = 900s  (but floored to MIN)
      15M→1M:  900/60=15   → window = 15 bars of 1M × 60s = 900s
      5M→1M:   300/60=5    → window = 5 bars of 1M × 60s = 300s
    The intent: allow roughly "one outer bar's worth" of slack for the
    inner FVG to have formed, since that's the natural time relationship
    between the two timeframes.
    """
    outer_sec = TIMEFRAME_SPECS[outer_tf]["bar_seconds"]
    inner_sec = TIMEFRAME_SPECS[inner_tf]["bar_seconds"]
    ratio     = max(1, outer_sec // inner_sec)
    window    = ratio * inner_sec
    return max(window, MIN_RECENCY_SECONDS)


def _within_recency(anchor_a: int, anchor_b: int, window_seconds: int) -> bool:
    return abs(anchor_a - anchor_b) <= window_seconds


# ── Main detection (generalized cascade) ────────────────────────

def find_mtf_zones(
    symbol:           str,
    pip_size:         float,
    selected_tfs:     List[str] = None,   # e.g. ["15M","5M","1M"] or ["5M","1M"]
    entry_tf:         str       = None,   # which selected tf is the tradeable zone
    min_gap_pips:     float = 1.0,
    lookback_15m:     int   = 50,
    lookback_5m:      int   = 100,
    lookback_1m:      int   = 200,
) -> List[MTFZone]:
    """
    Find all price zones where every SELECTED timeframe has an FVG,
    all the same direction, all temporally close to their neighbor
    in the cascade, and all overlapping in price.

    selected_tfs defaults to all three (["15M","5M","1M"]) for
    backward compatibility. Must contain at least 2 entries.
    entry_tf defaults to the smallest selected timeframe if not given.
    """
    if selected_tfs is None or len(selected_tfs) < 2:
        selected_tfs = ["15M", "5M", "1M"]

    # Normalize to largest → smallest order regardless of input order
    tfs = [tf for tf in TIMEFRAME_ORDER if tf in selected_tfs]
    if len(tfs) < 2:
        log.warning("MTF FVG: need at least 2 selected timeframes, got %s", selected_tfs)
        return []

    if entry_tf is None or entry_tf not in tfs:
        entry_tf = tfs[-1]   # smallest selected, by default

    lookback_map = {"15M": lookback_15m, "5M": lookback_5m, "1M": lookback_1m}

    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return []
    bid, ask = tick.bid, tick.ask

    # Scan every selected timeframe once
    fvg_lists = {}
    for tf in tfs:
        fvg_lists[tf] = _scan_fvgs(
            symbol, tf, lookback_map.get(tf, TIMEFRAME_SPECS[tf]["default_lookback"]),
            min_gap_pips, pip_size, bid, ask
        )

    if any(len(fvg_lists[tf]) == 0 for tf in tfs):
        log.debug("MTF FVG: insufficient active FVGs on one or more selected timeframes (%s)", tfs)
        return []

    zones = {}

    def _cascade(level_idx: int, kind: str, cur_bot: float, cur_top: float,
                last_anchor: int, last_tf: str, legs: dict):
        if level_idx == len(tfs):
            zone = MTFZone(
                kind       = kind,
                top        = cur_top,
                bottom     = cur_bot,
                legs       = dict(legs),
                entry_tf   = entry_tf,
                created_at = legs[entry_tf].time1,
                mitigated  = False,
            )
            if zone.name not in zones:
                zones[zone.name] = zone
            return

        tf = tfs[level_idx]
        window = _recency_window_seconds(last_tf, tf) if last_tf else None

        for fvg in fvg_lists[tf]:
            if fvg.kind != kind:
                continue
            if last_anchor is not None and not _within_recency(last_anchor, fvg.time1, window):
                continue

            inter = _overlap(cur_bot, cur_top, fvg.bottom, fvg.top)
            if inter is None:
                continue

            new_bot, new_top = inter
            legs[tf] = fvg
            _cascade(level_idx + 1, kind, new_bot, new_top, fvg.time1, tf, legs)
            del legs[tf]

    # Seed the cascade with the outermost (largest) timeframe
    outer_tf = tfs[0]
    for fvg in fvg_lists[outer_tf]:
        legs = {outer_tf: fvg}
        _cascade(1, fvg.kind, fvg.bottom, fvg.top, fvg.time1, outer_tf, legs)

    result = sorted(zones.values(), key=lambda z: z.top - z.bottom, reverse=True)

    log.info(
        "MTF FVG: %s (active, post-recency) → %d confluence zones | entry_tf=%s",
        " ".join(f"{tf}={len(fvg_lists[tf])}" for tf in tfs),
        len(result), entry_tf
    )
    for tf in tfs:
        for f in fvg_lists[tf][:3]:
            log.debug("  %s %s: %.5f-%.5f (%.1fp) anchor=%d", tf, f.kind, f.bottom, f.top, f.gap_pips, f.time1)

    return result


def check_mitigation(zones: List[MTFZone], symbol: str) -> List[MTFZone]:
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
        t_left  = zone.entry_fvg.time1
        t_right = t_left + RECT_EXTEND_BARS * 60
        commands.append(
            f"DRAW_RECT|{zone.name}|{t_left}|{zone.top}|"
            f"{t_right}|{zone.bottom}|{zone.color}|2|1"
        )
        drawn += 1
    _write_commands(symbol, commands)
    log.info("MTF FVG: drew %d zones", drawn)


def clear_mtf_zones(symbol: str):
    _write_commands(symbol, [
        f"DELETE_PREFIX|{MTFFVG_PREFIX}",
        f"DELETE_PREFIX|{MTFFVG_5M_PREFIX}",
    ])


def draw_mtf_zones_and_entries(symbol: str, zones: List[MTFZone],
                               max_draw: int = 20, extend_minutes: int = 60):
    """
    Combined draw: final-intersection boxes (filled) AND the
    entry-timeframe FVG rectangles (outline), in one write.
    """
    commands = [f"DELETE_PREFIX|{MTFFVG_PREFIX}", f"DELETE_PREFIX|{MTFFVG_5M_PREFIX}"]

    drawn      = 0
    seen_entry = set()
    for zone in zones:
        if zone.mitigated:
            continue
        if drawn >= max_draw:
            break

        t_left  = zone.entry_fvg.time1
        t_right = t_left + RECT_EXTEND_BARS * 60
        commands.append(
            f"DRAW_RECT|{zone.name}|{t_left}|{zone.top}|"
            f"{t_right}|{zone.bottom}|{zone.color}|2|1"
        )
        drawn += 1

        ef_key = zone.entry_fvg.time1
        if ef_key not in seen_entry:
            seen_entry.add(ef_key)
            ef = zone.entry_fvg
            e_left  = ef.time1
            e_right = ef.time1 + extend_minutes * 60
            e_name  = f"{MTFFVG_5M_PREFIX}{ef.time1}"
            commands.append(
                f"DRAW_RECT|{e_name}|{e_left}|{ef.top}|"
                f"{e_right}|{ef.bottom}|{zone.color}|2|0"
            )

    _write_commands(symbol, commands)
    log.info("MTF FVG: drew %d confluence zones + %d unique entry boxes",
             drawn, len(seen_entry))