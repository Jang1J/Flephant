"""
KR-Rebound-CNN StrategyCard Builder
- 학습된 모델로 종목별 rebound probability 예측
- StrategyCard 스키마 호환 출력
- artifacts/strategy_card_variants/rebound/SC-{date}.json 저장

Usage:
    python jobs/build_strategy_card_rebound.py 20260325
    python jobs/build_strategy_card_rebound.py 20260325 --publish  # 바로 publish
"""

import sys
import json
import pickle
import argparse
import shutil
from pathlib import Path
from datetime import datetime

_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE_DIR))

import numpy as np
import pandas as pd
import yaml

from connectors import make_snapshot_dt
from models.rebound_cnn.preprocess import compute_sector_relative_features, make_chart_tensor

# ── 경로 설정 ──────────────────────────────────────────────────
UNIVERSE_PATH = _BASE_DIR / "config" / "universe_v1.csv"
MODEL_DIR = _BASE_DIR / "models" / "rebound_cnn"
MODEL_CONFIG_PATH = MODEL_DIR / "config.yaml"
MODEL_PT_PATH = MODEL_DIR / "model.pt"
CALIBRATOR_PATH = MODEL_DIR / "calibrator.pkl"
SCALER_PATH = MODEL_DIR / "context_scaler.pkl"
OUTPUT_DIR = _BASE_DIR / "artifacts" / "strategy_card_variants" / "rebound"
SC_SCHEMA_PATH = _BASE_DIR / "schemas" / "strategy_card.json"
DMP_DIR = _BASE_DIR / "artifacts" / "daily_market_packet"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)



def load_model_config() -> dict:
    """모델 config.yaml 로드"""
    with open(MODEL_CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_universe() -> dict:
    """유니버스 CSV 로드 → {ticker: name} 매핑"""
    import pandas as pd
    uni = pd.read_csv(UNIVERSE_PATH)
    return {str(r["ticker"]).zfill(6): r["name"] for _, r in uni.iterrows()}


def load_dmp(target_date: str) -> dict:
    """DMP 로드"""
    dmp_path = DMP_DIR / f"DMP-{target_date}.json"
    if not dmp_path.exists():
        raise FileNotFoundError(f"[SCEmitter] DMP 파일 없음: {dmp_path}")
    with open(dmp_path) as f:
        return json.load(f)


def load_ohlcv_panel(target_date: str, lookback: int = 25) -> dict:
    """최근 lookback일 DMP를 스캔하여 종목별 OHLCV 시계열을 구성.

    PIT-Safety: target_date 이전(<=) 파일만 로드하여 미래 데이터 유입 차단.
    반환: {ticker: [{date, open, high, low, close, volume, tech_features}, ...]}
    """
    dmp_files = sorted(DMP_DIR.glob("DMP-*.json"))

    # target_date 이하 파일만 필터 (PIT-safe)
    valid_files = [f for f in dmp_files if f.stem.replace("DMP-", "") <= target_date]
    recent_files = valid_files[-lookback:]

    panel: dict = {}  # {ticker: [day_data, ...]}
    for dmp_path in recent_files:
        date_str = dmp_path.stem.replace("DMP-", "")
        try:
            with open(dmp_path, encoding="utf-8") as f:
                dmp = json.load(f)
        except Exception as e:
            print(f"[SCEmitter] DMP 로드 실패 ({dmp_path.name}): {e}")
            continue
        market_data = dmp.get("market_data", {})
        for ticker, tdata in market_data.items():
            ohlcv = tdata.get("ohlcv", {})
            if ticker not in panel:
                panel[ticker] = []
            panel[ticker].append({
                "date": date_str,
                "open": float(ohlcv.get("open", 0)),
                "high": float(ohlcv.get("high", 0)),
                "low": float(ohlcv.get("low", 0)),
                "close": float(ohlcv.get("close", 0)),
                "volume": float(tdata.get("volume", 0)),
                "tech_features": tdata.get("tech_features", {}),
            })
    return panel


def extract_ohlcv_history(dmp: dict, ticker: str, lookback_days: int) -> list:
    """
    DMP market_data에서 최근 OHLCV 추출.
    DMP는 단일 일자 스냅샷이므로, historical_ohlcv 필드를 우선 사용하고
    없으면 당일 OHLCV를 단일 포인트로 반환한다.
    (레거시 호환용 — main()에서는 load_ohlcv_panel을 사용)
    """
    market_data = dmp.get("market_data", {})
    ticker_data = market_data.get(ticker, {})

    # historical_ohlcv 필드가 있으면 사용 (리스트 of dict)
    hist = ticker_data.get("historical_ohlcv", [])
    if hist:
        return hist[-lookback_days:]

    # 없으면 당일 단일 포인트만 존재
    ohlcv = ticker_data.get("ohlcv", {})
    if ohlcv:
        return [{
            "open": ohlcv.get("open", 0),
            "high": ohlcv.get("high", 0),
            "low": ohlcv.get("low", 0),
            "close": ohlcv.get("close", 0),
            "volume": ticker_data.get("volume", 0),
        }]
    return []


def extract_tech_features(dmp: dict, ticker: str) -> dict:
    """DMP에서 종목의 tech_features 추출"""
    market_data = dmp.get("market_data", {})
    ticker_data = market_data.get(ticker, {})
    return ticker_data.get("tech_features", {})


def extract_macro_features(dmp: dict) -> dict:
    """DMP에서 macro_snapshot 추출"""
    return dmp.get("macro_snapshot", {})


def _load_universe_sector_info() -> tuple:
    """
    universe_v1.csv에서 섹터 매핑 및 WICS 섹터 목록 로드.
    반환: (sector_map: {ticker: sector_name}, wics_sectors: sorted list)
    """
    uni = pd.read_csv(UNIVERSE_PATH, dtype={"ticker": str})
    uni["ticker"] = uni["ticker"].apply(lambda x: str(x).zfill(6))
    sector_map = {row["ticker"]: row["wics_sector"] for _, row in uni.iterrows()}
    wics_sectors = sorted(uni["wics_sector"].unique().tolist())
    return sector_map, wics_sectors


def build_context_vector(
    dmp: dict,
    ticker: str,
    sector_map: dict,
    wics_sectors: list,
    mktcap_rank: float = 0.5,
    prev_close: float = None,  # 전일 종가 (overnight_gap 계산용)
) -> list:
    """
    34차원 context vector 조립 (설계서 §10.2 Context Branch).

    구성:
      macro(4) + technical(5) + price_stretch(2) + sector_relative(3)
      + confirmation(8) + sector_onehot(n_sectors) + market_cap_rank(1)
      = 23 + n_sectors 차원  ← n_context_features_base: 15 → 23

    dataset.py _build_context_vector()와 동일 로직 (훈련-추론 일관성 보장).
    mktcap_rank: 유니버스 내 시가총액 percentile rank (0~1). 호출부에서 계산 후 전달.
    """
    tech = extract_tech_features(dmp, ticker)
    macro_raw = extract_macro_features(dmp)

    market_data = dmp.get("market_data", {})
    ticker_data = market_data.get(ticker, {})
    ohlcv = ticker_data.get("ohlcv", {})
    close = float(ohlcv.get("close", 1.0))
    if close <= 0:
        close = 1.0
    open_price = float(ohlcv.get("open", close))
    high_price = float(ohlcv.get("high", close))
    low_price = float(ohlcv.get("low", close))

    # macro (4)
    macro_vec = [
        float(macro_raw.get("vix_proxy") or 0.0),
        float(macro_raw.get("market_breadth") or 0.0),
        float(macro_raw.get("usd_krw") or 0.0),
        float(macro_raw.get("base_rate") or 0.0),
    ]

    # technical (5): rsi_14, atr_14_ratio, volume_ratio_20, macd, macd_signal
    atr_14 = float(tech.get("atr_14", 0.0))
    atr_14_ratio = atr_14 / close if close > 0 else 0.0
    tech_vec = [
        float(tech.get("rsi_14", 50.0)),
        atr_14_ratio,
        float(tech.get("volume_ratio_20", 1.0)),
        float(tech.get("macd", 0.0)),
        float(tech.get("macd_signal", 0.0)),
    ]

    # price_stretch (2): close/sma_5 - 1, close/sma_20 - 1
    sma_5 = float(tech.get("sma_5", close))
    sma_20 = float(tech.get("sma_20", close))
    stretch_vec = [
        (close / sma_5 - 1.0) if sma_5 > 0 else 0.0,
        (close / sma_20 - 1.0) if sma_20 > 0 else 0.0,
    ]

    # sector_relative (3): ret_5d_sector_z, rsi_14_sector_z, volume_ratio_20_sector_pct
    sector_vec = [
        float(tech.get("ret_5d_sector_z", 0.0)),
        float(tech.get("rsi_14_sector_z", 0.0)),
        float(tech.get("volume_ratio_20_sector_pct", 0.5)),
    ]

    # confirmation (8): candlestick structure + sector ranking features
    hl_range = high_price - low_price + 1e-8

    # 1. ret_5d_rank_in_sector
    ret_5d_rank_in_sector = float(tech.get("ret_5d_rank_in_sector", 0.5))

    # 2. close_sma20_ratio_sector_rank
    close_sma20_ratio_sector_rank = float(tech.get("close_sma20_ratio_sector_rank", 0.5))

    # 3. bb_pos
    bb_pos = float(tech.get("bb_pos", 0.5))

    # 4. overnight_gap: (open - prev_close) / prev_close
    if prev_close is not None and prev_close > 0:
        overnight_gap = (open_price - prev_close) / prev_close
    else:
        overnight_gap = 0.0

    # 5. intraday_range: (high - low) / close
    intraday_range = hl_range / close if close > 0 else 0.0

    # 6. upper_shadow: (high - max(open, close)) / (high - low + 1e-8)
    upper_shadow = (high_price - max(open_price, close)) / hl_range

    # 7. lower_shadow: (min(open, close) - low) / (high - low + 1e-8)
    lower_shadow = (min(open_price, close) - low_price) / hl_range

    # 8. body_ratio: abs(close - open) / (high - low + 1e-8)
    body_ratio = abs(close - open_price) / hl_range

    confirm_vec = [
        ret_5d_rank_in_sector,
        close_sma20_ratio_sector_rank,
        bb_pos,
        overnight_gap,
        intraday_range,
        upper_shadow,
        lower_shadow,
        body_ratio,
    ]

    # meta: sector_onehot (n_sectors 차원) + market_cap_rank (1)
    sector_name = sector_map.get(ticker, "Unknown")
    n_sectors = len(wics_sectors)
    sector_oh = [0.0] * n_sectors
    if sector_name in wics_sectors:
        sector_oh[wics_sectors.index(sector_name)] = 1.0

    context = macro_vec + tech_vec + stretch_vec + sector_vec + confirm_vec + sector_oh + [mktcap_rank]
    return context


def load_context_scaler(scaler_path: Path):
    """
    context_scaler.pkl 로드. 없으면 None 반환.
    """
    if scaler_path.exists():
        with open(scaler_path, "rb") as f:
            scaler = pickle.load(f)
        print(f"[SCEmitter] context_scaler.pkl 로드: {scaler_path}")
        return scaler
    print(f"[SCEmitter] context_scaler.pkl 없음 ({scaler_path}), scaler 미적용")
    return None


def check_oversold_gate(ticker: str, dmp: dict, cfg: dict) -> tuple:
    """
    종목의 oversold gate 통과 여부 확인.
    dataset.py의 apply_oversold_gate와 동일한 로직을 사용 (훈련-추론 일관성 보장).

    4개 기본 조건 중 min_conditions개 이상 충족 AND sector-relative 약세 조건 충족 시 통과.

    반환: (passed: bool, gate_info: dict)
    gate_info: met_conditions(충족 조건 목록), relaxed(Gate 완화 여부), n_met(충족 조건 수),
               sector_weak(섹터 약세 여부)
    """
    gate_cfg = cfg.get("oversold_gate", {})
    tech = extract_tech_features(dmp, ticker)

    ret_5d = float(tech.get("ret_5d", 0.0))
    rsi_14 = float(tech.get("rsi_14", 50.0))
    close_sma20_ratio = float(tech.get("close_sma20_ratio", 0.0))
    bb_pos = float(tech.get("bb_pos", 0.5))
    ret_5d_sector_z = float(tech.get("ret_5d_sector_z", 0.0))
    ret_5d_rank = float(tech.get("ret_5d_rank_in_sector", 0.5))

    ret_5d_thr = float(gate_cfg.get("ret_5d_threshold", 0.0))
    rsi_thr = float(gate_cfg.get("rsi_14_threshold", 45))
    close_sma20_thr = float(gate_cfg.get("close_sma20_threshold", 0.0))
    bb_thr = float(gate_cfg.get("bb_pos_threshold", 0.35))
    z_thr = float(gate_cfg.get("sector_ret5_z_threshold", -0.25))
    bottom_pct = float(gate_cfg.get("sector_bottom_pct", 0.40))
    min_cond = int(gate_cfg.get("min_conditions", 2))

    # 4개 기본 조건 체크 (dataset.py apply_oversold_gate와 동일)
    met_conditions = []
    cond1 = ret_5d < ret_5d_thr
    cond2 = rsi_14 < rsi_thr
    cond3 = close_sma20_ratio < close_sma20_thr
    cond4 = bb_pos < bb_thr

    if cond1:
        met_conditions.append("ret_5d<0")
    if cond2:
        met_conditions.append(f"RSI14<{rsi_thr:.0f}")
    if cond3:
        met_conditions.append("close<SMA20")
    if cond4:
        met_conditions.append(f"BB_pos<{bb_thr:.2f}")

    n_met = len(met_conditions)

    # sector-relative 약세: 별도 AND 조건
    sector_weak = (ret_5d_sector_z <= z_thr) or (ret_5d_rank <= bottom_pct)

    if sector_weak:
        sector_tag = (
            f"섹터ret5d_z<={z_thr:.2f}" if ret_5d_sector_z <= z_thr
            else f"섹터하위{bottom_pct:.0%}"
        )

    passed = (n_met >= min_cond) and sector_weak
    relaxed = False

    # Gate Relaxation: 미통과 시 완화 조건 적용
    relaxation = gate_cfg.get("relaxation", {})
    if not passed and relaxation.get("enabled", False):
        rsi_relaxed = float(relaxation.get("rsi_relaxed", 48))
        sector_pct_relaxed = float(relaxation.get("sector_bottom_pct_relaxed", 0.50))

        # 완화된 cond2 재평가
        cond2_relaxed = rsi_14 < rsi_relaxed
        # 완화된 sector_weak 재평가
        sector_weak_relaxed = (ret_5d_sector_z <= z_thr) or (ret_5d_rank <= sector_pct_relaxed)

        relaxed_conditions = list(met_conditions)
        if cond2_relaxed and not cond2:
            relaxed_conditions.append(f"RSI14<{rsi_relaxed:.0f}(완화)")
        relaxed_n_met = len(relaxed_conditions)

        if (relaxed_n_met >= min_cond) and sector_weak_relaxed:
            passed = True
            relaxed = True
            met_conditions = relaxed_conditions
            sector_weak = sector_weak_relaxed
            n_met = relaxed_n_met

    if sector_weak and not any("섹터" in c for c in met_conditions):
        sector_tag = (
            f"섹터ret5d_z<={z_thr:.2f}" if ret_5d_sector_z <= z_thr
            else f"섹터하위{bottom_pct:.0%}"
        )
        met_conditions.append(sector_tag)

    gate_info = {
        "met_conditions": met_conditions,
        "relaxed": relaxed,
        "n_met": n_met,
        "sector_weak": sector_weak,
    }

    return passed, gate_info


def load_torch_model(model_pt_path: Path, cfg: dict):
    """학습된 PyTorch 모델 로드"""
    try:
        import torch
    except ImportError:
        raise ImportError("[SCEmitter] torch 미설치. pip install torch 후 재시도.")

    # n_context_features: training_log에서 동적 로드 (없으면 기본값)
    n_context_features = 26  # 설계서 §10.2 기본값
    training_log_path = MODEL_DIR / "training_log.json"
    if training_log_path.exists():
        try:
            with open(training_log_path, "r", encoding="utf-8") as f:
                tlog = json.load(f)
            n_ctx_logged = tlog.get("n_context_features")
            if n_ctx_logged is not None:
                n_context_features = int(n_ctx_logged)
        except Exception as e:
            print(f"[SCEmitter] training_log 로드 실패 ({e}), 기본값 {n_context_features} 사용")

    model_py = MODEL_DIR / "model.py"
    if model_py.exists():
        import importlib.util
        spec = importlib.util.spec_from_file_location("rebound_model", model_py)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        model = mod.KRReboundCNN(n_context_features=n_context_features)
    else:
        # Fallback: 3ch CNN 구조 (config.yaml channels=3 기준)
        import torch.nn as nn
        channels = cfg["data"]["image"]["channels"]

        _n_ctx = n_context_features

        class _FallbackCNN(nn.Module):
            def __init__(self):
                super().__init__()
                self.cnn = nn.Sequential(
                    nn.Conv2d(channels, 32, 3, padding=1), nn.ReLU(),
                    nn.MaxPool2d(2),
                    nn.Conv2d(32, 64, 3, padding=1), nn.ReLU(),
                    nn.MaxPool2d(2),
                    nn.Conv2d(64, 128, 3, padding=1), nn.ReLU(),
                    nn.AdaptiveAvgPool2d((4, 4)),
                )
                self.fc = nn.Sequential(
                    nn.Linear(128 * 4 * 4 + _n_ctx, 256), nn.ReLU(), nn.Dropout(0.3),
                    nn.Linear(256, 64), nn.ReLU(), nn.Dropout(0.2),
                    nn.Linear(64, 1), nn.Sigmoid(),
                )

            def forward(self, chart, context_features):
                out = self.cnn(chart)
                flat = out.view(out.size(0), -1)
                combined = __import__("torch").cat([flat, context_features], dim=1)
                return self.fc(combined)

        model = _FallbackCNN()

    model.load_state_dict(__import__("torch").load(model_pt_path, map_location="cpu"))
    model.eval()
    return model


def load_calibrator(calibrator_path: Path):
    """Platt/Isotonic calibrator 로드"""
    import pickle
    with open(calibrator_path, "rb") as f:
        return pickle.load(f)


def infer_batch(
    model,
    chart_tensors: "np.ndarray",
    context_features: "np.ndarray",
) -> "np.ndarray":
    """
    배치 추론 → probability 배열 반환.
    model(chart, context_features) 2-입력 forward 호출 (설계서 §10.2).
    모델 출력이 raw logit이므로 sigmoid 적용하여 확률로 변환.
    """
    import torch
    with torch.no_grad():
        chart = torch.tensor(chart_tensors, dtype=torch.float32)
        context = torch.tensor(context_features, dtype=torch.float32)
        logits = model(chart, context)
        probs = torch.sigmoid(logits)  # logits → probs
    return probs.squeeze(1).numpy()


def infer_ensemble(
    model_paths: list,
    cfg: dict,
    chart_tensors: "np.ndarray",
    context_features: "np.ndarray",
) -> tuple:
    """
    Ensemble 추론: 각 seed 모델 추론 후 평균/분산 반환.
    반환: (mean_probs: np.ndarray, variance: np.ndarray)
    """
    all_probs = []
    for pt_path in model_paths:
        try:
            model = load_torch_model(pt_path, cfg)
            probs = infer_batch(model, chart_tensors, context_features)
            all_probs.append(probs)
            print(f"[SCEmitter] Ensemble 모델 로드 완료: {pt_path.name}")
        except Exception as e:
            print(f"[SCEmitter] Ensemble 모델 로드 실패 ({pt_path.name}): {e}")

    if not all_probs:
        raise RuntimeError("[SCEmitter] 유효한 ensemble 모델이 없음")

    stack = np.stack(all_probs, axis=0)  # (n_models, N)
    mean_probs = stack.mean(axis=0)
    variance = stack.var(axis=0)
    return mean_probs, variance


def calibrate_probs(raw_probs: "np.ndarray", calibrator) -> "np.ndarray":
    """Calibrator 보정 (IsotonicRegression.predict: 1D array 직접 수용)"""
    return calibrator.predict(raw_probs)


def map_signal_and_direction(
    confidence: float, gate_pass: bool, cfg: dict = None
) -> tuple:
    """설계서 §13.2 signal/direction 매핑.

    v1 규칙:
    - gate 미통과: hold, neutral
    - confidence >= signal_map.strong_buy: strong_buy, long
    - confidence >= signal_map.buy: buy, long
    - 그 외: hold, neutral

    임계값은 config.yaml inference.signal_map에서 로드. 없으면 기본값 사용.
    sell/strong_sell은 v1에서 생성하지 않는다.
    """
    signal_map = {}
    if cfg is not None:
        signal_map = cfg.get("inference", {}).get("signal_map", {})
    strong_buy_thr = float(signal_map.get("strong_buy", 0.70))
    buy_thr = float(signal_map.get("buy", 0.55))

    if not gate_pass:
        return "hold", "neutral"
    if confidence >= strong_buy_thr:
        return "strong_buy", "long"
    elif confidence >= buy_thr:
        return "buy", "long"
    else:
        return "hold", "neutral"


def build_rationale(
    name: str,
    ticker: str,
    cal_prob: float,
    raw_prob: float,
    signal: str,
    gate_info: dict = None,
) -> str:
    """Deterministic 한국어 rationale 생성"""
    signal_desc = {
        "strong_buy": "강한 반등 신호",
        "buy": "반등 신호",
        "hold": "관망",
        "sell": "약세 신호",
        "strong_sell": "강한 약세 신호",
    }
    desc = signal_desc.get(signal, signal)

    parts = [f"[KR-Rebound-CNN] {name}({ticker})"]
    parts.append(f"반등확률 {cal_prob:.1%} ({desc})")

    if gate_info:
        conditions = gate_info.get("met_conditions", [])
        if conditions:
            parts.append(f"Oversold Gate 통과: {', '.join(conditions)}")
        if gate_info.get("relaxed"):
            parts.append("(Gate 완화 적용)")

    parts.append(f"raw_prob={raw_prob:.3f}")
    return ". ".join(parts)


def build_strategy_card(
    ticker: str,
    name: str,
    target_date: str,
    raw_prob: float,
    cal_prob: float,
    signal: str,
    direction: str,
    gate_info: dict = None,
    uncertainty: float = None,
) -> dict:
    """
    StrategyCard 딕셔너리 구성.
    GPT Pro 설계서 기준 필드 매핑:
      calibrated p_rebound  → confidence
      raw logit             → quant_score
      2*confidence-1        → pre_risk_score
      ensemble variance     → uncertainty_score
      §13.2 규칙 분류       → signal, direction
      0.0                   → news_signal
      "quant"               → source_strategy
    """

    # uncertainty_score: ensemble variance 우선, fallback은 확률 엔트로피 기반
    if uncertainty is not None:
        uncertainty_score = float(uncertainty)
    else:
        uncertainty_score = float(1.0 - max(cal_prob, 1.0 - cal_prob))

    rationale = build_rationale(name, ticker, cal_prob, raw_prob, signal, gate_info)

    # quant_score: raw logit (설계서 §13.1). prob → logit 역변환
    # gate 미통과(raw_prob=0.0)인 경우 logit=0.0으로 고정 (CNN 추론 안 함)
    if raw_prob <= 1e-7 or raw_prob >= 1 - 1e-7:
        raw_logit = 0.0
    else:
        raw_logit = float(np.log(raw_prob / (1 - raw_prob)))

    return {
        "card_id": f"SC-{target_date}-{ticker}",
        "snapshot_dt": make_snapshot_dt(target_date),
        "artifact_version": "v1.0",
        "ticker": ticker,
        "name": name,
        "direction": direction,
        "signal": signal,
        "confidence": round(float(cal_prob), 4),
        "pre_risk_score": round(float(cal_prob * 2 - 1), 4),
        "quant_score": round(raw_logit, 4),
        "news_signal": 0.0,
        "rationale": rationale,
        "source_strategy": "quant",
        "evidence_ids": [f"DMP-{target_date}-{ticker}"],
        "features_used": ["candlestick_body_wick", "volume_log_norm", "sma_bollinger_overlay",
                          "sector_relative", "macro"],
        "uncertainty_score": round(uncertainty_score, 4),
    }


def load_prev_sc(target_date: str) -> dict:
    """이전 거래일의 rebound SC를 로드하여 {ticker: {confidence, signal}} 반환.

    PIT-Safe: target_date 미만(strictly less)의 파일만 참조.
    파일 없으면 빈 dict 반환.
    """
    variant_dir = _BASE_DIR / "artifacts" / "strategy_card_variants" / "rebound"
    sc_files = sorted(variant_dir.glob("SC-*.json"))
    prev_files = [f for f in sc_files if f.stem.replace("SC-", "") < target_date]
    if not prev_files:
        return {}
    try:
        with open(prev_files[-1], encoding="utf-8") as f:
            cards = json.load(f)
        return {
            c["ticker"]: {
                "confidence": float(c.get("confidence", 0.0)),
                "signal": c.get("signal", "hold"),
            }
            for c in cards
        }
    except Exception as e:
        print(f"[SCEmitter] 이전 SC 로드 실패 ({prev_files[-1].name}): {e}")
        return {}


def apply_persistence_filter(
    cal_p: float, prev_conf: float, prev_signal: str
) -> tuple:
    """Signal Persistence Filter (설계서 §19.2).

    신호 급변 억제 규칙:
    - buy 신호(>=0.55): 이전 SC에서도 0.50+ 이어야 buy 유지,
      그렇지 않으면 hold 수준(<=0.54)으로 억제.
    - strong_buy 신호(>=0.70): confidence 급락 없어야 함.
      이전 대비 급락(>=0.05 하락) 시 buy 수준(<=0.69)으로 억제.
    - 이전에 buy/strong_buy이고 현재도 0.50 이상이면 buy 유지.

    반환: (filtered_confidence, filter_applied: bool)
    """
    # strong_buy 후보: 이전 대비 급락 없어야 함
    if cal_p >= 0.70:
        if prev_conf is not None and (cal_p - prev_conf) >= -0.05:
            return cal_p, False
        elif prev_conf is not None:
            return min(cal_p, 0.69), True
        # 이전 SC 없음 → 그대로 허용
        return cal_p, False

    # buy 후보: 이전에도 0.50+ 이어야 신규 buy 허용
    if cal_p >= 0.55:
        if prev_conf is not None and prev_conf >= 0.50:
            return cal_p, False
        elif prev_conf is not None:
            return min(cal_p, 0.54), True
        # 이전 SC 없음 → 그대로 허용
        return cal_p, False

    # 기존 buy 유지: 이전에 buy/strong_buy였고 현재도 0.50 이상이면 buy 수준 유지
    if prev_signal in ("buy", "strong_buy") and cal_p >= 0.50:
        return 0.55, True

    return cal_p, False


def validate_schema(cards: list) -> list:
    """스키마 검증 (jsonschema 없으면 필드 존재 여부만 확인)"""
    required = [
        "card_id", "snapshot_dt", "artifact_version", "ticker",
        "direction", "signal", "confidence", "pre_risk_score",
        "rationale", "source_strategy", "evidence_ids",
    ]
    issues = []
    try:
        import jsonschema
        with open(SC_SCHEMA_PATH) as f:
            schema = json.load(f)
        for card in cards:
            try:
                jsonschema.validate(instance=card, schema=schema)
            except jsonschema.ValidationError as e:
                issues.append(f"[SCEmitter] 스키마 오류 ({card.get('ticker')}): {e.message}")
    except ImportError:
        for card in cards:
            for field in required:
                if field not in card:
                    issues.append(f"[SCEmitter] 필수 필드 누락 ({card.get('ticker')}): {field}")
    return issues


def main():
    parser = argparse.ArgumentParser(description="KR-Rebound-CNN StrategyCard 생성")
    parser.add_argument("date", help="대상 날짜 (YYYYMMDD)")
    parser.add_argument("--publish", action="store_true", help="생성 후 즉시 canonical SC로 publish")
    args = parser.parse_args()

    target_date = args.date
    print(f"[SCEmitter] 시작: {target_date}")

    # 설정 + 유니버스 로드 (ensemble_seeds는 config에서 동적 로드)
    cfg = load_model_config()
    ensemble_seeds = cfg["training"].get("ensemble_seeds", [42, 123, 456])

    # Persistence Filter: 이전 SC 사전 로드 (enabled 여부와 무관하게 조기 로드)
    persistence_enabled = cfg.get("persistence_filter", {}).get("enabled", False)
    prev_sc_map: dict = {}
    if persistence_enabled:
        prev_sc_map = load_prev_sc(target_date)
        print(
            f"[SCEmitter] Persistence Filter 활성화. 이전 SC 로드: {len(prev_sc_map)}종목"
        )
    else:
        print("[SCEmitter] Persistence Filter 비활성화 (persistence_filter.enabled=false).")
    ensemble_pt_paths = {seed: MODEL_DIR / f"model_seed{seed}.pt" for seed in ensemble_seeds}

    # 모델 파일 존재 확인
    has_ensemble = all(p.exists() for p in ensemble_pt_paths.values())
    has_single = MODEL_PT_PATH.exists()

    if not has_ensemble and not has_single:
        print(
            f"[SCEmitter] 모델 파일 없음: {MODEL_PT_PATH}\n"
            f"  → 먼저 학습을 실행하세요: python jobs/train_rebound_cnn.py\n"
            f"  → 또는 Phase 2 학습 완료 후 재실행하세요."
        )
        sys.exit(1)

    if has_ensemble:
        print(f"[SCEmitter] Ensemble 모드: seed {ensemble_seeds}")
    else:
        print(f"[SCEmitter] 단일 모델 모드: {MODEL_PT_PATH.name}")
    ticker_name = load_universe()
    lookback = cfg["data"]["lookback_days"]
    # signal_map은 §13.2 map_signal_and_direction()으로 대체됨 (sell/strong_sell 금지)

    # universe 섹터 정보 로드 (context vector 조립용)
    sector_map, wics_sectors = _load_universe_sector_info()
    n_context_features = 23 + len(wics_sectors)  # base(23: macro4+tech5+stretch2+sector3+confirm8+mktcap1) + n_sectors
    print(f"[SCEmitter] n_context_features={n_context_features} (n_sectors={len(wics_sectors)})")

    # context_scaler 로드 (train fold 기준 StandardScaler)
    scaler = load_context_scaler(SCALER_PATH)

    # DMP 로드
    try:
        dmp = load_dmp(target_date)
    except FileNotFoundError as e:
        print(f"[SCEmitter] {e}")
        sys.exit(1)

    tickers = [str(t).zfill(6) for t in dmp.get("tickers", [])]
    print(f"[SCEmitter] 대상 종목: {len(tickers)}개")

    # 복수 DMP 스캔으로 20일 OHLCV 창 구성 (PIT-safe)
    panel = load_ohlcv_panel(target_date, lookback=lookback + 5)
    print(f"[SCEmitter] OHLCV panel 구성 완료: {len(panel)}종목")

    # P0-2: sector-relative 피처를 유니버스 기준으로 재계산 (train-infer drift 방지).
    # load_ohlcv_panel 결과를 compute_sector_relative_features()가 요구하는
    # {ticker: {date_str: {ohlcv, volume, tech_features}}} 형식으로 변환.
    universe_df = pd.read_csv(UNIVERSE_PATH)
    panel_as_history: dict = {}
    for tkr, day_list in panel.items():
        panel_as_history[tkr] = {}
        for item in day_list:
            d = item["date"]
            panel_as_history[tkr][d] = {
                "ohlcv": {
                    "open": item["open"],
                    "high": item["high"],
                    "low": item["low"],
                    "close": item["close"],
                },
                "volume": item["volume"],
                "tech_features": item.get("tech_features", {}),
            }

    try:
        sector_feats_map = compute_sector_relative_features(
            ticker_data=panel_as_history,
            universe=universe_df,
            date_str=target_date,
        )
        print(f"[SCEmitter] sector-relative 피처 재계산 완료: {len(sector_feats_map)}종목")
    except Exception as e:
        print(f"[SCEmitter] sector-relative 피처 재계산 실패 ({e}), DMP tech_features 사용")
        sector_feats_map = {}

    # P0-1: Oversold Gate 전에 모든 종목의 DMP tech_features에 sector_feats 주입.
    # check_oversold_gate()는 DMP tech_features를 직접 읽으므로,
    # sector_feats_map의 재계산 값을 gate 호출 전에 미리 반영해야 한다.
    if sector_feats_map:
        for ticker in tickers:
            sf = sector_feats_map.get(ticker)
            if sf is None:
                continue
            dmp_market = dmp.setdefault("market_data", {})
            ticker_entry = dmp_market.setdefault(ticker, {})
            tf = ticker_entry.setdefault("tech_features", {})
            tf["ret_5d"] = sf.get("ret_5d", tf.get("ret_5d", 0.0))
            tf["ret_5d_sector_z"] = sf.get("ret_5d_sector_z", tf.get("ret_5d_sector_z", 0.0))
            tf["rsi_14_sector_z"] = sf.get("rsi_14_sector_z", tf.get("rsi_14_sector_z", 0.0))
            tf["close_sma20_ratio"] = sf.get("close_sma20_ratio", tf.get("close_sma20_ratio", 0.0))
            tf["bb_pos"] = sf.get("bb_pos", tf.get("bb_pos", 0.5))
            tf["ret_5d_rank_in_sector"] = sf.get("ret_5d_rank_in_sector", tf.get("ret_5d_rank_in_sector", 0.5))
            tf["close_sma20_ratio_sector_rank"] = sf.get(
                "close_sma20_ratio_sector_rank", tf.get("close_sma20_ratio_sector_rank", 0.5)
            )
            tf["volume_ratio_20_sector_pct"] = sf.get(
                "volume_ratio_20_sector_pct", tf.get("volume_ratio_20_sector_pct", 0.5)
            )
        print(f"[SCEmitter] sector_feats → DMP tech_features 주입 완료: {len(sector_feats_map)}종목")

    # Oversold Gate 필터링 + 텐서/피처 빌드
    chart_tensors = []
    context_feat_list = []
    valid_tickers = []
    gate_passed_info = {}

    # gate 통과하지 않은 종목은 hold 카드로 처리
    hold_cards = []

    # ── dataset.py 집계 기반 Gate Relaxation ──────────────────────────
    # 1단계: 모든 종목에 대해 기본 조건으로 gate 체크 (relaxation 비활성)
    gate_cfg = cfg.get("oversold_gate", {})
    relaxation_cfg = gate_cfg.get("relaxation", {})
    relaxation_enabled = relaxation_cfg.get("enabled", True)
    min_candidates = int(relaxation_cfg.get("min_candidates", 3))

    # check_oversold_gate는 내부에서 relaxation도 적용하므로,
    # 1차 체크 시 relaxation 없는 cfg 사본 전달
    cfg_no_relax = dict(cfg)
    gate_cfg_no_relax = dict(gate_cfg)
    gate_cfg_no_relax["relaxation"] = {"enabled": False}
    cfg_no_relax["oversold_gate"] = gate_cfg_no_relax

    first_pass: dict = {}   # ticker → (passed, gate_info)
    for ticker in tickers:
        passed, gate_info = check_oversold_gate(ticker, dmp, cfg_no_relax)
        first_pass[ticker] = (passed, gate_info)

    gate_passed_tickers = [t for t, (p, _) in first_pass.items() if p]

    # 2단계: gate 통과 종목 수가 min_candidates 미만이면 완화 조건으로 미통과 종목 재평가
    if relaxation_enabled and len(gate_passed_tickers) < min_candidates:
        cfg_relaxed = dict(cfg)   # relaxation 활성화 상태 원본 cfg 사용
        failed_tickers = [t for t, (p, _) in first_pass.items() if not p]
        added = []
        for ticker in failed_tickers:
            passed_r, gate_info_r = check_oversold_gate(ticker, dmp, cfg_relaxed)
            if passed_r:
                first_pass[ticker] = (True, gate_info_r)
                added.append(ticker)
        if added:
            gate_passed_tickers = [t for t, (p, _) in first_pass.items() if p]
            print(
                f"[SCEmitter] Gate relaxation 적용: "
                f"{len(gate_passed_tickers)}종목 후보 (+{len(added)})"
            )

    # DMP market_data에서 유니버스 내 시가총액 percentile rank 계산
    # mktcap 없는 종목은 0.5 fallback
    mktcap_rank_map: dict = {}
    mktcaps_raw: dict = {}
    dmp_market = dmp.get("market_data", {})
    for t in tickers:
        mc_raw = dmp_market.get(t, {}).get("mktcap", None)
        if mc_raw is not None:
            try:
                mc_val = float(mc_raw)
                if mc_val > 0:
                    mktcaps_raw[t] = mc_val
            except (TypeError, ValueError):
                pass
    if mktcaps_raw:
        sorted_caps = sorted(mktcaps_raw.values())
        n_caps = len(sorted_caps)
        for t, mc in mktcaps_raw.items():
            mktcap_rank_map[t] = float(sorted_caps.index(mc) / max(n_caps - 1, 1))
    print(f"[SCEmitter] mktcap rank 계산: {len(mktcap_rank_map)}종목 / fallback(0.5): {len(tickers) - len(mktcap_rank_map)}종목")

    # 3단계: 최종 gate 결과 기반으로 텐서 빌드 및 hold 카드 구성
    for ticker in tickers:
        gate_passed, gate_info = first_pass[ticker]

        if not gate_passed:
            name = ticker_name.get(ticker, ticker)
            hold_card = build_strategy_card(
                ticker=ticker,
                name=name,
                target_date=target_date,
                raw_prob=0.0,
                cal_prob=0.0,
                signal="hold",
                direction="neutral",
                gate_info=None,
                uncertainty=0.0,
            )
            # hold 종목은 gate 미통과 메시지로 rationale 덮어쓰기
            hold_card["rationale"] = (
                f"[KR-Rebound-CNN] {name}({ticker}). "
                f"Oversold Gate 미통과 (충족 조건 {gate_info['n_met']}개). "
                f"signal=hold, confidence=0.0"
            )
            hold_cards.append(hold_card)
            print(f"[SCEmitter] {ticker} ({name}): Gate 미통과 → hold")
            continue

        # Gate 통과 종목: 텐서 빌드 (panel에서 최근 lookback일 OHLCV 사용)
        ohlcv_hist = panel.get(ticker, [])[-lookback:]
        if not ohlcv_hist:
            print(f"[SCEmitter] {ticker}: OHLCV 데이터 없음, 건너뜀")
            continue

        try:
            # P0-3: make_chart_tensor(dataset.py)를 직접 사용하여 학습-추론 텐서 완전 일치.
            # ohlcv_hist(flat dict)를 make_chart_tensor가 기대하는 {"ohlcv":{...}, "volume":...} 형식으로 변환.
            window_snaps = [
                {
                    "ohlcv": {
                        "open": item["open"],
                        "high": item["high"],
                        "low": item["low"],
                        "close": item["close"],
                    },
                    "volume": item["volume"],
                    "tech_features": item.get("tech_features", {}),
                }
                for item in ohlcv_hist
            ]
            height = cfg["data"]["image"]["height"]
            width = cfg["data"]["image"]["width"]
            chart_torch = make_chart_tensor(window_snaps, size=(height, width))
            tensor = chart_torch.numpy()  # torch.Tensor → numpy (3, H, W)

            # 전일 종가: ohlcv_hist[-2]가 있으면 사용 (overnight_gap 계산용, PIT-safe)
            prev_close_val = None
            if len(ohlcv_hist) >= 2:
                _pc = ohlcv_hist[-2].get("close")
                if _pc and float(_pc) > 0:
                    prev_close_val = float(_pc)
            context_vec = build_context_vector(
                dmp, ticker, sector_map, wics_sectors,
                mktcap_rank=mktcap_rank_map.get(ticker, 0.5),
                prev_close=prev_close_val,
            )
            chart_tensors.append(tensor)
            context_feat_list.append(context_vec)
            valid_tickers.append(ticker)
            gate_passed_info[ticker] = gate_info
        except Exception as e:
            print(f"[SCEmitter] {ticker}: 텐서 생성 실패 - {e}")

    print(f"[SCEmitter] Gate 통과 종목: {len(valid_tickers)}개 / Gate 미통과: {len(hold_cards)}개")

    # Gate 통과 종목이 없으면 hold 카드만 저장
    if not valid_tickers:
        print("[SCEmitter] Gate 통과 종목 없음. hold 카드만 저장.")
        cards = hold_cards
    else:
        chart_np = np.stack(chart_tensors, axis=0)                       # (N, 3, 64, 64)
        context_np = np.array(context_feat_list, dtype=np.float32)       # (N, n_context_features)

        # context_scaler 적용 (StandardScaler transform, PIT-safe)
        if scaler is not None:
            try:
                context_np = scaler.transform(context_np)
                print(f"[SCEmitter] context_scaler transform 적용: {context_np.shape}")
            except Exception as e:
                print(f"[SCEmitter] context_scaler transform 실패 ({e}), 미적용 진행")

        # Ensemble 또는 단일 모델 추론
        variances = None
        if has_ensemble:
            ensemble_paths = [ensemble_pt_paths[seed] for seed in ensemble_seeds]
            try:
                raw_probs, variances = infer_ensemble(
                    ensemble_paths, cfg, chart_np, context_np
                )
                print(f"[SCEmitter] Ensemble 추론 완료 (n_models={len(ensemble_paths)})")
            except Exception as e:
                print(f"[SCEmitter] Ensemble 추론 실패 ({e}), 단일 모델로 fallback")
                has_ensemble = False

        if not has_ensemble:
            try:
                model = load_torch_model(MODEL_PT_PATH, cfg)
            except Exception as e:
                print(f"[SCEmitter] 모델 로드 실패: {e}")
                sys.exit(1)
            raw_probs = infer_batch(model, chart_np, context_np)

        # Calibration
        if CALIBRATOR_PATH.exists():
            try:
                calibrator = load_calibrator(CALIBRATOR_PATH)
                cal_probs = calibrate_probs(raw_probs, calibrator)
                print("[SCEmitter] Calibration 적용됨")
            except Exception as e:
                print(f"[SCEmitter] Calibrator 로드 실패 ({e}), raw prob 사용")
                cal_probs = raw_probs
        else:
            print("[SCEmitter] calibrator.pkl 없음, raw prob 사용")
            cal_probs = raw_probs

        # Committee v1.1: Tree Core tabular 예측 → CNN 예측과 fusion
        committee_cfg = cfg.get("committee", {})
        committee_enabled = committee_cfg.get("enabled", False)
        tab_weight = float(committee_cfg.get("tab_weight", 0.65))
        cnn_weight = float(committee_cfg.get("cnn_weight", 0.35))
        agreement_threshold = float(committee_cfg.get("agreement_threshold", 0.55))

        p_tab_arr = None
        if committee_enabled:
            try:
                from models.rebound_cnn.committee import load_tree_core, fuse_scores as _fuse
                tree_core = load_tree_core()
                if tree_core is not None:
                    p_tab_arr = tree_core.predict_proba(context_np)[:, 1]
                    print(
                        f"[SCEmitter] Committee v1.1: Tree Core 예측 완료 "
                        f"(mean={p_tab_arr.mean():.3f})"
                    )
                else:
                    print("[SCEmitter] Committee: tree_core.pkl 없음, CNN 단독 사용")
            except Exception as e:
                print(f"[SCEmitter] Committee fusion 실패 ({e}), CNN 단독 사용")

        # StrategyCard 생성 (Gate 통과 종목)
        # §13.2: signal/direction은 map_signal_and_direction()으로 결정 (sell/strong_sell 금지)
        infer_cards = []
        for i, ticker in enumerate(valid_tickers):
            raw_p = float(raw_probs[i])
            cal_p = float(cal_probs[i])

            # Committee fusion 적용 (enabled + Tree Core 로드 성공 시만)
            # cnn_role: "confirmatory" → CNN은 보조 확인 branch로만 사용
            cnn_role = committee_cfg.get("cnn_role", "confirmatory")
            if committee_enabled and p_tab_arr is not None:
                from models.rebound_cnn.committee import fuse_scores as _fuse_scores
                p_tab = float(p_tab_arr[i])
                p_cnn = cal_p

                cal_p, agreement, committee_unc = _fuse_scores(
                    p_tab,
                    p_cnn,
                    tab_weight=tab_weight,
                    cnn_weight=cnn_weight,
                    agreement_threshold=agreement_threshold,
                )
                uncertainty = committee_unc

                # R8: Agreement 강화 — CNN과 Tree Core 방향 불일치 시 hold 강제
                # config.yaml committee.require_directional_agreement로 제어
                require_dir_agree = committee_cfg.get("require_directional_agreement", True)
                cnn_bullish = p_cnn >= 0.5
                tab_bullish = p_tab >= 0.5
                if require_dir_agree and cnn_bullish != tab_bullish:
                    # 방향 불일치: confidence를 min(cal_p, 0.54)로 캡 → hold 강제
                    cal_p = min(cal_p, 0.54)
                    print(
                        f"[SCEmitter] {ticker}: Agreement 불일치 "
                        f"(CNN={p_cnn:.3f}, Tab={p_tab:.3f}) → confidence 캡: {cal_p:.3f}"
                    )
            else:
                uncertainty = float(variances[i]) if variances is not None else None

            # Persistence Filter 적용 (enabled 시만)
            filter_applied = False
            if persistence_enabled and prev_sc_map:
                prev_info = prev_sc_map.get(ticker)
                prev_conf = prev_info["confidence"] if prev_info else None
                prev_signal = prev_info["signal"] if prev_info else "hold"
                cal_p, filter_applied = apply_persistence_filter(
                    cal_p, prev_conf, prev_signal
                )
                if filter_applied:
                    print(
                        f"[SCEmitter] {ticker}: Persistence Filter 적용 "
                        f"(prev_conf={prev_conf}, prev_signal={prev_signal}) → confidence={cal_p:.3f}"
                    )

            signal, direction = map_signal_and_direction(cal_p, gate_pass=True, cfg=cfg)
            name = ticker_name.get(ticker, ticker)
            gate_info = gate_passed_info.get(ticker)

            card = build_strategy_card(
                ticker=ticker,
                name=name,
                target_date=target_date,
                raw_prob=raw_p,
                cal_prob=cal_p,
                signal=signal,
                direction=direction,
                gate_info=gate_info,
                uncertainty=uncertainty,
            )
            infer_cards.append(card)
            print(f"[SCEmitter] {ticker} ({name}): signal={signal}, direction={direction}, confidence={cal_p:.3f}")

        # Top-N after gate: CNN score 상위 N개만 BUY 허용
        top_n = int(gate_cfg.get("top_n_after_gate", 5))
        if len(infer_cards) > top_n:
            # cal_prob (confidence) 내림차순 정렬
            infer_cards.sort(key=lambda c: c["confidence"], reverse=True)
            for card in infer_cards[top_n:]:
                if card["signal"] in ("buy", "strong_buy"):
                    card["signal"] = "hold"
                    card["direction"] = "neutral"
            print(f"[SCEmitter] Top-{top_n} shortlist 적용: 상위 {top_n}종목만 BUY 허용")

        cards = infer_cards + hold_cards

    # 스키마 검증
    issues = validate_schema(cards)
    if issues:
        for iss in issues:
            print(f"[SCEmitter] 경고: {iss}")
    else:
        print(f"[SCEmitter] 스키마 검증 통과 ({len(cards)}개 카드)")

    # 저장
    output_path = OUTPUT_DIR / f"SC-{target_date}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(cards, f, ensure_ascii=False, indent=2)
    print(f"[SCEmitter] 저장 완료: {output_path}")

    # --publish 옵션
    if args.publish:
        canonical_dir = _BASE_DIR / "artifacts" / "strategy_card"
        canonical_dir.mkdir(parents=True, exist_ok=True)
        canonical_path = canonical_dir / f"SC-{target_date}.json"
        shutil.copy2(output_path, canonical_path)
        print(f"[SCEmitter] Publish 완료: {canonical_path}")

    print(f"[SCEmitter] 완료. 총 {len(cards)}개 StrategyCard 생성.")


if __name__ == "__main__":
    main()
