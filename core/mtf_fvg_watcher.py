"""
mtf_fvg_watcher.py — Multi-Timeframe FVG Confluence Watcher
============================================================
Triggers on every completed 1M candle (not on a fixed timer).
Scans 15M, 5M, 1M simultaneously and finds overlapping, temporally
close FVG zones (see mtf_fvg.py for the recency-matching logic).

Fixes applied:
  - Candle-close detection now uses copy_rates_from_pos(..., 0, 3)
    and reads the second-to-last bar's time, which is the most
    recently CLOSED 1M candle. Previously this could fail to advance
    correctly across polls.
  - Logging only happens when the zone SET actually changes (new
    zone appears, or a zone is mitigated) — not on every poll. The
    old code re-logged the full identical zone list every second
    even when nothing had changed, which looked like the detector
    was "re-finding" the same zones repeatedly.
"""

import threading
import logging

log = logging.getLogger("mtf_fvg_watcher")


class MTFFVGWatcher(threading.Thread):

    def __init__(
        self,
        symbol:        str,
        pip_size:      float,
        min_gap_pips:  float = 1.0,
        lookback_15m:  int   = 50,
        lookback_5m:   int   = 100,
        lookback_1m:   int   = 200,
        max_zones:     int   = 20,
        max_draw:      int   = 20,
        draw_on_chart: bool  = True,
        poll_interval: float = 1.0,
        log_fn=None,
    ):
        threading.Thread.__init__(self)
        self.daemon        = True
        self.symbol        = symbol
        self.pip_size      = pip_size
        self.min_gap_pips  = min_gap_pips
        self.lookback_15m  = lookback_15m
        self.lookback_5m   = lookback_5m
        self.lookback_1m   = lookback_1m
        self.max_zones     = max_zones
        self.max_draw      = max_draw
        self.draw_on_chart = draw_on_chart
        self.poll_interval = poll_interval
        self._log          = log_fn or (lambda msg, level="INFO": log.info(msg))
        self._stop_event   = threading.Event()
        self._lock         = threading.Lock()

        self.latest_zones      = []
        self._last_candle_time = 0
        self._known_names      = set()
        self._mitigated_log    = set()

    # ── Public API ────────────────────────────────────────────────

    def stop(self):
        self._stop_event.set()

    def get_zones(self):
        with self._lock:
            return [z for z in self.latest_zones if not z.mitigated]

    def get_all_zones(self):
        with self._lock:
            return list(self.latest_zones)

    def update_settings(self, min_gap_pips=None, lookback_15m=None,
                        lookback_5m=None, lookback_1m=None,
                        max_zones=None, max_draw=None):
        if min_gap_pips is not None: self.min_gap_pips = min_gap_pips
        if lookback_15m is not None: self.lookback_15m = lookback_15m
        if lookback_5m  is not None: self.lookback_5m  = lookback_5m
        if lookback_1m  is not None: self.lookback_1m  = lookback_1m
        if max_zones    is not None: self.max_zones    = max_zones
        if max_draw     is not None: self.max_draw     = max_draw
        self._last_candle_time = 0
        self._known_names.clear()

    # ── Main loop ─────────────────────────────────────────────────

    def run(self):
        try:
            import MetaTrader5 as mt5
            from core.mtf_fvg import (
                find_mtf_zones, check_mitigation,
                draw_mtf_zones, clear_mtf_zones,
            )
        except Exception as e:
            self._log(f"💥 MTF FVG import error: {e}", "ERROR")
            return

        self._log(
            f"🟡  MTF FVG Watcher started | {self.symbol} | "
            f"min={self.min_gap_pips}pips | "
            f"lookback 15M={self.lookback_15m} 5M={self.lookback_5m} "
            f"1M={self.lookback_1m}",
            "INFO"
        )

        self._stop_event.wait(2.0)

        while not self._stop_event.is_set():
            try:
                import MetaTrader5 as mt5

                # Fetch 3 bars: [2 closed bars ..., 1 forming bar]
                # bars[-1] = currently forming (incomplete)
                # bars[-2] = most recently CLOSED candle
                bars = mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_M1, 0, 3)

                if bars is None or len(bars) < 2:
                    self._stop_event.wait(self.poll_interval)
                    continue

                latest_closed_time = int(bars[-2]["time"])
                new_candle = (latest_closed_time != self._last_candle_time)

                if new_candle:
                    self._last_candle_time = latest_closed_time
                    self._run_detection(find_mtf_zones, check_mitigation, draw_mtf_zones)
                else:
                    self._check_mitigation_only(check_mitigation, draw_mtf_zones)

            except Exception as e:
                import traceback as _tb
                self._log(f"💥 MTF FVG error: {e}", "ERROR")
                for line in _tb.format_exc().strip().splitlines():
                    self._log(f"   {line}", "ERROR")

            self._stop_event.wait(self.poll_interval)

        try:
            from core.mtf_fvg import clear_mtf_zones
            clear_mtf_zones(self.symbol)
        except Exception:
            pass

        self._log("🟡  MTF FVG Watcher stopped", "INFO")

    # ── Detection cycle (only runs on new 1M candle close) ────────

    def _run_detection(self, find_mtf_zones, check_mitigation, draw_mtf_zones):
        fresh_zones = find_mtf_zones(
            symbol       = self.symbol,
            pip_size     = self.pip_size,
            min_gap_pips = self.min_gap_pips,
            lookback_15m = self.lookback_15m,
            lookback_5m  = self.lookback_5m,
            lookback_1m  = self.lookback_1m,
        )

        existing = {z.name: z for z in self.latest_zones}
        merged   = []
        for z in fresh_zones:
            if z.name in existing:
                z.mitigated = existing[z.name].mitigated
            merged.append(z)
        merged = merged[:self.max_zones]
        merged = check_mitigation(merged, self.symbol)

        # Detect if anything actually changed before logging/drawing
        new_names = {z.name for z in merged if not z.mitigated}
        changed   = (new_names != self._known_names)

        with self._lock:
            self.latest_zones = merged

        if changed:
            self._log_new(merged)
            self._log_mitigated(merged)
            self._known_names = new_names

        if self.draw_on_chart:
            try:
                draw_mtf_zones(self.symbol, merged, max_draw=self.max_draw)
            except Exception as e:
                self._log(f"⚠️  MTF FVG draw: {e}", "WARN")

    # ── Lightweight mitigation-only check (no rescan) ──────────────

    def _check_mitigation_only(self, check_mitigation, draw_mtf_zones):
        with self._lock:
            current = list(self.latest_zones)

        if not current:
            return

        updated = check_mitigation(current, self.symbol)
        changed = any(
            updated[i].mitigated != current[i].mitigated
            for i in range(len(updated))
        )

        if changed:
            with self._lock:
                self.latest_zones = updated
            self._log_mitigated(updated)
            self._known_names = {z.name for z in updated if not z.mitigated}
            if self.draw_on_chart:
                try:
                    draw_mtf_zones(self.symbol, updated, max_draw=self.max_draw)
                except Exception:
                    pass

    # ── Logging (only called when something actually changed) ─────

    def _log_new(self, zones):
        active = [z for z in zones if not z.mitigated]
        new    = [z for z in active if z.name not in self._known_names]

        for z in new:
            icon   = "🟡" if z.kind == "BULL" else "🟣"
            height = round((z.top - z.bottom) / self.pip_size, 1)
            self._log(
                f"{icon}  MTF FVG Confluence | {z.kind} | "
                f"zone {z.bottom:.5f}–{z.top:.5f} ({height}pips) | "
                f"15M:{z.fvg_15m.gap_pips}p "
                f"5M:{z.fvg_5m.gap_pips}p "
                f"1M:{z.fvg_1m.gap_pips}p",
                "NEW"
            )

        bull = sum(1 for z in active if z.kind == "BULL")
        bear = sum(1 for z in active if z.kind == "BEAR")
        self._log(
            f"🟡  MTF FVG: {len(active)} active zones | 🟡{bull} bull  🟣{bear} bear",
            "INFO"
        )

    def _log_mitigated(self, zones):
        for z in zones:
            if z.mitigated and z.name not in self._mitigated_log:
                self._mitigated_log.add(z.name)
                icon = "🟡" if z.kind == "BULL" else "🟣"
                self._log(
                    f"{icon}  MTF FVG mitigated: {z.kind} "
                    f"zone {z.bottom:.5f}–{z.top:.5f} — removed from chart",
                    "WARN"
                )