"""
模块名称: hmm_model.py
所属层级: 制度识别层 (Regime)
输入来源: Binance REST API（训练时拉取历史 K 线）/ indicators Stream（推理时读取特征）
输出去向: regime_signal Stream（通过 regime worker 写入）
关键依赖: pandas, numpy, hmmlearn, aiohttp, structlog, joblib

修订记录:
- v1.0: 初始实现，HMM 离线训练 + 在线推理 + 自动制度映射 + RuleBased 降级
"""

from __future__ import annotations

import os
import pickle
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import structlog
from hmmlearn.hmm import GaussianHMM
from sklearn.exceptions import ConvergenceWarning
from sklearn.preprocessing import StandardScaler

# 抑制 hmmlearn 的 FutureWarning（sklearn API 兼容性）
warnings.filterwarnings("ignore", category=FutureWarning, module="hmmlearn")

logger = structlog.get_logger(__name__)


# ─── 常数 ─────────────────────────────────────────────────────

BINANCE_FAPI_BASE = "https://fapi.binance.com"

# 模型存储路径
_MODELS_DIR = Path(__file__).parent.parent / "models" / "hmm"

# 数据缓存路径
_CACHE_DIR = Path(__file__).parent.parent / "data" / "historical"

# 默认 HMM 参数
_DEFAULT_HMM_PARAMS: dict[str, Any] = {
    "n_components": 3,           # 3 个隐状态
    "covariance_type": "full",  # 每个状态有自己的协方差矩阵
    "n_iter": 200,               # EM 最大迭代次数
    "tol": 1e-3,                 # 收敛容差
    "random_state": 42,          # 可复现
}

# 训练数据量
TRAIN_LIMIT = 1000   # Binance API 单次最大 1000 条
MIN_TRAIN_SAMPLES = 500  # 最少 500 条才能训练

# 置信度阈值
HMM_CONFIDENCE_THRESHOLD = 0.6

# 训练新鲜度（7 天重新训练）
RETRAIN_DAYS = 7


# ─── 数据结构 ─────────────────────────────────────────────────


@dataclass
class HMMConfig:
    """HMM 模型配置"""
    n_components: int = 3
    covariance_type: str = "full"
    n_iter: int = 200
    tol: float = 1e-3
    random_state: int = 42
    lookback: int = 500       # 推理时使用的滚动窗口

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_components": self.n_components,
            "covariance_type": self.covariance_type,
            "n_iter": self.n_iter,
            "tol": self.tol,
            "random_state": self.random_state,
        }


@dataclass
class HMMModelArtifact:
    """保存/加载的模型工件"""
    model: GaussianHMM
    scaler: StandardScaler
    config: HMMConfig
    feature_names: list[str]
    state_regime_map: dict[int, str]
    train_timestamp: int  # Unix ms
    symbol: str
    timeframe: str


@dataclass
class HMMPrediction:
    """HMM 单次推理结果"""
    state: int              # 隐状态 ID (0, 1, 2)
    regime: str             # 映射后的制度
    confidence: float       # 后验概率 (0~1)
    state_probs: np.ndarray  # 所有状态的概率
    fallback_used: bool     # 是否降级到 RuleBased


# ─── 特征工程（纯 pandas/numpy，无 pandas_ta 依赖） ────────


def _compute_ema(series: pd.Series, period: int) -> pd.Series:
    """EMA 计算（避免引入 talib/pandas_ta）"""
    return series.ewm(span=period, adjust=False).mean()


def _compute_sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def _compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI 计算"""
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta.where(delta < 0, 0.0))

    avg_gain = gain.ewm(span=period, adjust=False).mean()
    avg_loss = loss.ewm(span=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def _compute_adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """ADX 计算"""
    prev_close = close.shift(1)

    # +DM 和 -DM
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low

    plus_dm = pd.Series(0.0, index=high.index)
    minus_dm = pd.Series(0.0, index=high.index)

    mask_up = (up_move > down_move) & (up_move > 0)
    mask_down = (down_move > up_move) & (down_move > 0)
    plus_dm[mask_up] = up_move[mask_up]
    minus_dm[mask_down] = down_move[mask_down]

    # TR
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # 平滑
    atr = _compute_ema(tr, period)
    plus_di = 100 * _compute_ema(plus_dm, period) / atr.replace(0, np.nan)
    minus_di = 100 * _compute_ema(minus_dm, period) / atr.replace(0, np.nan)

    # DX → ADX
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx = _compute_ema(dx, period)
    return adx


def _compute_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """ATR 计算"""
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return _compute_ema(tr, period)


def _compute_bb_width(close: pd.Series, period: int = 20, std_mult: float = 2.0) -> pd.Series:
    """布林带宽度 = (upper - lower) / mid"""
    sma = _compute_sma(close, period)
    std = close.rolling(window=period, min_periods=period).std(ddof=0)
    upper = sma + std_mult * std
    lower = sma - std_mult * std
    return (upper - lower) / sma.replace(0, np.nan)


def extract_features(df: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    """
    从 OHLCV DataFrame 提取 5 维 HMM 观测特征。
    纯 pandas/numpy 实现，无外部技术指标依赖。

    特征:
        0. log_return: 对数收益率
        1. atr_ratio: ATR(14) / close（归一化波动率）
        2. adx: 平均趋向指数（趋势强度）
        3. rsi: 相对强弱指标（动量位置）
        4. bb_width: (BB_upper - BB_lower) / BB_mid（波动收缩/扩张）

    参数:
        df: OHLCV DataFrame，必须含 high, low, close 列

    返回:
        (feature_matrix, feature_names)
        feature_matrix: shape (n_samples, 5)，已对齐（前 period 行为 NaN 被丢弃）
        feature_names: ["log_return", "atr_ratio", "adx", "rsi", "bb_width"]
    """
    required = ["high", "low", "close"]
    for col in required:
        if col not in df.columns or not pd.api.types.is_numeric_dtype(df[col]):
            raise ValueError(f"DataFrame 缺少有效的 '{col}' 列")

    close = df["close"]
    high = df["high"]
    low = df["low"]

    features = pd.DataFrame(index=df.index)

    # 1. log_return
    log_return = np.log(close / close.shift(1))
    features["log_return"] = log_return

    # 2. ATR_ratio = ATR(14) / close
    atr = _compute_atr(high, low, close, period=14)
    features["atr_ratio"] = atr / close.replace(0, np.nan)

    # 3. ADX(14)
    features["adx"] = _compute_adx(high, low, close, period=14)

    # 4. RSI(14)
    features["rsi"] = _compute_rsi(close, period=14)

    # 5. BB_width(20)
    features["bb_width"] = _compute_bb_width(close, period=20)

    # 丢弃所有包含 NaN 的行（前 ~20 行因 warmup 不足会产生 NaN）
    feature_names = ["log_return", "atr_ratio", "adx", "rsi", "bb_width"]
    features = features[feature_names]
    features = features.dropna()

    if len(features) < MIN_TRAIN_SAMPLES:
        logger.warning(
            "特征数据不足",
            samples=len(features),
            minimum=MIN_TRAIN_SAMPLES,
        )

    return features.values.astype(np.float64), feature_names


# ─── 数据获取（Binance REST API） ──────────────────────────


async def fetch_historical_klines(
    symbol: str,
    timeframe: str,
    limit: int = TRAIN_LIMIT,
    start_time: int | None = None,
    end_time: int | None = None,
) -> pd.DataFrame:
    """
    从 Binance Futures REST API 拉取历史 K 线数据。

    参数:
        symbol: 交易对，如 "BTCUSDT"
        timeframe: 时间周期，如 "1h"
        limit: 最大条数（Binance 限制 1000）
        start_time: 起始时间戳（毫秒）
        end_time: 结束时间戳（毫秒）

    返回:
        OHLCV DataFrame，列: open_time, open, high, low, close, volume
    """
    import aiohttp

    url = f"{BINANCE_FAPI_BASE}/fapi/v1/klines"
    params = {
        "symbol": symbol.upper(),
        "interval": _convert_tf_to_binance(timeframe),
        "limit": min(limit, 1000),
    }
    if start_time is not None:
        params["startTime"] = start_time
    if end_time is not None:
        params["endTime"] = end_time

    logger.info("拉取历史 K 线", symbol=symbol, timeframe=timeframe, limit=limit)

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as session:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.error("拉取 K 线失败", symbol=symbol, status=resp.status)
                    text = await resp.text()
                    logger.error("响应内容", text=text[:500])
                    return pd.DataFrame()

                data = await resp.json()

    except Exception as e:
        logger.error("请求 klines 异常", symbol=symbol, error=str(e))
        return pd.DataFrame()

    if not data:
        logger.warning("K 线数据为空", symbol=symbol)
        return pd.DataFrame()

    # Binance API 返回格式:
    # [open_time, open, high, low, close, volume, close_time, ...]
    records = []
    for k in data:
        records.append({
            "open_time": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })

    df = pd.DataFrame(records)
    df = df.sort_values("open_time").reset_index(drop=True)
    logger.info(
        "K 线数据已获取",
        symbol=symbol,
        count=len(df),
        time_range=f"{df['open_time'].iloc[0]} ~ {df['open_time'].iloc[-1]}",
    )
    return df


def _convert_tf_to_binance(timeframe: str) -> str:
    """将项目内 timeframe 格式转换为 Binance API 格式"""
    mapping = {
        "1m": "1m", "5m": "5m", "15m": "15m",
        "1h": "1h", "4h": "4h",
        "1d": "1d", "1w": "1w",
    }
    return mapping.get(timeframe, "1h")


# ─── 数据缓存（本地 Parquet） ───────────────────────────────


def get_cache_path(symbol: str, timeframe: str) -> Path:
    """获取本地缓存文件路径"""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f"{symbol}_{timeframe}.pkl"


def save_to_cache(df: pd.DataFrame, symbol: str, timeframe: str) -> bool:
    """将 DataFrame 缓存到本地 pickle 文件"""
    if df.empty:
        return False
    path = get_cache_path(symbol, timeframe)
    try:
        with open(path, "wb") as f:
            pickle.dump(df, f)
        logger.debug("数据已缓存", path=str(path), rows=len(df))
        return True
    except Exception as e:
        logger.error("缓存写入失败", path=str(path), error=str(e))
        return False


def load_from_cache(symbol: str, timeframe: str) -> pd.DataFrame:
    """从本地缓存加载 DataFrame"""
    path = get_cache_path(symbol, timeframe)
    if not path.exists():
        return pd.DataFrame()

    # 检查文件新鲜度（超过 7 天需要重新拉取）
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    if datetime.now(timezone.utc) - mtime > timedelta(days=RETRAIN_DAYS):
        logger.info("缓存已过期，将重新拉取", path=str(path), age_days=(datetime.now(timezone.utc) - mtime).days)
        return pd.DataFrame()

    try:
        with open(path, "rb") as f:
            df = pickle.load(f)
        logger.debug("缓存已加载", path=str(path), rows=len(df))
        return df
    except Exception as e:
        logger.error("缓存读取失败", path=str(path), error=str(e))
        return pd.DataFrame()


async def get_training_data(
    symbol: str,
    timeframe: str,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    获取训练数据：先尝试本地缓存，缓存缺失/过期则从 Binance 拉取。

    参数:
        symbol: 交易对
        timeframe: 时间周期
        force_refresh: 是否强制重新拉取（忽略缓存）

    返回:
        OHLCV DataFrame
    """
    if not force_refresh:
        df = load_from_cache(symbol, timeframe)
        if not df.empty and len(df) >= MIN_TRAIN_SAMPLES:
            return df

    df = await fetch_historical_klines(symbol, timeframe, limit=TRAIN_LIMIT)
    if not df.empty:
        save_to_cache(df, symbol, timeframe)
    return df


# ─── 状态 → 制度自动映射 ───────────────────────────────────


def _build_state_regime_map(
    model: GaussianHMM,
    observations: np.ndarray,
    feature_names: list[str],
) -> dict[int, str]:
    """
    根据 HMM 各隐状态的均值特征向量，自动映射到市场制度。

    规则（与 ARCH.md 第 7 节一致）:
        - bb_width 最高 + 波动最大 → HIGH_VOLATILITY
        - ADX 最高 + log_return 方向明确 → TRENDING
        - 其余 → RANGING

    参数:
        model: 已训练的 GaussianHMM
        observations: 训练用的观测矩阵
        feature_names: 特征名列表

    返回:
        {state_id: regime_name}
    """
    states = model.predict(observations)
    n_components = model.n_components

    # 收集每个状态的均值
    profiles = {}
    for s in range(n_components):
        mask = states == s
        if mask.sum() == 0:
            profiles[s] = {name: 0.0 for name in feature_names}
            continue
        state_obs = observations[mask]
        profiles[s] = {
            name: float(np.mean(state_obs[:, i]))
            for i, name in enumerate(feature_names)
        }

    # 按特征排序决定映射
    # 1. 挑出 bb_width 最高的 → HIGH_VOLATILITY
    bb_width_idx = feature_names.index("bb_width")
    high_vol_state = max(profiles.keys(), key=lambda s: profiles[s]["bb_width"])

    # 2. 剩余状态中挑 ADX 最高的 → TRENDING
    remaining = [s for s in range(n_components) if s != high_vol_state]
    adx_idx = feature_names.index("adx")
    trending_state = max(remaining, key=lambda s: profiles[s]["adx"])

    # 3. 剩下的 → RANGING
    ranging_state = [s for s in remaining if s != trending_state][0]

    regime_map = {
        trending_state: "TRENDING",
        ranging_state: "RANGING",
        high_vol_state: "HIGH_VOLATILITY",
    }

    logger.info("状态自动映射完成", mapping={str(k): v for k, v in regime_map.items()})
    for s, r in regime_map.items():
        logger.debug(
            "状态画像",
            state=s,
            regime=r,
            **{f"avg_{n}": f"{profiles[s][n]:.4f}" for n in feature_names},
        )

    return regime_map


# ─── HMM 训练器 ─────────────────────────────────────────────


class HMMTrainer:
    """
    HMM 模型训练器。

    职责:
        1. 获取历史数据（缓存或 Binance API）
        2. 提取 5 维特征
        3. 训练 GaussianHMM
        4. 自动映射状态到制度
        5. 保存模型到 models/hmm/

    用法:
        trainer = HMMTrainer()
        artifact = await trainer.train("BTCUSDT", "1h")
        trainer.save(artifact)
    """

    def __init__(self, config: HMMConfig | None = None):
        self.config = config or HMMConfig()

    async def train(
        self,
        symbol: str,
        timeframe: str,
        force_refresh: bool = False,
    ) -> HMMModelArtifact | None:
        """
        完整的训练流程。

        参数:
            symbol: 交易对
            timeframe: 时间周期
            force_refresh: 是否忽略缓存重新拉取

        返回:
            HMMModelArtifact，失败返回 None
        """
        logger.info("开始 HMM 训练", symbol=symbol, timeframe=timeframe)

        # 1. 获取数据
        df = await get_training_data(symbol, timeframe, force_refresh=force_refresh)
        if df.empty or len(df) < MIN_TRAIN_SAMPLES:
            logger.error(
                "训练数据不足",
                symbol=symbol,
                rows=len(df),
                minimum=MIN_TRAIN_SAMPLES,
            )
            return None

        logger.info("数据已就绪", symbol=symbol, rows=len(df))

        # 2. 提取特征
        observations, feature_names = extract_features(df)
        if len(observations) < MIN_TRAIN_SAMPLES:
            logger.error(
                "特征数据不足（含 NaN 丢弃后）",
                samples=len(observations),
                minimum=MIN_TRAIN_SAMPLES,
            )
            return None

        logger.info(
            "特征已提取",
            shape=list(observations.shape),
            feature_names=feature_names,
        )

        # 3. 标准化
        scaler = StandardScaler()
        scaled_obs = scaler.fit_transform(observations)

        # 4. 训练 HMM
        model = GaussianHMM(**self.config.to_dict())
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ConvergenceWarning)
            model.fit(scaled_obs)

        logger.info(
            "HMM 训练完成",
            n_components=model.n_components,
            log_likelihood=model.monitor_.history[-1] if model.monitor_.history else None,
            converged=model.monitor_.converged,
            iterations=model.monitor_.iter,
        )

        # 5. 自动映射状态 → 制度
        state_regime_map = _build_state_regime_map(model, scaled_obs, feature_names)

        # 6. 构建 artifact
        artifact = HMMModelArtifact(
            model=model,
            scaler=scaler,
            config=self.config,
            feature_names=feature_names,
            state_regime_map=state_regime_map,
            train_timestamp=int(datetime.now(timezone.utc).timestamp() * 1000),
            symbol=symbol,
            timeframe=timeframe,
        )

        return artifact

    def save(self, artifact: HMMModelArtifact) -> Path:
        """保存模型工件到磁盘"""
        _MODELS_DIR.mkdir(parents=True, exist_ok=True)
        path = _MODELS_DIR / f"{artifact.symbol}_{artifact.timeframe}.pkl"

        with open(path, "wb") as f:
            pickle.dump(artifact, f)

        logger.info("HMM 模型已保存", path=str(path))
        return path

    def load(self, symbol: str, timeframe: str) -> HMMModelArtifact | None:
        """从磁盘加载模型工件"""
        path = _MODELS_DIR / f"{symbol}_{timeframe}.pkl"
        if not path.exists():
            logger.warning("模型文件不存在", path=str(path))
            return None

        try:
            with open(path, "rb") as f:
                artifact = pickle.load(f)
            logger.debug("HMM 模型已加载", path=str(path))
            return artifact
        except Exception as e:
            logger.error("模型加载失败", path=str(path), error=str(e))
            return None

    def needs_retrain(self, symbol: str, timeframe: str) -> bool:
        """检查是否需要重新训练（超过 RETRAIN_DAYS 天）"""
        artifact = self.load(symbol, timeframe)
        if artifact is None:
            return True

        last_train = datetime.fromtimestamp(artifact.train_timestamp / 1000, tz=timezone.utc)
        age = datetime.now(timezone.utc) - last_train
        return age > timedelta(days=RETRAIN_DAYS)


# ─── HMM 推理器 ─────────────────────────────────────────────


class HMMClassifier:
    """
    HMM 在线分类器。

    职责:
        1. 加载已训练的模型
        2. 接收特征向量，预测隐状态
        3. 映射为制度
        4. 计算置信度
        5. 必要时降级到 RuleBasedDetector

    用法:
        classifier = HMMClassifier()
        classifier.load_or_fallback("BTCUSDT", "1h")
        prediction = classifier.classify(feature_vector)
    """

    def __init__(self):
        self._artifact: HMMModelArtifact | None = None
        self._fallback_detector = None  # 延迟导入

    def load_or_fallback(
        self,
        symbol: str,
        timeframe: str,
        trainer: HMMTrainer | None = None,
    ) -> bool:
        """
        加载模型。如果模型不存在或过期，尝试训练。

        返回:
            True 表示成功加载模型，False 表示只能降级到 RuleBased
        """
        if trainer is None:
            trainer = HMMTrainer()

        artifact = trainer.load(symbol, timeframe)
        if artifact is not None:
            self._artifact = artifact
            return True

        logger.warning(
            "HMM 模型未找到，后续将降级到 RuleBasedDetector",
            symbol=symbol,
            timeframe=timeframe,
        )
        return False

    def classify(
        self,
        feature_vector: np.ndarray | list[float],
    ) -> HMMPrediction:
        """
        对当前特征向量进行分类。

        参数:
            feature_vector: shape (5,) 的特征向量
                [log_return, atr_ratio, adx, rsi, bb_width]

        返回:
            HMMPrediction
        """
        if self._artifact is None:
            return self._fallback_prediction()

        model = self._artifact.model
        scaler = self._artifact.scaler

        # 确保是 2D
        fv = np.asarray(feature_vector, dtype=np.float64).reshape(1, -1)

        # 标准化
        fv_scaled = scaler.transform(fv)

        # 预测
        state = model.predict(fv_scaled)[0]

        # 后验概率
        state_probs = model.predict_proba(fv_scaled)[0]
        confidence = float(state_probs[state])

        # 映射到制度
        regime = self._artifact.state_regime_map.get(state, "UNKNOWN")

        fallback_used = False

        # 低置信度降级
        if confidence < HMM_CONFIDENCE_THRESHOLD:
            logger.debug(
                "HMM 置信度不足，降级到 RuleBased",
                state=state,
                confidence=confidence,
                threshold=HMM_CONFIDENCE_THRESHOLD,
            )
            return self._fallback_prediction()

        return HMMPrediction(
            state=int(state),
            regime=regime,
            confidence=confidence,
            state_probs=state_probs,
            fallback_used=fallback_used,
        )

    def _fallback_prediction(self) -> HMMPrediction:
        """降级到 RuleBasedDetector"""
        from regime.detector import RuleBasedDetector

        if self._fallback_detector is None:
            self._fallback_detector = RuleBasedDetector()

        # 如果没有特征数据，返回 UNKNOWN
        n_states = self._artifact.model.n_components if self._artifact else 3
        return HMMPrediction(
            state=-1,
            regime="UNKNOWN",
            confidence=0.0,
            state_probs=np.full(n_states, 1.0 / n_states),
            fallback_used=True,
        )

    def get_feature_vector_from_df(self, df: pd.DataFrame) -> np.ndarray:
        """
        从 DataFrame 中提取最新一行的特征向量。

        参数:
            df: 包含足够列的 OHLCV DataFrame

        返回:
            shape (5,) 的特征向量
        """
        _, feature_names = extract_features(df)
        features_df = pd.DataFrame(index=df.index)

        close = df["close"]
        high = df["high"]
        low = df["low"]

        features_df["log_return"] = np.log(close / close.shift(1))
        features_df["atr_ratio"] = _compute_atr(high, low, close, 14) / close.replace(0, np.nan)
        features_df["adx"] = _compute_adx(high, low, close, 14)
        features_df["rsi"] = _compute_rsi(close, 14)
        features_df["bb_width"] = _compute_bb_width(close, 20)

        latest = features_df[feature_names].iloc[-1:].dropna()
        if latest.empty:
            logger.warning("最新行特征包含 NaN，无法分类")
            return np.full(5, np.nan)

        return latest.values[0]


# ─── 便捷函数（供 regime worker 调用） ─────────────────────


async def train_and_save(
    symbol: str = "BTCUSDT",
    timeframe: str = "1h",
    force_refresh: bool = False,
) -> bool:
    """
    一站式训练函数：训练 HMM 并保存模型。

    参数:
        symbol: 交易对
        timeframe: 时间周期
        force_refresh: 是否强制重新拉取数据

    返回:
        True 表示训练成功
    """
    trainer = HMMTrainer()
    artifact = await trainer.train(symbol, timeframe, force_refresh=force_refresh)
    if artifact is None:
        return False
    trainer.save(artifact)
    return True


def check_and_retrain_if_needed(
    symbol: str = "BTCUSDT",
    timeframe: str = "1h",
) -> bool:
    """
    检查模型是否需要重新训练（超过 RETRAIN_DAYS），
    如需则触发异步训练。

    返回:
        True 表示模型有效（或已触发异步训练），False 表示无模型
    """
    trainer = HMMTrainer()

    if trainer.needs_retrain(symbol, timeframe):
        logger.info("模型需要重新训练，触发异步训练", symbol=symbol, timeframe=timeframe)
        import asyncio
        asyncio.ensure_future(train_and_save(symbol, timeframe))
        return True

    return trainer.load(symbol, timeframe) is not None


# ─── 公开 API ────────────────────────────────────────────────

__all__ = [
    "HMMConfig",
    "HMMModelArtifact",
    "HMMPrediction",
    "HMMTrainer",
    "HMMClassifier",
    "extract_features",
    "fetch_historical_klines",
    "get_training_data",
    "train_and_save",
    "check_and_retrain_if_needed",
    "HMM_CONFIDENCE_THRESHOLD",
    "RETRAIN_DAYS",
]
