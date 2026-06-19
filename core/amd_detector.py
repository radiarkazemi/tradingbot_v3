"""
amd_detector.py — Quarter Theory AMD Detector
==============================================
Divides time into nested fractal quarters following the AMD
(Accumulation, Manipulation, Distribution) structure.

Hierarchy:
  Year     → 4 Quarters (Q1/Q2/Q3/Q4)   — 3 months each
  Quarter  → 3 Months
  Month    → 4 Weeks
  Week     → 5 Trading Days (Mon–Fri)
  Day      → 6 × 4H sessions
  4H       → 4 × 1H
  1H       → 12 × 5M
  5M       → 5 × 1M

AMD Assignment:
  For any period split into 4 parts:
    Part 1 → A (Accumulation)   — Green
    Part 2 → M (Manipulation)  — Red
    Part 3 → D (Distribution)  — Blue
    Part 4 → C (Continuation)  — Gray (confirmation/reversal)

  For periods that split into non-4 parts (weeks=5, months=3):
    We still label them proportionally:
      Week: Mon=A, Tue=A, Wed=M, Thu=D, Fri=D
      Quarter: Month1=A, Month2=M, Month3=D

Chart drawing:
  Uses same command-file bridge as FVG/OB.
  Prefix: "AMD_"
  Draws boxes for the current candle's period at each timeframe level.

Info table:
  Drawn in top-right corner via chart objects showing current phase
  at every level simultaneously.
"""

import MetaTrader5 as mt5
import os
import time as _time
import logging
from dataclasses import dataclass
from typing import Optional
from datetime import datetime, timedelta
import math

log = logging.getLogger("amd_detector")

AMD_PREFIX   = "AMD_"
TABLE_PREFIX = "AMDT_"

# MQL5 BGR colors
COLOR_A    = 0x0000FF00   # Green  (Accumulation)
COLOR_M    = 0x000000FF   # Red    (Manipulation)
COLOR_D    = 0x00FF0000   # Blue   (Distribution)
COLOR_C    = 0x00808080   # Gray   (Continuation)
COLOR_TEXT = 0x00FFFFFF   # White  (table text)

PHASE_NAMES = {
    "A": "Accumulation",
    "M": "Manipulation",
    "D": "Distribution",
    "C": "Continuation",
}

PHASE_COLORS = {
    "A": COLOR_A,
    "M": COLOR_M,
    "D": COLOR_D,
    "C": COLOR_C,
}


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class AMDPhase:
    """A single AMD box for one timeframe level."""
    level:      str     # "1M","5M","1H","4H","Day","Week","Month","Quarter","Year"
    phase:      str     # "A","M","D","C"
    t_start:    int     # unix timestamp of period start
    t_end:      int     # unix timestamp of period end
    high:       float   # highest price in this period so far
    low:        float   # lowest price in this period so far
    is_current: bool    # True if this is the currently active period

    @property
    def name(self) -> str:
        return f"{AMD_PREFIX}{self.level}_{self.phase}_{self.t_start}"

    @property
    def color(self) -> int:
        return PHASE_COLORS.get(self.phase, COLOR_C)


@dataclass
class AMDStatus:
    """Current AMD phase at every level — used for the info table."""
    minute:   str = "?"
    m5:       str = "?"
    h1:       str = "?"
    h4:       str = "?"
    day:      str = "?"
    week:     str = "?"
    month:    str = "?"
    quarter:  str = "?"
    year:     str = "?"


# ── Time helpers ──────────────────────────────────────────────────────────────

def _floor_to_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)

def _floor_to_5m(dt: datetime) -> datetime:
    return dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)

def _floor_to_1h(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)

def _floor_to_4h(dt: datetime) -> datetime:
    return dt.replace(hour=(dt.hour // 4) * 4, minute=0, second=0, microsecond=0)

def _floor_to_day(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)

def _floor_to_week(dt: datetime) -> datetime:
    """Monday of the current week."""
    return _floor_to_day(dt) - timedelta(days=dt.weekday())

def _floor_to_month(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

def _floor_to_quarter(dt: datetime) -> datetime:
    q_month = ((dt.month - 1) // 3) * 3 + 1
    return dt.replace(month=q_month, day=1, hour=0, minute=0, second=0, microsecond=0)

def _floor_to_year(dt: datetime) -> datetime:
    return dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)


def _phase_4(index: int) -> str:
    """For a 4-part division, return the phase letter."""
    return ["A", "M", "D", "C"][min(index, 3)]


def _dt_to_timestamp(dt: datetime) -> int:
    return int(dt.timestamp())


# ── Phase calculation ──────────────────────────────────────────────────────────

def _get_phases_for_level(now: datetime):
    """
    Returns (current_phase, period_start, period_end, sub_periods)
    for every level in the hierarchy.

    sub_periods: list of (phase, t_start, t_end) for the full period
                 so we can draw all 4 boxes, not just the current one.
    """
    results = {}

    # ── 1M inside 5M (5 parts) ────────────────────────────────────
    m5_start = _floor_to_5m(now)
    minute_offset = now.minute - m5_start.minute
    # 5 minutes → A,A,M,D,D  (rough mapping)
    minute_map = ["A", "M", "M", "D", "D"]
    m1_phase = minute_map[min(minute_offset, 4)]
    subs_1m = []
    for i in range(5):
        ts = _dt_to_timestamp(m5_start + timedelta(minutes=i))
        te = _dt_to_timestamp(m5_start + timedelta(minutes=i + 1))
        subs_1m.append((minute_map[i], ts, te))
    results["1M"] = {
        "phase":    m1_phase,
        "start":    _dt_to_timestamp(m5_start + timedelta(minutes=minute_offset)),
        "end":      _dt_to_timestamp(m5_start + timedelta(minutes=minute_offset + 1)),
        "subs":     subs_1m,
        "period_start": _dt_to_timestamp(m5_start),
        "period_end":   _dt_to_timestamp(m5_start + timedelta(minutes=5)),
    }

    # ── 5M inside 1H (12 parts → group into 4) ───────────────────
    h1_start = _floor_to_1h(now)
    m5_index = (now.minute // 5)           # 0..11
    m5_group = m5_index // 3               # 0..3 → A/M/D/C
    m5_phase = _phase_4(m5_group)
    subs_5m = []
    for i in range(4):
        ts = _dt_to_timestamp(h1_start + timedelta(minutes=i * 15))
        te = _dt_to_timestamp(h1_start + timedelta(minutes=(i + 1) * 15))
        subs_5m.append((_phase_4(i), ts, te))
    results["5M"] = {
        "phase":    m5_phase,
        "start":    _dt_to_timestamp(h1_start + timedelta(minutes=m5_group * 15)),
        "end":      _dt_to_timestamp(h1_start + timedelta(minutes=(m5_group + 1) * 15)),
        "subs":     subs_5m,
        "period_start": _dt_to_timestamp(h1_start),
        "period_end":   _dt_to_timestamp(h1_start + timedelta(hours=1)),
    }

    # ── 1H inside 4H (4 parts) ───────────────────────────────────
    h4_start = _floor_to_4h(now)
    h1_index = now.hour - h4_start.hour    # 0..3
    h1_phase = _phase_4(h1_index)
    subs_1h = []
    for i in range(4):
        ts = _dt_to_timestamp(h4_start + timedelta(hours=i))
        te = _dt_to_timestamp(h4_start + timedelta(hours=i + 1))
        subs_1h.append((_phase_4(i), ts, te))
    results["1H"] = {
        "phase":    h1_phase,
        "start":    _dt_to_timestamp(h4_start + timedelta(hours=h1_index)),
        "end":      _dt_to_timestamp(h4_start + timedelta(hours=h1_index + 1)),
        "subs":     subs_1h,
        "period_start": _dt_to_timestamp(h4_start),
        "period_end":   _dt_to_timestamp(h4_start + timedelta(hours=4)),
    }

    # ── 4H inside Day (6 parts → group into 3 pairs = A/M/D) ─────
    day_start = _floor_to_day(now)
    h4_index  = now.hour // 4             # 0..5
    # 6 sessions → A(0,1), M(2,3), D(4,5)
    h4_map    = ["A", "A", "M", "M", "D", "D"]
    h4_phase  = h4_map[h4_index]
    subs_4h = []
    for i in range(6):
        ts = _dt_to_timestamp(day_start + timedelta(hours=i * 4))
        te = _dt_to_timestamp(day_start + timedelta(hours=(i + 1) * 4))
        subs_4h.append((h4_map[i], ts, te))
    results["4H"] = {
        "phase":    h4_phase,
        "start":    _dt_to_timestamp(day_start + timedelta(hours=h4_index * 4)),
        "end":      _dt_to_timestamp(day_start + timedelta(hours=(h4_index + 1) * 4)),
        "subs":     subs_4h,
        "period_start": _dt_to_timestamp(day_start),
        "period_end":   _dt_to_timestamp(day_start + timedelta(hours=24)),
    }

    # ── Day inside Week (5 trading days → Mon=A,Tue=A,Wed=M,Thu=D,Fri=D) ──
    week_start  = _floor_to_week(now)
    day_of_week = now.weekday()            # 0=Mon..4=Fri
    day_map     = ["A", "A", "M", "D", "D"]
    day_phase   = day_map[min(day_of_week, 4)]
    subs_day = []
    for i in range(5):
        ts = _dt_to_timestamp(week_start + timedelta(days=i))
        te = _dt_to_timestamp(week_start + timedelta(days=i + 1))
        subs_day.append((day_map[i], ts, te))
    results["Day"] = {
        "phase":    day_phase,
        "start":    _dt_to_timestamp(week_start + timedelta(days=day_of_week)),
        "end":      _dt_to_timestamp(week_start + timedelta(days=day_of_week + 1)),
        "subs":     subs_day,
        "period_start": _dt_to_timestamp(week_start),
        "period_end":   _dt_to_timestamp(week_start + timedelta(days=5)),
    }

    # ── Week inside Month (4 weeks) ───────────────────────────────
    month_start = _floor_to_month(now)
    week_num    = (now.day - 1) // 7      # 0..3
    week_phase  = _phase_4(week_num)
    subs_week = []
    for i in range(4):
        ts = _dt_to_timestamp(month_start + timedelta(weeks=i))
        te = _dt_to_timestamp(month_start + timedelta(weeks=i + 1))
        subs_week.append((_phase_4(i), ts, te))
    results["Week"] = {
        "phase":    week_phase,
        "start":    _dt_to_timestamp(month_start + timedelta(weeks=week_num)),
        "end":      _dt_to_timestamp(month_start + timedelta(weeks=week_num + 1)),
        "subs":     subs_week,
        "period_start": _dt_to_timestamp(month_start),
        "period_end":   _dt_to_timestamp(month_start + timedelta(weeks=4)),
    }

    # ── Month inside Quarter (3 months → A/M/D) ──────────────────
    q_start     = _floor_to_quarter(now)
    month_in_q  = (now.month - q_start.month)  # 0..2
    month_map   = ["A", "M", "D"]
    month_phase = month_map[min(month_in_q, 2)]
    subs_month = []
    for i in range(3):
        m = q_start.month + i
        y = q_start.year + (m - 1) // 12
        m = ((m - 1) % 12) + 1
        ts = _dt_to_timestamp(datetime(y, m, 1))
        m2 = m + 1
        y2 = y
        if m2 > 12: m2 = 1; y2 += 1
        te = _dt_to_timestamp(datetime(y2, m2, 1))
        subs_month.append((month_map[i], ts, te))
    results["Month"] = {
        "phase":    month_phase,
        "start":    subs_month[month_in_q][1],
        "end":      subs_month[month_in_q][2],
        "subs":     subs_month,
        "period_start": _dt_to_timestamp(q_start),
        "period_end":   _dt_to_timestamp(
            q_start.replace(month=q_start.month + 3)
            if q_start.month <= 9
            else datetime(q_start.year + 1, 1, 1)
        ),
    }

    # ── Quarter inside Year (4 quarters) ─────────────────────────
    year_start  = _floor_to_year(now)
    q_index     = (now.month - 1) // 3    # 0..3
    q_phase     = _phase_4(q_index)
    q_starts    = [1, 4, 7, 10]
    subs_q = []
    for i in range(4):
        qm = q_starts[i]
        ts = _dt_to_timestamp(datetime(now.year, qm, 1))
        next_qm = q_starts[i + 1] if i < 3 else 13
        next_y  = now.year if next_qm <= 12 else now.year + 1
        next_qm = next_qm if next_qm <= 12 else 1
        te = _dt_to_timestamp(datetime(next_y, next_qm, 1))
        subs_q.append((_phase_4(i), ts, te))
    results["Quarter"] = {
        "phase":    q_phase,
        "start":    subs_q[q_index][1],
        "end":      subs_q[q_index][2],
        "subs":     subs_q,
        "period_start": _dt_to_timestamp(year_start),
        "period_end":   _dt_to_timestamp(datetime(now.year + 1, 1, 1)),
    }

    # ── Year (standalone — no parent shown) ──────────────────────
    results["Year"] = {
        "phase":    f"Y{now.year}",
        "start":    _dt_to_timestamp(year_start),
        "end":      _dt_to_timestamp(datetime(now.year + 1, 1, 1)),
        "subs":     [],
        "period_start": _dt_to_timestamp(year_start),
        "period_end":   _dt_to_timestamp(datetime(now.year + 1, 1, 1)),
    }

    return results


def get_current_amd_status(symbol: str) -> Optional[AMDStatus]:
    """
    Returns AMDStatus showing which phase we are in at every level right now.
    Labels use clean numeric positions (Q1-4, S1-6, H1-24, G1-4, m1-5)
    with no parentheses and no embedded phase letters.
    """
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return None

    now    = datetime.fromtimestamp(tick.time)
    phases = _get_phases_for_level(now)

    # Quarter: Q1-Q4
    q_index   = (now.month - 1) // 3 + 1          # 1..4
    # 4H session: S1-S6 (6 × 4H blocks per day)
    s_index   = now.hour // 4 + 1                  # 1..6
    # Hour: H1-H24
    h_index   = now.hour + 1                        # 1..24
    # 5M group: G1-G4 (4 × 15-min groups per hour)
    g_index   = (now.minute // 15) + 1             # 1..4
    # Minute within 5M bar: m1-m5
    m5_start  = (now.minute // 5) * 5
    min_index = now.minute - m5_start + 1          # 1..5

    return AMDStatus(
        minute  = f"m{min_index}",
        m5      = f"G{g_index}",
        h1      = f"H{h_index}",
        h4      = f"S{s_index}",
        day     = phases["Day"]["phase"],
        week    = phases["Week"]["phase"],
        month   = phases["Month"]["phase"],
        quarter = f"Q{q_index}",
        year    = str(now.year),
    )


def get_amd_boxes(symbol: str, visible_levels: list = None) -> list:
    """
    Returns list of AMDPhase boxes to draw on chart.
    visible_levels: which levels to draw (default: all except 1M/5M for clarity)
    """
    if visible_levels is None:
        visible_levels = ["1H", "4H", "Day", "Week", "Month", "Quarter"]

    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return []

    now    = datetime.fromtimestamp(tick.time)
    phases = _get_phases_for_level(now)
    boxes  = []

    for level in visible_levels:
        if level not in phases:
            continue
        info = phases[level]

        # Fetch OHLC for the sub-periods to get high/low of each box
        tf_map = {
            "1M":      mt5.TIMEFRAME_M1,
            "5M":      mt5.TIMEFRAME_M5,
            "1H":      mt5.TIMEFRAME_H1,
            "4H":      mt5.TIMEFRAME_H4,
            "Day":     mt5.TIMEFRAME_D1,
            "Week":    mt5.TIMEFRAME_W1,
            "Month":   mt5.TIMEFRAME_MN1,
            "Quarter": mt5.TIMEFRAME_MN1,
        }

        # For drawing: just draw the current phase box using the sub-period range
        for (phase, ts, te) in info["subs"]:
            # Try to get high/low from MT5 for this sub-period
            h, l = _get_hl_for_period(symbol, ts, te, tf_map.get(level, mt5.TIMEFRAME_H1))
            if h == 0 and l == 0:
                h = tick.bid + 0.001
                l = tick.bid - 0.001

            is_current = (ts <= tick.time < te)
            boxes.append(AMDPhase(
                level      = level,
                phase      = phase,
                t_start    = ts,
                t_end      = te,
                high       = h,
                low        = l,
                is_current = is_current,
            ))

    return boxes


def _get_hl_for_period(symbol: str, t_start: int, t_end: int, timeframe: int):
    """Get the high and low for a time period from MT5 bars."""
    try:
        bars = mt5.copy_rates_range(symbol, timeframe, t_start, t_end)
        if bars is None or len(bars) == 0:
            return 0.0, 0.0
        high = max(float(b["high"]) for b in bars)
        low  = min(float(b["low"])  for b in bars)
        return high, low
    except Exception:
        return 0.0, 0.0


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


def draw_amd_on_chart(symbol: str, boxes: list, status: AMDStatus,
                      show_current_only: bool = False):
    """
    Draw AMD boxes (outline only, no fill) on chart.
    Also writes a human-readable status file alongside the command file
    so any indicator/EA that reads it can display it on the chart.
    """
    commands = [f"DELETE_PREFIX|{AMD_PREFIX}", f"DELETE_PREFIX|{TABLE_PREFIX}"]

    drawn = 0
    for box in boxes:
        if show_current_only and not box.is_current:
            continue
        # Outline only (fill=0) — solid boxes obscure candles
        commands.append(
            f"DRAW_RECT|{box.name}|{box.t_start}|{box.high}|"
            f"{box.t_end}|{box.low}|{box.color}|2|0"
        )
        drawn += 1

    _write_commands(symbol, commands)

    # ── Write compact status as a text file for chart display ────
    # Written next to the command file so an EA/indicator can pick it up.
    # Also used by the GUI label refresh directly.
    if status:
        phase_icon = {"A": "▲", "M": "■", "D": "▼", "C": "○"}
        di = phase_icon.get(status.day,   "")
        mi = phase_icon.get(status.month, "")
        wi = phase_icon.get(status.week,  "")
        line1 = (f"Y:{status.year}  {status.quarter}  "
                 f"Month:{status.month}{mi}  Week:{status.week}{wi}")
        line2 = (f"Day:{status.day}{di}  "
                 f"Session:{status.h4}  Hour:{status.h1}")
        line3 = f"5min:{status.m5}  1min:{status.minute}"
        try:
            appdata = os.environ.get("APPDATA", "")
            status_path = os.path.join(
                appdata, "MetaQuotes", "Terminal", "Common", "Files",
                f"trader_amd_status_{symbol}.txt"
            )
            os.makedirs(os.path.dirname(status_path), exist_ok=True)
            with open(status_path, "w", encoding="utf-8") as f:
                f.write(f"{line1}\n{line2}\n{line3}\n")
        except Exception:
            pass

    log.info("AMD: drew %d boxes", drawn)


def clear_amd_on_chart(symbol: str):
    _write_commands(symbol, [
        f"DELETE_PREFIX|{AMD_PREFIX}",
        f"DELETE_PREFIX|{TABLE_PREFIX}",
    ])