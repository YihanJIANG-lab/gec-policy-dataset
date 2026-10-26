# -*- coding: utf-8 -*-
"""Table 2 模型库：baseline 与事件增强变体。

每个模型统一接口：fit_predict(train_y, train_x, test_x, horizon=1) -> float
  train_y: list[float] 历史目标；train_x/test_x: list[list[float]] 外生变量（可为 None）
可用性探测：prophet / torch / chronos 缺装则标记 unavailable，从结果剔除并说明。
"""
import logging
import math
import warnings
import hashlib
import os
from pathlib import Path

import numpy as np

warnings.filterwarnings("ignore")
logger = logging.getLogger("forecast_models")
MODEL_SEED = 42
TORCH_THREADS = 1
CHRONOS_REVISION = "29d808298f1a62493e7b9a5e08529d0d930fa189"

# ---------------- 统计基线 ----------------

def naive(train_y, train_x, test_x):
    return train_y[-1]


def seasonal_naive(train_y, train_x, test_x):
    return train_y[-4] if len(train_y) >= 4 else train_y[-1]


def _arima_order(train_y):
    from statsmodels.tsa.arima.model import ARIMA
    best, best_aic = (1, 1, 0), np.inf
    for order in [(1, 1, 0), (0, 1, 1), (1, 1, 1), (2, 1, 0), (0, 1, 0)]:
        try:
            fit = ARIMA(train_y, order=order).fit()
            if fit.aic < best_aic:
                best, best_aic = order, fit.aic
        except Exception:
            continue
    return best


def arima(train_y, train_x, test_x):
    from statsmodels.tsa.arima.model import ARIMA
    order = _arima_order(train_y)
    fit = ARIMA(train_y, order=order).fit()
    return float(np.asarray(fit.forecast(1)).ravel()[0])


def arimax(train_y, train_x, test_x):
    from statsmodels.tsa.arima.model import ARIMA
    order = _arima_order(train_y)
    fit = ARIMA(train_y, order=order, exog=np.array(train_x)).fit()
    fc = fit.forecast(1, exog=np.array(test_x, ndmin=2))
    return float(np.asarray(fc).ravel()[0])


# ---------------- Prophet ----------------

def _prophet_available():
    try:
        import prophet  # noqa: F401
        return True
    except Exception:
        return False


def _prophet_fit(train_y, train_x, periods):
    from prophet import Prophet
    import pandas as pd
    df = pd.DataFrame({"ds": periods, "y": train_y})
    m = Prophet(weekly_seasonality=False, daily_seasonality=False,
                changepoint_prior_scale=0.05)
    cols = []
    if train_x is not None:
        for i, name in enumerate(f"x{k}" for k in range(len(train_x[0]))):
            df[name] = [x[i] for x in train_x]
            m.add_regressor(name)
            cols.append(name)
    m.fit(df)
    return m, cols


def prophet_base(train_y, train_x, test_x, periods, next_period):
    m, _ = _prophet_fit(train_y, None, periods)
    import pandas as pd
    fut = pd.DataFrame({"ds": [next_period]})
    return float(m.predict(fut)["yhat"].iloc[0])


def prophet_exog(train_y, train_x, test_x, periods, next_period):
    import pandas as pd
    m, cols = _prophet_fit(train_y, train_x, periods)
    fut = pd.DataFrame({"ds": [next_period]})
    for i, name in enumerate(cols):
        fut[name] = [test_x[i]]
    return float(m.predict(fut)["yhat"].iloc[0])


# ---------------- torch 小模型 ----------------

def _torch_available():
    try:
        import torch  # noqa: F401
        return True
    except Exception:
        return False


class _LSTMNet:
    @staticmethod
    def build(n_feat, hidden=32):
        import torch.nn as nn

        class Net(nn.Module):
            def __init__(self):
                super().__init__()
                self.lstm = nn.LSTM(n_feat, hidden, batch_first=True)
                self.head = nn.Linear(hidden, 1)

            def forward(self, x):
                out, _ = self.lstm(x)
                return self.head(out[:, -1, :])
        return Net()


class _PatchTSTNet:
    @staticmethod
    def build(seq_len, n_feat, patch=4, d_model=32, n_heads=4, n_layers=2):
        import torch
        import torch.nn as nn

        class Net(nn.Module):
            def __init__(self):
                super().__init__()
                self.patch = patch
                self.n_patches = seq_len // patch
                self.embed = nn.Linear(patch * n_feat, d_model)
                self.pos = nn.Parameter(torch.randn(1, self.n_patches, d_model) * 0.02)
                layer = nn.TransformerEncoderLayer(
                    d_model, n_heads, dim_feedforward=64, batch_first=True,
                    dropout=0.1, activation="gelu")
                self.enc = nn.TransformerEncoder(layer, n_layers)
                self.head = nn.Linear(self.n_patches * d_model, 1)

            def forward(self, x):
                b = x.shape[0]
                x = x.reshape(b, self.n_patches, self.patch * x.shape[-1])
                z = self.embed(x) + self.pos
                z = self.enc(z)
                return self.head(z.flatten(1))
        return Net()


def _train_torch(net_fn, train_y, train_x, seq_len, epochs=300, lr=0.01, seed=42):
    """训练并做 1 步预测。train_x 为 None 时仅用 y 单变量。"""
    import torch
    import torch.nn as nn

    torch.set_num_threads(TORCH_THREADS)
    torch.use_deterministic_algorithms(True)
    torch.manual_seed(seed)
    feats = []
    for i in range(len(train_y)):
        row = [train_y[i]]
        if train_x is not None:
            row += list(train_x[i])
        feats.append(row)
    X = np.array(feats, dtype=np.float32)
    mu, sd = X.mean(0), X.std(0) + 1e-8
    Xs = (X - mu) / sd
    n_feat = Xs.shape[1]
    xs, ys = [], []
    for t in range(seq_len, len(Xs)):
        xs.append(Xs[t - seq_len:t])
        ys.append(Xs[t, 0])
    xs = torch.tensor(np.array(xs))
    ys = torch.tensor(np.array(ys)).unsqueeze(1)
    net = net_fn(n_feat)
    opt = torch.optim.Adam(net.parameters(), lr=lr)
    lossf = nn.MSELoss()
    net.train()
    for _ in range(epochs):
        opt.zero_grad()
        loss = lossf(net(xs), ys)
        loss.backward()
        opt.step()
    net.eval()
    with torch.no_grad():
        last = torch.tensor(Xs[-seq_len:]).unsqueeze(0)
        pred = net(last).item()
    return pred * sd[0] + mu[0]


def lstm_base(train_y, train_x, test_x, seq_len):
    return _train_torch(lambda nf: _LSTMNet.build(nf), train_y, None, seq_len)


def lstm_exog(train_y, train_x, test_x, seq_len):
    # 将 test 点的外生变量追加一行参与滑窗（仅用其外生信息，目标未知）
    full_x = list(train_x) + [list(test_x)]
    return _train_torch_exog_tail(train_y, full_x, seq_len, patch=False)


def patchtst_base(train_y, train_x, test_x, seq_len):
    return _train_torch(lambda nf: _PatchTSTNet.build(seq_len, nf), train_y, None, seq_len)


def patchtst_exog(train_y, train_x, test_x, seq_len):
    full_x = list(train_x) + [list(test_x)]
    return _train_torch_exog_tail(train_y, full_x, seq_len, patch=True)


def _train_torch_exog_tail(train_y, full_x, seq_len, patch):
    """v4.2: identical target-aligned y-history/x-next layout at fit and predict."""
    import torch
    import torch.nn as nn

    torch.set_num_threads(TORCH_THREADS)
    torch.use_deterministic_algorithms(True)
    torch.manual_seed(MODEL_SEED)
    train_windows, targets, inference, mu, sd = make_exog_windows(
        train_y, full_x[:-1], full_x[-1], seq_len)
    n_feat = train_windows.shape[-1]
    xs = torch.tensor(train_windows, dtype=torch.float32)
    ys = torch.tensor(targets, dtype=torch.float32).unsqueeze(1)
    if patch:
        net = _PatchTSTNet.build(seq_len, n_feat)
    else:
        net = _LSTMNet.build(n_feat)
    opt = torch.optim.Adam(net.parameters(), lr=0.01)
    lossf = nn.MSELoss()
    net.train()
    for _ in range(300):
        opt.zero_grad()
        loss = lossf(net(xs), ys)
        loss.backward()
        opt.step()
    net.eval()
    with torch.no_grad():
        pred = net(torch.tensor(inference, dtype=torch.float32).unsqueeze(0)).item()
    return pred * sd[0] + mu[0]


def make_exog_windows(train_y, train_x, test_x, seq_len):
    """Pure preprocessing used by BOTH neural event models (no torch required).

    To predict y[j], rows are [y[i], x[i+1]] for i=j-L..j-1.
    Every historical y occurs once per window. The inference window uses exactly
    the same layout with final row [y[n-1], test_x]. Scalers fit historical
    train_y/train_x only: test_x never participates in their fit.
    """
    y = np.asarray(train_y, dtype=np.float32)
    x = np.asarray(train_x, dtype=np.float32)
    tx = np.asarray(test_x, dtype=np.float32)
    if len(y) <= seq_len or x.ndim != 2 or len(x) != len(y) or tx.shape != x.shape[1:]:
        raise ValueError("Invalid or too-short aligned neural training inputs")
    history = np.column_stack([y, x])
    mu, sd = history.mean(axis=0), history.std(axis=0) + 1e-8
    ys = (y-mu[0])/sd[0]
    xs = (x-mu[1:])/sd[1:]
    txs = (tx-mu[1:])/sd[1:]
    windows = [np.column_stack([ys[j-seq_len:j], xs[j-seq_len+1:j+1]])
               for j in range(seq_len, len(y))]
    inference = np.column_stack([ys[-seq_len:], np.vstack([xs[-seq_len+1:], txs])])
    return np.asarray(windows, dtype=np.float32), ys[seq_len:], inference.astype(np.float32), mu, sd


# ---------------- Chronos 零样本 ----------------

_CHRONOS_PIPE = None

def _chronos_available():
    try:
        import chronos  # noqa: F401
        import torch  # noqa: F401
        return True
    except Exception:
        return False


def chronos_zs(train_y, train_x, test_x):
    global _CHRONOS_PIPE
    import torch
    torch.set_num_threads(TORCH_THREADS)
    torch.use_deterministic_algorithms(True)
    if _CHRONOS_PIPE is None:
        from chronos import ChronosPipeline
        _CHRONOS_PIPE = ChronosPipeline.from_pretrained(
            str(chronos_model_path()), device_map="cpu", torch_dtype=torch.float32,
            local_files_only=True)
    ctx = torch.tensor(np.array(train_y, dtype=np.float32))
    with torch.random.fork_rng(devices=[]):
        torch.manual_seed(MODEL_SEED)
        fc = _CHRONOS_PIPE.predict(ctx, prediction_length=1, num_samples=20)
    return float(fc[0].median().item())


def chronos_model_path():
    explicit = os.environ.get("GEC_CHRONOS_MODEL_PATH")
    candidates = ([Path(explicit)] if explicit else []) + [
        Path(__file__).resolve().parents[2] / "models/chronos-t5-tiny",
        Path.home() / ".cache/huggingface/hub/models--amazon--chronos-t5-tiny/snapshots" / CHRONOS_REVISION]
    for candidate in candidates:
        if candidate.is_dir() and (candidate / "config.json").is_file():
            return candidate.resolve()
    raise FileNotFoundError("Pinned local Chronos weights missing; set GEC_CHRONOS_MODEL_PATH. Network fallback is disabled.")


def chronos_asset_metadata():
    folder = chronos_model_path()
    return {"model": "amazon/chronos-t5-tiny", "revision": CHRONOS_REVISION,
            "files_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in sorted(folder.iterdir()) if p.is_file()}}


# ---------------- 注册表 ----------------

def model_registry(seq_len):
    reg = {
        "Naive": {"fn": naive, "exog": False},
        "SNaive(4)": {"fn": seasonal_naive, "exog": False},
        "ARIMA": {"fn": arima, "exog": False},
        "ARIMA+Event": {"fn": arimax, "exog": True},
        "ARIMA+Event-lag1": {"fn": arimax, "exog": True, "lag": 1},
        "LSTM": {"fn": lambda y, x, tx: lstm_base(y, x, tx, seq_len), "exog": False,
                 "requires": "torch"},
        "LSTM+Event": {"fn": lambda y, x, tx: lstm_exog(y, x, tx, seq_len), "exog": True,
                       "requires": "torch"},
        "LSTM+Event-lag1": {"fn": lambda y, x, tx: lstm_exog(y, x, tx, seq_len), "exog": True,
                            "requires": "torch", "lag": 1},
        "PatchTST": {"fn": lambda y, x, tx: patchtst_base(y, x, tx, seq_len), "exog": False,
                     "requires": "torch"},
        "PatchTST+Event": {"fn": lambda y, x, tx: patchtst_exog(y, x, tx, seq_len), "exog": True,
                           "requires": "torch"},
        "PatchTST+Event-lag1": {"fn": lambda y, x, tx: patchtst_exog(y, x, tx, seq_len),
                                "exog": True, "requires": "torch", "lag": 1},
        "Chronos-t5-tiny(zs)": {"fn": chronos_zs, "exog": False, "requires": "chronos"},
    }
    if _prophet_available():
        reg["Prophet"] = {"fn": None, "exog": False, "prophet": True}
        reg["Prophet+Event"] = {"fn": None, "exog": True, "prophet": True}
        reg["Prophet+Event-lag1"] = {"fn": None, "exog": True, "prophet": True, "lag": 1}
    return reg


def availability():
    return {"torch": _torch_available(), "chronos": _chronos_available(),
            "prophet": _prophet_available()}
