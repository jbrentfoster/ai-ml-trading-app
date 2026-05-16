"""
Model Signals — Page 3
========================
Displays per-model scores (LSTM, XGBoost, FinBERT) and the ensemble
composite score over time, the signal log table, XGBoost feature
importance, current regime badge, and ensemble weight donut chart.

Includes a "Generate Signal" button that runs inference (loading saved
models) and a "Train & Generate" button that trains models first if
none exist yet.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from config.settings import config
from data.ui_queries import (
    query_company_name,
    query_latest_ensemble_weights,
    query_news,
    query_signal_log,
    symbol_picker,
)

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Model Signals",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Signal generation helpers ─────────────────────────────────────────────────

def _models_exist(symbol: str) -> bool:
    """Return True if both LSTM and XGBoost model files exist for `symbol`."""
    base = Path(f"models/cache/{symbol}")
    return (base / "lstm.pt").exists() and (base / "xgb.ubj").exists()


def _generate_signal(symbol: str, interval: str, train_first: bool, quick_mode: bool):
    """
    Generate a signal for `symbol` and log it to SQLite.

    If `train_first` is True (or no saved models exist), runs a quick
    walk-forward training pass before predicting.

    Returns a SignalResult dataclass instance.
    """
    from data.indicators import IndicatorEngine
    from data.database import log_signal
    from models.walk_forward import MLWalkForwardOrchestrator

    engine = IndicatorEngine()
    df = engine.run(symbol, interval=interval)

    if df is None or df.empty:
        raise ValueError(
            f"No data for {symbol} ({interval}).  "
            "Run the data pipeline first (Market Data page → Refresh)."
        )

    cache_dir = Path(f"models/cache/{symbol}")
    orch = MLWalkForwardOrchestrator(symbol)

    need_training = train_first or not _models_exist(symbol)

    if need_training:
        saved = {}
        if quick_mode:
            saved = {
                "lstm_epochs":      config.ml.lstm_epochs,
                "xgb_n_estimators": config.ml.xgb_n_estimators,
                "wf_n_splits":      config.ml.wf_n_splits,
                "wf_train_bars":    config.ml.wf_train_bars,
                "wf_test_bars":     config.ml.wf_test_bars,
            }
            config.ml.lstm_epochs      = 5
            config.ml.xgb_n_estimators = 50
            config.ml.wf_n_splits      = 2
            config.ml.wf_train_bars    = 60   # 3 months; min = 60+1+2*10 = 81 bars
            config.ml.wf_test_bars     = 10
        try:
            orch.run(df)
            cache_dir.mkdir(parents=True, exist_ok=True)
            orch.save_models(cache_dir)
        finally:
            for k, v in saved.items():
                setattr(config.ml, k, v)
    else:
        orch.load_models(cache_dir)

    result = orch.predict(df)

    log_signal({
        "symbol":         result.symbol,
        "generated_at":   result.generated_at,
        "bar_timestamp":  result.bar_timestamp,
        "lstm_score":     result.lstm_score,
        "xgb_score":      result.xgb_score,
        "finbert_score":  result.finbert_score,
        "ensemble_score": result.ensemble_score,
        "regime":         result.regime.value,
        "signal":         result.signal,
        "passed_gate":    result.passed_gate,
        "gate_reason":    result.gate_reason,
    })

    return result


# ── Sidebar ───────────────────────────────────────────────────────────────────

st.sidebar.title("🤖 Model Signals")
st.sidebar.markdown("---")

symbol = symbol_picker("Symbol", default="AAPL", key="ms_symbol")
interval  = st.sidebar.selectbox("Interval", ["1d", "1h"], key="ms_interval")

_today   = datetime.now(timezone.utc).date()
_d_start = _today - timedelta(days=config.ml.signal_lookback_days)
ms_start = st.sidebar.date_input("From", value=_d_start, key="ms_start")
ms_end   = st.sidebar.date_input("To",   value=_today,   key="ms_end")

st.sidebar.markdown("**Generate Signal**")

quick_mode = st.sidebar.checkbox(
    "Quick mode",
    value=True,
    key="ms_quick",
    help="5 LSTM epochs / 50 XGB trees / 2 folds when training is required",
)

models_ready = _models_exist(symbol)
if models_ready:
    st.sidebar.caption(f"Saved models found for **{symbol}**.")
else:
    st.sidebar.caption(f"No saved models for **{symbol}** — training required.")

gen_btn   = st.sidebar.button(
    "Generate Signal",
    type="primary",
    key="ms_gen",
    help="Use saved models (instant) or train first if none exist",
)
retrain_btn = st.sidebar.button(
    "Re-train & Generate",
    key="ms_retrain",
    help="Force re-training even if saved models exist",
)

st.sidebar.markdown("---")

# ── VIX cache status ──────────────────────────────────────────────────────────
try:
    from data.database import get_bars as _get_bars
    _vix = _get_bars("^VIX", "1d", limit=1)
    if _vix.empty:
        st.sidebar.caption(
            "**VIX:** no cache.  Run `python run_pipeline.py` to populate."
        )
    else:
        _vix_ts   = _vix.index[-1]
        _vix_val  = float(_vix["Close"].iloc[-1])
        _vix_age  = (datetime.now(timezone.utc).replace(tzinfo=None) - _vix_ts)
        _vix_age_h = _vix_age.total_seconds() / 3600
        _stale    = "⚠ stale — " if _vix_age_h > 4 else ""
        st.sidebar.caption(
            f"**VIX:** {_vix_val:.2f}  ({_stale}{_vix_ts.strftime('%Y-%m-%d')})"
            f"  \nRefreshes via `run_pipeline.py`"
        )
except Exception:
    pass

if st.sidebar.button("Refresh cache", key="ms_refresh"):
    query_signal_log.clear()
    query_latest_ensemble_weights.clear()
    query_news.clear()
    st.rerun()

# ── Execute signal generation ─────────────────────────────────────────────────

for btn_clicked, force_train in [(gen_btn, False), (retrain_btn, True)]:
    if not btn_clicked:
        continue

    need_train = force_train or not models_ready
    label = (
        f"Re-training and generating signal for **{symbol}** …"
        if need_train
        else f"Generating signal for **{symbol}** using saved models …"
    )
    with st.spinner(label):
        try:
            result = _generate_signal(symbol, interval, train_first=force_train,
                                      quick_mode=quick_mode)
            query_signal_log.clear()
            query_latest_ensemble_weights.clear()

            sig_color = {"BUY": "#26a69a", "SELL": "#ef5350"}.get(result.signal, "#888888")
            st.success(
                f"Signal logged: **:{('green' if result.signal == 'BUY' else 'red' if result.signal == 'SELL' else 'gray')}[{result.signal}]**  |  "
                f"Ensemble: {result.ensemble_score:+.3f}  |  "
                f"Regime: {result.regime.value}  |  "
                f"Gate: {'passed' if result.passed_gate else 'blocked'} — {result.gate_reason}"
            )
            st.rerun()
        except Exception as exc:
            st.error(f"Signal generation failed: {exc}")
    break   # only one button can be clicked per render

# ── Load signal log ───────────────────────────────────────────────────────────

sig_df = query_signal_log(symbol, ms_start, ms_end)

_company = query_company_name(symbol)
st.title(f"Model Signals — {symbol}" + (f" ({_company})" if _company else ""))

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — LATEST SCORES & REGIME
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
st.subheader("Latest Model Output")

col_scores, col_regime = st.columns([3, 2])

with col_scores:
    if sig_df.empty:
        st.info(
            "No signals yet for the selected filters.  "
            "Click **Generate Signal** in the sidebar to run inference."
        )
    else:
        last = sig_df.iloc[0]

        s1, s2, s3, s4 = st.columns(4)
        for col_w, label, key in [
            (s1, "LSTM",     "LSTM Score"),
            (s2, "XGBoost",  "XGB Score"),
            (s3, "FinBERT",  "FinBERT Score"),
            (s4, "Ensemble", "Ensemble Score"),
        ]:
            val = last.get(key)
            col_w.metric(label, f"{val:+.3f}" if pd.notna(val) else "—")

        sig   = last.get("Signal", "HOLD")
        color = {"BUY": "#26a69a", "SELL": "#ef5350"}.get(sig, "#888888")
        st.markdown(
            f"<div style='text-align:center;font-size:1.4rem;font-weight:bold;"
            f"background:{color}22;border:2px solid {color};border-radius:8px;"
            f"padding:10px;margin-top:8px;color:{color};'>"
            f"Latest Signal: {sig}</div>",
            unsafe_allow_html=True,
        )

with col_regime:
    regime = sig_df.iloc[0].get("Regime") if not sig_df.empty else None
    if regime:
        regime_colors = {
            "TRENDING":        "#2196f3",
            "MEAN_REVERTING":  "#ff9800",
            "HIGH_VOLATILITY": "#ef5350",
        }
        hex_c = regime_colors.get(regime, "#888888")
        st.markdown(
            f"<div style='text-align:center;font-size:1.1rem;font-weight:bold;"
            f"background:{hex_c}22;border:3px solid {hex_c};border-radius:12px;"
            f"padding:20px 10px;margin-bottom:16px;color:{hex_c};'>"
            f"REGIME<br><span style='font-size:1.4rem'>{regime}</span></div>",
            unsafe_allow_html=True,
        )
    else:
        st.info("Regime will appear after the first signal is generated.")

    weights = query_latest_ensemble_weights()
    if weights:
        wt_fig = go.Figure(go.Pie(
            labels=["LSTM", "XGBoost", "FinBERT"],
            values=[weights["lstm"], weights["xgb"], weights["finbert"]],
            hole=0.55,
            marker=dict(colors=["#2196f3", "#26a69a", "#ff9800"]),
        ))
        wt_fig.update_layout(
            height=220, template="plotly_dark",
            margin=dict(l=0, r=0, t=20, b=0),
            legend=dict(orientation="h", y=-0.1),
            title=dict(text="Ensemble Weights", x=0.5),
        )
        st.plotly_chart(wt_fig, use_container_width=True)
    else:
        st.info("Ensemble weights appear after first training run.")

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — SCORE HISTORY CHART
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
st.subheader("Score History")
st.caption(
    "Each 'Generate Signal' call produces one data point per model.  "
    "**LSTM** captures sequential price momentum over the last 60 bars.  "
    "**XGBoost** classifies the current bar using technical indicators and fundamental ratios.  "
    "**FinBERT** aggregates recent news sentiment with exponential time-decay.  "
    "The **Ensemble** (white line) is a weighted average of all three — "
    "it must cross the threshold line AND have at least 2 of 3 models agree on direction "
    "before the signal gate issues a BUY or SELL."
)

if sig_df.empty:
    st.info("Score history will populate after signals are generated.")
else:
    chart_df = sig_df[["Date", "LSTM Score", "XGB Score",
                        "FinBERT Score", "Ensemble Score"]].sort_values("Date")

    score_fig = go.Figure()
    score_fig.add_trace(go.Scatter(x=chart_df["Date"], y=chart_df["LSTM Score"],
        name="LSTM",     line=dict(color="#2196f3", width=1.2)))
    score_fig.add_trace(go.Scatter(x=chart_df["Date"], y=chart_df["XGB Score"],
        name="XGBoost",  line=dict(color="#26a69a", width=1.2)))
    score_fig.add_trace(go.Scatter(x=chart_df["Date"], y=chart_df["FinBERT Score"],
        name="FinBERT",  line=dict(color="#ff9800", width=1.2)))
    score_fig.add_trace(go.Scatter(x=chart_df["Date"], y=chart_df["Ensemble Score"],
        name="Ensemble", line=dict(color="#ffffff", width=2.5)))

    score_fig.add_hline(y=0, line_dash="dash", line_color="rgba(255,255,255,0.3)")
    score_fig.add_hline(y=config.ml.signal_threshold,  line_dash="dot",
                        line_color="rgba(38,166,154,0.5)",
                        annotation_text=f"Buy threshold ({config.ml.signal_threshold})")
    score_fig.add_hline(y=-config.ml.signal_threshold, line_dash="dot",
                        line_color="rgba(239,83,80,0.5)",
                        annotation_text=f"Sell threshold (-{config.ml.signal_threshold})")

    score_fig.update_layout(
        height=380, template="plotly_dark",
        yaxis=dict(title="Score", range=[-1, 1]),
        legend=dict(orientation="h", y=1.05, x=0),
        margin=dict(l=0, r=0, t=20, b=0),
    )
    st.plotly_chart(score_fig, use_container_width=True)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — SIGNAL LOG TABLE
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
st.subheader("Signal Log")

if sig_df.empty:
    st.info("Signal log is empty for the selected filters.")
else:
    display_cols = [
        "Date", "Symbol", "Signal", "Passed Gate",
        "Ensemble Score", "LSTM Score", "XGB Score", "FinBERT Score",
        "Regime", "Gate Reason",
    ]
    display_cols = [c for c in display_cols if c in sig_df.columns]

    def _signal_style(val) -> str:
        if val == "BUY":
            return "background-color: rgba(38,166,154,0.2); color: #26a69a; font-weight: bold"
        if val == "SELL":
            return "background-color: rgba(239,83,80,0.2); color: #ef5350; font-weight: bold"
        return "color: #888888"

    score_cols = [c for c in ["Ensemble Score", "LSTM Score", "XGB Score", "FinBERT Score"]
                  if c in sig_df.columns]

    st.dataframe(
        sig_df[display_cols].style
            .map(_signal_style, subset=["Signal"])
            .format({c: "{:+.3f}" for c in score_cols}, na_rep="—"),
        use_container_width=True,
        height=360,
    )

    csv_bytes = sig_df.to_csv(index=False).encode("utf-8")
    st.download_button("Download signal log CSV", csv_bytes,
                       file_name="signal_log.csv", mime="text/csv")

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — XGBOOST FEATURE IMPORTANCE
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
st.subheader("XGBoost Feature Importance")
st.caption(
    "XGBoost trains an ensemble of decision trees, each splitting on the feature that most reduces prediction error.  "
    "This chart shows **gain-weighted importance** — how much each feature contributed to reducing error across all splits.  "
    "Features at the top had the highest predictive value in the most recent training run.  "
    "RSI, MACD, and ATR typically dominate because they directly encode the short-term momentum and volatility "
    "patterns the model targets.  Fundamental features (P/E, ROE, etc.) tend to rank lower on daily bars "
    "because they change slowly, but they help distinguish high-quality from low-quality setups."
)

_model_path = Path(f"models/cache/{symbol}/xgb.ubj")

if _model_path.exists():
    try:
        import xgboost as xgb  # type: ignore
        from models.xgboost_model import _ALL_FEATURES
        booster = xgb.XGBClassifier()
        booster.load_model(str(_model_path))

        n_feat = len(booster.feature_importances_)
        imp = pd.DataFrame({
            "Feature":    _ALL_FEATURES[:n_feat],
            "Importance": booster.feature_importances_,
        }).sort_values("Importance", ascending=True).tail(15)

        imp_fig = go.Figure(go.Bar(
            x=imp["Importance"], y=imp["Feature"],
            orientation="h", marker_color="#2196f3",
        ))
        imp_fig.update_layout(
            height=420, template="plotly_dark",
            xaxis=dict(title="Feature Importance"),
            margin=dict(l=0, r=0, t=10, b=0),
        )
        st.plotly_chart(imp_fig, use_container_width=True)
    except Exception as exc:
        st.warning(f"Could not load XGBoost model for importance chart: {exc}")
else:
    st.info(
        f"Feature importance will appear here after the XGBoost model has been "
        f"trained.  Use **Generate Signal** or **Re-train & Generate** in the sidebar."
    )

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — LSTM ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
st.subheader("LSTM Analysis")

_lstm_path = Path(f"models/cache/{symbol}/lstm.pt")

if not _lstm_path.exists():
    st.info(
        "LSTM analysis will appear after the model has been trained.  "
        "Use **Generate Signal** or **Re-train & Generate** in the sidebar."
    )
else:
    try:
        from data.ui_queries import query_bars
        from data.indicators import compute_indicators
        from models.lstm_model import LSTMModel

        # Load 500 bars (>seq_len of 60 + display range); compute indicators
        _df_full = query_bars(symbol, interval, limit=500)
        _df_full = compute_indicators(_df_full)

        # Load saved LSTM and score every bar
        _lstm = LSTMModel()
        _lstm.load(_lstm_path)
        _score_full = _lstm.score_series(_df_full)

        # Filter to the selected date range for display
        _df_disp = _df_full.copy()
        if ms_start:
            _df_disp = _df_disp[_df_disp.index >= pd.Timestamp(ms_start)]
        if ms_end:
            _df_disp = _df_disp[_df_disp.index <= pd.Timestamp(ms_end)]
        _score_disp = _score_full.reindex(_df_disp.index)

        if _df_disp.empty:
            st.warning("No bars in the selected date range for LSTM analysis.")
        else:

            # ── Chart 1: Price + LSTM Score + Volume ─────────────────────────
            st.markdown("**Price vs. LSTM Score**")
            st.caption(
                "The LSTM reads a rolling window of the last **60 bars** of OHLCV + "
                "technical indicators and outputs a score in **[-1, +1]** using a tanh "
                "activation.  A positive score means the model expects upward momentum "
                "over the next 5 bars; negative means downward.  The shaded region "
                "shows where the score crosses the signal threshold."
            )

            _fig = make_subplots(
                rows=3, cols=1,
                shared_xaxes=True,
                row_heights=[0.52, 0.30, 0.18],
                vertical_spacing=0.03,
                subplot_titles=(f"{symbol} Price", "LSTM Score  [-1 → +1]", "Volume"),
            )

            # Candlesticks
            _fig.add_trace(go.Candlestick(
                x=_df_disp.index,
                open=_df_disp["Open"], high=_df_disp["High"],
                low=_df_disp["Low"],   close=_df_disp["Close"],
                name="OHLC",
                increasing_line_color="#26a69a",
                decreasing_line_color="#ef5350",
                showlegend=False,
            ), row=1, col=1)

            # EMA overlays
            for _col, _col_color, _ema_name in [
                ("ema_9",  "#ff9800", "EMA 9"),
                ("ema_21", "#2196f3", "EMA 21"),
            ]:
                if _col in _df_disp.columns:
                    _fig.add_trace(go.Scatter(
                        x=_df_disp.index, y=_df_disp[_col],
                        name=_ema_name, line=dict(color=_col_color, width=1.2),
                    ), row=1, col=1)

            # LSTM score — positive region (bullish)
            _pos_score = _score_disp.clip(lower=0)
            _neg_score = _score_disp.clip(upper=0)
            _fig.add_trace(go.Scatter(
                x=_score_disp.index, y=_pos_score,
                name="Bullish", line=dict(color="#26a69a", width=2),
                fill="tozeroy", fillcolor="rgba(38,166,154,0.18)",
            ), row=2, col=1)
            _fig.add_trace(go.Scatter(
                x=_score_disp.index, y=_neg_score,
                name="Bearish", line=dict(color="#ef5350", width=2),
                fill="tozeroy", fillcolor="rgba(239,83,80,0.18)",
            ), row=2, col=1)

            _thresh = config.ml.signal_threshold
            _fig.add_hline(y=_thresh,  line_dash="dot", line_color="rgba(38,166,154,0.6)",
                           annotation_text=f"Buy (+{_thresh})",
                           annotation_position="top right", row=2, col=1)
            _fig.add_hline(y=-_thresh, line_dash="dot", line_color="rgba(239,83,80,0.6)",
                           annotation_text=f"Sell (-{_thresh})",
                           annotation_position="bottom right", row=2, col=1)
            _fig.add_hline(y=0, line_color="rgba(255,255,255,0.2)", row=2, col=1)

            # Volume bars
            _vol_colors = [
                "#26a69a" if _df_disp["Close"].iloc[i] >= _df_disp["Open"].iloc[i]
                else "#ef5350"
                for i in range(len(_df_disp))
            ]
            _fig.add_trace(go.Bar(
                x=_df_disp.index, y=_df_disp["Volume"],
                name="Volume", marker_color=_vol_colors, showlegend=False,
            ), row=3, col=1)

            _fig.update_layout(
                height=700, template="plotly_dark",
                xaxis_rangeslider_visible=False,
                legend=dict(orientation="h", y=1.03, x=0),
                margin=dict(l=0, r=0, t=40, b=0),
            )
            _fig.update_yaxes(title_text="Price ($)",   row=1, col=1)
            _fig.update_yaxes(title_text="Score",       row=2, col=1, range=[-1, 1])
            _fig.update_yaxes(title_text="Volume",      row=3, col=1)
            st.plotly_chart(_fig, use_container_width=True)

            # ── Chart 2: Input Sequence Heatmap ──────────────────────────────
            st.markdown("**Input Sequence Heatmap — last 60 bars**")
            st.caption(
                "Every bar, the LSTM reads a **60-bar sliding window** of 17 normalized "
                "features (OHLCV + RSI + MACD + Bollinger + EMA + ATR + Volume SMA).  "
                "This heatmap shows the current window: each row is one feature; each "
                "column is one bar.  Green = above training average, Red = below.  "
                "Bright cells are the strongest signals the model is reacting to right now."
            )

            _seq_len = _lstm._seq_len
            if len(_df_full) >= _seq_len:
                _last_df = _df_full.iloc[-_seq_len:]
                _normed  = _lstm._dataset.transform(_last_df)   # (seq_len, n_features)
                _feat_cols = list(_lstm._dataset.feature_cols)

                _date_fmt = "%m/%d %H:%M" if interval == "1h" else "%m/%d"
                _x_labels = _last_df.index.strftime(_date_fmt).tolist()

                _heat_fig = go.Figure(go.Heatmap(
                    z=_normed.T,
                    x=_x_labels,
                    y=_feat_cols,
                    colorscale="RdYlGn",
                    zmid=0,
                    colorbar=dict(title="Normalized<br>Value", thickness=14),
                    hovertemplate="Feature: %{y}<br>Bar: %{x}<br>Value: %{z:.2f}<extra></extra>",
                ))
                _heat_fig.update_layout(
                    height=440, template="plotly_dark",
                    xaxis=dict(title="Bar date", tickangle=-45,
                               tickmode="array",
                               tickvals=_x_labels[::max(1, _seq_len // 10)],
                               ticktext=_x_labels[::max(1, _seq_len // 10)]),
                    yaxis=dict(title="Feature", autorange="reversed"),
                    margin=dict(l=0, r=0, t=20, b=60),
                )
                st.plotly_chart(_heat_fig, use_container_width=True)

            # ── Chart 3: Directional Accuracy ────────────────────────────────
            st.markdown("**Directional Accuracy — LSTM score vs. realized 5-bar return**")
            st.caption(
                "For each bar the model scored, we compare its **direction** (score > 0 = "
                "bullish, score < 0 = bearish) against the **realized 5-bar forward return**.  "
                "Green bars = model called it right.  Red = wrong.  "
                "Bars above 50% accuracy suggest the model has learned a real signal; "
                "random guessing would average ~50%."
            )

            _fwd_bars = 5
            if len(_df_disp) > _fwd_bars + _lstm._seq_len:
                _fwd_ret = (
                    _df_disp["Close"].shift(-_fwd_bars) / _df_disp["Close"] - 1
                ).dropna()
                _common  = _score_disp.dropna().index.intersection(_fwd_ret.index)

                if len(_common) > 0:
                    _s = _score_disp.reindex(_common)
                    _r = _fwd_ret.reindex(_common)
                    _correct = (_s > 0) == (_r > 0)

                    _acc_pct    = _correct.mean() * 100
                    _n_correct  = int(_correct.sum())
                    _n_total    = len(_correct)

                    _acc_fig = go.Figure()
                    _acc_fig.add_trace(go.Bar(
                        x=_common,
                        y=_r * 100,
                        marker_color=["#26a69a" if c else "#ef5350" for c in _correct],
                        name="5-bar Return",
                        hovertemplate="Date: %{x}<br>Return: %{y:.2f}%<extra></extra>",
                    ))
                    _acc_fig.add_hline(y=0, line_color="rgba(255,255,255,0.25)")
                    _acc_fig.update_layout(
                        height=300, template="plotly_dark",
                        title=dict(
                            text=(f"Directional accuracy: {_acc_pct:.1f}%  "
                                  f"({_n_correct} / {_n_total} bars correct)"),
                            font=dict(size=13),
                        ),
                        yaxis=dict(title="5-bar forward return (%)"),
                        margin=dict(l=0, r=0, t=40, b=0),
                    )
                    st.plotly_chart(_acc_fig, use_container_width=True)
                else:
                    st.info("Not enough overlapping scored bars to compute accuracy.")
            else:
                st.info("Select a wider date range to see directional accuracy.")

    except Exception as _exc:
        st.warning(f"LSTM analysis unavailable: {_exc}")

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — FINBERT SENTIMENT ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
st.subheader("FinBERT Sentiment Analysis")
st.caption(
    "**FinBERT** (`ProsusAI/finbert`) is a BERT model fine-tuned on financial news.  "
    "Each headline is scored in **[-1, +1]** (negative → bearish, positive → bullish).  "
    "The ensemble score uses **exponential time-decay** so recent articles carry more weight — "
    f"articles older than {config.ml.sentiment_staleness_days} days contribute 0.  "
    "Run `python run_pipeline.py` (or use the Fetch & Score News button on Page 2) to populate the news cache."
)

_news_days = config.alpaca.news_lookback_days or 30
_news_df   = query_news(symbol, days_back=_news_days)

_scored_df = _news_df[_news_df["sentiment_score"].notna()] if not _news_df.empty else _news_df

if _news_df.empty:
    st.info(
        f"No news cached for **{symbol}**.  "
        "Go to **Fundamentals & News** → Fetch & Score News, or run `python run_pipeline.py`."
    )
else:
    # ── Coverage stats ────────────────────────────────────────────────────────
    _total_articles = len(_news_df)
    _scored_count   = len(_scored_df)
    _unscored_count = _total_articles - _scored_count
    _avg_score      = _scored_df["sentiment_score"].mean() if _scored_count > 0 else None
    _date_range_str = (
        f"{_news_df['published_at'].min().strftime('%Y-%m-%d')}  →  "
        f"{_news_df['published_at'].max().strftime('%Y-%m-%d')}"
        if not _news_df.empty else "—"
    )

    weights = query_latest_ensemble_weights()
    _finbert_wt = weights["finbert"] if weights else None

    _c1, _c2, _c3, _c4 = st.columns(4)
    _c1.metric("Total Articles", _total_articles)
    _c2.metric("Scored by FinBERT", _scored_count,
               delta=f"{_unscored_count} unscored" if _unscored_count > 0 else None,
               delta_color="off")
    _c3.metric(
        "Avg Sentiment Score",
        f"{_avg_score:+.3f}" if _avg_score is not None else "—",
    )
    _c4.metric(
        "FinBERT Ensemble Weight",
        f"{_finbert_wt:.0%}" if _finbert_wt is not None else "—",
    )
    st.caption(f"Articles from: {_date_range_str}")

    if _scored_count > 0:
        # ── Chart 1: Rolling 7-day average sentiment ──────────────────────────
        st.markdown("**Rolling Sentiment Trend**")
        st.caption(
            "Daily average of all FinBERT scores for articles published that day.  "
            "The 7-day rolling average (thicker line) smooths out day-to-day noise.  "
            "A sustained move above 0 (teal) suggests broadly positive news coverage; "
            "below 0 (red) suggests negative."
        )

        _trend = (
            _scored_df
            .set_index("published_at")["sentiment_score"]
            .resample("1D").mean()
            .dropna()
        )
        if len(_trend) > 0:
            _rolling = _trend.rolling(7, min_periods=1).mean()

            _trend_fig = go.Figure()
            _trend_fig.add_trace(go.Bar(
                x=_trend.index,
                y=_trend.values,
                name="Daily Avg",
                marker_color=[
                    "rgba(38,166,154,0.45)" if v >= 0 else "rgba(239,83,80,0.45)"
                    for v in _trend.values
                ],
            ))
            _trend_fig.add_trace(go.Scatter(
                x=_rolling.index,
                y=_rolling.values,
                name="7-day MA",
                line=dict(color="#ff9800", width=2.5),
            ))
            _trend_fig.add_hline(y=0, line_color="rgba(255,255,255,0.25)")
            _trend_fig.update_layout(
                height=280, template="plotly_dark",
                yaxis=dict(title="Avg Sentiment Score", zeroline=False),
                legend=dict(orientation="h", y=1.05, x=0),
                margin=dict(l=0, r=0, t=20, b=0),
            )
            st.plotly_chart(_trend_fig, use_container_width=True)

        # ── Chart 2: Score distribution ───────────────────────────────────────
        st.markdown("**Score Distribution**")
        st.caption(
            "Histogram of all scored article sentiments.  "
            "A right-skewed distribution (more positive bars) indicates the news coverage "
            "for this symbol has been generally optimistic over the lookback window."
        )

        _dist_fig = go.Figure(go.Histogram(
            x=_scored_df["sentiment_score"],
            nbinsx=30,
            marker_color="#2196f3",
            marker_line_color="rgba(255,255,255,0.15)",
            marker_line_width=0.5,
        ))
        _dist_fig.add_vline(x=0, line_dash="dash", line_color="rgba(255,255,255,0.4)")
        if _avg_score is not None:
            _dist_fig.add_vline(
                x=_avg_score,
                line_dash="dot", line_color="#ff9800",
                annotation_text=f"Mean {_avg_score:+.3f}",
                annotation_position="top right",
            )
        _dist_fig.update_layout(
            height=220, template="plotly_dark",
            xaxis=dict(title="Sentiment Score", range=[-1, 1]),
            yaxis=dict(title="Article Count"),
            margin=dict(l=0, r=0, t=10, b=0),
        )
        st.plotly_chart(_dist_fig, use_container_width=True)

    # ── Table: recent scored headlines ────────────────────────────────────────
    st.markdown("**Recent Headlines**")

    _display_news = _news_df.head(50).copy()
    _display_news["Published"] = _display_news["published_at"].dt.strftime("%Y-%m-%d")
    _display_news["Score"]     = _display_news["sentiment_score"]
    _display_news["Sentiment"] = _display_news["sentiment_label"]
    _display_news["Headline"]  = _display_news["headline"]

    def _sent_style(val) -> str:
        if val == "Positive":
            return "background-color: rgba(38,166,154,0.15); color: #26a69a"
        if val == "Negative":
            return "background-color: rgba(239,83,80,0.15); color: #ef5350"
        return "color: #888888"

    _tbl = _display_news[["Published", "Sentiment", "Score", "Headline"]]
    st.dataframe(
        _tbl.style
            .map(_sent_style, subset=["Sentiment"])
            .format({"Score": lambda v: f"{v:+.3f}" if pd.notna(v) else "—"}),
        use_container_width=True,
        height=380,
    )

    if st.button("Refresh news cache", key="ms_news_refresh"):
        query_news.clear()
        st.rerun()

# ── Footer ────────────────────────────────────────────────────────────────────

st.markdown("---")
st.caption(
    f"Signal threshold: {config.ml.signal_threshold} · "
    f"Confirmation: {config.ml.signal_confirmation}-of-3 models · "
    f"Quick mode: 5 epochs / 50 trees / 2 folds"
)
