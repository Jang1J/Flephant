"""
KR-Rebound-CNN Dataset Generator
- historical DMP에서 3-channel chart tensor + sector-relative features 추출
- excess return label 생성 (y=1 if excess_i_5d > 0.0%)
- Oversold Gate 적용 (4조건 중 2개 이상 + sector-relative 약세)
- PyTorch Dataset 클래스 제공
"""

import sys
import json
import pickle
import random
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.utils.data
import yaml
from sklearn.preprocessing import StandardScaler

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_BASE_DIR))

from models.rebound_cnn.preprocess import (
    compute_sector_relative_features,
    make_chart_tensor,
    _build_context_vector,
    _build_wics_sector_list,
)

DMP_DIR = _BASE_DIR / "artifacts" / "daily_market_packet"
UNIVERSE_CSV = _BASE_DIR / "config" / "universe_v1.csv"


def _set_seed(seed: int = 42):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def load_dmp_history(dates: list, dmp_dir: Path = DMP_DIR) -> dict:
    """
    여러 날짜의 DMP를 로드하여 종목별 시계열 dict 구성.
    반환: {ticker: {date_str: {ohlcv, volume, tech_features, macro}}}
    """
    history: dict = {}

    for date_str in sorted(dates):
        path = dmp_dir / f"DMP-{date_str}.json"
        if not path.exists():
            # KRX connector fallback 시도
            try:
                import pandas as _pd
                from connectors.krx import get_ohlcv as _krx_get_ohlcv

                universe_csv = _BASE_DIR / "config" / "universe_v1.csv"
                _univ = _pd.read_csv(universe_csv, dtype={"ticker": str})
                _tickers = [str(t).zfill(6) for t in _univ["ticker"].tolist()]

                print(f"[Modeler] DMP 없음, KRX connector fallback 시도: {date_str}")
                fallback_loaded = 0
                for _ticker in _tickers:
                    try:
                        _df = _krx_get_ohlcv(_ticker, date_str, date_str)
                        if _df is None or _df.empty:
                            continue
                        _row = _df.iloc[0]
                        if _ticker not in history:
                            history[_ticker] = {}
                        history[_ticker][date_str] = {
                            "ohlcv": {
                                "open": float(_row.get("시가", _row.get("Open", 0.0))),
                                "high": float(_row.get("고가", _row.get("High", 0.0))),
                                "low": float(_row.get("저가", _row.get("Low", 0.0))),
                                "close": float(_row.get("종가", _row.get("Close", 0.0))),
                            },
                            "volume": float(_row.get("거래량", _row.get("Volume", 0.0))),
                            "tech_features": {},
                            "macro": {
                                "vix_proxy": 0.0,
                                "market_breadth": 0.0,
                                "fx_rate": 0.0,
                                "base_rate": 0.0,
                            },
                        }
                        fallback_loaded += 1
                    except Exception as _e:
                        pass  # 개별 종목 실패는 조용히 skip
                if fallback_loaded > 0:
                    print(f"[Modeler] KRX fallback 완료: {date_str} ({fallback_loaded}종목)")
                    continue
            except Exception as e:
                print(f"[Modeler] KRX fallback 실패 ({date_str}): {e}")
            print(f"[Modeler] DMP 파일 없음 (skip): {path}")
            continue

        with open(path, "r", encoding="utf-8") as f:
            dmp = json.load(f)

        market_data = dmp.get("market_data", {})
        macro_raw = dmp.get("macro_snapshot", {})

        # macro features 추출 (없으면 0.0으로 채움)
        macro = {
            "vix_proxy": float(macro_raw.get("vix_proxy") or 0.0),
            "market_breadth": float(macro_raw.get("market_breadth") or 0.0),
            "fx_rate": float(macro_raw.get("usd_krw") or 0.0),
            "base_rate": float(macro_raw.get("base_rate") or 0.0),
        }

        for ticker, tdata in market_data.items():
            ohlcv = tdata.get("ohlcv", {})
            volume = tdata.get("volume", 0)
            tech = tdata.get("tech_features", {})

            if ticker not in history:
                history[ticker] = {}

            history[ticker][date_str] = {
                "ohlcv": {
                    "open": float(ohlcv.get("open", 0.0)),
                    "high": float(ohlcv.get("high", 0.0)),
                    "low": float(ohlcv.get("low", 0.0)),
                    "close": float(ohlcv.get("close", 0.0)),
                },
                "volume": float(volume),
                "tech_features": {k: float(v if v is not None else 0.0) for k, v in tech.items()},
                "macro": macro,
                "mktcap": float(tdata.get("mktcap") or 0),
            }

    print(f"[Modeler] DMP history 로드 완료: {len(dates)}일 / {len(history)}종목")
    return history



# compute_sector_relative_features, make_chart_tensor, _build_context_vector, _build_wics_sector_list
# → models/rebound_cnn/preprocess.py로 분리 (GPT Pro 권고 #1)




def make_excess_return_label(
    ticker_future_rets: list,
    universe_future_rets_ew: list,
    threshold: float = 0.0,
) -> int:
    """
    Excess return label.
    excess_i_5d = r_i(t,t+5) - r_EW26(t,t+5)
    y = 1 if excess > threshold, else 0

    PIT-safe: label은 t+1~t+5 데이터로만 계산.

    ticker_future_rets: 해당 종목의 [ret_t+1, ..., ret_t+5] 기준가 대비 수익률
    universe_future_rets_ew: 유니버스 동일가중 평균 [ret_t+1, ..., ret_t+5]
    threshold: excess return 기준 (기본 0.0%)
    """
    if not ticker_future_rets or not universe_future_rets_ew:
        return 0

    # t+5 기준 수익률 사용 (마지막 horizon 시점)
    r_i = ticker_future_rets[-1]
    r_ew = universe_future_rets_ew[-1]
    excess = r_i - r_ew
    return 1 if excess > threshold else 0


def apply_oversold_gate(
    ticker: str,
    sector_feats: dict,
    gate_config: dict,
) -> bool:
    """
    Oversold Gate 조건 체크.

    4개 조건 중 min_conditions개 이상 충족 AND sector-relative 약세 조건 충족 시 True.

    조건:
      1. ret_5d < ret_5d_threshold (기본 0.0, 즉 ret_5d < 0)
      2. rsi_14 < rsi_14_threshold (기본 45)
      3. close_sma20_ratio < 0 (close < sma_20)
      4. bb_pos < bb_pos_threshold (기본 0.35)

    Sector-relative 약세:
      ret_5d_sector_z <= sector_ret5_z_threshold (-0.25)
      OR ret_5d_rank_in_sector <= sector_bottom_pct (하위 40%)
    """
    ret_5d = sector_feats.get("ret_5d", 0.0)
    rsi_14 = sector_feats.get("rsi_14", 50.0)
    close_sma20_ratio = sector_feats.get("close_sma20_ratio", 0.0)
    bb_pos = sector_feats.get("bb_pos", 0.5)
    ret_5d_sector_z = sector_feats.get("ret_5d_sector_z", 0.0)
    ret_5d_rank = sector_feats.get("ret_5d_rank_in_sector", 0.5)

    ret_5d_thr = gate_config.get("ret_5d_threshold", 0.0)
    rsi_thr = gate_config.get("rsi_14_threshold", 45)
    bb_thr = gate_config.get("bb_pos_threshold", 0.35)
    z_thr = gate_config.get("sector_ret5_z_threshold", -0.25)
    bottom_pct = gate_config.get("sector_bottom_pct", 0.40)
    min_cond = gate_config.get("min_conditions", 2)

    # 4개 조건 체크
    cond1 = ret_5d < ret_5d_thr
    cond2 = rsi_14 < rsi_thr
    cond3 = close_sma20_ratio < gate_config.get("close_sma20_threshold", 0.0)
    cond4 = bb_pos < bb_thr

    n_met = sum([cond1, cond2, cond3, cond4])

    # sector-relative 약세 체크
    sector_weak = (ret_5d_sector_z <= z_thr) or (ret_5d_rank <= bottom_pct)

    return (n_met >= min_cond) and sector_weak


def compute_universe_ew_returns(
    history: dict,
    universe_tickers: list,
    base_date: str,
    future_dates: list,
) -> list:
    """
    유니버스 26종목 동일가중 평균 수익률 계산.
    PIT-safe: base_date 종가 기준, future_dates (t+1~t+5) 수익률.

    반환: [ew_ret_t+1, ew_ret_t+2, ..., ew_ret_t+horizon]
    """
    horizon = len(future_dates)
    ew_rets = [0.0] * horizon
    counts = [0] * horizon

    for ticker in universe_tickers:
        td = history.get(ticker)
        if td is None:
            continue
        base_close = td.get(base_date, {}).get("ohlcv", {}).get("close")
        if not base_close or base_close <= 0:
            continue
        for h, fd in enumerate(future_dates):
            if fd in td:
                fc = td[fd]["ohlcv"]["close"]
                ew_rets[h] += (fc - base_close) / base_close
                counts[h] += 1

    # 평균
    result = []
    for h in range(horizon):
        result.append(ew_rets[h] / counts[h] if counts[h] > 0 else 0.0)
    return result




class ReboundDataset(torch.utils.data.Dataset):
    """
    KR-Rebound-CNN PyTorch Dataset.
    각 샘플: (chart_tensor, context_features, label)

    - context_features: 26차원 (설계서 §10.2, 실제 차원 = 15 + n_sectors)
    - excess return label 사용 (y=1 if excess_i_5d > threshold)
    - Oversold Gate 적용 (gate 통과 종목만 포함)
    - 3-channel chart tensor
    - StandardScaler 지원 (fit_scaler / apply_scaler)
    """

    def __init__(self, dmp_dir: Path, dates: list, config: dict, sample_dates: list = None):
        """
        PIT-Safety: DMP는 장마감(18:00 KST) 후 생성되므로 미래 데이터를 포함하지 않음.
        label 계산은 t+1~t+5 미래 데이터만 사용하며, 학습 입력에는 t일 이전 데이터만 포함.

        dates: history 로드용 전체 날짜 목록 (lookback용 과거 포함)
        sample_dates: 실제 샘플 생성 대상 날짜 (None이면 dates 전체 사용)
                      val split 시 train_dates + val_dates를 dates로, val_dates만 sample_dates로 전달.
        """
        self.config = config
        self.samples: list = []
        self.scaler = None  # StandardScaler (fit_scaler 호출 후 설정)

        lookback = config["data"]["lookback_days"]
        horizon = config["data"]["forecast_horizon"]
        threshold = config["data"]["rebound_threshold"]
        gate_config = config.get("oversold_gate", {})

        universe = pd.read_csv(UNIVERSE_CSV, dtype={"ticker": str})
        universe["ticker"] = universe["ticker"].apply(lambda x: str(x).zfill(6))
        universe_tickers = universe["ticker"].tolist()

        # 섹터 관련 매핑 (context vector 조립용)
        wics_sectors = _build_wics_sector_list(universe)
        sector_map: dict = {
            str(row["ticker"]).zfill(6): row["wics_sector"]
            for _, row in universe.iterrows()
        }
        self.n_context_features = 15 + len(wics_sectors)  # base(15) + n_sectors

        # 충분한 히스토리 확보를 위해 앞뒤로 추가 날짜 필요
        # dates: history 로드용 전체 날짜 (lookback용 과거 포함)
        history = load_dmp_history(dates, dmp_dir)
        dates_sorted = sorted(dates)

        # sample_dates: 실제 샘플 생성 대상 날짜 집합 (None이면 dates 전체)
        target_dates_set = set(sample_dates) if sample_dates is not None else None

        print(
            f"[Modeler] 샘플 생성 시작 (lookback={lookback}, horizon={horizon}, "
            f"n_context_features={self.n_context_features}, n_sectors={len(wics_sectors)}"
            + (f", sample_dates={len(sample_dates)}일" if sample_dates is not None else "")
            + ")"
        )

        relaxation = gate_config.get("relaxation", {})
        relaxation_enabled = relaxation.get("enabled", True)
        min_candidates = relaxation.get("min_candidates", 3)

        for t_idx in range(lookback, len(dates_sorted) - horizon):
            t_date = dates_sorted[t_idx]
            # sample_dates가 지정된 경우 해당 날짜에 속하는 t_date만 샘플 생성
            if target_dates_set is not None and t_date not in target_dates_set:
                continue
            window_dates = dates_sorted[t_idx - lookback: t_idx]
            future_dates = dates_sorted[t_idx + 1: t_idx + 1 + horizon]

            sector_feats_map = compute_sector_relative_features(
                history, universe, t_date
            )

            # 유니버스 내 시가총액 percentile rank 계산 (PIT-safe: t일 DMP mktcap 사용)
            mktcap_ranks: dict = {}
            mktcaps_raw: dict = {}
            for t in universe_tickers:
                mc_raw = history.get(t, {}).get(t_date, {}).get("mktcap", None)
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
                    rank = sorted_caps.index(mc) / max(n_caps - 1, 1)
                    mktcap_ranks[t] = float(rank)

            # 유니버스 동일가중 평균 수익률 (PIT-safe: t+1~t+5)
            ew_rets = compute_universe_ew_returns(
                history, universe_tickers, t_date, future_dates
            )

            # Oversold Gate 1차 적용
            gate_candidates = []
            for ticker in universe_tickers:
                if ticker not in history:
                    continue
                sf = sector_feats_map.get(ticker, {})
                if apply_oversold_gate(ticker, sf, gate_config):
                    gate_candidates.append(ticker)

            # Gate Relaxation: 후보 부족 시 RSI/sector 임계값 완화
            # 기존 통과 종목은 보존하고, 미통과 종목에 대해서만 완화 조건 재평가
            if relaxation_enabled and len(gate_candidates) < min_candidates:
                relaxed_gate_config = dict(gate_config)
                relaxed_gate_config["rsi_14_threshold"] = relaxation.get("rsi_relaxed", 48)
                relaxed_gate_config["sector_bottom_pct"] = relaxation.get(
                    "sector_bottom_pct_relaxed", 0.50
                )
                remaining = [t for t in universe_tickers if t not in gate_candidates]
                added = []
                for ticker in remaining:
                    if ticker not in history:
                        continue
                    sf = sector_feats_map.get(ticker, {})
                    if apply_oversold_gate(ticker, sf, relaxed_gate_config):
                        gate_candidates.append(ticker)
                        added.append(ticker)
                if added:
                    print(
                        f"[Modeler] {t_date} Gate relaxation 적용: "
                        f"{len(gate_candidates)}종목 후보 (+{len(added)})"
                    )

            # gate 후보 종목에 대해 샘플 생성
            for ticker in gate_candidates:
                td = history.get(ticker)
                if td is None:
                    continue

                # 윈도우 데이터 완전성 확인
                window_snaps = [td.get(d) for d in window_dates]
                if any(s is None for s in window_snaps):
                    continue

                # 기준가 (t일 종가)
                base_close = td.get(t_date, {}).get("ohlcv", {}).get("close")
                if not base_close or base_close <= 0:
                    continue

                # 해당 종목 미래 수익률 (PIT-safe: t+1~t+5)
                ticker_rets = []
                for fd in future_dates:
                    if fd in td:
                        fc = td[fd]["ohlcv"]["close"]
                        ticker_rets.append((fc - base_close) / base_close)

                if len(ticker_rets) < horizon:
                    continue

                # Excess return label
                label = make_excess_return_label(ticker_rets, ew_rets, threshold)

                chart_tensor = make_chart_tensor(
                    window_snaps,
                    size=(
                        config["data"]["image"]["height"],
                        config["data"]["image"]["width"],
                    ),
                )

                # 26차원 context vector 조립 (설계서 §10.2)
                sf = sector_feats_map.get(ticker, {})
                mktcap_rank = mktcap_ranks.get(ticker, 0.5)
                context_list = _build_context_vector(
                    ticker, t_date, td, sf, sector_map, wics_sectors, mktcap_rank
                )
                context_tensor = torch.tensor(context_list, dtype=torch.float32)

                # ret_5d raw 값 추출 (style orthogonality 평가용, PIT-safe: t일 이전 데이터)
                sf_for_ret5d = sector_feats_map.get(ticker, {})
                raw_ret_5d = float(sf_for_ret5d.get("ret_5d", 0.0))

                self.samples.append({
                    "chart_tensor": chart_tensor,
                    "context_features": context_tensor,
                    "label": torch.tensor(label, dtype=torch.float32),
                    "ticker": ticker,
                    "date": t_date,
                    "ret_5d": raw_ret_5d,  # raw 5일 수익률 (설계서 style orthogonality 용)
                })

        print(f"[Modeler] 총 {len(self.samples)}개 샘플 생성 완료")

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, idx: int):
        s = self.samples[idx]
        return (
            s["chart_tensor"],
            s["context_features"],
            s["label"],
        )

    def fit_scaler(self) -> StandardScaler:
        """
        train set의 context_features로 StandardScaler fit.
        설계서 §7.2: "scaling fit은 train fold에서만"

        반환: fit된 StandardScaler 인스턴스
        """
        if len(self.samples) == 0:
            raise ValueError("[Modeler] 샘플이 없어 scaler fit 불가")
        all_ctx = np.array([s["context_features"].numpy() for s in self.samples])
        self.scaler = StandardScaler()
        self.scaler.fit(all_ctx)
        print(
            f"[Modeler] StandardScaler fit 완료: "
            f"n_samples={len(all_ctx)}, n_features={all_ctx.shape[1]}"
        )
        return self.scaler

    def apply_scaler(self, scaler: StandardScaler) -> None:
        """
        외부 scaler를 적용하여 모든 샘플의 context_features를 in-place 변환.
        val/test set은 train scaler로 transform만 수행 (설계서 §7.2 PIT-Safety).
        """
        for s in self.samples:
            ctx = s["context_features"].numpy().reshape(1, -1)
            scaled = scaler.transform(ctx).flatten()
            s["context_features"] = torch.tensor(scaled, dtype=torch.float32)
        print(f"[Modeler] StandardScaler transform 적용 완료: {len(self.samples)}개 샘플")


def build_dataset(config_path: Path, output_dir: Path):
    """
    config 로드 → DMP history 로드 → dataset 생성 → .pt 파일 저장.
    출력: dataset_train.pt, dataset_val.pt, dataset_test.pt
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    _set_seed(config["training"]["seed"])

    # DMP 디렉토리에서 가용 날짜 목록 수집
    dmp_dir = Path(DMP_DIR)
    dmp_files = sorted(dmp_dir.glob("DMP-*.json"))
    all_dates = [p.stem.replace("DMP-", "") for p in dmp_files]

    if len(all_dates) == 0:
        print("[Modeler] DMP 파일이 없습니다. 빌드 중단.")
        return

    # walk-forward 분할
    wf = config["training"]["walk_forward"]
    train_w = wf["train_window"]
    val_w = wf["val_window"]
    min_hist = config["data"]["min_history_days"]

    total = len(all_dates)
    lookback = config["data"]["lookback_days"]
    horizon = config["data"]["forecast_horizon"]

    # 가용 날짜가 부족하면 전체를 train으로
    if total < min_hist:
        print(f"[Modeler] 가용 날짜 {total}일 < min_history_days {min_hist}. 전체를 train 사용.")
        train_dates = all_dates
        val_dates = all_dates
        test_dates = all_dates
    else:
        test_end = total
        test_start = max(0, test_end - val_w)
        val_start = max(0, test_start - val_w)
        train_start = max(0, val_start - train_w)

        train_dates = all_dates[train_start: val_start]
        val_dates = all_dates[val_start: test_start]
        test_dates = all_dates[test_start: test_end]

    print(f"[Modeler] train={len(train_dates)}일, val={len(val_dates)}일, test={len(test_dates)}일")

    splits = {
        "train": train_dates,
        "val": val_dates,
        "test": test_dates,
    }

    for split_name, dates in splits.items():
        if len(dates) <= lookback + horizon:
            print(f"[Modeler] {split_name} 날짜 부족 ({len(dates)}일) → 스킵")
            continue

        ds = ReboundDataset(dmp_dir, dates, config)
        out_path = output_dir / f"dataset_{split_name}.pt"
        torch.save(ds, out_path)
        print(f"[Modeler] {split_name} dataset 저장: {out_path} ({len(ds)}샘플)")


if __name__ == "__main__":
    config_path = Path(__file__).resolve().parent / "config.yaml"
    output_dir = Path(__file__).resolve().parent
    build_dataset(config_path, output_dir)
