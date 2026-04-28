"""
LSTM momentum model.

Architecture:
  - 2-layer LSTM, hidden_size=128, dropout=0.2
  - Linear output layer → tanh → score in [-1, 1]
  - Input: sequence of (seq_len, n_features) normalised bars
  - Target: sign of forward 5-bar return

Training normalisation uses only training-window statistics (no lookahead).
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

from config.settings import config
from core.logger import get_logger
from models.base_model import BaseModel

log = get_logger("models.lstm")

# ── Feature columns fed into the LSTM ─────────────────────────────────────────
_FEATURE_COLS = [
    "Open", "High", "Low", "Close", "Volume",
    "rsi_14", "macd", "macd_signal", "macd_hist",
    "bb_upper", "bb_middle", "bb_lower",
    "ema_9", "ema_21", "ema_50",
    "atr_14", "volume_sma_20",
]


class DatasetBuilder:
    """Converts a bar DataFrame into (X, y) tensors for the LSTM."""

    def __init__(self, seq_len: int, feature_cols: list[str]) -> None:
        self.seq_len      = seq_len
        self.feature_cols = feature_cols
        self._mean: pd.Series | None = None   # stored as ndarray in checkpoints
        self._std:  pd.Series | None = None   # stored as ndarray in checkpoints

    def fit(self, df: pd.DataFrame) -> None:
        """Compute normalisation stats from training data only."""
        cols = [c for c in self.feature_cols if c in df.columns]
        self._mean = df[cols].mean()
        self._std  = df[cols].std().replace(0, 1)

    def transform(self, df: pd.DataFrame) -> np.ndarray:
        """
        Apply training-window normalisation.  Columns missing from df are
        filled with 0 after normalisation.
        """
        assert self._mean is not None, "Call fit() before transform()"
        # Build a frame aligned to all feature cols, filling missing with 0
        aligned = pd.DataFrame(index=df.index, columns=self.feature_cols, dtype=float)
        for col in self.feature_cols:
            if col in df.columns:
                aligned[col] = df[col]
            else:
                aligned[col] = 0.0
        normed = (aligned - self._mean) / self._std
        return normed.fillna(0).values.astype(np.float32)

    def build(self, df: pd.DataFrame, forward_bars: int = 5) -> tuple[np.ndarray, np.ndarray]:
        """
        Build (X, y) arrays.
          X shape: (n_samples, seq_len, n_features)
          y shape: (n_samples,) — sign of forward `forward_bars` return
        """
        data   = self.transform(df)
        closes = df["Close"].values if "Close" in df.columns else np.ones(len(df))

        X, y = [], []
        for i in range(self.seq_len, len(data) - forward_bars):
            X.append(data[i - self.seq_len : i])
            fwd_return = (closes[i + forward_bars] - closes[i]) / closes[i]
            y.append(1.0 if fwd_return > 0 else -1.0)

        return np.array(X, dtype=np.float32), np.array(y, dtype=np.float32)


class _LSTMNet:
    """PyTorch LSTM network (imported lazily to avoid hard dependency at import time)."""

    def __init__(self, n_features: int, hidden: int, layers: int, dropout: float) -> None:
        import torch
        import torch.nn as nn

        class _Net(nn.Module):
            def __init__(self):
                super().__init__()
                self.lstm = nn.LSTM(
                    input_size=n_features,
                    hidden_size=hidden,
                    num_layers=layers,
                    dropout=dropout if layers > 1 else 0.0,
                    batch_first=True,
                )
                self.fc = nn.Linear(hidden, 1)

            def forward(self, x):
                out, _ = self.lstm(x)
                return torch.tanh(self.fc(out[:, -1, :]))

        self.net = _Net()

    def parameters(self):
        return self.net.parameters()

    def train(self):
        self.net.train()

    def eval(self):
        self.net.eval()

    def __call__(self, x):
        return self.net(x)

    def state_dict(self):
        return self.net.state_dict()

    def load_state_dict(self, sd):
        self.net.load_state_dict(sd)


class LSTMModel(BaseModel):

    def __init__(self) -> None:
        cfg = config.ml
        self._seq_len     = cfg.lstm_sequence_length
        self._hidden      = cfg.lstm_hidden_size
        self._layers      = cfg.lstm_num_layers
        self._dropout     = cfg.lstm_dropout
        self._epochs      = cfg.lstm_epochs
        self._batch_size  = cfg.lstm_batch_size
        self._lr          = cfg.lstm_learning_rate

        self._dataset  = DatasetBuilder(self._seq_len, _FEATURE_COLS)
        self._net: _LSTMNet | None = None

    @property
    def name(self) -> str:
        return "lstm"

    def train(self, train_df: pd.DataFrame) -> None:
        import torch
        import torch.nn as nn
        from torch.utils.data import DataLoader, TensorDataset

        self._dataset.fit(train_df)
        X, y = self._dataset.build(train_df)

        if len(X) < self._batch_size:
            log.warning("LSTM: too few samples (%d) to train meaningfully", len(X))
            return

        n_features = X.shape[2]
        self._net  = _LSTMNet(n_features, self._hidden, self._layers, self._dropout)

        loader = DataLoader(
            TensorDataset(torch.from_numpy(X), torch.from_numpy(y).unsqueeze(1)),
            batch_size=self._batch_size,
            shuffle=True,
        )

        optimiser = torch.optim.Adam(self._net.parameters(), lr=self._lr)
        criterion = nn.MSELoss()
        self._net.train()

        for epoch in range(self._epochs):
            total_loss = 0.0
            for xb, yb in loader:
                optimiser.zero_grad()
                pred = self._net(xb)
                loss = criterion(pred, yb)
                loss.backward()
                optimiser.step()
                total_loss += loss.item()
            if (epoch + 1) % 10 == 0:
                log.debug("LSTM epoch %d/%d - loss=%.4f", epoch + 1, self._epochs, total_loss)

        log.info("LSTM training complete (%d samples, %d epochs)", len(X), self._epochs)

    def predict(self, df: pd.DataFrame) -> float:
        if self._net is None:
            return 0.0
        import torch
        data = self._dataset.transform(df)
        if len(data) < self._seq_len:
            return 0.0
        seq = torch.from_numpy(data[-self._seq_len:]).unsqueeze(0)
        self._net.eval()
        with torch.no_grad():
            score = float(self._net(seq).item())
        return max(-1.0, min(1.0, score))

    def evaluate(self, test_df: pd.DataFrame) -> dict:
        if self._net is None or "Close" not in test_df.columns:
            return {"total_return": 0.0, "sharpe_ratio": 0.0}

        data = self._dataset.transform(test_df)
        closes = test_df["Close"].values

        import torch
        scores = []
        self._net.eval()
        with torch.no_grad():
            for i in range(self._seq_len, len(data)):
                seq   = torch.from_numpy(data[i - self._seq_len : i]).unsqueeze(0)
                score = float(self._net(seq).item())
                scores.append(score)

        if not scores:
            return {"total_return": 0.0, "sharpe_ratio": 0.0}

        score_s = pd.Series(scores, index=test_df.index[self._seq_len:])
        price_s = pd.Series(closes[self._seq_len:], index=test_df.index[self._seq_len:])
        return self._returns_metrics(score_s, price_s)

    def score_series(self, df: pd.DataFrame) -> pd.Series:
        """
        Compute a per-bar LSTM score for every bar in df that has enough history.

        The first ``seq_len`` bars have NaN (no complete context window yet).
        The returned Series shares df's DatetimeIndex so it can be overlaid
        directly on price or indicator charts.
        """
        if self._net is None or len(df) < self._seq_len:
            return pd.Series(float("nan"), index=df.index)
        import torch
        data   = self._dataset.transform(df)
        nans   = [float("nan")] * self._seq_len
        scores: list[float] = []
        self._net.eval()
        with torch.no_grad():
            for i in range(self._seq_len, len(data)):
                seq = torch.from_numpy(data[i - self._seq_len : i]).unsqueeze(0)
                scores.append(float(self._net(seq).item()))
        return pd.Series(nans + scores, index=df.index)

    def save(self, path: str | Path) -> None:
        import torch
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if self._net:
            # Store mean/std as torch tensors and feature_cols as a plain list so
            # the checkpoint loads cleanly with weights_only=True (PyTorch ≥ 2.6).
            mean_t = torch.tensor(self._dataset._mean.values, dtype=torch.float64) \
                     if self._dataset._mean is not None else None
            std_t  = torch.tensor(self._dataset._std.values,  dtype=torch.float64) \
                     if self._dataset._std  is not None else None
            torch.save({
                "state_dict":   self._net.state_dict(),
                "mean":         mean_t,
                "std":          std_t,
                "feature_cols": list(self._dataset.feature_cols),
            }, path)
            log.info("LSTM saved to %s", path)

    def load(self, path: str | Path) -> None:
        import torch
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"No LSTM checkpoint at {path}")
        # weights_only=True is safe: checkpoint stores only tensors + plain lists.
        # Old checkpoints (pre-fix) stored pd.Series; fall back to weights_only=False
        # for those so they still load, then warn once to retrain.
        try:
            ckpt = torch.load(path, map_location="cpu", weights_only=True)
        except Exception:
            log.warning(
                "LSTM checkpoint at %s uses an old format (pd.Series). "
                "Loading with weights_only=False. Retrain to update the checkpoint.",
                path,
            )
            ckpt = torch.load(path, map_location="cpu", weights_only=False)

        n_features = len(_FEATURE_COLS)
        self._net  = _LSTMNet(n_features, self._hidden, self._layers, self._dropout)
        self._net.load_state_dict(ckpt["state_dict"])

        cols     = ckpt.get("feature_cols", _FEATURE_COLS)
        mean_raw = ckpt["mean"]
        std_raw  = ckpt["std"]
        # New format: tensors → convert to Series. Old format: already Series.
        if mean_raw is not None and not isinstance(mean_raw, pd.Series):
            mean_raw = pd.Series(mean_raw.numpy(), index=cols)
        if std_raw is not None and not isinstance(std_raw, pd.Series):
            std_raw = pd.Series(std_raw.numpy(), index=cols)
        self._dataset._mean = mean_raw
        self._dataset._std  = std_raw
        log.info("LSTM loaded from %s", path)
