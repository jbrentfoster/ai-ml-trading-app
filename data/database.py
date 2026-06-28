"""
SQLite database layer — OHLCV price history, indicator snapshots, and ML tables.

Tables:
  ohlcv_bars              — raw price bars (daily + intraday; also stores ^VIX)
  indicator_snapshots     — computed technical indicator values per bar
  fundamental_data        — yfinance fundamental snapshot per symbol
  news_cache              — Alpaca news articles with FinBERT scores
  signal_log              — generated trading signals with metadata
  ensemble_weight_history — per-rebalance model weight snapshots
  walk_forward_results    — performance metrics from each WF fold
  universe_assets         — dynamic stock universe candidates
  universe_run_log        — per-stage run log for universe selection
  circuit_breaker_log     — halt/reset events from CircuitBreaker
  equity_snapshots        — daily NLV snapshot for circuit-breaker baseline
  order_decisions         — per-signal order decisions from OrderManager
  signal_runner_log       — daily signal_runner.py run summaries
  trade_log               — closed-trade outcomes (walk-forward simulator + live fills)
  intraday_run_log        — intraday lightweight-runner outcomes (CB check + trail ratchets)
  fill_log                — raw IBKR executions (Phase B reconciliation audit trail)
  reconciliation_state    — Phase B reconciliation watermark per (source, account)

All timestamps are stored as UTC-naive datetimes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    desc,
    func,
    or_,
)
from sqlalchemy.orm import DeclarativeBase, Session

from config.settings import config
from core.logger import get_logger

log = get_logger("data.database")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── ORM models ────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class OHLCVBar(Base):
    __tablename__ = "ohlcv_bars"

    id        = Column(Integer, primary_key=True, autoincrement=True)
    symbol    = Column(String(10), nullable=False)
    interval  = Column(String(5),  nullable=False)   # "1d", "1h", "5m", …
    timestamp = Column(DateTime,   nullable=False)
    open      = Column(Float,      nullable=False)
    high      = Column(Float,      nullable=False)
    low       = Column(Float,      nullable=False)
    close     = Column(Float,      nullable=False)
    volume    = Column(Float,      nullable=False)
    created_at = Column(DateTime,  default=_utc_now)

    __table_args__ = (
        UniqueConstraint("symbol", "interval", "timestamp", name="uq_bar"),
    )


class IndicatorSnapshot(Base):
    __tablename__ = "indicator_snapshots"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    symbol       = Column(String(10), nullable=False)
    interval     = Column(String(5),  nullable=False)
    timestamp    = Column(DateTime,   nullable=False)
    # Momentum
    rsi_14       = Column(Float)
    # MACD
    macd         = Column(Float)
    macd_signal  = Column(Float)
    macd_hist    = Column(Float)
    # Bollinger Bands
    bb_upper     = Column(Float)
    bb_middle    = Column(Float)
    bb_lower     = Column(Float)
    # EMAs
    ema_9        = Column(Float)
    ema_21       = Column(Float)
    ema_50       = Column(Float)
    # Volatility / volume
    atr_14       = Column(Float)
    volume_sma_20 = Column(Float)

    created_at = Column(DateTime, default=_utc_now)

    __table_args__ = (
        UniqueConstraint("symbol", "interval", "timestamp", name="uq_indicator"),
    )


class FundamentalData(Base):
    """Snapshot of yfinance fundamental metrics — append-only history.

    Multiple rows per symbol are expected; readers should order by
    ``fetched_at DESC`` to get the latest snapshot. The 24h cache check
    in ``FundamentalsClient.get`` prevents duplicate same-day inserts.
    """
    __tablename__ = "fundamental_data"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    symbol     = Column(String(10), nullable=False, index=True)
    fetched_at = Column(DateTime,   nullable=False)

    # Valuation
    market_cap      = Column(Float)
    pe_ratio        = Column(Float)
    forward_pe      = Column(Float)
    price_to_book   = Column(Float)
    ev_to_ebitda    = Column(Float)

    # Growth / profitability
    revenue_growth  = Column(Float)
    earnings_growth = Column(Float)
    profit_margin   = Column(Float)
    roe             = Column(Float)

    # Balance sheet
    debt_to_equity  = Column(Float)
    current_ratio   = Column(Float)
    free_cashflow   = Column(Float)

    # Price targets
    analyst_target  = Column(Float)

    # Classification — raw yfinance GICS sector label (e.g. "Technology",
    # "Financial Services").  Normalised to the project's simplified scheme at
    # read time in risk/portfolio_guard.py:get_sector.  NULL until the next
    # fundamentals fetch back-fills it.
    sector          = Column(String(40))


class NewsCache(Base):
    """Alpaca news article with FinBERT sentiment score."""
    __tablename__ = "news_cache"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    symbol       = Column(String(10), nullable=False)
    article_id   = Column(String(64), nullable=False)   # Alpaca article ID
    published_at = Column(DateTime,   nullable=False)
    headline     = Column(Text,       nullable=False)
    sentiment_score = Column(Float)    # [-1, 1]; positive = bullish (FinBERT, headline-only)
    body         = Column(Text)        # full article text (HTML-stripped); NULL until ingested
                                       # by scripts/ingest_news_bodies.py (IBKR reqNewsArticle)

    __table_args__ = (
        UniqueConstraint("symbol", "article_id", name="uq_news"),
    )


class SignalLog(Base):
    """Generated trading signal with full provenance."""
    __tablename__ = "signal_log"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    symbol       = Column(String(10), nullable=False)
    generated_at = Column(DateTime,   nullable=False)
    bar_timestamp = Column(DateTime,  nullable=False)   # bar the signal was based on

    # Raw model outputs
    lstm_score    = Column(Float)
    xgb_score     = Column(Float)
    finbert_score = Column(Float)

    # Ensemble
    ensemble_score = Column(Float)
    regime         = Column(String(20))    # RegimeType.value

    # Gate decision
    signal         = Column(String(10))    # "BUY" | "SELL" | "HOLD"
    passed_gate    = Column(Boolean,       default=False)
    gate_reason    = Column(String(100))   # human-readable gate outcome


class EnsembleWeightHistory(Base):
    """Snapshot of ensemble model weights after each rebalance."""
    __tablename__ = "ensemble_weight_history"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    recorded_at  = Column(DateTime, nullable=False)
    symbol       = Column(String(10), index=True)   # NULL on pre-2026-05-14 rows
    run_id       = Column(String(36), index=True)
    lstm_weight  = Column(Float,    nullable=False)
    xgb_weight   = Column(Float,    nullable=False)
    finbert_weight = Column(Float,  nullable=False)
    trigger      = Column(String(50))   # "rebalance" | "fold_end" | "manual"


class WalkForwardResult(Base):
    """Per-fold performance metrics from MLWalkForwardOrchestrator."""
    __tablename__ = "walk_forward_results"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    run_id          = Column(String(36), nullable=False)   # UUID per training run
    symbol          = Column(String(10), nullable=False)
    fold_index      = Column(Integer,    nullable=False)
    train_start     = Column(DateTime,   nullable=False)
    train_end       = Column(DateTime,   nullable=False)
    test_start      = Column(DateTime,   nullable=False)
    test_end        = Column(DateTime,   nullable=False)
    total_return    = Column(Float)
    annualized_return = Column(Float)
    sharpe_ratio    = Column(Float)
    max_drawdown    = Column(Float)
    win_rate        = Column(Float)
    n_signals       = Column(Integer)
    recorded_at     = Column(DateTime, nullable=False)
    sentiment_note  = Column(Text)     # set when FinBERT was suppressed for this fold
    # "dynamic" when this run was driven by UniverseSelector (subject to
    # survivorship bias — the universe was determined using *today's* data, so
    # historical folds may contain symbols that only became candidates in
    # hindsight); "static" when driven by the configured watchlist.  NULL on
    # rows written before this column was added (2026-05-12 migration).
    universe_policy = Column(String(20))


class UniverseAsset(Base):
    """Candidate stock/ETF tracked by the automated universe selector."""
    __tablename__ = "universe_assets"

    symbol          = Column(String(10), primary_key=True)
    name            = Column(String(200))
    asset_class     = Column(String(20))    # "us_equity" | "etf"
    exchange        = Column(String(20))    # "NYSE" | "NASDAQ" | "ARCA" | "BATS" etc.
    is_fixture      = Column(Boolean, default=False)
    stage           = Column(Integer)       # last funnel stage reached (1/2/3)
    market_cap      = Column(Float)
    avg_dollar_volume = Column(Float)       # (close x volume).mean() over 20 bars
    stage3_score    = Column(Float)        # Stage 3 rank-percentile blend of 20d return + ADV; None if not scored
    active          = Column(Boolean, default=True)
    added_at        = Column(DateTime, nullable=False)
    last_scored_at  = Column(DateTime)
    removed_at      = Column(DateTime)     # set when dropped from active list


class UniverseRunLog(Base):
    """Per-stage timing + count log for each universe selection run."""
    __tablename__ = "universe_run_log"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    run_id           = Column(String(36), nullable=False)
    run_type         = Column(String(20), nullable=False)   # "full" | "rescore"
    stage            = Column(Integer,    nullable=False)   # 1 / 2 / 3
    symbol_count     = Column(Integer,    nullable=False)
    duration_seconds = Column(Float)
    recorded_at      = Column(DateTime,   nullable=False)
    notes            = Column(Text)


class CircuitBreakerLog(Base):
    """Halt and reset events from the CircuitBreaker."""
    __tablename__ = "circuit_breaker_log"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    event            = Column(String(20), nullable=False)   # "TRIGGERED" | "RESET" | "AUTO_RESET"
    reason           = Column(Text)
    daily_loss_pct   = Column(Float)
    weekly_loss_pct  = Column(Float)
    triggered_at     = Column(DateTime)    # set on TRIGGERED; carried on RESET row
    reset_at         = Column(DateTime)    # set on RESET / AUTO_RESET rows
    recorded_at      = Column(DateTime, nullable=False)


class EquitySnapshot(Base):
    """
    Daily NLV snapshot used as the loss-pct baseline for the circuit breaker.

    Written once per signal_runner.py run (Phase 1) before any orders are
    submitted.  `snapshot_date` is unique — re-running the runner the same day
    overwrites the row via log_equity_snapshot().
    """
    __tablename__ = "equity_snapshots"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_date    = Column(String(10), nullable=False, unique=True)  # YYYY-MM-DD
    net_liquidation  = Column(Float, nullable=False)
    total_cash       = Column(Float)
    unrealized_pnl   = Column(Float)
    realized_pnl     = Column(Float)
    recorded_at      = Column(DateTime, nullable=False)


class OrderDecisionRecord(Base):
    """Per-signal order decision logged by OrderManager."""
    __tablename__ = "order_decisions"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    run_id           = Column(String(36))
    symbol           = Column(String(10),  nullable=False)
    signal           = Column(String(10),  nullable=False)   # "BUY" | "SELL"
    decision         = Column(String(20),  nullable=False)   # "APPROVED" | "REJECTED" | "DRY_RUN"
    shares           = Column(Integer,     default=0)
    entry_price      = Column(Float)
    stop_price       = Column(Float)
    take_profit_price = Column(Float)
    position_value   = Column(Float)
    reject_reason    = Column(Text)
    decided_at       = Column(DateTime, nullable=False)


class SignalRunnerLog(Base):
    """Summary row written after each signal_runner.py run."""
    __tablename__ = "signal_runner_log"

    id                    = Column(Integer, primary_key=True, autoincrement=True)
    run_id                = Column(String(36), nullable=False)
    run_date              = Column(String(10))             # YYYY-MM-DD
    mode                  = Column(String(20))             # "dry_run" | "paper" | "live"
    symbols_processed     = Column(Integer, default=0)
    signals_generated     = Column(Integer, default=0)
    orders_submitted      = Column(Integer, default=0)
    orders_rejected       = Column(Integer, default=0)
    skipped_duplicates    = Column(Integer, default=0)
    skipped_pending_orders = Column(Integer, default=0)  # unfilled-entry dedup (Phase 4)
    skipped_stale         = Column(Integer, default=0)
    longs_closed          = Column(Integer, default=0)
    trailing_conversions  = Column(Integer, default=0)
    hold_timeouts         = Column(Integer, default=0)
    duration_seconds      = Column(Float)
    recorded_at           = Column(DateTime, nullable=False)
    notes                 = Column(Text)


class TrailingStopLog(Base):
    """
    One row per position evaluated by TrailingStopManager per run.

    action ∈ {"CONVERTED", "SKIPPED", "FAILED"} — see risk/trailing_stop.py.
    Written by TrailingStopManager.manage() during Phase 3.5; read back by
    Page 8 for a retrospective view of trailing-stop decisions.
    """
    __tablename__ = "trailing_stop_log"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    run_id         = Column(String(36))
    symbol         = Column(String(10), nullable=False)
    action         = Column(String(20), nullable=False)
    shares         = Column(Integer, default=0)
    entry_price    = Column(Float)
    current_price  = Column(Float)
    atr            = Column(Float)
    trail_amount   = Column(Float)
    reason         = Column(Text)
    decided_at     = Column(DateTime, nullable=False)


class TradeLog(Base):
    """
    Closed-trade outcomes for both the walk-forward simulator and live fills.

    Phase A populates rows with source='walk_forward' from
    MLWalkForwardOrchestrator's bracket simulation.  Phase B will add
    source='live' rows from IBKRConnection fill subscriptions.  Phase C reads
    these rows (filtered to entry_ts < as_of for forward-only safety) to
    compute realised-Kelly position sizing.

    exit_reason ∈ {'stop', 'tp', 'trailing', 'signal_flip', 'fold_end',
                    'manual_close'}.
    """
    __tablename__ = "trade_log"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    source        = Column(String(20), nullable=False)   # 'walk_forward' | 'live'
    run_id        = Column(String(36))
    fold_index    = Column(Integer)
    symbol        = Column(String(10), nullable=False)
    signal        = Column(String(10), nullable=False)   # 'BUY' | 'SELL'
    entry_ts      = Column(DateTime, nullable=False)
    entry_px      = Column(Float,    nullable=False)
    exit_ts       = Column(DateTime, nullable=False)
    exit_px       = Column(Float,    nullable=False)
    exit_reason   = Column(String(20), nullable=False)
    shares        = Column(Float,    nullable=False)
    pnl           = Column(Float,    nullable=False)
    pnl_pct       = Column(Float,    nullable=False)
    costs_charged = Column(Float,    default=0.0)
    # Raw price return on the benchmark (config.data.benchmark_symbol — SPY by
    # default) over the trade's holding period: (bench_exit / bench_entry) - 1.
    # NOT net of any costs.  The trade's pnl_pct is already net of costs; this
    # column intentionally is not, because the comparison "net of my costs vs
    # raw benchmark return" is the correct retail-alpha frame (a frictionless
    # buy-and-hold benchmark is the standard counterfactual).  NULL when the
    # benchmark has no bar on entry_ts or exit_ts (logged + skipped, not
    # silently zeroed).  Populated by scripts/backfill_benchmark_returns.py.
    benchmark_return_pct = Column(Float)
    recorded_at   = Column(DateTime, nullable=False)
    # Phase B live-fill linkage (NULL on source='walk_forward' rows).  The
    # closing fill's exec_id (exit_exec_id) is the per-round-trip dedup key,
    # enforced by the partial unique index uq_trade_live_exit.
    entry_exec_id   = Column(String(40))
    exit_exec_id    = Column(String(40))
    parent_order_id = Column(Integer)
    account         = Column(String(20))


class IntradayRunLog(Base):
    """
    One row per intraday_check.py invocation (12:00 ET / 15:30 ET on weekdays).

    Separate from ``signal_runner_log`` because the intraday runner has a
    different cadence (multiple per day vs. one per day) and a different scope
    (Phase 1 circuit-breaker check + Phase 3.5 trailing-stop ratchet-only by
    default; opt-in mid-day conversions gated by
    ``RiskConfig.intraday_trail_conversion_enabled``).  Never writes to
    ``signal_log`` and never regenerates signals — those stay on the daily
    cadence.

    ``status`` ∈ {'completed', 'gateway_down', 'cb_tripped', 'error'}.
    Gateway-down rows have NULL loss_pct fields because no IBKR account read
    succeeded — they exist as observability so a missed run is visible on
    Page 8 the next morning rather than vanishing silently.
    """
    __tablename__ = "intraday_run_log"

    run_id              = Column(String(36), primary_key=True)
    run_timestamp       = Column(DateTime,   nullable=False)
    mode                = Column(String(20), nullable=False)  # 'intraday' (future: 'pre-market', etc.)
    status              = Column(String(20), nullable=False)
    daily_loss_pct      = Column(Float)
    weekly_loss_pct     = Column(Float)
    cb_tripped          = Column(Integer)                     # 0/1, NULL when status='gateway_down'
    positions_flattened = Column(Integer, default=0)
    trailing_evaluated  = Column(Integer, default=0)
    trailing_ratcheted  = Column(Integer, default=0)
    trailing_converted  = Column(Integer, default=0)
    duration_seconds    = Column(Float)
    error_message       = Column(Text)


class FillLog(Base):
    """
    Raw IBKR executions ingested by Phase B reconciliation — the audit trail
    from which trade_log source='live' rows are aggregated.

    One row per IBKR Execution.  ``exec_id`` is IBKR's stable per-fill ID and
    the sole idempotency key: re-running reconciliation over an overlapping
    window can never double-write.  The only mutable columns after insert are
    ``commission`` / ``realized_pnl`` — commissionReport can arrive on a later
    fetch than the Execution itself (see upsert_fill + the commission-race note
    in execution/reconciliation.py).
    """
    __tablename__ = "fill_log"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    exec_id         = Column(String(40), nullable=False, unique=True)
    order_id        = Column(Integer)
    perm_id         = Column(Integer)
    parent_order_id = Column(Integer)
    account         = Column(String(20))
    symbol          = Column(String(10), nullable=False)
    conid           = Column(Integer)
    side            = Column(String(4),  nullable=False)   # 'BUY' | 'SELL'
    order_type      = Column(String(10))                   # 'LMT'|'STP'|'STP LMT'|'TRAIL'|'MKT'|None
    shares          = Column(Float, nullable=False)
    price           = Column(Float, nullable=False)        # avg fill price for this exec
    commission      = Column(Float)                        # may be None until commissionReport lands
    realized_pnl    = Column(Float)                        # IBKR per-fill realised P&L (cross-check only)
    exec_time       = Column(DateTime, nullable=False)     # UTC-naive
    recorded_at     = Column(DateTime, nullable=False)


class ReconciliationState(Base):
    """
    Watermark for Phase B reconciliation — one row per (source, account).

    ``last_reconciled_ts`` is the newest exec_time persisted so far; it bounds
    the next reqExecutions ExecutionFilter.time.  NULL (first run) defaults to
    now - 7d, the IBKR server-side retention horizon.
    """
    __tablename__ = "reconciliation_state"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    source             = Column(String(20), nullable=False)   # 'live'
    account            = Column(String(20))                   # None for the current single-account setup
    last_reconciled_ts = Column(DateTime)
    last_run_ts        = Column(DateTime)
    last_n_fills       = Column(Integer)
    notes              = Column(Text)

    __table_args__ = (
        UniqueConstraint("source", "account", name="uq_reconciliation_state"),
    )


class LLMNewsAnalysis(Base):
    """
    Local-LLM full-article news analysis (shadow workflow — NOT read by
    signal_runner).  One row per (symbol, article_id, model): the structured
    extraction an 8B model produced from the article body, plus the
    deterministic composite sentiment score derived from those fields.

    ``symbol`` is the IBKR feed tag the article was fetched under.
    ``primary_entity`` / ``attributed_symbol`` is the company the LLM judged the
    article to be *actually about* — frequently a different ticker (IBKR
    symbol-tagged news regularly mentions a name in passing).  Aggregation and
    the dashboard attribute the score to ``attributed_symbol``, not ``symbol``;
    mismatches are the most interesting rows (the current FinBERT path
    misattributes them).

    ``composite_score`` is the score of record (sign x magnitude x novelty —
    explainable, tunable, computed by models/llm_analyst.py:compute_composite_score).
    ``llm_direct_score`` is the model's own [-1,1] guess, stored only as a
    shadow cross-check.  Idempotency key: (symbol, article_id, model).
    """
    __tablename__ = "llm_news_analysis"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    symbol          = Column(String(10), nullable=False)   # IBKR feed-tag symbol
    article_id      = Column(String(64), nullable=False)
    model           = Column(String(40), nullable=False)   # e.g. 'llama3.1:8b'
    provider        = Column(String(20))
    published_at    = Column(DateTime)
    headline        = Column(Text)

    # ── structured extraction (what the LLM is good at) ──
    event_type      = Column(String(20))   # earnings|guidance|mgmt_change|mna|litigation|regulatory|product|analyst|macro|other
    direction       = Column(String(10))   # bullish | bearish | neutral
    magnitude       = Column(Integer)      # 1-5 materiality
    time_horizon    = Column(String(12))   # immediate | days | quarter | longterm
    novelty         = Column(Integer)      # 1-5 how new/surprising
    confidence      = Column(Integer)      # 1-5 model's self-rated confidence
    entities        = Column(Text)         # JSON list of names mentioned
    primary_entity  = Column(String(80))   # the name the article is *about*
    attributed_symbol = Column(String(10)) # ticker resolved from primary_entity (may differ from symbol)
    summary         = Column(Text)         # one-sentence LLM summary
    rationale       = Column(Text)         # short "why this score" explanation for the dashboard

    # ── scores ──
    composite_score = Column(Float)        # [-1,1] derived deterministically — score of record
    llm_direct_score = Column(Float)       # [-1,1] model's own guess — shadow cross-check only

    # ── telemetry (for the dashboard + cost tracking) ──
    raw_response    = Column(Text)         # full model JSON (audit / re-parse)
    prompt_tokens   = Column(Integer)
    output_tokens   = Column(Integer)
    duration_ms     = Column(Integer)
    parse_ok        = Column(Boolean, default=True)  # False = JSON parse failed; row kept for visibility

    scored_at       = Column(DateTime, nullable=False)
    recorded_at     = Column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("symbol", "article_id", "model", name="uq_llm_news_analysis"),
    )


# ── Target allocation (the new risk-premia system) ────────────────────────────

class TargetAllocation(Base):
    """Desired portfolio weights — the rebalancer's source of truth.

    Holds the fixed ETF core and the dynamic stock satellite (quality-value +
    conviction big-bets).  ``sleeve`` is one of 'core', 'satellite_qv',
    'satellite_bigbet'.  For core / satellite_qv, ``target_weight`` is the
    rebalance *target* (fraction of NLV); for satellite_bigbet it is the *entry
    cap* — the rebalancer never drift-trades big-bets (see
    docs/strategy/risk_premia_harvesting.md §4 and portfolio/allocation.py).
    ``active=False`` rows are history (e.g. a replaced satellite name); at most
    one active row per ticker.
    """
    __tablename__ = "target_allocation"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    ticker        = Column(String(12), nullable=False)
    sleeve        = Column(String(20), nullable=False)
    target_weight = Column(Float, nullable=False)
    label         = Column(String(80))
    active        = Column(Boolean, nullable=False, default=True)
    updated_at    = Column(DateTime, nullable=False)


# ── Engine (lazy singleton) ───────────────────────────────────────────────────

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        db_path = Path(config.data.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(f"sqlite:///{db_path}", echo=False)
        Base.metadata.create_all(_engine)
        _migrate(_engine)
        log.info("Database ready at %s", db_path.resolve())
    return _engine


def _migrate(engine) -> None:
    """Apply additive schema migrations to existing databases.

    SQLite does not support DROP COLUMN or type changes, so only
    ADD COLUMN migrations are needed here.  Each migration is idempotent:
    it checks the current column list before running ALTER TABLE.
    """
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    with engine.connect() as conn:
        # walk_forward_results.sentiment_note  (added for FinBERT-suppression notes)
        wf_cols = {c["name"] for c in insp.get_columns("walk_forward_results")}
        if "sentiment_note" not in wf_cols:
            conn.execute(text(
                "ALTER TABLE walk_forward_results ADD COLUMN sentiment_note TEXT"
            ))
            conn.commit()
            log.info("Migration applied: walk_forward_results.sentiment_note")

        # ensemble_weight_history.symbol + run_id  (2026-05-14 — Page 4 chart needs
        # per-symbol filtering; pre-migration rows backfill as NULL).
        if "ensemble_weight_history" in insp.get_table_names():
            ewh_cols = {c["name"] for c in insp.get_columns("ensemble_weight_history")}
            if "symbol" not in ewh_cols:
                conn.execute(text(
                    "ALTER TABLE ensemble_weight_history ADD COLUMN symbol VARCHAR(10)"
                ))
                conn.commit()
                log.info("Migration applied: ensemble_weight_history.symbol")
            if "run_id" not in ewh_cols:
                conn.execute(text(
                    "ALTER TABLE ensemble_weight_history ADD COLUMN run_id VARCHAR(36)"
                ))
                conn.commit()
                log.info("Migration applied: ensemble_weight_history.run_id")

        # walk_forward_results.universe_policy  (2026-05-12 — survivorship-bias
        # flag: "dynamic" rows came from a UniverseSelector-driven run, "static"
        # rows came from the watchlist).  Existing rows backfill as NULL since
        # we don't know retroactively which policy produced them.
        if "universe_policy" not in wf_cols:
            conn.execute(text(
                "ALTER TABLE walk_forward_results ADD COLUMN universe_policy VARCHAR(20)"
            ))
            conn.commit()
            log.info("Migration applied: walk_forward_results.universe_policy")

        # universe_assets.exchange  (added to support exchange-based ADR filtering)
        if "universe_assets" in insp.get_table_names():
            ua_cols = {c["name"] for c in insp.get_columns("universe_assets")}
            if "exchange" not in ua_cols:
                conn.execute(text(
                    "ALTER TABLE universe_assets ADD COLUMN exchange VARCHAR(20)"
                ))
                conn.commit()
                log.info("Migration applied: universe_assets.exchange")
            # universe_assets.xgb_score → stage3_score (Stage 3 ranker switched
            # from per-symbol XGBoost hijack to momentum + ADV rank-percentile)
            if "xgb_score" in ua_cols and "stage3_score" not in ua_cols:
                conn.execute(text(
                    "ALTER TABLE universe_assets RENAME COLUMN xgb_score TO stage3_score"
                ))
                conn.commit()
                log.info("Migration applied: universe_assets.xgb_score -> stage3_score")

        # signal_runner_log.skipped_duplicates + longs_closed  (added for GOOG/GOOGL dedup
        # and long-only SELL tracking)
        if "signal_runner_log" in insp.get_table_names():
            srl_cols = {c["name"] for c in insp.get_columns("signal_runner_log")}
            if "skipped_duplicates" not in srl_cols:
                conn.execute(text(
                    "ALTER TABLE signal_runner_log ADD COLUMN skipped_duplicates INTEGER DEFAULT 0"
                ))
                conn.commit()
                log.info("Migration applied: signal_runner_log.skipped_duplicates")
            if "longs_closed" not in srl_cols:
                conn.execute(text(
                    "ALTER TABLE signal_runner_log ADD COLUMN longs_closed INTEGER DEFAULT 0"
                ))
                conn.commit()
                log.info("Migration applied: signal_runner_log.longs_closed")
            if "trailing_conversions" not in srl_cols:
                conn.execute(text(
                    "ALTER TABLE signal_runner_log ADD COLUMN trailing_conversions INTEGER DEFAULT 0"
                ))
                conn.commit()
                log.info("Migration applied: signal_runner_log.trailing_conversions")
            if "skipped_stale" not in srl_cols:
                conn.execute(text(
                    "ALTER TABLE signal_runner_log ADD COLUMN skipped_stale INTEGER DEFAULT 0"
                ))
                conn.commit()
                log.info("Migration applied: signal_runner_log.skipped_stale")
            if "hold_timeouts" not in srl_cols:
                conn.execute(text(
                    "ALTER TABLE signal_runner_log ADD COLUMN hold_timeouts INTEGER DEFAULT 0"
                ))
                conn.commit()
                log.info("Migration applied: signal_runner_log.hold_timeouts")
            if "skipped_pending_orders" not in srl_cols:
                conn.execute(text(
                    "ALTER TABLE signal_runner_log ADD COLUMN skipped_pending_orders INTEGER DEFAULT 0"
                ))
                conn.commit()
                log.info("Migration applied: signal_runner_log.skipped_pending_orders")

        # trade_log.benchmark_return_pct  (2026-05-19 — benchmark-relative
        # performance tracking on Page 10).  Raw (cost-unadjusted) benchmark
        # price return over the trade's holding period.  NULL for pre-migration
        # rows; populated by scripts/backfill_benchmark_returns.py.
        if "trade_log" in insp.get_table_names():
            tl_cols = {c["name"] for c in insp.get_columns("trade_log")}
            if "benchmark_return_pct" not in tl_cols:
                conn.execute(text(
                    "ALTER TABLE trade_log ADD COLUMN benchmark_return_pct FLOAT"
                ))
                conn.commit()
                log.info("Migration applied: trade_log.benchmark_return_pct")

            # trade_log live-fill linkage (Phase B — 2026-05-29).  Links an
            # aggregated source='live' trade back to the IBKR executions it was
            # built from.  walk_forward rows leave these NULL.
            for col, coltype in (
                ("entry_exec_id",   "VARCHAR(40)"),
                ("exit_exec_id",    "VARCHAR(40)"),
                ("parent_order_id", "INTEGER"),
                ("account",         "VARCHAR(20)"),
            ):
                if col not in tl_cols:
                    conn.execute(text(
                        f"ALTER TABLE trade_log ADD COLUMN {col} {coltype}"
                    ))
                    conn.commit()
                    log.info("Migration applied: trade_log.%s", col)

            # Partial unique index on the closing exec_id — the per-round-trip
            # dedup key for live reconciliation.  Scoped to source='live' so
            # walk_forward rows (NULL exit_exec_id) are excluded entirely.
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_live_exit "
                "ON trade_log(exit_exec_id) WHERE source='live'"
            ))
            conn.commit()

        # news_cache.body  (LLM news analyst — full article text, HTML-stripped,
        # ingested by scripts/ingest_news_bodies.py).  NULL until ingested; the
        # scoring pass only touches rows where body IS NOT NULL.
        if "news_cache" in insp.get_table_names():
            nc_cols = {c["name"] for c in insp.get_columns("news_cache")}
            if "body" not in nc_cols:
                conn.execute(text("ALTER TABLE news_cache ADD COLUMN body TEXT"))
                conn.commit()
                log.info("Migration applied: news_cache.body")

        # fundamental_data: drop UNIQUE(symbol) so the table can hold append-only
        # snapshot history (needed for derived features like analyst-target
        # revisions). SQLite cannot drop a column constraint in place — rebuild
        # the table once, preserving existing rows. Detect via the original
        # CREATE TABLE DDL (auto-generated UNIQUE indexes have sql=NULL, so
        # we can't rely on sqlite_master.indexes; check the table DDL instead).
        if "fundamental_data" in insp.get_table_names():
            ddl = conn.execute(text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='fundamental_data'"
            )).scalar() or ""
            if "UNIQUE" in ddl.upper() or "sqlite_autoindex_fundamental_data" in {
                row[0] for row in conn.execute(text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND tbl_name='fundamental_data' AND sql IS NULL"
                )).fetchall()
            }:
                log.info("Migration applied: fundamental_data -> append-only (rebuilding table)")
                conn.execute(text("""
                    CREATE TABLE fundamental_data_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol VARCHAR(10) NOT NULL,
                        fetched_at DATETIME NOT NULL,
                        market_cap FLOAT,
                        pe_ratio FLOAT,
                        forward_pe FLOAT,
                        price_to_book FLOAT,
                        ev_to_ebitda FLOAT,
                        revenue_growth FLOAT,
                        earnings_growth FLOAT,
                        profit_margin FLOAT,
                        roe FLOAT,
                        debt_to_equity FLOAT,
                        current_ratio FLOAT,
                        free_cashflow FLOAT,
                        analyst_target FLOAT
                    )
                """))
                conn.execute(text("""
                    INSERT INTO fundamental_data_new
                    SELECT id, symbol, fetched_at, market_cap, pe_ratio, forward_pe,
                           price_to_book, ev_to_ebitda, revenue_growth, earnings_growth,
                           profit_margin, roe, debt_to_equity, current_ratio,
                           free_cashflow, analyst_target
                    FROM fundamental_data
                """))
                conn.execute(text("DROP TABLE fundamental_data"))
                conn.execute(text("ALTER TABLE fundamental_data_new RENAME TO fundamental_data"))
                conn.execute(text(
                    "CREATE INDEX ix_fundamental_data_symbol ON fundamental_data (symbol)"
                ))
                conn.commit()

            # fundamental_data.sector  (data-driven sector classification —
            # raw yfinance GICS label captured at fetch time; normalised to the
            # project scheme at read time in risk/portfolio_guard.py:get_sector
            # so the weekly-rotating universe no longer calcifies the hardcoded
            # _SECTOR_MAP).  NULL until the next fundamentals fetch back-fills it.
            fd_cols = {
                row[1] for row in conn.execute(
                    text("PRAGMA table_info('fundamental_data')")
                ).fetchall()
            }
            if "sector" not in fd_cols:
                conn.execute(text(
                    "ALTER TABLE fundamental_data ADD COLUMN sector VARCHAR(40)"
                ))
                conn.commit()
                log.info("Migration applied: fundamental_data.sector")


# ── OHLCV helpers ─────────────────────────────────────────────────────────────

def upsert_bars(
    df: pd.DataFrame,
    symbol: str,
    interval: str,
    overwrite: bool = False,
) -> int:
    """
    Insert rows from a yfinance-style DataFrame (Open/High/Low/Close/Volume,
    DatetimeIndex).  Returns the count of rows inserted OR updated.

    Default (overwrite=False): skips rows that already exist — the steady-state
    incremental-fetch path used by run_pipeline.py / signal_runner.py / fetcher.

    overwrite=True: updates OHLCV in place for any (symbol, interval, timestamp)
    row that already exists.  Used by refresh_recent_bars.py to replace mid-day
    partial bars with the final post-close values once yfinance has them.
    """
    if df.empty:
        return 0

    # Normalise to plain Python datetimes once, deduping on timestamp (last wins).
    rows_by_ts = {
        (ts.to_pydatetime().replace(tzinfo=None) if hasattr(ts, "to_pydatetime") else ts): row
        for ts, row in df.iterrows()
    }

    engine = get_engine()
    affected = 0

    with Session(engine) as session:
        # One range query for every existing row instead of a SELECT per bar.
        existing = {
            r.timestamp: r
            for r in session.query(OHLCVBar).filter(
                OHLCVBar.symbol == symbol,
                OHLCVBar.interval == interval,
                OHLCVBar.timestamp >= min(rows_by_ts),
                OHLCVBar.timestamp <= max(rows_by_ts),
            ).all()
        }

        new_objs = []
        for ts_dt, row in rows_by_ts.items():
            exists = existing.get(ts_dt)
            if exists is None:
                new_objs.append(OHLCVBar(
                    symbol=symbol,
                    interval=interval,
                    timestamp=ts_dt,
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(row["Volume"]),
                ))
                affected += 1
            elif overwrite:
                exists.open   = float(row["Open"])
                exists.high   = float(row["High"])
                exists.low    = float(row["Low"])
                exists.close  = float(row["Close"])
                exists.volume = float(row["Volume"])
                affected += 1

        if new_objs:
            session.add_all(new_objs)
        session.commit()

    return affected


def get_bars(symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
    """
    Return the most recent `limit` bars for (symbol, interval) as a
    DataFrame with a UTC-naive DatetimeIndex named 'timestamp'.
    Returns an empty DataFrame if no data is stored.
    """
    engine = get_engine()

    with Session(engine) as session:
        rows = (
            session.query(OHLCVBar)
            .filter_by(symbol=symbol, interval=interval)
            .order_by(desc(OHLCVBar.timestamp))
            .limit(limit)
            .all()
        )

    if not rows:
        return pd.DataFrame()

    data = [
        {
            "timestamp": r.timestamp,
            "Open":   r.open,
            "High":   r.high,
            "Low":    r.low,
            "Close":  r.close,
            "Volume": r.volume,
        }
        for r in reversed(rows)   # ascending chronological order
    ]
    df = pd.DataFrame(data).set_index("timestamp")
    df.index = pd.DatetimeIndex(df.index)
    return df


# ── Indicator helpers ─────────────────────────────────────────────────────────

_INDICATOR_COLS = [
    "rsi_14", "macd", "macd_signal", "macd_hist",
    "bb_upper", "bb_middle", "bb_lower",
    "ema_9", "ema_21", "ema_50",
    "atr_14", "volume_sma_20",
]


def upsert_indicators(
    df: pd.DataFrame,
    symbol: str,
    interval: str,
    overwrite: bool = False,
) -> int:
    """
    Persist indicator columns from `df` (must share the same DatetimeIndex
    as the corresponding OHLCV bars).  Returns the count of rows inserted
    OR updated.

    Default (overwrite=False): skips rows that already exist.

    overwrite=True: updates the indicator columns in place — used when the
    underlying OHLCV bars have been refreshed (refresh_recent_bars.py) and
    the derived indicators need to track the corrected price data.
    """
    if df.empty:
        return 0

    rows_by_ts = {
        (ts.to_pydatetime().replace(tzinfo=None) if hasattr(ts, "to_pydatetime") else ts): row
        for ts, row in df.iterrows()
    }

    engine = get_engine()
    affected = 0

    with Session(engine) as session:
        # One range query for every existing row instead of a SELECT per bar.
        existing = {
            r.timestamp: r
            for r in session.query(IndicatorSnapshot).filter(
                IndicatorSnapshot.symbol == symbol,
                IndicatorSnapshot.interval == interval,
                IndicatorSnapshot.timestamp >= min(rows_by_ts),
                IndicatorSnapshot.timestamp <= max(rows_by_ts),
            ).all()
        }

        new_objs = []
        for ts_dt, row in rows_by_ts.items():
            exists = existing.get(ts_dt)
            kwargs = {
                col: (None if pd.isna(row.get(col)) else float(row[col]))
                for col in _INDICATOR_COLS
                if col in df.columns
            }
            if exists is None:
                new_objs.append(IndicatorSnapshot(
                    symbol=symbol, interval=interval, timestamp=ts_dt, **kwargs
                ))
                affected += 1
            elif overwrite:
                for col, val in kwargs.items():
                    setattr(exists, col, val)
                affected += 1

        if new_objs:
            session.add_all(new_objs)
        session.commit()

    return affected


def get_latest_indicators(symbol: str, interval: str) -> dict | None:
    """
    Return the most recent IndicatorSnapshot for (symbol, interval) as a
    plain dict, or None if none exists.
    """
    engine = get_engine()

    with Session(engine) as session:
        row = (
            session.query(IndicatorSnapshot)
            .filter_by(symbol=symbol, interval=interval)
            .order_by(desc(IndicatorSnapshot.timestamp))
            .first()
        )

    if not row:
        return None

    return {
        "timestamp":    row.timestamp,
        "rsi_14":       row.rsi_14,
        "macd":         row.macd,
        "macd_signal":  row.macd_signal,
        "macd_hist":    row.macd_hist,
        "bb_upper":     row.bb_upper,
        "bb_middle":    row.bb_middle,
        "bb_lower":     row.bb_lower,
        "ema_9":        row.ema_9,
        "ema_21":       row.ema_21,
        "ema_50":       row.ema_50,
        "atr_14":       row.atr_14,
        "volume_sma_20": row.volume_sma_20,
    }


def get_indicators_history(
    symbol: str,
    interval: str,
    start=None,
    end=None,
    limit: int = 1000,
) -> pd.DataFrame:
    """
    Return a time series of IndicatorSnapshot rows for (symbol, interval) as a
    DataFrame with a UTC-naive DatetimeIndex named 'timestamp' (ascending).

    The historical analogue of :func:`get_latest_indicators` — used by the
    Trade Forensics panel to overlay MACD/RSI/BB across a trade's holding
    window.  Fetches the most recent `limit` rows, then trims to [start, end]
    in-memory (same pattern as :func:`get_bars` / ui_queries.query_bars).
    Returns an empty DataFrame when nothing is stored.
    """
    engine = get_engine()
    with Session(engine) as session:
        rows = (
            session.query(IndicatorSnapshot)
            .filter_by(symbol=symbol, interval=interval)
            .order_by(desc(IndicatorSnapshot.timestamp))
            .limit(limit)
            .all()
        )

    if not rows:
        return pd.DataFrame()

    data = [
        {
            "timestamp":    r.timestamp,
            "rsi_14":       r.rsi_14,
            "macd":         r.macd,
            "macd_signal":  r.macd_signal,
            "macd_hist":    r.macd_hist,
            "bb_upper":     r.bb_upper,
            "bb_middle":    r.bb_middle,
            "bb_lower":     r.bb_lower,
            "ema_9":        r.ema_9,
            "ema_21":       r.ema_21,
            "ema_50":       r.ema_50,
            "atr_14":       r.atr_14,
            "volume_sma_20": r.volume_sma_20,
        }
        for r in reversed(rows)   # ascending chronological order
    ]
    df = pd.DataFrame(data).set_index("timestamp")
    df.index = pd.DatetimeIndex(df.index)
    if start is not None:
        df = df[df.index >= pd.Timestamp(start)]
    if end is not None:
        df = df[df.index < pd.Timestamp(end) + pd.Timedelta(days=1)]
    return df


# ── Fundamental helpers ───────────────────────────────────────────────────────

def upsert_fundamentals(symbol: str, data: dict) -> None:
    """Append a fundamental snapshot row for `symbol`.

    Always inserts a new row (the table is append-only history). The 24h
    cache in ``FundamentalsClient.get`` is what prevents same-day duplicates.
    Function name kept for caller compatibility; behaviour is now insert-only.
    """
    engine = get_engine()
    with Session(engine) as session:
        row = FundamentalData(symbol=symbol)
        for k, v in data.items():
            if hasattr(row, k):
                setattr(row, k, v)
        session.add(row)
        session.commit()


def get_fundamentals(symbol: str) -> dict | None:
    """Return the latest stored fundamental snapshot for `symbol`, or None."""
    engine = get_engine()
    with Session(engine) as session:
        row = (
            session.query(FundamentalData)
            .filter_by(symbol=symbol)
            .order_by(FundamentalData.fetched_at.desc())
            .first()
        )
    if not row:
        return None
    return {c.name: getattr(row, c.name) for c in FundamentalData.__table__.columns}


def get_latest_sector(symbol: str) -> str | None:
    """Return the latest non-NULL raw yfinance sector label for `symbol`, or None.

    Reads from the append-only fundamental_data history (newest first).  The
    raw GICS label is normalised to the project's simplified scheme by the
    caller (risk/portfolio_guard.py:get_sector).  Returns None when the symbol
    has no fundamentals row yet, or every row predates the sector back-fill.
    """
    engine = get_engine()
    with Session(engine) as session:
        row = (
            session.query(FundamentalData.sector)
            .filter(FundamentalData.symbol == symbol)
            .filter(FundamentalData.sector.isnot(None))
            .order_by(FundamentalData.fetched_at.desc())
            .first()
        )
    return row[0] if row else None


def get_fundamentals_history(symbol: str, limit: int | None = None) -> list[dict]:
    """Return all stored fundamental snapshots for `symbol`, newest first."""
    engine = get_engine()
    with Session(engine) as session:
        q = (
            session.query(FundamentalData)
            .filter_by(symbol=symbol)
            .order_by(FundamentalData.fetched_at.desc())
        )
        if limit is not None:
            q = q.limit(limit)
        rows = q.all()
    return [
        {c.name: getattr(r, c.name) for c in FundamentalData.__table__.columns}
        for r in rows
    ]


# ── News / sentiment helpers ──────────────────────────────────────────────────

def upsert_news(symbol: str, article_id: str, published_at: datetime,
                headline: str, sentiment_score: float | None) -> bool:
    """
    Insert or update a news article.

    - If the row does not exist: insert it.  Returns True.
    - If the row exists and sentiment_score was None: update the score.  Returns False.
    - If the row exists and already has a score: leave it unchanged.  Returns False.
    """
    engine = get_engine()
    with Session(engine) as session:
        row = session.query(NewsCache).filter_by(
            symbol=symbol, article_id=article_id
        ).first()
        if row is None:
            session.add(NewsCache(
                symbol=symbol,
                article_id=article_id,
                published_at=published_at,
                headline=headline,
                sentiment_score=sentiment_score,
            ))
            session.commit()
            return True
        # Update score only when a real score is now available
        if row.sentiment_score is None and sentiment_score is not None:
            row.sentiment_score = sentiment_score
            session.commit()
    return False


def get_recent_news(symbol: str, since: datetime) -> list[dict]:
    """Return news articles for `symbol` published on or after `since`."""
    engine = get_engine()
    with Session(engine) as session:
        rows = (
            session.query(NewsCache)
            .filter(NewsCache.symbol == symbol, NewsCache.published_at >= since)
            .order_by(desc(NewsCache.published_at))
            .all()
        )
    return [
        {
            "article_id":      r.article_id,
            "published_at":    r.published_at,
            "headline":        r.headline,
            "sentiment_score": r.sentiment_score,
        }
        for r in rows
    ]


# ── LLM news analyst helpers (shadow workflow) ────────────────────────────────

def set_news_body(symbol: str, article_id: str, body: str) -> bool:
    """Fill ``news_cache.body`` for one article when currently NULL.

    Returns True if a body was written, False if the row is missing or already
    has a body (idempotent — re-running ingestion never overwrites)."""
    engine = get_engine()
    with Session(engine) as session:
        row = session.query(NewsCache).filter_by(
            symbol=symbol, article_id=article_id
        ).first()
        if row is None:
            return False
        if row.body is None and body:
            row.body = body
            session.commit()
            return True
    return False


def get_news_body(symbol: str, article_id: str) -> str | None:
    """Return the stored full article text for one article, or None."""
    engine = get_engine()
    with Session(engine) as session:
        row = session.query(NewsCache.body).filter_by(
            symbol=symbol, article_id=article_id
        ).first()
    return row[0] if row else None


def get_news_needing_body(symbols: list[str] | None, since: datetime) -> list[dict]:
    """News rows (optionally restricted to ``symbols``) since ``since`` whose
    body is still NULL — the work list for scripts/ingest_news_bodies.py."""
    engine = get_engine()
    with Session(engine) as session:
        q = session.query(NewsCache).filter(
            NewsCache.published_at >= since,
            NewsCache.body.is_(None),
        )
        if symbols:
            q = q.filter(NewsCache.symbol.in_(symbols))
        rows = q.order_by(desc(NewsCache.published_at)).all()
    return [
        {
            "symbol":       r.symbol,
            "article_id":   r.article_id,
            "headline":     r.headline,
            "published_at": r.published_at,
        }
        for r in rows
    ]


def get_news_for_scoring(
    symbols: list[str] | None,
    since: datetime,
    model: str,
    min_chars: int = 0,
) -> list[dict]:
    """Articles with a stored body that have NOT yet been scored by ``model``.

    Idempotency: filtered against ``llm_news_analysis`` on
    (symbol, article_id, model), so re-running the scorer only picks up new
    articles.  ``min_chars`` drops sub-threshold stub bodies."""
    engine = get_engine()
    with Session(engine) as session:
        already = session.query(LLMNewsAnalysis.id).filter(
            LLMNewsAnalysis.symbol == NewsCache.symbol,
            LLMNewsAnalysis.article_id == NewsCache.article_id,
            LLMNewsAnalysis.model == model,
        )
        q = session.query(NewsCache).filter(
            NewsCache.published_at >= since,
            NewsCache.body.isnot(None),
            ~already.exists(),
        )
        if symbols:
            q = q.filter(NewsCache.symbol.in_(symbols))
        rows = q.order_by(desc(NewsCache.published_at)).all()

    result = []
    for r in rows:
        if min_chars and (not r.body or len(r.body) < min_chars):
            continue
        provider = r.article_id.split("$", 1)[0] if "$" in r.article_id else None
        result.append({
            "symbol":       r.symbol,
            "article_id":   r.article_id,
            "provider":     provider,
            "headline":     r.headline,
            "published_at": r.published_at,
            "body":         r.body,
        })
    return result


def upsert_llm_analysis(record: dict) -> bool:
    """Insert one LLM analysis row, keyed (symbol, article_id, model).

    Returns True on insert, False if a row for that key already exists
    (idempotent — re-scoring the same article with the same model is a no-op).
    ``entities`` is expected pre-serialised to a JSON string by the caller."""
    engine = get_engine()
    now = datetime.utcnow()
    with Session(engine) as session:
        exists_row = session.query(LLMNewsAnalysis).filter_by(
            symbol=record["symbol"],
            article_id=record["article_id"],
            model=record["model"],
        ).first()
        if exists_row is not None:
            return False
        rec = dict(record)
        rec.setdefault("scored_at", now)
        rec.setdefault("recorded_at", now)
        session.add(LLMNewsAnalysis(**rec))
        session.commit()
    return True


def get_llm_analysis(
    symbols: list[str] | None = None,
    since: datetime | None = None,
    model: str | None = None,
    attributed: bool = False,
    limit: int | None = None,
) -> pd.DataFrame:
    """Read llm_news_analysis as a DataFrame (newest first) for the dashboard.

    ``attributed=True`` filters on ``attributed_symbol`` (the ticker the article
    is *about*) instead of the feed-tag ``symbol`` — the honest per-ticker view."""
    engine = get_engine()
    with Session(engine) as session:
        q = session.query(LLMNewsAnalysis)
        if since is not None:
            q = q.filter(LLMNewsAnalysis.published_at >= since)
        if model:
            q = q.filter(LLMNewsAnalysis.model == model)
        if symbols:
            col = LLMNewsAnalysis.attributed_symbol if attributed else LLMNewsAnalysis.symbol
            q = q.filter(col.in_(symbols))
        q = q.order_by(desc(LLMNewsAnalysis.published_at))
        if limit:
            q = q.limit(limit)
        rows = q.all()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([{
        "symbol":            r.symbol,
        "attributed_symbol": r.attributed_symbol,
        "primary_entity":    r.primary_entity,
        "article_id":        r.article_id,
        "model":             r.model,
        "provider":          r.provider,
        "published_at":      r.published_at,
        "headline":          r.headline,
        "event_type":        r.event_type,
        "direction":         r.direction,
        "magnitude":         r.magnitude,
        "time_horizon":      r.time_horizon,
        "novelty":           r.novelty,
        "confidence":        r.confidence,
        "entities":          r.entities,
        "summary":           r.summary,
        "rationale":         r.rationale,
        "composite_score":   r.composite_score,
        "llm_direct_score":  r.llm_direct_score,
        "parse_ok":          r.parse_ok,
        "prompt_tokens":     r.prompt_tokens,
        "output_tokens":     r.output_tokens,
        "duration_ms":       r.duration_ms,
        "scored_at":         r.scored_at,
    } for r in rows])


def build_company_name_map() -> dict:
    """{TICKER: [name variants]} from universe_assets.name + watchlist tickers.

    Used by the LLM analyst's attribution resolver to map a primary_entity
    (company name) back to a ticker.  Each ticker is always a variant of itself
    (so a bare-ticker primary_entity resolves)."""
    engine = get_engine()
    out: dict = {}
    with Session(engine) as session:
        for sym, name in session.query(UniverseAsset.symbol, UniverseAsset.name).all():
            if not sym:
                continue
            variants = [sym]
            if name:
                variants.append(name)
            out[sym.upper()] = variants
    for s in config.data.watchlist:
        out.setdefault(s.upper(), [s])
    return out


def get_earliest_news_date(symbol: str | None = None) -> datetime | None:
    """Return the oldest ``published_at`` in ``news_cache``, or ``None`` if empty.

    Scoped to ``symbol`` when provided; otherwise across all symbols. Used by
    the walk-forward orchestrator to derive the ``news_available_from`` cutoff
    directly from the data instead of a hardcoded config date.
    """
    engine = get_engine()
    with Session(engine) as session:
        q = session.query(func.min(NewsCache.published_at))
        if symbol is not None:
            q = q.filter(NewsCache.symbol == symbol)
        result = q.scalar()
    return result


# ── Signal log helpers ────────────────────────────────────────────────────────

def log_signal(record: dict) -> None:
    """Append a signal record to signal_log.  Silently ignores errors."""
    try:
        engine = get_engine()
        with Session(engine) as session:
            session.add(SignalLog(**record))
            session.commit()
    except Exception:
        pass


def get_latest_buy_signal_ts(symbol: str) -> datetime | None:
    """
    Return the most recent `bar_timestamp` from signal_log where the symbol
    fired a BUY signal that passed the gate.  Used by the Phase 3.6 hold-timeout
    rule to decide whether the model still re-confirms a held position.

    Returns None if the symbol has no qualifying signal_log rows — caller
    treats this as "no anchor for staleness" and skips the position rather
    than flattening a manual / pre-history holding.
    """
    try:
        engine = get_engine()
        with Session(engine) as session:
            row = (
                session.query(SignalLog.bar_timestamp)
                .filter(SignalLog.symbol == symbol)
                .filter(SignalLog.signal == "BUY")
                .filter(SignalLog.passed_gate == True)  # noqa: E712 (SQLAlchemy)
                .order_by(SignalLog.bar_timestamp.desc())
                .first()
            )
            return row[0] if row else None
    except Exception:
        return None


# ── Ensemble weight helpers ───────────────────────────────────────────────────

def log_ensemble_weights(lstm: float, xgb: float, finbert: float,
                         trigger: str = "rebalance",
                         symbol: str | None = None,
                         run_id: str | None = None) -> None:
    """Persist current ensemble weights."""
    from datetime import timezone
    engine = get_engine()
    with Session(engine) as session:
        session.add(EnsembleWeightHistory(
            recorded_at=datetime.now(timezone.utc).replace(tzinfo=None),
            symbol=symbol,
            run_id=run_id,
            lstm_weight=lstm,
            xgb_weight=xgb,
            finbert_weight=finbert,
            trigger=trigger,
        ))
        session.commit()


# ── Walk-forward result helpers ───────────────────────────────────────────────

def log_walk_forward_result(record: dict) -> None:
    """Persist one fold's walk-forward metrics."""
    engine = get_engine()
    with Session(engine) as session:
        session.add(WalkForwardResult(**record))
        session.commit()


# ── Universe helpers ──────────────────────────────────────────────────────────

def upsert_universe_asset(asset_dict: dict) -> None:
    """
    Insert or update a universe asset record keyed by symbol.

    Preserves `added_at` from the existing row when updating.
    Pandas NaT / NaN values are coerced to None — callers that round-trip
    through `df.to_dict("records")` will otherwise hand SQLAlchemy a NaT
    that the SQLite DateTime processor can't serialize.
    """
    clean = {
        k: (None if isinstance(v, float) and pd.isna(v)
            else None if v is pd.NaT
            else v)
        for k, v in asset_dict.items()
    }
    engine = get_engine()
    with Session(engine) as session:
        row = session.query(UniverseAsset).filter_by(
            symbol=clean["symbol"]
        ).first()
        if row is None:
            session.add(UniverseAsset(**clean))
        else:
            for k, v in clean.items():
                if k == "added_at":
                    continue   # never overwrite the original insertion date
                if hasattr(row, k):
                    setattr(row, k, v)
        session.commit()


def get_universe_assets(active_only: bool = True) -> pd.DataFrame:
    """Return universe_assets as a DataFrame (all columns).  Empty DF if none."""
    engine = get_engine()
    with Session(engine) as session:
        q = session.query(UniverseAsset)
        if active_only:
            q = q.filter(UniverseAsset.active == True)  # noqa: E712
        rows = q.order_by(UniverseAsset.symbol).all()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([{
        "symbol":            r.symbol,
        "name":              r.name,
        "asset_class":       r.asset_class,
        "exchange":          r.exchange,
        "is_fixture":        r.is_fixture,
        "stage":             r.stage,
        "market_cap":        r.market_cap,
        "avg_dollar_volume": r.avg_dollar_volume,
        "stage3_score":      r.stage3_score,
        "active":            r.active,
        "added_at":          r.added_at,
        "last_scored_at":    r.last_scored_at,
        "removed_at":        r.removed_at,
    } for r in rows])


def log_universe_run(record: dict) -> None:
    """Append one stage entry to universe_run_log."""
    engine = get_engine()
    with Session(engine) as session:
        session.add(UniverseRunLog(**record))
        session.commit()


def get_universe_run_log(limit: int = 100) -> pd.DataFrame:
    """Return the most recent `limit` universe_run_log entries, newest first."""
    engine = get_engine()
    with Session(engine) as session:
        rows = (
            session.query(UniverseRunLog)
            .order_by(desc(UniverseRunLog.recorded_at))
            .limit(limit)
            .all()
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([{
        "id":               r.id,
        "run_id":           r.run_id,
        "run_type":         r.run_type,
        "stage":            r.stage,
        "symbol_count":     r.symbol_count,
        "duration_seconds": r.duration_seconds,
        "recorded_at":      r.recorded_at,
        "notes":            r.notes,
    } for r in rows])


# ── Circuit breaker helpers ────────────────────────────────────────────────────

def log_circuit_breaker_event(record: dict) -> None:
    """Append a circuit-breaker event (TRIGGERED / RESET / AUTO_RESET)."""
    engine = get_engine()
    with Session(engine) as session:
        session.add(CircuitBreakerLog(**record))
        session.commit()


def get_latest_circuit_breaker_event() -> dict | None:
    """Return the most recent circuit_breaker_log row, or None."""
    engine = get_engine()
    with Session(engine) as session:
        row = (
            session.query(CircuitBreakerLog)
            .order_by(desc(CircuitBreakerLog.recorded_at))
            .first()
        )
    if not row:
        return None
    return {
        "id":              row.id,
        "event":           row.event,
        "reason":          row.reason,
        "daily_loss_pct":  row.daily_loss_pct,
        "weekly_loss_pct": row.weekly_loss_pct,
        "triggered_at":    row.triggered_at,
        "reset_at":        row.reset_at,
        "recorded_at":     row.recorded_at,
    }


def get_circuit_breaker_log(limit: int = 50) -> pd.DataFrame:
    """Return recent circuit_breaker_log entries, newest first."""
    engine = get_engine()
    with Session(engine) as session:
        rows = (
            session.query(CircuitBreakerLog)
            .order_by(desc(CircuitBreakerLog.recorded_at))
            .limit(limit)
            .all()
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([{
        "event":           r.event,
        "reason":          r.reason,
        "daily_loss_pct":  r.daily_loss_pct,
        "weekly_loss_pct": r.weekly_loss_pct,
        "triggered_at":    r.triggered_at,
        "reset_at":        r.reset_at,
        "recorded_at":     r.recorded_at,
    } for r in rows])


# ── Equity snapshot helpers ────────────────────────────────────────────────────

def log_equity_snapshot(record: dict) -> None:
    """
    Upsert a daily equity snapshot keyed by ``snapshot_date`` (YYYY-MM-DD).

    Re-running signal_runner.py on the same day overwrites the existing row so
    the circuit-breaker baseline always reflects the latest NLV read for that
    date.
    """
    engine = get_engine()
    with Session(engine) as session:
        existing = (
            session.query(EquitySnapshot)
            .filter_by(snapshot_date=record["snapshot_date"])
            .first()
        )
        if existing is None:
            session.add(EquitySnapshot(**record))
        else:
            for k, v in record.items():
                setattr(existing, k, v)
        session.commit()


def get_equity_snapshot_on_or_before(snapshot_date: str) -> dict | None:
    """
    Return the most recent snapshot whose ``snapshot_date`` is <= the given
    date string (YYYY-MM-DD), or None if no snapshot exists.
    """
    engine = get_engine()
    with Session(engine) as session:
        row = (
            session.query(EquitySnapshot)
            .filter(EquitySnapshot.snapshot_date <= snapshot_date)
            .order_by(desc(EquitySnapshot.snapshot_date))
            .first()
        )
    if not row:
        return None
    return {
        "snapshot_date":    row.snapshot_date,
        "net_liquidation":  row.net_liquidation,
        "total_cash":       row.total_cash,
        "unrealized_pnl":   row.unrealized_pnl,
        "realized_pnl":     row.realized_pnl,
        "recorded_at":      row.recorded_at,
    }


# ── Target allocation helpers (the new system) ────────────────────────────────

def get_target_allocation(active_only: bool = True, sleeve: str | None = None) -> list[dict]:
    """Return target-allocation rows (ticker / sleeve / target_weight / label /
    active / updated_at), ordered by sleeve then ticker."""
    engine = get_engine()
    with Session(engine) as session:
        q = session.query(TargetAllocation)
        if active_only:
            q = q.filter(TargetAllocation.active.is_(True))
        if sleeve is not None:
            q = q.filter(TargetAllocation.sleeve == sleeve)
        rows = q.order_by(TargetAllocation.sleeve, TargetAllocation.ticker).all()
    return [{
        "ticker":        r.ticker,
        "sleeve":        r.sleeve,
        "target_weight": r.target_weight,
        "label":         r.label,
        "active":        r.active,
        "updated_at":    r.updated_at,
    } for r in rows]


def replace_target_sleeves(rows: list[dict], sleeves: set[str]) -> int:
    """Atomically set the active targets for the given ``sleeves``.

    Deactivates every currently-active row whose sleeve is in ``sleeves`` (kept as
    history), then inserts ``rows`` as the new active set.  Each row needs at least
    ``ticker`` / ``sleeve`` / ``target_weight`` (``label`` optional).  Returns the
    number inserted.  Use this to (re)set the core, or to rewrite the satellite
    after a re-screen, without touching the other sleeves.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    engine = get_engine()
    with Session(engine) as session:
        (session.query(TargetAllocation)
         .filter(TargetAllocation.active.is_(True))
         .filter(TargetAllocation.sleeve.in_(sleeves))
         .update({TargetAllocation.active: False}, synchronize_session=False))
        for r in rows:
            session.add(TargetAllocation(
                ticker=r["ticker"].upper(),
                sleeve=r["sleeve"],
                target_weight=float(r["target_weight"]),
                label=r.get("label"),
                active=True,
                updated_at=now,
            ))
        session.commit()
    return len(rows)


# ── Order decision helpers ─────────────────────────────────────────────────────

def log_order_decision(record: dict) -> None:
    """Persist one order decision row."""
    engine = get_engine()
    with Session(engine) as session:
        session.add(OrderDecisionRecord(**record))
        session.commit()


def get_order_decisions(limit: int = 100, run_id: str = "") -> pd.DataFrame:
    """Return recent order_decisions rows, newest first."""
    engine = get_engine()
    with Session(engine) as session:
        q = session.query(OrderDecisionRecord)
        if run_id:
            q = q.filter(OrderDecisionRecord.run_id == run_id)
        rows = q.order_by(desc(OrderDecisionRecord.decided_at)).limit(limit).all()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([{
        "run_id":           r.run_id,
        "symbol":           r.symbol,
        "signal":           r.signal,
        "decision":         r.decision,
        "shares":           r.shares,
        "entry_price":      r.entry_price,
        "stop_price":       r.stop_price,
        "take_profit_price": r.take_profit_price,
        "position_value":   r.position_value,
        "reject_reason":    r.reject_reason,
        "decided_at":       r.decided_at,
    } for r in rows])


def get_latest_risk_levels(symbols: list[str]) -> dict[str, dict]:
    """
    Return the most recent APPROVED or DRY_RUN order decision for each symbol,
    keyed by symbol.  Used to enrich the open-positions table with stop-loss and
    take-profit prices that were set at signal time.

    Returns a dict: { "AAPL": {"entry_price": ..., "stop_price": ..., "take_profit_price": ...,
                                "signal": ..., "decided_at": ...}, ... }
    """
    if not symbols:
        return {}
    engine = get_engine()
    result: dict[str, dict] = {}
    with Session(engine) as session:
        for sym in symbols:
            row = (
                session.query(OrderDecisionRecord)
                .filter(
                    OrderDecisionRecord.symbol == sym,
                    OrderDecisionRecord.decision.in_(["APPROVED", "DRY_RUN"]),
                )
                .order_by(desc(OrderDecisionRecord.decided_at))
                .first()
            )
            if row:
                result[sym] = {
                    "entry_price":       row.entry_price,
                    "stop_price":        row.stop_price,
                    "take_profit_price": row.take_profit_price,
                    "signal":            row.signal,
                    "decided_at":        row.decided_at,
                }
    return result


# ── Signal runner log helpers ──────────────────────────────────────────────────

def log_signal_runner_run(record: dict) -> None:
    """Persist a signal_runner.py run summary."""
    engine = get_engine()
    with Session(engine) as session:
        session.add(SignalRunnerLog(**record))
        session.commit()


def get_signal_runner_log(limit: int = 50) -> pd.DataFrame:
    """Return recent signal_runner_log entries, newest first."""
    engine = get_engine()
    with Session(engine) as session:
        rows = (
            session.query(SignalRunnerLog)
            .order_by(desc(SignalRunnerLog.recorded_at))
            .limit(limit)
            .all()
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([{
        "run_id":               r.run_id,
        "run_date":             r.run_date,
        "mode":                 r.mode,
        "symbols_processed":    r.symbols_processed,
        "signals_generated":    r.signals_generated,
        "orders_submitted":     r.orders_submitted,
        "orders_rejected":      r.orders_rejected,
        "skipped_duplicates":   r.skipped_duplicates,
        "skipped_pending_orders": r.skipped_pending_orders,
        "skipped_stale":        r.skipped_stale,
        "longs_closed":         r.longs_closed,
        "trailing_conversions": r.trailing_conversions,
        "hold_timeouts":        r.hold_timeouts,
        "duration_seconds":     r.duration_seconds,
        "recorded_at":          r.recorded_at,
        "notes":                r.notes,
    } for r in rows])


# ── Trailing stop log helpers ──────────────────────────────────────────────────

def log_trailing_stop_action(record: dict) -> None:
    """Persist a single TrailingStopAction as a trailing_stop_log row."""
    engine = get_engine()
    with Session(engine) as session:
        session.add(TrailingStopLog(**record))
        session.commit()


def get_trailing_stop_log(
    limit: int = 100,
    run_id: str | None = None,
) -> pd.DataFrame:
    """Return recent trailing_stop_log entries, newest first.

    Pass `run_id` to scope to a single signal_runner run.
    """
    engine = get_engine()
    with Session(engine) as session:
        q = session.query(TrailingStopLog)
        if run_id:
            q = q.filter(TrailingStopLog.run_id == run_id)
        rows = q.order_by(desc(TrailingStopLog.decided_at)).limit(limit).all()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([{
        "run_id":        r.run_id,
        "symbol":        r.symbol,
        "action":        r.action,
        "shares":        r.shares,
        "entry_price":   r.entry_price,
        "current_price": r.current_price,
        "atr":           r.atr,
        "trail_amount":  r.trail_amount,
        "reason":        r.reason,
        "decided_at":    r.decided_at,
    } for r in rows])


def get_latest_trailing_stop_log_for_symbol(symbol: str) -> dict | None:
    """Return the most recent trailing_stop_log row for ``symbol``, or None.

    Used by TrailingStopManager.manage() for ratchet detection: the previous
    (current_price, trail_amount) pair implies the previous trail trigger
    (current_price - trail_amount), which is compared against the live
    Order.trailStopPrice to decide whether IBKR has ratcheted the stop up.
    """
    engine = get_engine()
    with Session(engine) as session:
        row = (
            session.query(TrailingStopLog)
            .filter(TrailingStopLog.symbol == symbol)
            .order_by(desc(TrailingStopLog.decided_at))
            .first()
        )
    if not row:
        return None
    return {
        "run_id":        row.run_id,
        "symbol":        row.symbol,
        "action":        row.action,
        "shares":        row.shares,
        "entry_price":   row.entry_price,
        "current_price": row.current_price,
        "atr":           row.atr,
        "trail_amount":  row.trail_amount,
        "reason":        row.reason,
        "decided_at":    row.decided_at,
    }


# ── Intraday run log helpers ──────────────────────────────────────────────────

def log_intraday_run(record: dict) -> None:
    """Persist one intraday_check.py run summary."""
    engine = get_engine()
    with Session(engine) as session:
        session.add(IntradayRunLog(**record))
        session.commit()


def get_intraday_run_log(
    limit: int = 50,
    on_date: str | None = None,
) -> pd.DataFrame:
    """Return recent intraday_run_log entries, newest first.

    Pass ``on_date`` (YYYY-MM-DD) to scope to a single calendar day — used by
    Page 8's "Intraday checks (today)" section.
    """
    engine = get_engine()
    with Session(engine) as session:
        q = session.query(IntradayRunLog)
        if on_date:
            # SQLite stores DATETIME as ISO text; substring match is the simplest
            # date filter that does not depend on driver-specific date functions.
            q = q.filter(func.substr(
                func.cast(IntradayRunLog.run_timestamp, String), 1, 10
            ) == on_date)
        rows = q.order_by(desc(IntradayRunLog.run_timestamp)).limit(limit).all()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([{
        "run_id":              r.run_id,
        "run_timestamp":       r.run_timestamp,
        "mode":                r.mode,
        "status":              r.status,
        "daily_loss_pct":      r.daily_loss_pct,
        "weekly_loss_pct":     r.weekly_loss_pct,
        "cb_tripped":          r.cb_tripped,
        "positions_flattened": r.positions_flattened,
        "trailing_evaluated":  r.trailing_evaluated,
        "trailing_ratcheted":  r.trailing_ratcheted,
        "trailing_converted":  r.trailing_converted,
        "duration_seconds":    r.duration_seconds,
        "error_message":       r.error_message,
    } for r in rows])


# ── Trade log helpers ─────────────────────────────────────────────────────────

def log_trade(record: dict) -> None:
    """Persist one closed trade to trade_log."""
    engine = get_engine()
    with Session(engine) as session:
        session.add(TradeLog(**record))
        session.commit()


def log_trades_bulk(records: list[dict]) -> int:
    """Persist multiple closed trades in a single transaction.

    Used by MLWalkForwardOrchestrator at the end of each fold to write all
    simulated trades together rather than one round-trip per trade.
    Returns the number of rows inserted.
    """
    if not records:
        return 0
    engine = get_engine()
    with Session(engine) as session:
        session.add_all([TradeLog(**r) for r in records])
        session.commit()
    return len(records)


def get_trade_log(
    symbol: str | None = None,
    source: str | None = None,
    before_ts: datetime | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    """Return trade_log entries with optional filters, newest first.

    Phase C uses ``before_ts`` to enforce the forward-only invariant when
    computing realised-Kelly sizing (only trades that closed before the
    current bar's timestamp are eligible).
    """
    engine = get_engine()
    with Session(engine) as session:
        q = session.query(TradeLog)
        if symbol:
            q = q.filter(TradeLog.symbol == symbol)
        if source:
            q = q.filter(TradeLog.source == source)
        if before_ts is not None:
            q = q.filter(TradeLog.entry_ts < before_ts)
        q = q.order_by(desc(TradeLog.entry_ts))
        if limit:
            q = q.limit(limit)
        rows = q.all()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([{
        "id":            r.id,
        "source":        r.source,
        "run_id":        r.run_id,
        "fold_index":    r.fold_index,
        "symbol":        r.symbol,
        "signal":        r.signal,
        "entry_ts":      r.entry_ts,
        "entry_px":      r.entry_px,
        "exit_ts":       r.exit_ts,
        "exit_px":       r.exit_px,
        "exit_reason":   r.exit_reason,
        "shares":        r.shares,
        "pnl":           r.pnl,
        "pnl_pct":       r.pnl_pct,
        "costs_charged": r.costs_charged,
        "benchmark_return_pct": r.benchmark_return_pct,
        "recorded_at":   r.recorded_at,
        "entry_exec_id": r.entry_exec_id,
        "exit_exec_id":  r.exit_exec_id,
        "parent_order_id": r.parent_order_id,
        "account":       r.account,
    } for r in rows])


# ── Fill log / reconciliation helpers (Phase B) ───────────────────────────────

def upsert_fill(record: dict) -> str:
    """Ingest one IBKR execution into fill_log.  Returns the action taken.

    - ``"inserted"``    — new exec_id, row created.
    - ``"cost_updated"``— exec_id already present with commission IS NULL and the
                          incoming record carries a value: only the cost columns
                          (commission, realized_pnl) are refreshed.  This is the
                          *deliberate* exception to insert-or-ignore — see the
                          commission-race note in execution/reconciliation.py.
    - ``"skipped"``     — exec_id already present and nothing to update.

    ``exec_id`` is the sole dedup key; only commission / realized_pnl are ever
    mutated on an existing row.
    """
    engine = get_engine()
    with Session(engine) as session:
        existing = (
            session.query(FillLog)
            .filter_by(exec_id=record["exec_id"])
            .first()
        )
        if existing is None:
            row = FillLog(recorded_at=_utc_now())
            for k, v in record.items():
                if hasattr(row, k):
                    setattr(row, k, v)
            session.add(row)
            session.commit()
            return "inserted"

        if existing.commission is None and record.get("commission") is not None:
            existing.commission = record.get("commission")
            existing.realized_pnl = record.get("realized_pnl")
            session.commit()
            return "cost_updated"

        return "skipped"


def get_fills(
    since: datetime | None = None,
    symbol: str | None = None,
) -> list[dict]:
    """Return fill_log rows (optionally filtered), oldest first.

    Ordered ascending by ``exec_time`` so the reconciliation aggregator can walk
    fills chronologically to pair entries with exits.
    """
    engine = get_engine()
    with Session(engine) as session:
        q = session.query(FillLog)
        if since is not None:
            q = q.filter(FillLog.exec_time >= since)
        if symbol:
            q = q.filter(FillLog.symbol == symbol)
        rows = q.order_by(FillLog.exec_time.asc()).all()
    return [{
        "exec_id":         r.exec_id,
        "order_id":        r.order_id,
        "perm_id":         r.perm_id,
        "parent_order_id": r.parent_order_id,
        "account":         r.account,
        "symbol":          r.symbol,
        "conid":           r.conid,
        "side":            r.side,
        "order_type":      r.order_type,
        "shares":          r.shares,
        "price":           r.price,
        "commission":      r.commission,
        "realized_pnl":    r.realized_pnl,
        "exec_time":       r.exec_time,
    } for r in rows]


def get_reconciliation_state(source: str, account: str | None) -> dict | None:
    """Return the reconciliation watermark row for (source, account), or None."""
    engine = get_engine()
    with Session(engine) as session:
        row = (
            session.query(ReconciliationState)
            .filter_by(source=source, account=account)
            .first()
        )
    if not row:
        return None
    return {
        "source":             row.source,
        "account":            row.account,
        "last_reconciled_ts": row.last_reconciled_ts,
        "last_run_ts":        row.last_run_ts,
        "last_n_fills":       row.last_n_fills,
        "notes":              row.notes,
    }


def set_reconciliation_state(
    source: str,
    account: str | None,
    last_reconciled_ts: datetime | None,
    last_run_ts: datetime | None,
    last_n_fills: int | None,
    notes: str | None = None,
) -> None:
    """Upsert the reconciliation watermark for (source, account)."""
    engine = get_engine()
    with Session(engine) as session:
        row = (
            session.query(ReconciliationState)
            .filter_by(source=source, account=account)
            .first()
        )
        if row is None:
            row = ReconciliationState(source=source, account=account)
            session.add(row)
        row.last_reconciled_ts = last_reconciled_ts
        row.last_run_ts        = last_run_ts
        row.last_n_fills       = last_n_fills
        row.notes              = notes
        session.commit()


def live_trade_exists(exit_exec_id: str) -> bool:
    """True if a source='live' trade_log row already closed on ``exit_exec_id``.

    The per-round-trip dedup guard — mirrors the uq_trade_live_exit partial
    unique index but lets the caller skip with a clean log line instead of
    catching an IntegrityError.
    """
    if not exit_exec_id:
        return False
    engine = get_engine()
    with Session(engine) as session:
        row = (
            session.query(TradeLog.id)
            .filter(TradeLog.source == "live", TradeLog.exit_exec_id == exit_exec_id)
            .first()
        )
    return row is not None


def live_trade_uses_exec_ids(exec_ids: list[str]) -> bool:
    """True if any of ``exec_ids`` is already a leg of a source='live' trade.

    The ``uq_trade_live_exit`` index (and ``live_trade_exists``) only guard the
    *exit* leg, so the same physical fill can be re-paired as a *different* round
    trip's entry/exit and dodge dedup.  Concretely (2026-06-05 SLV): the
    2026-04-29 orphan-short fills were recorded once as the correct short
    (id=2002, with the MKT-buy as its exit leg) and then re-paired by the
    aggregator in the opposite direction (id=2022, with the STP-sell as its exit
    leg) — two distinct exit_exec_ids, so the exit-only guard let the duplicate
    through.  This helper closes that gap: a fill already consumed as EITHER an
    entry or exit leg of any live row must not be re-paired into a new trade.
    """
    ids = [e for e in exec_ids if e]
    if not ids:
        return False
    engine = get_engine()
    with Session(engine) as session:
        row = (
            session.query(TradeLog.id)
            .filter(
                TradeLog.source == "live",
                or_(
                    TradeLog.entry_exec_id.in_(ids),
                    TradeLog.exit_exec_id.in_(ids),
                ),
            )
            .first()
        )
    return row is not None


# ── Exit-reason inference helpers (Phase B reconciliation) ────────────────────

def get_latest_approved_bracket(symbol: str, before_ts: datetime) -> dict | None:
    """Latest APPROVED BUY order_decision for ``symbol`` before ``before_ts``.

    Returns the recorded entry/stop/TP prices so the reconciler can match a
    live exit price to the original bracket levels (the
    ``order_decisions_price_match`` exit-reason path).  None if no qualifying row.
    """
    engine = get_engine()
    with Session(engine) as session:
        row = (
            session.query(OrderDecisionRecord)
            .filter(
                OrderDecisionRecord.symbol == symbol,
                OrderDecisionRecord.signal == "BUY",
                OrderDecisionRecord.decision == "APPROVED",
                OrderDecisionRecord.decided_at < before_ts,
            )
            .order_by(desc(OrderDecisionRecord.decided_at))
            .first()
        )
    if not row:
        return None
    return {
        "entry_price":       row.entry_price,
        "stop_price":        row.stop_price,
        "take_profit_price": row.take_profit_price,
        "decided_at":        row.decided_at,
    }


def has_converted_trailing_before(symbol: str, before_ts: datetime) -> bool:
    """True if ``symbol`` has a CONVERTED trailing_stop_log row before ``before_ts``.

    Used by the reconciler's session-independent exit-reason inference: a
    position whose bracket TP was converted to a TRAIL and then filled exits
    via ``trailing`` even when the order is no longer in the live session.
    """
    engine = get_engine()
    with Session(engine) as session:
        row = (
            session.query(TrailingStopLog.id)
            .filter(
                TrailingStopLog.symbol == symbol,
                TrailingStopLog.action == "CONVERTED",
                TrailingStopLog.decided_at < before_ts,
            )
            .first()
        )
    return row is not None


def has_closed_long_near(symbol: str, ts: datetime, minutes: int = 1) -> bool:
    """True if ``symbol`` has a CLOSED_LONG order_decision within ±``minutes`` of ``ts``.

    Disambiguates a MKT exit: a market sell paired with an in-window CLOSED_LONG
    decision is a signal-flip close, otherwise it's a manual close.
    """
    from datetime import timedelta
    lo = ts - timedelta(minutes=minutes)
    hi = ts + timedelta(minutes=minutes)
    engine = get_engine()
    with Session(engine) as session:
        row = (
            session.query(OrderDecisionRecord.id)
            .filter(
                OrderDecisionRecord.symbol == symbol,
                OrderDecisionRecord.decision == "CLOSED_LONG",
                OrderDecisionRecord.decided_at >= lo,
                OrderDecisionRecord.decided_at <= hi,
            )
            .first()
        )
    return row is not None


def has_cb_flatten_near(symbol: str, ts: datetime, minutes: int = 5) -> bool:
    """True if ``symbol`` has a CB_FLATTENED order_decision within ±``minutes`` of ``ts``.

    Disambiguates a forced circuit-breaker liquidation from a discretionary
    close.  ``intraday_check.py`` / ``order_manager.flatten_all_longs`` cancel a
    position's bracket children and submit a plain MKT sell when the daily/weekly
    loss limit trips — leaving no signal the reconciler's exit-reason waterfall
    recognises, so it otherwise mislabels these as ``manual_close`` (MKT default)
    or even ``trailing`` (if a stale trailing_stop_log row predates the fill).
    The ``CB_FLATTENED`` decision is the authoritative record (one per symbol per
    flatten event, timestamped to the second) so a tight ±``minutes`` match is
    unambiguous — CB flatten events are rare and the decision lands within ~1s of
    the fill.  Used by ``_infer_exit_reason`` ahead of the trailing-log /
    price-match / default branches.
    """
    from datetime import timedelta
    lo = ts - timedelta(minutes=minutes)
    hi = ts + timedelta(minutes=minutes)
    engine = get_engine()
    with Session(engine) as session:
        row = (
            session.query(OrderDecisionRecord.id)
            .filter(
                OrderDecisionRecord.symbol == symbol,
                OrderDecisionRecord.decision == "CB_FLATTENED",
                OrderDecisionRecord.decided_at >= lo,
                OrderDecisionRecord.decided_at <= hi,
            )
            .first()
        )
    return row is not None
