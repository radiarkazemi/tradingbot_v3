"""
amd_detector.py — Quarter Theory AMD Detector
==============================================
Divides time into nested fractal quarters following the AMD
(Accumulation, Manipulation, Distribution) structure.

Hierarchy:
  Year     → 4 Quarters  (Q1/Q2/Q3/Q4)
  Quarter  → 3 Months    (M1/M2/M3)
  Month    → 4 Weeks     (W1/W2/W3/W4)
  Week     → 5 Days      (D1=Mon .. D5=Fri)
  Day      → 6 x 4H      (S1..S6, grouped A/M/D)
  4H       → 4 x 1H      (H1..H4)
  1H       → 12 x 5M     (grouped into 4 x 15min)
  5M       → 5 x 1M      (grouped A/M/D)

Status format example:
  Y2026  Q2(M)  M2(M)  W3(D)  D3(M)  4H:S2(A)  1H:H2(M)  5M:G2(M)  1M:m3(D)

Colors:
  A = Green   M = Red   D = Blue   C = Gray
"""

import MetaTrader5 as mt5
import os
import time as _time
import logging
from dataclasses import dataclass
from typing import Optional
from datetime import datetime, timedelta

log = logging.getLogger("amd_detector")

AMD_PREFIX   = "AMD_"
TABLE_PREFIX = "AMDT_"

# MQL5 BGR colors
COLOR_A = 0x0000FF00   # Green
COLOR_M = 0x000000FF   # Red
COLOR_D = 0x00FF0000   # Blue
COLOR_C = 0x00808080   # Gray

PHASE_NAMES = {
    "A": "Accumulation",
    "M": "Manipulation",
    "D": "Distribution",
    "C": "Continuation",
}

PHASE_COLORS = {"A": COLOR_A, "M": COLOR_M, "D": COLOR_D, "C": COLOR_C}


# ── Data classes ──────────────────────────────────────────────────

@dataclass
class AMDStatus:
    """
    Current AMD phase at every level with both position number and phase letter.
    Example: year="2026"  quarter="Q2(M)"  month="M2(M)"  week="W3(D)" ...
    """
    year:    str = "?"   # e.g. "2026"
    quarter: str = "?"   # e.g. "Q2(M)"
    month:   str = "?"   # e.g. "M2(M)"  — month inside quarter
    week:    str = "?"   # e.g. "W3(D)"  — week inside month
    day:     str = "?"   # e.g. "D3(M)"  — day inside week (1=Mon)
    h4:      str = "?"   # e.g. "S2(A)"  — 4H session inside day (1..6)
    h1:      str = "?"   # e.g. "H3(D)"  — 1H inside 4H (1..4)
    m5:      str = "?"   # e.g. "G2(M)"  — 15min group inside 1H (1..4)
    minute:  str = "?"   # e.g. "m3(D)"  — 1M slot inside 5M (1..5)


@dataclass
class AMDPhase:
    """A single AMD box for one timeframe level."""
    level:      str
    phase:      str
    position:   int     # 1-based position number within its parent
    t_start:    int
    t_end:      int
    high:       float
    low:        float
    is_current: bool

    @property
    def name(self):
        return f"{AMD_PREFIX}{self.level}_{self.position}_{self.t_start}"

    @property
    def color(self):
        return PHASE_COLORS.get(self.phase, COLOR_C)

    @property
    def label(self):
        return f"{self.position}({self.phase})"


# ── Helpers ───────────────────────────────────────────────────────

def _phase_4(index):
    return ["A", "M", "D", "C"][min(index, 3)]

def _ts(dt):
    return int(dt.timestamp())

def _floor_to_5m(dt):
    return dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)

def _floor_to_1h(dt):
    return dt.replace(minute=0, second=0, microsecond=0)

def _floor_to_4h(dt):
    return dt.replace(hour=(dt.hour // 4) * 4, minute=0, second=0, microsecond=0)

def _floor_to_day(dt):
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)

def _floor_to_week(dt):
    return _floor_to_day(dt) - timedelta(days=dt.weekday())

def _floor_to_month(dt):
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

def _floor_to_quarter(dt):
    q_month = ((dt.month - 1) // 3) * 3 + 1
    return dt.replace(month=q_month, day=1, hour=0, minute=0, second=0, microsecond=0)

def _floor_to_year(dt):
    return dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)


# ── Core calculation ──────────────────────────────────────────────

def get_current_amd_status(symbol):
    """
    Returns AMDStatus with position + phase at every level.
    e.g. quarter="Q2(M)" means we are in Quarter 2 which is the M phase.
    """
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return None

    now = datetime.fromtimestamp(tick.time)

    # ── Year ──────────────────────────────────────────────────────
    year_str = str(now.year)

    # ── Quarter inside Year ───────────────────────────────────────
    q_index  = (now.month - 1) // 3        # 0..3
    q_num    = q_index + 1                  # 1..4
    q_phase  = _phase_4(q_index)
    quarter_str = f"Q{q_num}({q_phase})"

    # ── Month inside Quarter ──────────────────────────────────────
    q_start     = _floor_to_quarter(now)
    month_in_q  = now.month - q_start.month   # 0..2
    month_num   = month_in_q + 1              # 1..3
    month_map   = ["A", "M", "D"]
    month_phase = month_map[min(month_in_q, 2)]
    month_str   = f"M{month_num}({month_phase})"

    # ── Week inside Month ─────────────────────────────────────────
    week_num   = (now.day - 1) // 7           # 0..3
    week_pos   = week_num + 1                 # 1..4
    week_phase = _phase_4(week_num)
    week_str   = f"W{week_pos}({week_phase})"

    # ── Day inside Week ───────────────────────────────────────────
    day_of_week = now.weekday()               # 0=Mon..4=Fri
    day_pos     = day_of_week + 1             # 1..5
    day_map     = ["A", "A", "M", "D", "D"]
    day_phase   = day_map[min(day_of_week, 4)]
    day_str     = f"D{day_pos}({day_phase})"

    # ── 4H Session inside Day ─────────────────────────────────────
    h4_index  = now.hour // 4               # 0..5
    h4_pos    = h4_index + 1               # 1..6
    h4_map    = ["A", "A", "M", "M", "D", "D"]
    h4_phase  = h4_map[h4_index]
    h4_str    = f"S{h4_pos}({h4_phase})"

    # ── 1H inside 4H ─────────────────────────────────────────────
    h4_start  = _floor_to_4h(now)
    h1_index  = now.hour - h4_start.hour   # 0..3
    h1_pos    = h1_index + 1               # 1..4
    h1_phase  = _phase_4(h1_index)
    h1_str    = f"H{h1_pos}({h1_phase})"

    # ── 15min Group (5M) inside 1H ───────────────────────────────
    m5_index  = now.minute // 5            # 0..11
    grp_index = m5_index // 3             # 0..3  (groups of 3 × 5min = 15min)
    grp_pos   = grp_index + 1             # 1..4
    grp_phase = _phase_4(grp_index)
    m5_str    = f"G{grp_pos}({grp_phase})"

    # ── 1M inside 5M ─────────────────────────────────────────────
    m5_start    = _floor_to_5m(now)
    min_offset  = now.minute - m5_start.minute   # 0..4
    min_pos     = min_offset + 1                 # 1..5
    min_map     = ["A", "M", "M", "D", "D"]
    min_phase   = min_map[min(min_offset, 4)]
    min_str     = f"m{min_pos}({min_phase})"

    return AMDStatus(
        year    = year_str,
        quarter = quarter_str,
        month   = month_str,
        week    = week_str,
        day     = day_str,
        h4      = h4_str,
        h1      = h1_str,
        m5      = m5_str,
        minute  = min_str,
    )


def get_amd_boxes(symbol, visible_levels=None):
    """
    Returns list of AMDPhase boxes to draw on chart.
    Each box has position number + phase label.
    """
    if visible_levels is None:
        visible_levels = ["1H", "4H", "Day", "Week", "Month", "Quarter"]

    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return []

    now    = datetime.fromtimestamp(tick.time)
    boxes  = []

    # ── Quarter boxes (4 per year) ────────────────────────────────
    if "Quarter" in visible_levels:
        q_starts = [1, 4, 7, 10]
        q_index  = (now.month - 1) // 3
        for i in range(4):
            qm = q_starts[i]
            ts = _ts(datetime(now.year, qm, 1))
            next_qm = q_starts[i + 1] if i < 3 else 1
            next_y  = now.year if i < 3 else now.year + 1
            te = _ts(datetime(next_y, next_qm, 1))
            h, l = _get_hl(symbol, ts, te, mt5.TIMEFRAME_MN1)
            boxes.append(AMDPhase(
                level="Quarter", phase=_phase_4(i), position=i+1,
                t_start=ts, t_end=te, high=h, low=l,
                is_current=(i == q_index)
            ))

    # ── Month boxes (3 per quarter) ───────────────────────────────
    if "Month" in visible_levels:
        q_start    = _floor_to_quarter(now)
        month_map  = ["A", "M", "D"]
        month_in_q = now.month - q_start.month
        for i in range(3):
            m = q_start.month + i
            y = q_start.year
            if m > 12: m -= 12; y += 1
            ts = _ts(datetime(y, m, 1))
            m2 = m + 1; y2 = y
            if m2 > 12: m2 = 1; y2 += 1
            te = _ts(datetime(y2, m2, 1))
            h, l = _get_hl(symbol, ts, te, mt5.TIMEFRAME_MN1)
            boxes.append(AMDPhase(
                level="Month", phase=month_map[i], position=i+1,
                t_start=ts, t_end=te, high=h, low=l,
                is_current=(i == month_in_q)
            ))

    # ── Week boxes (4 per month) ──────────────────────────────────
    if "Week" in visible_levels:
        month_start = _floor_to_month(now)
        week_num    = (now.day - 1) // 7
        for i in range(4):
            ts = _ts(month_start + timedelta(weeks=i))
            te = _ts(month_start + timedelta(weeks=i + 1))
            h, l = _get_hl(symbol, ts, te, mt5.TIMEFRAME_D1)
            boxes.append(AMDPhase(
                level="Week", phase=_phase_4(i), position=i+1,
                t_start=ts, t_end=te, high=h, low=l,
                is_current=(i == week_num)
            ))

    # ── Day boxes (5 per week) ────────────────────────────────────
    if "Day" in visible_levels:
        week_start  = _floor_to_week(now)
        dow         = now.weekday()
        day_map     = ["A", "A", "M", "D", "D"]
        for i in range(5):
            ts = _ts(week_start + timedelta(days=i))
            te = _ts(week_start + timedelta(days=i + 1))
            h, l = _get_hl(symbol, ts, te, mt5.TIMEFRAME_H4)
            boxes.append(AMDPhase(
                level="Day", phase=day_map[i], position=i+1,
                t_start=ts, t_end=te, high=h, low=l,
                is_current=(i == dow)
            ))

    # ── 4H boxes (6 per day) ──────────────────────────────────────
    if "4H" in visible_levels:
        day_start = _floor_to_day(now)
        h4_index  = now.hour // 4
        h4_map    = ["A", "A", "M", "M", "D", "D"]
        for i in range(6):
            ts = _ts(day_start + timedelta(hours=i * 4))
            te = _ts(day_start + timedelta(hours=(i + 1) * 4))
            h, l = _get_hl(symbol, ts, te, mt5.TIMEFRAME_H1)
            boxes.append(AMDPhase(
                level="4H", phase=h4_map[i], position=i+1,
                t_start=ts, t_end=te, high=h, low=l,
                is_current=(i == h4_index)
            ))

    # ── 1H boxes (4 per 4H) ───────────────────────────────────────
    if "1H" in visible_levels:
        h4_start = _floor_to_4h(now)
        h1_index = now.hour - h4_start.hour
        for i in range(4):
            ts = _ts(h4_start + timedelta(hours=i))
            te = _ts(h4_start + timedelta(hours=i + 1))
            h, l = _get_hl(symbol, ts, te, mt5.TIMEFRAME_M5)
            boxes.append(AMDPhase(
                level="1H", phase=_phase_4(i), position=i+1,
                t_start=ts, t_end=te, high=h, low=l,
                is_current=(i == h1_index)
            ))

    # ── 5M boxes (4 × 15min groups per 1H) ───────────────────────
    if "5M" in visible_levels:
        h1_start  = _floor_to_1h(now)
        grp_index = (now.minute // 5) // 3
        for i in range(4):
            ts = _ts(h1_start + timedelta(minutes=i * 15))
            te = _ts(h1_start + timedelta(minutes=(i + 1) * 15))
            h, l = _get_hl(symbol, ts, te, mt5.TIMEFRAME_M1)
            boxes.append(AMDPhase(
                level="5M", phase=_phase_4(i), position=i+1,
                t_start=ts, t_end=te, high=h, low=l,
                is_current=(i == grp_index)
            ))

    # ── 1M boxes (5 per 5M) ───────────────────────────────────────
    if "1M" in visible_levels:
        m5_start   = _floor_to_5m(now)
        min_offset = now.minute - m5_start.minute
        min_map    = ["A", "M", "M", "D", "D"]
        for i in range(5):
            ts = _ts(m5_start + timedelta(minutes=i))
            te = _ts(m5_start + timedelta(minutes=i + 1))
            h, l = _get_hl(symbol, ts, te, mt5.TIMEFRAME_M1)
            boxes.append(AMDPhase(
                level="1M", phase=min_map[i], position=i+1,
                t_start=ts, t_end=te, high=h, low=l,
                is_current=(i == min_offset)
            ))

    return boxes


def _get_hl(symbol, t_start, t_end, timeframe):
    try:
        bars = mt5.copy_rates_range(symbol, timeframe, t_start, t_end)
        if bars is None or len(bars) == 0:
            return 0.0, 0.0
        return max(float(b["high"]) for b in bars), min(float(b["low"]) for b in bars)
    except Exception:
        return 0.0, 0.0


# ── Chart drawing ─────────────────────────────────────────────────

def _command_file(symbol):
    appdata = os.environ.get("APPDATA", "")
    return os.path.join(
        appdata, "MetaQuotes", "Terminal", "Common", "Files",
        f"trader_commands_{symbol}.txt"
    )


def _write_commands(symbol, commands):
    path = _command_file(symbol)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for _ in range(5):
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(commands) + "\n")
            return
        except PermissionError:
            _time.sleep(0.05)


def draw_amd_on_chart(symbol, boxes, status, show_current_only=True):
    commands = [f"DELETE_PREFIX|{AMD_PREFIX}", f"DELETE_PREFIX|{TABLE_PREFIX}"]

    for box in boxes:
        if show_current_only and not box.is_current:
            continue
        if box.high == 0 and box.low == 0:
            continue
        fill = 1 if box.is_current else 0
        commands.append(
            f"DRAW_RECT|{box.name}|{box.t_start}|{box.high}|"
            f"{box.t_end}|{box.low}|{box.color}|1|{fill}"
        )

    # ── Info table top-right ──────────────────────────────────────
    if status:
        COLOR_TEXT = 0x00FFFFFF
        icon = {"A": "A", "M": "M", "D": "D", "C": "C"}

        def row_color(field):
            # Extract phase letter from e.g. "Q2(M)" → "M"
            if "(" in field and ")" in field:
                ph = field[field.index("(")+1:field.index(")")]
                return PHASE_COLORS.get(ph, COLOR_TEXT)
            return COLOR_TEXT

        rows = [
            ("── AMD QUARTER THEORY ──", COLOR_TEXT),
            (f"Year    : {status.year}",    COLOR_TEXT),
            (f"Quarter : {status.quarter}", row_color(status.quarter)),
            (f"Month   : {status.month}",   row_color(status.month)),
            (f"Week    : {status.week}",    row_color(status.week)),
            (f"Day     : {status.day}",     row_color(status.day)),
            (f"4H Sess : {status.h4}",      row_color(status.h4)),
            (f"1H      : {status.h1}",      row_color(status.h1)),
            (f"15M Grp : {status.m5}",      row_color(status.m5)),
            (f"1M      : {status.minute}",  row_color(status.minute)),
        ]
        for i, (text, color) in enumerate(rows):
            commands.append(
                f"DRAW_LABEL|{TABLE_PREFIX}row{i}|1|10|{20 + i * 18}|{text}|{color}|10"
            )

    _write_commands(symbol, commands)
    log.info("AMD: drew %d boxes", sum(1 for b in boxes if not show_current_only or b.is_current))


def clear_amd_on_chart(symbol):
    _write_commands(symbol, [
        f"DELETE_PREFIX|{AMD_PREFIX}",
        f"DELETE_PREFIX|{TABLE_PREFIX}",
    ])