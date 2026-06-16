"""
resume.py — Bot State Recovery
================================
Saves session state to session_SYMBOL.json on every significant change.
On resume, loads exact state — no inference needed.

State file contains:
  - line price (exact)
  - dist_pips (exact, from when session started)
  - base_lot
  - round number
  - buy_lot, sell_lot
  - buy_pos_ticket, sell_pos_ticket
  - buy_ticket, sell_ticket (pending orders)
"""
import MetaTrader5 as mt5
import logging
import json
import os
import time as _time
from config import MAGIC_NUMBER
from core.order_manager import get_pip_size
from core.position_monitor import SourceState

log = logging.getLogger("resume")


def session_file(symbol: str) -> str:
    return f"session_{symbol}.json"


def save_session(state: SourceState):
    """Save current SourceState to disk. Called after every state change."""
    if state.name.startswith("RESUMED_") or not state.price:
        return
    data = {
        "name":              state.name,
        "price":             state.price,
        "dist_pips":         state.dist_pips,
        "pip_size":          state.pip_size,
        "base_lot":          state.base_lot,
        "round":             state.round,
        "buy_lot":           state.buy_lot,
        "sell_lot":          state.sell_lot,
        "buy_ticket":        state.buy_ticket,
        "sell_ticket":       state.sell_ticket,
        "buy_pos_ticket":    state.buy_pos_ticket,
        "sell_pos_ticket":   state.sell_pos_ticket,
        "buy_confirmed":     state._buy_confirmed,
        "sell_confirmed":    state._sell_confirmed,
        "state":             state.state,
        "saved_at":          _time.time(),
    }
    try:
        with open(session_file(state.symbol), "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning("Could not save session: %s", e)


def clear_session(symbol: str):
    """Delete session file when sequence completes cleanly."""
    path = session_file(symbol)
    try:
        if os.path.exists(path):
            os.remove(path)
            log.info("Session file cleared")
    except Exception:
        pass


def scan_and_resume(symbol: str, dist_pips: float, pip_size: float,
                    base_lot: float, start_balance: float,
                    log_fn=None, stop_fn=None) -> list:
    """
    Rebuild SourceState from saved session file + live MT5 data.
    Returns list of (name, SourceState) tuples.
    """
    _log = log_fn or (lambda msg, level="INFO": log.info(msg))

    # ── Try to load saved session file ────────────────────────────
    sf   = session_file(symbol)
    data = None
    if os.path.exists(sf):
        try:
            with open(sf) as f:
                data = json.load(f)
        except Exception as e:
            _log(f"⚠️  Could not read session file: {e} — falling back to MT5 scan", "WARN")

    # ── Check what's actually live in MT5 ────────────────────────
    positions = mt5.positions_get(symbol=symbol) or []
    orders    = mt5.orders_get(symbol=symbol) or []
    bot_pos   = [p for p in positions if p.magic == MAGIC_NUMBER]
    bot_ord   = [o for o in orders    if o.magic == MAGIC_NUMBER]

    if not bot_pos and not bot_ord:
        _log("ℹ️  Resume: no open positions or orders found in MT5", "INFO")
        clear_session(symbol)
        return []

    _log(
        f"🔄  Resume: found {len(bot_pos)} position(s), "
        f"{len(bot_ord)} pending order(s)", "NEW"
    )

    # ── Reconstruct state ─────────────────────────────────────────
    if data:
        # Use saved session data for exact values
        line      = data["price"]
        d_pips    = data["dist_pips"]    # exact dist from original session
        p_size    = data["pip_size"]
        b_lot     = data["base_lot"]
        rnd       = data["round"]
        buy_lot   = data["buy_lot"]
        sell_lot  = data["sell_lot"]
        _log(f"📂  Loaded session file: line={line:.5f} dist={d_pips}pips R{rnd}", "INFO")
    else:
        # Fallback: infer from MT5 data
        line   = _infer_line(bot_pos, bot_ord, dist_pips, pip_size)
        d_pips = dist_pips
        p_size = pip_size
        b_lot  = base_lot
        rnd    = _infer_round(bot_pos, bot_ord, base_lot)
        buy_lot, sell_lot = _infer_lots(bot_pos, bot_ord)
        _log(f"⚠️  No session file — inferred: line={line:.5f} R{rnd}", "WARN")

    name  = data["name"] if data else f"RESUMED_{int(line * 100000)}"
    state = SourceState(
        name          = name,
        price         = line,
        pip_size      = p_size,
        symbol        = symbol,
        base_lot      = b_lot,
        dist_pips     = d_pips,
        start_balance = start_balance,
        log_fn        = log_fn,
        stop_fn       = stop_fn,
    )
    state.round    = rnd
    state.buy_lot  = buy_lot
    state.sell_lot = sell_lot

    # ── Map live MT5 objects to state ─────────────────────────────
    buy_pos  = [p for p in bot_pos if p.type == 0]
    sell_pos = [p for p in bot_pos if p.type == 1]
    buy_ord  = [o for o in bot_ord if o.type == mt5.ORDER_TYPE_BUY_STOP]
    sell_ord = [o for o in bot_ord if o.type == mt5.ORDER_TYPE_SELL_STOP]

    if buy_pos:
        pos = sorted(buy_pos, key=lambda p: p.time, reverse=True)[0]
        state.buy_pos_ticket = pos.ticket
        state.buy_sl         = pos.sl
        state.buy_lot        = pos.volume   # use actual volume from MT5
        state._buy_confirmed = True

    if sell_pos:
        pos = sorted(sell_pos, key=lambda p: p.time, reverse=True)[0]
        state.sell_pos_ticket = pos.ticket
        state.sell_sl         = pos.sl
        state.sell_lot        = pos.volume
        state._sell_confirmed = True

    if buy_ord:
        o = sorted(buy_ord, key=lambda o: o.time_setup, reverse=True)[0]
        state.buy_ticket = o.ticket
        if not buy_pos:  # no position, so pending lot is the BUY lot
            state.buy_lot = o.volume_current

    if sell_ord:
        o = sorted(sell_ord, key=lambda o: o.time_setup, reverse=True)[0]
        state.sell_ticket = o.ticket
        if not sell_pos:
            state.sell_lot = o.volume_current

    # ── Set state ─────────────────────────────────────────────────
    if bot_pos:
        state.state         = SourceState.ACTIVE
        state._activated_at = _time.time() - 30
    elif bot_ord:
        state.state = SourceState.PENDING
    else:
        state.state = SourceState.IDLE

    # ── Log summary ───────────────────────────────────────────────
    parts = []
    if buy_pos:  parts.append(f"BUY pos#{buy_pos[0].ticket} lot={buy_pos[0].volume:.2f}")
    if sell_pos: parts.append(f"SELL pos#{sell_pos[0].ticket} lot={sell_pos[0].volume:.2f}")
    if buy_ord:  parts.append(f"BUY-STOP#{buy_ord[0].ticket} lot={buy_ord[0].volume_current:.2f}")
    if sell_ord: parts.append(f"SELL-STOP#{sell_ord[0].ticket} lot={sell_ord[0].volume_current:.2f}")

    _log(
        f"✅  Resumed: line={line:.5f} dist={d_pips}pips R{state.round} | "
        + " | ".join(parts), "NEW"
    )
    return [(name, state)]


# ── Inference helpers (fallback when no session file) ─────────────

def _infer_line(bot_pos, bot_ord, dist_pips, pip_size) -> float:
    dist = dist_pips * pip_size
    for p in bot_pos:
        is_buy = p.type == 0
        if p.sl and p.sl > 0:
            return round(p.sl + dist if is_buy else p.sl - dist, 5)
    for o in bot_ord:
        is_buy = o.type == mt5.ORDER_TYPE_BUY_STOP
        return round(o.price_open - dist if is_buy else o.price_open + dist, 5)
    return 0.0


def _infer_round(bot_pos, bot_ord, base_lot) -> int:
    all_lots = ([p.volume for p in bot_pos] +
                [o.volume_current for o in bot_ord])
    if not all_lots or base_lot <= 0:
        return 1
    import math
    try:
        return max(1, round(math.log2(max(all_lots) / base_lot) + 1))
    except Exception:
        return 1


def _infer_lots(bot_pos, bot_ord):
    buy_lot  = next((p.volume for p in bot_pos if p.type == 0), None) or \
               next((o.volume_current for o in bot_ord if o.type == mt5.ORDER_TYPE_BUY_STOP), 0.01)
    sell_lot = next((p.volume for p in bot_pos if p.type == 1), None) or \
               next((o.volume_current for o in bot_ord if o.type == mt5.ORDER_TYPE_SELL_STOP), 0.01)
    return buy_lot, sell_lot