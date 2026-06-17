"""
mtf_fvg_watcher.py — Multi-Timeframe FVG Confluence Watcher + Entry Manager
============================================================================
Triggers full detection on every completed 1M candle close. Scans the
trader-selected subset of {15M, 5M, 1M} (minimum 2) for overlapping,
temporally close FVG zones — see mtf_fvg.py for the generalized cascade
and recency-matching logic.

Entry management:
  For each active confluence zone, the entry-timeframe leg
  (zone.entry_fvg — whichever timeframe the trader designated as the
  tradeable one, not necessarily the smallest) is used to create/
  maintain an FVGEntryState. When live tick price returns into that
  FVG's price range, the entry state places its edge-anchored
  BUY-STOP/SELL-STOP pair (see core/fvg_entry_state.py).

  One FVGEntryState per unique entry-timeframe FVG (multiple
  confluence zones can share the same entry FVG).
"""

import threading
import logging

log = logging.getLogger("mtf_fvg_watcher")


class MTFFVGWatcher(threading.Thread):

    def __init__(
        self,
        symbol:          str,
        pip_size:        float,
        selected_tfs:    list  = None,    # e.g. ["15M","5M","1M"] or ["5M","1M"]
        entry_tf:        str   = None,    # which selected tf is tradeable; default = smallest
        min_gap_pips:    float = 1.0,
        lookback_15m:    int   = 50,
        lookback_5m:     int   = 100,
        lookback_1m:     int   = 200,
        max_zones:       int   = 20,
        max_draw:        int   = 20,
        draw_on_chart:   bool  = True,
        poll_interval:   float = 1.0,
        enable_entries:  bool  = False,
        entry_base_lot:  float = 0.01,
        entry_dist_pips: float = 1.5,
        start_balance:   float = 0.0,
        log_fn=None,
        stop_fn=None,
    ):
        threading.Thread.__init__(self)
        self.daemon          = True
        self.symbol          = symbol
        self.pip_size        = pip_size
        self.selected_tfs    = list(selected_tfs) if selected_tfs else ["15M", "5M", "1M"]
        self.entry_tf        = entry_tf
        self.min_gap_pips    = min_gap_pips
        self.lookback_15m    = lookback_15m
        self.lookback_5m     = lookback_5m
        self.lookback_1m     = lookback_1m
        self.max_zones       = max_zones
        self.max_draw        = max_draw
        self.draw_on_chart   = draw_on_chart
        self.poll_interval   = poll_interval
        self.enable_entries  = enable_entries
        self.entry_base_lot  = entry_base_lot
        self.entry_dist_pips = entry_dist_pips
        self.start_balance   = start_balance
        self._log            = log_fn or (lambda msg, level="INFO": log.info(msg))
        self._stop_fn        = stop_fn
        self._stop_event     = threading.Event()
        self._lock           = threading.Lock()

        self.latest_zones      = []
        self._last_candle_time = 0
        self._known_names      = set()
        self._mitigated_log    = set()

        # Keyed by the entry-timeframe FVG's anchor time, so multiple
        # confluence zones sharing one entry FVG map to a single
        # FVGEntryState instead of duplicating entries.
        self._entry_states = {}

    # ── Public API ────────────────────────────────────────────────

    def stop(self):
        self._stop_event.set()

    def get_zones(self):
        with self._lock:
            return [z for z in self.latest_zones if not z.mitigated]

    def get_all_zones(self):
        with self._lock:
            return list(self.latest_zones)

    def get_entry_states(self):
        with self._lock:
            return list(self._entry_states.values())

    def update_settings(self, selected_tfs=None, entry_tf=None,
                        min_gap_pips=None, lookback_15m=None,
                        lookback_5m=None, lookback_1m=None,
                        max_zones=None, max_draw=None,
                        enable_entries=None, entry_base_lot=None,
                        entry_dist_pips=None):
        if selected_tfs    is not None: self.selected_tfs    = list(selected_tfs)
        if entry_tf         is not None: self.entry_tf        = entry_tf
        if min_gap_pips    is not None: self.min_gap_pips    = min_gap_pips
        if lookback_15m    is not None: self.lookback_15m    = lookback_15m
        if lookback_5m     is not None: self.lookback_5m     = lookback_5m
        if lookback_1m     is not None: self.lookback_1m     = lookback_1m
        if max_zones       is not None: self.max_zones       = max_zones
        if max_draw        is not None: self.max_draw        = max_draw
        if enable_entries  is not None: self.enable_entries  = enable_entries
        if entry_base_lot  is not None: self.entry_base_lot  = entry_base_lot
        if entry_dist_pips is not None: self.entry_dist_pips = entry_dist_pips
        self._last_candle_time = 0
        self._known_names.clear()

    # ── Main loop ─────────────────────────────────────────────────

    def run(self):
        try:
            import MetaTrader5 as mt5
            from core.mtf_fvg import (
                find_mtf_zones, check_mitigation,
                draw_mtf_zones_and_entries, clear_mtf_zones,
            )
            from core.fvg_entry_state import FVGEntryState
        except Exception as e:
            self._log(f"💥 MTF FVG import error: {e}", "ERROR")
            return

        tf_label = "+".join(self.selected_tfs)
        self._log(
            f"🟡  MTF FVG Watcher started | {self.symbol} | tfs={tf_label} | "
            f"entry_tf={self.entry_tf or '(smallest)'} | "
            f"min={self.min_gap_pips}pips | "
            f"entries={'ON' if self.enable_entries else 'off'}",
            "INFO"
        )

        self._stop_event.wait(2.0)

        while not self._stop_event.is_set():
            try:
                bars = mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_M1, 0, 3)

                if bars is None or len(bars) < 2:
                    self._stop_event.wait(self.poll_interval)
                    continue

                latest_closed_time = int(bars[-2]["time"])
                new_candle = (latest_closed_time != self._last_candle_time)

                if new_candle:
                    self._last_candle_time = latest_closed_time
                    self._run_detection(find_mtf_zones, check_mitigation,
                                        draw_mtf_zones_and_entries, FVGEntryState)
                else:
                    self._check_mitigation_only(check_mitigation, draw_mtf_zones_and_entries)

                if self.enable_entries:
                    self._poll_entry_states(mt5)

            except Exception as e:
                import traceback as _tb
                self._log(f"💥 MTF FVG error: {e}", "ERROR")
                for line in _tb.format_exc().strip().splitlines():
                    self._log(f"   {line}", "ERROR")

            self._stop_event.wait(self.poll_interval)

        self._cancel_all_entries()
        try:
            from core.mtf_fvg import clear_mtf_zones
            clear_mtf_zones(self.symbol)
        except Exception:
            pass

        self._log("🟡  MTF FVG Watcher stopped", "INFO")

    # ── Detection cycle ───────────────────────────────────────────

    def _run_detection(self, find_mtf_zones, check_mitigation,
                       draw_mtf_zones_and_entries, FVGEntryState):
        fresh_zones = find_mtf_zones(
            symbol       = self.symbol,
            pip_size     = self.pip_size,
            selected_tfs = self.selected_tfs,
            entry_tf     = self.entry_tf,
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
                draw_mtf_zones_and_entries(self.symbol, merged, max_draw=self.max_draw)
            except Exception as e:
                self._log(f"⚠️  MTF FVG draw: {e}", "WARN")

        if self.enable_entries:
            self._sync_entry_states(merged, FVGEntryState)

    def _check_mitigation_only(self, check_mitigation, draw_mtf_zones_and_entries):
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
                    draw_mtf_zones_and_entries(self.symbol, updated, max_draw=self.max_draw)
                except Exception:
                    pass
            if self.enable_entries:
                from core.fvg_entry_state import FVGEntryState
                self._sync_entry_states(updated, FVGEntryState)

    # ── Entry state lifecycle ────────────────────────────────────

    def _sync_entry_states(self, zones, FVGEntryState):
        """
        Create an FVGEntryState for each unique active entry-timeframe
        FVG that doesn't have one yet. Drop entry states whose backing
        zone is no longer active, but only if still IDLE.
        """
        active_zones  = [z for z in zones if not z.mitigated]
        active_anchors = set()

        for z in active_zones:
            ef  = z.entry_fvg
            key = ef.time1
            active_anchors.add(key)

            if key in self._entry_states:
                continue

            entry = FVGEntryState(
                name          = f"MTFFVG5M_{key}",
                fvg_top       = ef.top,
                fvg_bottom    = ef.bottom,
                pip_size      = self.pip_size,
                symbol        = self.symbol,
                base_lot      = self.entry_base_lot,
                dist_pips     = self.entry_dist_pips,
                start_balance = self.start_balance,
                log_fn        = self._log,
                stop_fn       = self._stop_fn,
            )
            with self._lock:
                self._entry_states[key] = entry
            self._log(
                f"📌  [{ef.timeframe} entry] registered | "
                f"{z.kind} FVG [{ef.bottom:.5f}-{ef.top:.5f}] | "
                f"waiting for price to return", "INFO"
            )

        with self._lock:
            stale = [k for k, e in self._entry_states.items()
                    if k not in active_anchors and e.state == e.IDLE]
            for k in stale:
                del self._entry_states[k]

    def _poll_entry_states(self, mt5):
        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            return

        with self._lock:
            states = list(self._entry_states.values())

        for entry in states:
            if entry.state == entry.IDLE:
                entry.check_touch(tick.bid, tick.ask)
            elif entry.state in (entry.PENDING, entry.ACTIVE):
                entry.check({"BID": tick.bid})

    def _cancel_all_entries(self):
        with self._lock:
            states = list(self._entry_states.values())
        for entry in states:
            try:
                entry.reset()
            except Exception:
                pass
        with self._lock:
            self._entry_states.clear()

    # ── Logging ───────────────────────────────────────────────────

    def _log_new(self, zones):
        active = [z for z in zones if not z.mitigated]
        new    = [z for z in active if z.name not in self._known_names]

        for z in new:
            icon   = "🟡" if z.kind == "BULL" else "🟣"
            height = round((z.top - z.bottom) / self.pip_size, 1)
            self._log(
                f"{icon}  MTF FVG Confluence | {z.kind} | "
                f"zone {z.bottom:.5f}–{z.top:.5f} ({height}pips) | "
                f"{z.legs_summary()}",
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