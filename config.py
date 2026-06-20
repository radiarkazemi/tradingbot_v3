"""
╔══════════════════════════════════════════════════════════════════╗
║         TraderBot v2 — Configuration                            ║
║         2-Order Martingale Bot (Buy-Stop + Sell-Stop)           ║
╚══════════════════════════════════════════════════════════════════╝
"""

# ── MT5 CREDENTIALS ──────────────────────────────────────────────
MT5_LOGIN    = 91246510
MT5_PASSWORD = "@Radiar9841@"
MT5_SERVER   = "LiteFinance-MT5-Demo"

# ── SYMBOL TO WATCH ──────────────────────────────────────────────
WATCH_SYMBOL = "EURUSD"

# ── SCAN SETTINGS ────────────────────────────────────────────────
SCAN_INTERVAL_SEC = 2

# ── ORDER SETTINGS ───────────────────────────────────────────────
ORDER_DISTANCE_PIPS = 1.5

LOT_SIZE        = 0.01

# Broker's minimum lot increment (most brokers/symbols use 0.01; check
# yours via mt5.symbol_info(symbol).volume_step if unsure).
LOT_STEP        = 0.01

# Lot growth multiplier applied each recovery round (new_lot = prior_lot
# × LOT_MULTIPLIER). 2.0 = current behavior (full doubling, exact
# martingale recovery math). Lowering this (e.g. 1.4-1.7) smooths the
# growth curve so the account survives more recovery rounds before
# margin protection forces a reduction — at the cost of needing a
# larger TP distance to fully recover cumulative losses each round.
# The TP formula already adapts automatically via cumulative_loss, so
# this can be tuned without touching position_monitor.py.
LOT_MULTIPLIER  = 1.5

TP_RR_RATIO     = 0.0
MAGIC_NUMBER    = 998877

# ── HARD STOP-LOSS (kill switch) ─────────────────────────────────
# Independent of any per-position SL, risk-free, or margin protection.
# If account balance falls to this fraction of the session-start
# balance, ALL positions/pending orders are closed immediately and the
# bot stops — regardless of what round any source is in. This is the
# last-resort circuit breaker for when the recovery cycle itself is
# failing (margin squeeze, slippage, a code-path bug, etc), not a
# replacement for per-position SLs.
HARD_STOP_LOSS_RATIO = 0.80   # e.g. 0.80 = stop at a 20% balance drawdown

# ── BALANCE TAKE-PROFIT ───────────────────────────────────────────
# Session ends (close everything, stop bot) once balance ≥ start ×
# this ratio. gui.py overwrites this live from the "Balance TP %"
# spinbox when you click Start — this default only matters for
# headless/no-GUI runs.
BALANCE_TP_RATIO = 1.10

# ── RECOVERY / RISK TUNING ────────────────────────────────────────
# Everything below was previously hardcoded inline in
# position_monitor.py. Change it here only — nowhere else needs to
# be touched.

# Risk-free locks in profit once floating profit reaches this many
# multiples of the position's own R (entry-to-SL distance).
RISK_FREE_TRIGGER_R = 1.0

# TP sizing reward:risk — tries TP_RR_PRIMARY first (e.g. 3 = 1:3),
# falls back to TP_RR_FALLBACK if that would push the TP further than
# TP_PIPS_CEILING pips away. Floor is always TP_RR_PRIMARY × dist_pips
# so round 1 always has a sane minimum target.
TP_RR_PRIMARY   = 3
TP_RR_FALLBACK  = 2
TP_PIPS_CEILING = 200.0

# Once a recovery round's lot reaches this size, require real OB+FVG
# bounce confluence before doubling again instead of blindly chasing.
DEEP_ROUND_LOT_THRESHOLD = 0.64

# Below this free-margin/equity ratio, also require bounce confluence
# even if lot hasn't reached DEEP_ROUND_LOT_THRESHOLD yet.
TIGHT_MARGIN_RATIO = 0.30

# ── PARTIAL EXIT ──────────────────────────────────────────────────
# Once an OPEN position's lot reaches this size, close off a slice of
# it (PARTIAL_EXIT_FRACTION) to reduce exposure mid-cycle instead of
# waiting for that position's own SL/TP or risk-free to act. Set
# below DEEP_ROUND_LOT_THRESHOLD so this fires as an earlier, gentler
# step before the harder confluence gate kicks in. Fires once per
# position (tracked per side). Set the threshold above any realistic
# lot size to disable this feature entirely.
PARTIAL_EXIT_LOT_THRESHOLD = 0.32
PARTIAL_EXIT_FRACTION       = 0.30   # close 30% of the position

# Cushion kept free (as a fraction of equity) before a new order is
# blocked as unaffordable — see _can_afford().
MARGIN_SAFETY_RATIO = 0.05

# Round-trip commission your broker charges per 1.0 lot (in account
# currency), if any. Used to pad the risk-free lock distance so the
# REALIZED profit after costs still covers what it's supposed to —
# set to 0.0 if your broker doesn't charge commission (spread-only).
COMMISSION_PER_LOT = 0.0

# Seconds after activation before _check_legs starts acting on closes
# (gives MT5 time to settle position/SL fields after a fill).
ACTIVATION_GRACE_SEC = 5

# ── ORDER SPACING / SLIPPAGE BUFFER ───────────────────────────────
# Floor on the BUY/SELL leg distance (see SourceState.__init__) — the
# zero-spread mirror-SL design needs real room between the two legs,
# or slippage on a fill can squeeze that gap enough to break it.
MIN_LEG_SPACING_PIPS = 2.0

# Extra buffer added on top of the broker's own min-stop-distance
# (order_manager._min_stop_dist) so a stop/SL/TP never sits so close
# to the broker's literal minimum that ordinary slippage pushes a
# placement into rejection territory.
SLIPPAGE_BUFFER_PIPS = 1.0

# Floor on how much of the intended entry distance (dist_pips) spread
# compensation is allowed to eat. Without this, a wide/volatile
# spread — or ORDER_DISTANCE_PIPS set too close to typical spread —
# can collapse the BUY-STOP/SELL-STOP entry to nearly the touch price
# itself, causing it to fill as a MARKET order instantly instead of
# waiting as a real pending stop. This guarantees at least this many
# pips of real spacing always remain, regardless of spread width.
MIN_ENTRY_SPACING_PIPS = 1.0

# ── PARTIAL EXIT ───────────────────────────────────────────────────
# Once an open position's lot reaches this size (a deep recovery
# round), close PARTIAL_EXIT_FRACTION of it at market immediately —
# reduces exposure right away instead of waiting for that round's own
# SL/TP on the full size. Runs at most once per side per round.
PARTIAL_EXIT_LOT_THRESHOLD = 0.32
PARTIAL_EXIT_FRACTION      = 0.30   # close 30% of the position

# ── CAPITAL MODE PRESETS (Soft / Aggressive) ──────────────────────
# Bundles the recovery/risk knobs above into two ready-made profiles
# instead of tuning each one individually. Soft trades upside for
# safety — smaller lot growth, earlier risk-free, tighter hard stop.
# Aggressive is closer to the original unprotected behavior — full
# doubling, later risk-free, looser hard stop. Selected from the GUI
# (Capital Mode dropdown) and applied via apply_capital_mode() before
# the bot starts; "Custom" leaves whatever is set above untouched.
CAPITAL_MODES = {
    "soft": dict(
        LOT_MULTIPLIER=1.3,
        RISK_FREE_TRIGGER_R=1.0,
        HARD_STOP_LOSS_RATIO=0.90,      # stop at 10% drawdown
        MARGIN_SAFETY_RATIO=0.10,
        DEEP_ROUND_LOT_THRESHOLD=0.32,  # require confluence sooner
        TIGHT_MARGIN_RATIO=0.40,
        TP_RR_PRIMARY=2,
        TP_RR_FALLBACK=2,
    ),
    "aggressive": dict(
        LOT_MULTIPLIER=2.0,
        RISK_FREE_TRIGGER_R=2.0,
        HARD_STOP_LOSS_RATIO=0.70,      # stop at 30% drawdown
        MARGIN_SAFETY_RATIO=0.05,
        DEEP_ROUND_LOT_THRESHOLD=0.64,
        TIGHT_MARGIN_RATIO=0.30,
        TP_RR_PRIMARY=3,
        TP_RR_FALLBACK=2,
    ),
}


def apply_capital_mode(mode: str):
    """
    Overwrite this module's risk-tuning attributes with one of the
    CAPITAL_MODES presets ("soft" or "aggressive"). No-op for any
    other value (including "custom"), leaving whatever is already
    configured above untouched. Called from gui.py's _start() before
    launching the watcher/backtest, so every place that reads these
    via getattr(cfg, "NAME", default) — which is everywhere in
    position_monitor.py — picks the new values up automatically.
    """
    preset = CAPITAL_MODES.get(mode.lower())
    if not preset:
        return
    globals().update(preset)

# ── OBJECT FILTERING ─────────────────────────────────────────────
# CRITICAL: every prefix used by any detector/drawer in this bot
# MUST be listed here, or the watcher will treat its own drawn
# rectangles/labels as trader-drawn signal lines and start trading
# on them automatically.
AUTO_OBJECT_PREFIXES = [
    "PA_", "CT", "GB_", "TB2_", "autotrade",
    "FVG_",       # FVG detector rectangles
    "OB_",        # Order Block rectangles
    "OBFVG_",     # OB+FVG Confluence rectangles
    "AMD_",       # AMD Quarter Theory boxes
    "AMDT_",      # AMD info table labels
    "MTFFVG_",    # Multi-timeframe FVG confluence intersection zones
    "MTFFVG5M_",  # Multi-timeframe FVG 5M entry rectangles (NEW)
]

BOT_LINE_PREFIX = "TB2_"

# ── LOGGING ──────────────────────────────────────────────────────
LOG_LEVEL = "INFO"