"""
watcher.py — TraderBot v2
Reads trader_objects_SYMBOL.txt (written by ObjectExporter EA).
Detects candle touches on trader-drawn lines, delegates to SourceState.
"""
import MetaTrader5 as mt5
import threading
import time as _time
import os as _os
import sys
import logging
from dataclasses import dataclass
from typing import Optional
from datetime import datetime

sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import config as cfg
from core.order_manager import get_pip_size
from core.position_monitor import SourceState

log = logging.getLogger("watcher_v2")

# How many consecutive scans a line must be absent before we treat it as deleted.
# At SCAN_INTERVAL_SEC=2, a grace of 3 = 6 seconds.
# Prevents spurious resets when the EA rewrites the file mid-scan.
REMOVAL_GRACE = 3


# ── File-bridge helpers ───────────────────────────────────────────

def _get_file_paths(symbol=None):
    appdata   = _os.environ.get("APPDATA", "")
    paths     = []
    fname_sym = f"trader_objects_{symbol}.txt" if symbol else None
    fname_gen = "trader_objects.txt"

    common = _os.path.join(appdata, "MetaQuotes", "Terminal", "Common", "Files")
    if fname_sym: paths.append(_os.path.join(common, fname_sym))
    paths.append(_os.path.join(common, fname_gen))

    terminal_root = _os.path.join(appdata, "MetaQuotes", "Terminal")
    try:
        if _os.path.isdir(terminal_root):
            for tid in _os.listdir(terminal_root):
                t_path = _os.path.join(terminal_root, tid, "MQL5", "Files")
                if _os.path.isdir(t_path):
                    if fname_sym: paths.append(_os.path.join(t_path, fname_sym))
                    paths.append(_os.path.join(t_path, fname_gen))
    except Exception:
        pass

    roaming = _os.path.join(_os.environ.get("USERPROFILE", ""),
                            "AppData", "Roaming", "MetaQuotes", "Terminal")
    try:
        if _os.path.isdir(roaming):
            for tid in _os.listdir(roaming):
                t_path = _os.path.join(roaming, tid, "MQL5", "Files")
                if _os.path.isdir(t_path):
                    if fname_sym: paths.append(_os.path.join(t_path, fname_sym))
                    paths.append(_os.path.join(t_path, fname_gen))
    except Exception:
        pass

    return paths


def _find_objects_file(symbol=None):
    syms = [symbol]
    if symbol:
        syms.append(symbol[:-2] if symbol.endswith("_i") else symbol + "_i")
        syms.append(None)
    best_path, best_age = None, float("inf")
    for sym in syms:
        for p in _get_file_paths(sym):
            if _os.path.exists(p):
                try:
                    age = _time.time() - _os.path.getmtime(p)
                    if age < best_age:
                        best_age  = age
                        best_path = p
                except Exception:
                    if best_path is None:
                        best_path = p
    return best_path


@dataclass
class ChartObject:
    name:     str
    obj_type: str
    type_id:  int
    price1:   float
    price2:   float

    @property
    def is_hline(self):
        return self.obj_type == "HLINE"

    @property
    def is_rectangle(self):
        return self.obj_type in ("RECTANGLE",) or self.type_id in (16, 20)

    @property
    def rect_valid(self):
        return abs(self.price1 - self.price2) > 1e-8

    @property
    def rect_top(self):
        return max(self.price1, self.price2)

    @property
    def rect_bottom(self):
        return min(self.price1, self.price2)


def _parse_file(path: str):
    """Returns (objects, candle_dict, file_symbol)."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except PermissionError:
        # EA is writing the file at this exact moment — transient, skip silently.
        return [], {}, None
    except Exception as e:
        log.warning("Could not read %s: %s", path, e)
        return [], {}, None

    candle, file_symbol, objects = {}, None, []

    for line in lines:
        line = line.strip()
        if line.startswith("SYMBOL:"):
            file_symbol = line.split(":", 1)[1].strip()
        elif line.startswith(("CANDLE_", "PREV_", "BID:")):
            k, _, v = line.partition(":")
            try:
                candle[k] = float(v) if "." in v else int(v)
            except ValueError:
                pass
        elif line.startswith("OBJ"):
            parts = line.split("|")
            data  = {}
            for p in parts[1:]:
                if ":" in p:
                    k, v = p.split(":", 1)
                    data[k] = v
            try:
                name = data.get("NAME", "?")
                if any(name.startswith(pfx) for pfx in cfg.AUTO_OBJECT_PREFIXES):
                    continue
                objects.append(ChartObject(
                    name     = name,
                    obj_type = data.get("TYPE", "OTHER"),
                    type_id  = int(data.get("TYPEID", 0)),
                    price1   = float(data.get("PRICE1", 0)),
                    price2   = float(data.get("PRICE2", 0)),
                ))
            except Exception:
                pass

    return objects, candle, file_symbol


# ── Signals ───────────────────────────────────────────────────────

class WatcherSignals:
    def __init__(self):
        self._log_cbs    = []
        self._status_cbs = []
        self._state_cbs  = []
        self._candle_cbs = []
        self._stop_cbs   = []   # called when balance TP fires

    def on_log(self, fn):    self._log_cbs.append(fn)
    def on_status(self, fn): self._status_cbs.append(fn)
    def on_state(self, fn):  self._state_cbs.append(fn)
    def on_candle(self, fn): self._candle_cbs.append(fn)
    def on_stop(self, fn):   self._stop_cbs.append(fn)   # GUI registers here

    def emit_log(self, msg, level="INFO"):
        for fn in self._log_cbs: fn(msg, level)

    def emit_status(self, msg):
        for fn in self._status_cbs: fn(msg)

    def emit_state(self, states):
        for fn in self._state_cbs: fn(states)

    def emit_candle(self, candle):
        for fn in self._candle_cbs: fn(candle)

    def emit_stop(self):
        for fn in self._stop_cbs: fn()


# ── Watcher Thread ────────────────────────────────────────────────

class WatcherThread(threading.Thread):

    def __init__(self, symbol: str, lot_size: float,
                 follow_enabled: bool = True, resume_enabled: bool = False):
        super().__init__(daemon=True)
        self.symbol          = symbol
        self.lot_size        = lot_size
        self.follow_enabled  = follow_enabled
        self._resume_enabled = resume_enabled
        self.sig             = WatcherSignals()
        self._stop_event     = threading.Event()
        self._sources: dict[str, SourceState] = {}
        self._seen:    set = set()
        self._skipped: set = set()

        # Grace period: name → consecutive absent-scan count.
        # Line must be missing REMOVAL_GRACE scans before we reset it.
        self._missing_counts: dict[str, int] = {}

    def stop(self):
        self._stop_event.set()

    def _on_balance_tp(self):
        """
        Called by SourceState when balance TP is hit.
        Sets the stop event so the main loop exits cleanly.
        mt5.shutdown() is handled at the end of run() — not here —
        so FVG/OB watchers are stopped by the GUI before MT5 closes.
        """
        self._stop_event.set()
        # Tell the GUI to stop all watchers cleanly (FVG, OB, Confluence)
        self.sig.emit_stop()

    def _save_start_balance(self, path: str, json_mod):
        try:
            with open(path, "w") as f:
                json_mod.dump({"start_balance": self._start_balance,
                               "symbol": self.symbol}, f)
        except Exception as e:
            log.warning("Could not save start balance: %s", e)

    def log(self, msg: str, level: str = "INFO"):
        ts = datetime.now().strftime("%H:%M:%S")
        self.sig.emit_log(f"{ts}  {msg}", level)
        getattr(log, "warning" if level == "WARN" else
                     level.lower() if level.lower() in ("info", "error", "debug") else "info")(msg)

    def run(self):
        self.log("=" * 60)
        self.log("  TraderBot v2 — 2-Order Martingale Bot")
        self.log("=" * 60)

        if not self._connect():
            return

        pip = get_pip_size(self.symbol)
        self.log(f"Symbol: {self.symbol}  pip={pip:.5f}  "
                 f"order_dist={cfg.ORDER_DISTANCE_PIPS}pips"
                 f"={cfg.ORDER_DISTANCE_PIPS * pip:.5f}")

        # ── Start balance ─────────────────────────────────────────
        acct            = mt5.account_info()
        current_balance = acct.balance if acct else 0.0

        import json as _json
        _bal_file = f"start_balance_{self.symbol}.json"

        if self._resume_enabled and _os.path.exists(_bal_file):
            try:
                with open(_bal_file) as f:
                    saved = _json.load(f)
                saved_bal = saved.get("start_balance", 0.0)
                if saved_bal > 0:
                    self._start_balance = saved_bal
                    self.log(
                        f"💰  Resumed start balance: {self._start_balance:.2f} | "
                        f"Current: {current_balance:.2f} | "
                        f"Target: {self._start_balance * cfg.BALANCE_TP_RATIO:.2f} "
                        f"(+{(cfg.BALANCE_TP_RATIO - 1) * 100:.0f}%)"
                    )
                else:
                    raise ValueError("invalid saved balance")
            except Exception:
                self._start_balance = current_balance
                self._save_start_balance(_bal_file, _json)
                self.log(
                    f"💰  Start balance: {self._start_balance:.2f} | "
                    f"Target: {self._start_balance * cfg.BALANCE_TP_RATIO:.2f} "
                    f"(+{(cfg.BALANCE_TP_RATIO - 1) * 100:.0f}%)"
                )
        else:
            self._start_balance = current_balance
            self._save_start_balance(_bal_file, _json)
            self.log(
                f"💰  Start balance: {self._start_balance:.2f} | "
                f"Target: {self._start_balance * cfg.BALANCE_TP_RATIO:.2f} "
                f"(+{(cfg.BALANCE_TP_RATIO - 1) * 100:.0f}%)"
            )

        self.log("⏳  Waiting for ObjectExporter EA file…")
        self.sig.emit_status("⏳  Waiting for EA…")

        # ── Resume previous session ───────────────────────────────
        if self._resume_enabled:
            from core.resume import scan_and_resume
            recovered = scan_and_resume(
                symbol        = self.symbol,
                dist_pips     = cfg.ORDER_DISTANCE_PIPS,
                pip_size      = pip,
                base_lot      = self.lot_size,
                start_balance = self._start_balance,
                log_fn        = self.log,
                stop_fn       = self._on_balance_tp,
            )
            for name, state in recovered:
                self._sources[name] = state
                self._seen.add(name)

        warned_missing = stale_warned = False
        last_ea_warn   = None

        while not self._stop_event.is_set():
            try:
                dist_pips = cfg.ORDER_DISTANCE_PIPS
                path      = _find_objects_file(self.symbol)

                if path is None:
                    if not warned_missing:
                        self.log(
                            "⚠️  trader_objects.txt not found — "
                            "is ObjectExporter EA running?", "WARN"
                        )
                        warned_missing = True
                    self.sig.emit_status("⏳  Waiting for EA…")
                    self._stop_event.wait(cfg.SCAN_INTERVAL_SEC)
                    continue

                warned_missing = False

                try:
                    file_age = _time.time() - _os.path.getmtime(path)
                except Exception:
                    file_age = 0

                if file_age > 15:
                    if not stale_warned:
                        self.log(f"⚠️  EA file {file_age:.0f}s old — EA not running?", "WARN")
                        stale_warned = True
                    self.sig.emit_status(f"⚠️  EA stopped ({file_age:.0f}s)")
                    self._stop_event.wait(cfg.SCAN_INTERVAL_SEC)
                    continue
                else:
                    if stale_warned:
                        self.log("✅  EA writing again — resuming")
                    stale_warned = False

                objects, candle, ea_sym = _parse_file(path)

                # _parse_file returns empty on PermissionError — skip silently
                if objects is None and candle is None:
                    self._stop_event.wait(cfg.SCAN_INTERVAL_SEC)
                    continue

                if ea_sym and ea_sym != self.symbol:
                    if ea_sym != last_ea_warn:
                        last_ea_warn = ea_sym
                        self.log(
                            f"⚠️  EA on '{ea_sym}' — bot watching '{self.symbol}'", "WARN"
                        )
                    self.sig.emit_status(f"⚠️  EA on wrong chart ({ea_sym})")
                    self._stop_event.wait(cfg.SCAN_INTERVAL_SEC)
                    continue

                last_ea_warn = None
                self.sig.emit_candle(candle)

                cur_t  = candle.get("CANDLE_T", 0)
                cur_h  = candle.get("CANDLE_H", 0.0)
                cur_l  = candle.get("CANDLE_L", 0.0)
                cur_c  = candle.get("CANDLE_C", 0.0)
                prev_h = candle.get("PREV_H", 0.0)
                prev_l = candle.get("PREV_L", 0.0)
                prev_c = candle.get("PREV_C", 0.0)
                prev_t = candle.get("PREV_T", 0)
                bid    = candle.get("BID", 0.0)

                tick          = mt5.symbol_info_tick(self.symbol)
                current_price = (tick.bid + tick.ask) / 2 if tick else bid or cur_c

                cur_names = {o.name for o in objects}

                # ── New lines ─────────────────────────────────────
                for o in objects:
                    n = o.name
                    if n in self._seen or n in self._skipped:
                        continue

                    if current_price > 0 and o.price1 > 0:
                        ratio = o.price1 / current_price
                        if ratio < 0.5 or ratio > 2.0:
                            self._skipped.add(n)
                            continue

                    src = None
                    if o.is_hline:
                        src = o.price1
                    elif o.is_rectangle and o.rect_valid:
                        src = round((o.rect_top + o.rect_bottom) / 2, 5)

                    if src is not None:
                        state = SourceState(
                            name          = n,
                            price         = src,
                            pip_size      = pip,
                            symbol        = self.symbol,
                            base_lot      = self.lot_size,
                            dist_pips     = dist_pips,
                            start_balance = self._start_balance,
                            log_fn        = self.log,
                            stop_fn       = self._on_balance_tp,
                        )
                        state.registered_at = cur_t
                        state.last_prev_t   = prev_t
                        self._sources[n]    = state
                        self._seen.add(n)
                        self.log(
                            f"🆕  [{n[:25]}] @ {src:.5f} registered | "
                            f"dist={dist_pips}pips | waiting for candle touch"
                        )

                # ── Removed lines (with grace period) ─────────────
                # A line must be absent for REMOVAL_GRACE consecutive scans
                # before we reset it. Prevents false resets when the EA
                # briefly clears the file while rewriting it.
                for n in list(self._sources.keys()):
                    if n not in cur_names:
                        if n.startswith("RESUMED_"):
                            continue
                        self._missing_counts[n] = self._missing_counts.get(n, 0) + 1
                        if self._missing_counts[n] >= REMOVAL_GRACE:
                            self.log(f"🗑️  [{n[:25]}] removed — cancelling orders")
                            self._sources[n].reset()
                            del self._sources[n]
                            self._seen.discard(n)
                            self._missing_counts.pop(n, None)
                        # else: absent within grace — silently wait
                    else:
                        # Present this scan — clear any pending removal counter
                        self._missing_counts.pop(n, None)

                # ── Moved lines ───────────────────────────────────
                if self.follow_enabled:
                    for o in objects:
                        n = o.name
                        if n not in self._sources:
                            continue
                        if n.startswith("RESUMED_"):
                            continue
                        state     = self._sources[n]
                        new_price = None
                        if o.is_hline:
                            new_price = o.price1
                        elif o.is_rectangle and o.rect_valid:
                            new_price = round((o.rect_top + o.rect_bottom) / 2, 5)
                        if new_price and abs(new_price - state.price) > 1e-6:
                            self.log(
                                f"↕️  [{n[:25]}] moved "
                                f"{state.price:.5f}→{new_price:.5f} — resetting"
                            )
                            state.reset()
                            state.price         = new_price
                            state.dist_pips     = dist_pips
                            state.registered_at = cur_t
                            state.last_prev_t   = prev_t
                            self._missing_counts.pop(n, None)

                # ── Touch detection ───────────────────────────────
                for n, state in self._sources.items():
                    if state.state != SourceState.IDLE:
                        continue

                    src     = state.price
                    reg     = state.registered_at
                    touched = False
                    desc    = ""

                    if cur_h > 0 and cur_t != reg:
                        if cur_l <= src <= cur_h:
                            touched = True
                            desc    = f"current candle C={cur_c:.5f}"

                    if not touched and prev_h > 0:
                        if prev_t > state.last_prev_t and prev_t > reg:
                            state.last_prev_t = prev_t
                            if prev_l <= src <= prev_h:
                                touched = True
                                desc    = f"prev candle C={prev_c:.5f}"

                    if touched:
                        self.log(
                            f"🎯  [{n[:20]}] touched @ {src:.5f} ({desc}) | "
                            f"dist={state.dist_pips}pips | placing orders", "NEW"
                        )
                        state.place_initial_pair()

                # ── Monitor active/pending states ─────────────────
                for n, state in list(self._sources.items()):
                    if state.state in (SourceState.PENDING, SourceState.ACTIVE):
                        state.check(candle)
                    if n.startswith("RESUMED_") and state.state == SourceState.EXHAUSTED:
                        self.log(
                            f"✅  [{n[:25]}] resumed sequence complete — removing", "INFO"
                        )
                        del self._sources[n]
                        self._seen.discard(n)

                # ── Emit state to GUI ─────────────────────────────
                self.sig.emit_state([s.summary for s in self._sources.values()])

                idle = [n for n, s in self._sources.items() if s.state == SourceState.IDLE]
                if idle:
                    self.sig.emit_status(f"🟢  Watching {len(idle)} line(s) | bid={bid:.5f}")
                elif self._sources:
                    self.sig.emit_status(f"🟢  {len(self._sources)} sequence(s) active")
                else:
                    self.sig.emit_status("🟢  Running — draw a line to start")

            except Exception as e:
                import traceback as _tb
                self.log(f"💥 Watcher error: {type(e).__name__}: {e}", "ERROR")
                for line in _tb.format_exc().strip().splitlines():
                    self.log(f"   {line}", "ERROR")

            self._stop_event.wait(cfg.SCAN_INTERVAL_SEC)

        # ── Clean shutdown ────────────────────────────────────────
        # mt5.shutdown() is called here — after the loop exits — so all
        # watchers have already been stopped by the GUI's _stop() handler
        # before MT5 loses connection.
        mt5.shutdown()
        self.sig.emit_status("⚫  Stopped")
        self.log("Bot stopped.")

    def _connect(self) -> bool:
        if not mt5.initialize(login=cfg.MT5_LOGIN,
                              password=cfg.MT5_PASSWORD,
                              server=cfg.MT5_SERVER):
            self.log(f"❌ MT5 connection failed: {mt5.last_error()}", "ERROR")
            self.sig.emit_status("❌  MT5 connection failed")
            return False
        info = mt5.account_info()
        self.log(
            f"✅ Connected: {info.name} | "
            f"Balance: {info.balance:.2f} {info.currency}"
        )
        self.sig.emit_status(f"🟢  Connected — {info.name}")
        return True