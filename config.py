"""
╔══════════════════════════════════════════════════════════════════╗
║         TraderBot v2 — Configuration                            ║
║         2-Order Martingale Bot (Buy-Stop + Sell-Stop)           ║
╚══════════════════════════════════════════════════════════════════╝
"""

# ── MT5 CREDENTIALS ──────────────────────────────────────────────
MT5_LOGIN    = 52936622
MT5_PASSWORD = "@Radiar9841@"
MT5_SERVER   = "Alpari-MT5-Demo"

# ── SYMBOL TO WATCH ──────────────────────────────────────────────
WATCH_SYMBOL = "EURUSD"

# ── SCAN SETTINGS ────────────────────────────────────────────────
SCAN_INTERVAL_SEC = 2

# ── ORDER SETTINGS ───────────────────────────────────────────────
# Distance above/below the main line where the buy-stop / sell-stop are placed
ORDER_DISTANCE_PIPS = 1.5      # pips above line for BUY-STOP, below for SELL-STOP

# Stop-loss: mirrored across the main line
# e.g. BUY-STOP entry = line + distance → SL = line - distance
# SL is always the mirror of entry across the source line

LOT_SIZE        = 0.01          # initial lot size
LOT_MULTIPLIER  = 1.20          # kept for reference, not used in hedge mode

TP_RR_RATIO     = 0.0           # no fixed pip TP — balance TP used instead
MAGIC_NUMBER    = 998877        # unique ID so bot can identify its own orders

# ── OBJECT FILTERING ─────────────────────────────────────────────
# Prefixes of auto-drawn or indicator objects to ignore
AUTO_OBJECT_PREFIXES = [
    "PA_", "CT", "GB_", "TB2_", "autotrade",
    "FVG_",   # FVG detector rectangles — never treat as trade signals
]

BOT_LINE_PREFIX = "TB2_"        # prefix for bot-drawn lines

# ── LOGGING ──────────────────────────────────────────────────────
LOG_LEVEL = "INFO"