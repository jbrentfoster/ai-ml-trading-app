"""
Central configuration for the AI Trading App.

Settings are loaded in priority order:
  1. Python dataclass defaults (this file)
  2. config/settings.yaml  (user overrides — created by the Settings UI page)
  3. Environment variables  (secrets: ALPACA_API_KEY, ALPACA_SECRET_KEY)

Do NOT commit API keys to version control.  Use a .env file or shell env vars.
"""

import dataclasses
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root so ALPACA_API_KEY, ALPACA_SECRET_KEY, etc.
# are available to os.environ before any config dataclass is instantiated.
load_dotenv()


class TradingMode(Enum):
    SIMULATION = "simulation"   # IBKR paper trading account
    LIVE = "live"               # Real money — enable only when ready


@dataclass
class IBKRConfig:
    """Interactive Brokers connection settings."""

    # IB Gateway host — usually localhost
    host: str = "127.0.0.1"

    # Paper trading port: 4002 (IB Gateway)
    # Live trading port:  4001 (IB Gateway)
    paper_port: int = 4002
    live_port: int = 4001

    # Unique client ID — increment if you run multiple simultaneous connections
    client_id: int = 1

    # Seconds to wait for IBKR to respond before timing out
    connection_timeout: int = 10

    # How often (seconds) to ping IBKR to keep the connection alive
    heartbeat_interval: int = 30

    # Maximum reconnection attempts before giving up
    max_reconnect_attempts: int = 5

    # Delay (seconds) between reconnection attempts
    reconnect_delay: float = 5.0


@dataclass
class TradingConfig:
    """Runtime trading behaviour settings."""

    # Start in simulation — flip to LIVE only after thorough paper trading
    mode: TradingMode = TradingMode.SIMULATION

    # Account currency
    base_currency: str = "USD"

    # Maximum fraction of total equity at risk on any single trade
    max_position_size_pct: float = 0.05      # 5%

    # Portfolio-wide maximum drawdown before halting all new trades
    max_portfolio_drawdown_pct: float = 0.10  # 10%

    # Default stop-loss distance from entry
    default_stop_loss_pct: float = 0.02       # 2%

    # Default take-profit distance from entry
    default_take_profit_pct: float = 0.04     # 4%

    # Exchanges to trade on (SMART = IBKR smart routing)
    exchanges: list = field(default_factory=lambda: ["SMART"])

    # Asset classes in scope
    asset_classes: list = field(default_factory=lambda: ["STK"])  # stocks only for now

    # When True, submit bracket orders to the IBKR paper account in SIMULATION mode.
    # When False (default), signal_runner.py logs decisions as DRY_RUN without sending orders.
    paper_orders_enabled: bool = False

    # When False (default), SELL signals only close existing long positions — they never
    # open a new short position.  Set True to enable short selling (future use).
    allow_short_selling: bool = False

    # Assumed account equity when no live IBKR connection is available (dry-run / simulation)
    paper_equity: float = 100_000.0

    # Fraction of total equity to keep in cash at all times.
    # PositionSizer uses (equity × (1 - cash_reserve_pct)) as the investable base,
    # so each position is sized against the deployable capital rather than total equity.
    # e.g. 0.20 = keep 20% in cash; a 5% position becomes 5% of 80% = 4% of total equity.
    cash_reserve_pct: float = 0.20


@dataclass
class DataConfig:
    """Market data pipeline settings (Step 2)."""

    # Symbols to track — these populate the dashboard and feed ML models
    watchlist: list = field(default_factory=lambda: [
        "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
        "META", "TSLA", "JPM", "V", "UNH",
    ])

    # SQLite database path (relative to project root)
    db_path: str = "db/trading.db"

    # Daily bar history to fetch on first run
    daily_lookback_days: int = 365

    # Intraday history (yfinance caps 1h data at ~60 days)
    intraday_interval: str = "1h"
    intraday_lookback_days: int = 59

    # How often the dashboard auto-suggests refreshing (informational only)
    auto_refresh_interval_minutes: int = 60

    # Benchmark symbol used for relative-performance tracking on Page 10.
    # OHLCV is stored alongside equities in ohlcv_bars (same convention as ^VIX),
    # fetched unconditionally by run_pipeline.py and refresh_recent_bars.py so
    # ingestion does not depend on whether universe selection is enabled.
    benchmark_symbol: str = "SPY"


@dataclass
class AlpacaConfig:
    """Alpaca Markets API settings (news + bar data)."""

    # Set ALPACA_API_KEY and ALPACA_SECRET_KEY in your environment or .env file
    api_key:    str = field(default_factory=lambda: os.environ.get("ALPACA_API_KEY", ""))
    secret_key: str = field(default_factory=lambda: os.environ.get("ALPACA_SECRET_KEY", ""))

    # News lookback window when fetching articles for sentiment scoring
    news_lookback_days: int = 30

    # Maximum articles to fetch/score per symbol per fetch.
    # IBKR caps at 300; set high enough to cover 30 days on busy tickers (TSLA ~6/day).
    max_articles_per_symbol: int = 300


@dataclass
class MLConfig:
    """Machine-learning model and ensemble settings."""

    # ── LSTM ──────────────────────────────────────────────────────────────────
    lstm_sequence_length: int  = 60      # input window in bars
    lstm_hidden_size:     int  = 128
    lstm_num_layers:      int  = 2
    lstm_dropout:         float = 0.2
    lstm_epochs:          int  = 50
    lstm_batch_size:      int  = 32
    lstm_learning_rate:   float = 1e-3
    lstm_random_seed:     int | None = 42   # set to None for non-deterministic training

    # ── XGBoost ───────────────────────────────────────────────────────────────
    xgb_n_estimators:    int   = 300
    xgb_max_depth:       int   = 5
    xgb_learning_rate:   float = 0.05
    xgb_subsample:       float = 0.8
    xgb_colsample:       float = 0.8

    # ── FinBERT ───────────────────────────────────────────────────────────────
    finbert_model_name:  str   = "ProsusAI/finbert"
    sentiment_half_life_hours: float = 24.0   # exponential decay half-life
    sentiment_staleness_days:  int   = 7      # return 0.0 if no news within window

    # ── Ensemble weights (must sum to 1.0) ────────────────────────────────────
    ensemble_lstm_weight:    float = 0.40
    ensemble_xgb_weight:     float = 0.35
    ensemble_finbert_weight: float = 0.25

    # Minimum per-model weight (floor) before re-normalisation
    ensemble_weight_floor:   float = 0.10

    # Fraction to nudge weights toward better-performing model each rebalance
    ensemble_nudge:          float = 0.10

    # ── Signal gating ─────────────────────────────────────────────────────────
    signal_threshold:       float = 0.35    # base long/short threshold
    signal_confirmation:    int   = 2       # models that must agree (of 3)
    signal_lookback_days:   int   = 365     # default date range on Model Signals page

    # ── Regime-aware adjustments ──────────────────────────────────────────────
    # XGBoost is a 5-bar mean-reversion classifier and emits systematically
    # bearish scores in trending markets (RSI/MACD/BB hit extreme regions and
    # the model bets on revert). Downweighting it in TRENDING regimes lets
    # LSTM (sequence) and FinBERT (news) dominate.
    xgb_trending_weight_multiplier: float = 0.5

    # Regime-adjusted signal-gate threshold multipliers. TRENDING was 0.9
    # (more permissive — letting through SELL-dominated XGBoost output);
    # set >1.0 to be stricter when XGBoost is structurally biased.
    trending_threshold_multiplier: float = 1.2
    high_vol_threshold_multiplier: float = 1.5

    # ── Walk-forward training ─────────────────────────────────────────────────
    # Defaults sized for a ~1-year daily dataset (~249 trading bars).
    # min_bars_required = train + gap + test = 120 + 1 + 21 = 142
    # With 5 splits: 120 + 1 + 5*21 = 226 bars needed (fits in 249).
    wf_train_bars: int = 120     # ~6 months daily
    wf_test_bars:  int = 21      # ~1 month daily
    wf_gap_bars:   int = 1
    wf_n_splits:   int = 5

    # ── Sentiment availability ────────────────────────────────────────────────
    # Set to the earliest date for which news data is reliably available.
    # Walk-forward windows whose test period starts before this date will run
    # with finbert_score=0.0 and FinBERT's weight redistributed equally to
    # LSTM and XGBoost.  None = no restriction (FinBERT always active).
    news_available_from: datetime | None = None

    # ── Cost model (applied on both entry and exit) ────────────────────────────
    slippage_pct:   float = 0.001   # 0.1 % per side
    commission_per_share: float = 0.005


@dataclass
class RiskConfig:
    """Position sizing and portfolio risk management settings."""

    # ── Kelly criterion ───────────────────────────────────────────────────────
    # Fractional Kelly multiplier: 0.25 = quarter-Kelly (conservative)
    kelly_fraction: float = 0.25

    # Hard cap on position size regardless of Kelly output
    kelly_max_position_pct: float = 0.10    # 10% max per position

    # Minimum signal-log history required to use Kelly (falls back to fixed if <)
    kelly_min_trades: int = 10

    # ── ATR-based stop / take-profit ──────────────────────────────────────────
    atr_stop_multiplier: float = 2.0        # stop = entry ± ATR × multiplier
    atr_take_profit_multiplier: float = 3.0  # take-profit = entry ± ATR × multiplier

    # Fallback stop when ATR is zero / unavailable
    fixed_stop_loss_pct: float = 0.02       # 2% fixed stop

    # ── Portfolio guard ───────────────────────────────────────────────────────
    max_sector_exposure_pct: float = 0.30   # 30% cap per sector
    max_correlated_positions: int = 3       # max positions with corr > threshold
    correlation_threshold: float = 0.70     # Pearson r above this = "highly correlated"
    correlation_lookback_bars: int = 60     # bars used for correlation calculation

    # ── Circuit breaker ───────────────────────────────────────────────────────
    circuit_breaker_daily_loss_pct: float = 0.05   # 5% single-day loss → halt
    circuit_breaker_weekly_loss_pct: float = 0.10  # 10% weekly loss → halt
    circuit_breaker_reset_hours: int = 24           # auto-reset after N hours

    # ── Stale-bar gate ────────────────────────────────────────────────────────
    # Drop a symbol from signal generation when its newest cached daily bar is
    # older than this many *calendar* days.  3 days handles a normal weekend
    # (Fri close → Mon open) without false positives; longer holiday gaps still
    # trigger the gate.  Set higher to be more permissive.
    max_bar_staleness_days: int = 3

    # ── Trailing stop ─────────────────────────────────────────────────────────
    # When True and paper_orders_enabled=True, the signal runner converts the
    # LMT take-profit leg of each qualifying long position into a standalone
    # GTC TRAIL order so winners can run past the original take-profit target.
    # The original ATR stop and TP are both cancelled; the TRAIL serves as the
    # sole exit, ratcheting upward as price rises and triggering on reversal.
    trailing_stop_enabled: bool = False

    # Activation threshold — convert once price has moved this many ATRs in
    # favour of entry (for a long: current_price >= entry + activation_atr × ATR).
    # Default 2.0 matches trailing_stop_trail_atr so the initial trail stop
    # lands at entry = break-even protection at the moment of conversion.
    trailing_stop_activation_atr: float = 2.0

    # Trail distance in ATR multiples once activated (typically matches
    # atr_stop_multiplier so the initial trail stop isn't looser than the
    # bracket's original stop)
    trailing_stop_trail_atr: float = 2.0

    # ── Intraday trailing-stop runner (scripts/intraday_check.py) ────────────
    # The intraday runner (12:00 ET / 15:30 ET on weekdays) re-evaluates
    # trailing stops against live IBKR price.  By default it only logs
    # ratchet events and does NOT perform new bracket TP→TRAIL conversions —
    # mid-day conversions face a sub-second "no stop" window during the
    # cancel-TP / cancel-STP / submit-TRAIL sequence that is riskier than the
    # same window at 09:35 (lower liquidity, faster moves).  Flip this to
    # True (and ensure ``paper_orders_enabled=True``) to allow intraday
    # conversions, but only after operational experience with the runner.
    intraday_trail_conversion_enabled: bool = False

    # When intraday conversions are enabled, an additional buffer above the
    # daily-Phase-3.5 activation threshold is required: a position only
    # converts mid-day if ``current_price >= entry +
    # (trailing_stop_activation_atr + intraday_conversion_buffer_atr) × ATR``.
    # The buffer exists because anything still un-converted by 12:00 ET was
    # already evaluated at 09:35 ET against the same daily ATR — anything
    # close to activation by mid-day has, by definition, just crossed the
    # threshold, and the marginal cases are the worst-positioned for the
    # no-stop window risk.
    intraday_conversion_buffer_atr: float = 0.5

    # ── Hold timeout ──────────────────────────────────────────────────────────
    # When True and paper_orders_enabled=True, the signal runner closes any
    # held long that hasn't received a fresh BUY signal (signal_log row with
    # signal='BUY' AND passed_gate=True) within the last `max_hold_days`
    # calendar days.  Guards against positions sitting indefinitely in
    # sparse-signal regimes where neither stop, TP, nor SELL ever fires.
    # A held position whose most recent BUY in signal_log is older than the
    # threshold is flattened via a market sell (bracket children cancelled
    # first), persisted as decision='CLOSED_TIMEOUT'.  Symbols with NO BUY
    # history at all are skipped (we have no anchor for "stale").
    hold_timeout_enabled: bool = False

    # Calendar-day threshold for the hold-timeout rule.  30 ≈ 22 trading days
    # (~1 month) — long enough to ride out consolidation, short enough to
    # surface positions the model no longer favours.
    max_hold_days: int = 30

    # ── Walk-forward bracket simulation (Phase 4.5 — Phase A) ────────────────
    # When the walk-forward simulator fills a stop intra-bar, the realised
    # exit price is worse than the trigger by `stop_slippage_multiplier × slippage_pct`.
    # Gap-through stops (Open already through the trigger) are NOT charged
    # this extra — the gap itself IS the slippage.  Default 2.0 reflects the
    # typical relationship between a quiet quote and a stop-hunt fill.
    stop_slippage_multiplier: float = 2.0

    # ── Realised-Kelly (Phase C — not yet wired) ─────────────────────────────
    # Minimum closed trades for `compute_realised_kelly` to produce a sized
    # output; below this threshold, PositionSizer falls back to the
    # |ensemble_score| proxy used today.
    min_trades_for_realised_kelly: int = 30


@dataclass
class UniverseConfig:
    """Automated stock universe selection settings."""

    # Set to True to replace the static watchlist with the dynamic universe
    enabled: bool = False

    # Stage 1 — pull from Alpaca assets endpoint (us_equity, active, tradable)
    # Alpaca returns ~33k assets in internal (non-alphabetical) order; large-caps
    # like AAPL appear at position 30k+.  Keep this >> total listed equities (~10k
    # active tradable) to avoid silently excluding symbols.
    stage1_max: int = 50000

    # Stage 2 — pass liquidity / market-cap filter
    stage2_max: int = 300
    min_market_cap: float = 1_000_000_000   # $1B minimum market cap
    # Average daily dollar volume = (close × volume).mean() over 20 bars
    min_avg_dollar_volume: float = 5_000_000  # $5M

    # Stage 3 — XGBoost-ranked candidates (or market-cap sorted if no model)
    stage3_max: int = 50

    # Only include equities listed on these exchanges.
    # OTC is excluded by default — that's where most foreign ADRs trade.
    # Fixtures bypass this check so ETFs on ARCA are always included.
    allowed_exchanges: list = field(default_factory=lambda: [
        "NYSE", "NASDAQ", "ARCA", "BATS", "AMEX",
    ])

    # Always include these regardless of funnel results
    permanent_fixtures: list = field(default_factory=lambda: [
        "SPY", "QQQ", "IWM", "DIA",
        "XLF", "XLE", "XLK", "XLV", "XLI", "XLP", "XLY", "XLU", "XLB", "XLRE",
        "TLT", "GLD", "SLV", "USO",
    ])

    # How many days to retain universe_run_log entries
    run_log_retention_days: int = 90


@dataclass
class LoggingConfig:
    """Logging settings."""
    level: str = "INFO"            # DEBUG | INFO | WARNING | ERROR
    log_dir: str = "logs"
    log_to_file: bool = True
    log_to_console: bool = True
    max_file_size_mb: int = 50
    backup_count: int = 5


@dataclass
class LLMConfig:
    """
    Local-LLM news analyst (shadow workflow — NOT consumed by signal_runner).

    Runs full-article sentiment extraction via Ollama against stage-3 universe
    news.  Scores are stored in ``llm_news_analysis`` and surfaced on the
    dashboard only; nothing in the trading path reads them yet.
    """
    enabled:              bool  = False                 # opt-in (mirrors other shadow features)
    model:                str   = "llama3.1:8b"         # Ollama model tag
    ollama_url:           str   = "http://localhost:11434/api/generate"
    num_predict:          int   = 300                   # max output tokens per article
    request_timeout_s:    int   = 1200                  # per-article hard cap
    min_body_chars:       int   = 800                   # skip stubs below this (matches spike 'full' floor)
    lookback_days:        int   = 3                     # body-ingest / scoring window
    novelty_discount_floor: float = 0.5                 # composite score: novelty multiplier floor in [floor, 1.0]


@dataclass
class FlexConfig:
    """IBKR Flex Query Web Service — durable, session-independent trade history.

    The real-time ``reqExecutions`` poll only sees the current Gateway session,
    which the overnight reset wipes; the Flex Web Service retains a year+ and is
    polled once per daily run by ``scripts/reconcile_flex.py`` to backfill any
    prior-day fills the live path missed.  ``token`` is a secret (read from env,
    never written to YAML — see ``_SECRET_FIELDS``); generate it + a Trades
    (Level of Detail = Execution, period Month-to-Date or Last N Days) query in
    IBKR Account Management.  When ``token``/``query_id`` are unset the reconcile
    step is a no-op, so the feature is opt-in by simply not setting the env vars.

    ``source_tz`` is the timezone the query's ``dateTime`` field is emitted in;
    this account's queries emit US/Eastern (verified 2026-06-09: a VRT fill at
    Flex ``143817`` == ``18:38:17`` UTC in fill_log).  Dedup is keyed on
    ``ibExecID`` so a wrong tz never double-writes — it only shifts shown times.
    """
    token:     str = field(default_factory=lambda: os.environ.get("IBKR_FLEX_TOKEN", ""))
    query_id:  str = field(default_factory=lambda: os.environ.get("IBKR_FLEX_QUERY_ID", ""))
    source_tz: str = "America/New_York"

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.query_id)


@dataclass
class AllocationConfig:
    """Risk-premia rebalancer settings (portfolio/ + scripts/rebalance.py).

    ``rebalance_orders_enabled`` is the *second* of the two execution gates:
    orders are submitted only when BOTH ``scripts/rebalance.py --no-dry-run`` is
    passed AND this is True (default False).  Big-bets are drift-exempt in the
    engine regardless of these settings.
    """
    rebalance_band:           float = 0.05   # per-sleeve drift band (fraction of NLV)
    cash_buffer:              float = 0.01   # fraction of NLV held back as cash
    rebalance_orders_enabled: bool  = False  # second execution gate (default OFF)
    slippage_cap:             float = 0.005  # marketable-limit offset from the reference price
    share_precision:          int   = 4      # fractional-share rounding (0 = whole shares)


@dataclass
class AppConfig:
    """Top-level app config — compose all sub-configs here."""
    ibkr:     IBKRConfig     = field(default_factory=IBKRConfig)
    trading:  TradingConfig  = field(default_factory=TradingConfig)
    data:     DataConfig     = field(default_factory=DataConfig)
    alpaca:   AlpacaConfig   = field(default_factory=AlpacaConfig)
    ml:       MLConfig       = field(default_factory=MLConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    risk:     RiskConfig     = field(default_factory=RiskConfig)
    logging:  LoggingConfig  = field(default_factory=LoggingConfig)
    llm:      LLMConfig      = field(default_factory=LLMConfig)
    flex:     FlexConfig     = field(default_factory=FlexConfig)
    allocation: AllocationConfig = field(default_factory=AllocationConfig)


# ── Singleton instance ────────────────────────────────────────────────────────
# Import this in any module:  from config.settings import config
config = AppConfig()

# ── YAML persistence ──────────────────────────────────────────────────────────

_YAML_PATH = Path(__file__).parent / "settings.yaml"

_SECTION_MAP = {
    "ibkr":     lambda: config.ibkr,
    "trading":  lambda: config.trading,
    "data":     lambda: config.data,
    "alpaca":   lambda: config.alpaca,
    "ml":       lambda: config.ml,
    "universe": lambda: config.universe,
    "risk":     lambda: config.risk,
    "logging":  lambda: config.logging,
    "llm":      lambda: config.llm,
    "flex":     lambda: config.flex,
}

# Fields that should never be written to YAML (live in env vars only)
_SECRET_FIELDS = {"api_key", "secret_key", "token"}


def _apply_yaml_section(obj, values: dict, section_name: str = "") -> None:
    """Write `values` dict onto a dataclass instance, converting types as needed.

    Logs a warning for any YAML key that doesn't match a dataclass field on
    `obj`. Without this, typos like `min_trades_for_realised_kellly`
    (three L's — spotted 2026-05-07 during Phase C verification) silently
    disable the intended override. Surfacing the typo at process start is
    cheaper than waiting for the behavioural symptom downstream.
    """
    import warnings
    valid_fields = {f.name for f in dataclasses.fields(obj)}
    for key, value in values.items():
        if key not in valid_fields:
            label = f"{section_name}.{key}" if section_name else key
            warnings.warn(
                f"Unknown YAML key '{label}' in config/settings.yaml — ignored."
            )
            continue
        current = getattr(obj, key)
        try:
            if isinstance(current, Enum):
                setattr(obj, key, type(current)(value))
            elif isinstance(current, datetime) or (
                current is None and key == "news_available_from"
            ):
                if value is None:
                    setattr(obj, key, None)
                else:
                    setattr(obj, key, datetime.fromisoformat(str(value)))
            else:
                setattr(obj, key, value)
        except Exception:
            pass   # ignore bad values — keep the existing default


def load_yaml_config() -> None:
    """Load config/settings.yaml and apply any overrides to the singleton."""
    if not _YAML_PATH.exists():
        return
    try:
        import warnings
        import yaml
        with open(_YAML_PATH, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

        # Warn on any top-level section that doesn't map to a config object.
        # A typo in a section name (e.g. `mlmodels:` instead of `ml:`) would
        # otherwise drop every override under it without complaint.
        for section in data:
            if section not in _SECTION_MAP:
                warnings.warn(
                    f"Unknown YAML section '{section}' in config/settings.yaml "
                    f"— ignored."
                )

        for section, getter in _SECTION_MAP.items():
            if section in data and isinstance(data[section], dict):
                _apply_yaml_section(getter(), data[section], section_name=section)
    except Exception as exc:
        # Never crash on a bad YAML file — just use defaults
        import warnings
        warnings.warn(f"Could not load config/settings.yaml: {exc}")


def save_yaml_config() -> None:
    """Serialise the current in-memory config to config/settings.yaml."""
    import yaml

    def _section_to_dict(obj) -> dict:
        out = {}
        for f in dataclasses.fields(obj):
            if f.name in _SECRET_FIELDS:
                continue
            val = getattr(obj, f.name)
            if isinstance(val, Enum):
                out[f.name] = val.value
            elif isinstance(val, datetime):
                out[f.name] = val.isoformat()
            else:
                out[f.name] = val
        return out

    data = {section: _section_to_dict(getter())
            for section, getter in _SECTION_MAP.items()}

    _YAML_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_YAML_PATH, "w", encoding="utf-8") as fh:
        yaml.dump(data, fh, default_flow_style=False, sort_keys=False,
                  allow_unicode=True)


# Apply YAML overrides on import (env vars for secrets are already loaded above)
load_yaml_config()
