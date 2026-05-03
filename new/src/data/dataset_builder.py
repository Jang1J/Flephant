"""S1-0 Batch B DatasetBuilder. backfill parquet → LightGBM 학습용 DataFrame.

파이프라인:
  artifacts/data/{ticker}/bars_1m_*.parquet
    → load_ticker_bars(raw OHLCV time series)
    → compute_rolling_features(multi-scale features per ticker)
    → generate_labels(PIT-safe forward return)
    → cross_sectional_rank(매 ts_close 내 20종목 pct rank → relevance grade)
    → join_dual_source_features(S4-2: Dual-Source 5피처 날짜 기준 join)
    → 최종 panel DataFrame (MultiIndex ticker × ts_close)

불변 원칙 준수:
  - 모든 수치는 risk_config.yaml에서 로드 (하드코딩 금지)
  - PIT-Safety: label_5m_ret은 t+horizon close 참조 → training 전용.
    drop_last_n_bars로 마지막 horizon bar 제거.
  - 종목코드: pad_ticker() 6자리 zero-padded

S4-2 Dual-Source 확장:
  - dual_source.enabled_for_lgbm=true 시 5피처 join (날짜 기준 left join).
  - 결측 시 기본값 0.0 (PIT-safe: 배치 점수 없으면 중립 처리).
  - 5피처명: news_score_t, comm_score_t_1, comm_score_t_2,
             news_comm_divergence, community_noise_multiplier.

Batch C lgbm_trainer가 이 DataFrame을 소비.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from src.data.dual_source_runner import load_latest_scores
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.ticker_utils import pad_ticker

# S4-2: Dual-Source 5피처 컬럼명 (SSOT). risk_config.yaml dual_source_feature_cols와 동기화.
DUAL_SOURCE_FEATURES: tuple[str, ...] = (
    "news_score_t",
    "comm_score_t_1",
    "comm_score_t_2",
    "news_comm_divergence",
    "community_noise_multiplier",
)

logger = get_logger("dataset_builder")

_KST = ZoneInfo("Asia/Seoul")

_ARTIFACTS_ROOT = Path(__file__).resolve().parents[3] / "artifacts" / "data"


class DatasetBuildError(Exception):
    """dataset 생성 실패."""


class LabelLeakageError(ValueError):
    """label 생성 시 PIT-Safety 위반 감지."""


def _import_pandas():
    """lazy pandas import. 없으면 DatasetBuildError."""
    try:
        import pandas as pd  # type: ignore[import]

        return pd
    except ImportError as e:
        raise DatasetBuildError(
            "pandas 미설치. DatasetBuilder는 pandas 필수. "
            "pip install pandas pyarrow 로 설치."
        ) from e


class DatasetBuilder:
    """LightGBM 학습용 panel DataFrame 생성기.

    Input: artifacts/data/{ticker}/bars_1m_*.parquet (또는 .jsonl fallback)
    Output: pd.DataFrame with MultiIndex (ticker, ts_close).

    주요 컬럼:
        open, high, low, close, volume             # raw OHLCV
        feat_1m_close_robust_z                     # 60m rolling MAD robust Z
        feat_5m_ret                                # close_t / close_{t-5} - 1
        feat_30m_vol                               # std(ret_{t-29..t})
        feat_60m_trend                             # 60-bar linear slope / mean
        label_5m_ret                               # close_{t+5} / close_t - 1 (PIT-safe)
        cs_rank                                    # cross-sectional pct rank (0~1)
        relevance                                  # LambdaRank grade (0~3)

    PIT-Safety:
        label_5m_ret 계산 시 t+horizon 참조 = training-time only.
        마지막 horizon bars는 자동 drop.
        leakage_guard=True면 label 음수/NaN 패턴 재검증.
    """

    def __init__(
        self,
        artifacts_dir: Path | None = None,
    ) -> None:
        self._artifacts_dir = artifacts_dir or _ARTIFACTS_ROOT

        # risk_config.yaml 로드 (불변 원칙 5)
        self._cfg_label: dict = config_load("risk_config.yaml", "label")
        self._cfg_preprocessor: dict = config_load("risk_config.yaml", "preprocessor")
        self._cfg_lgbm: dict = config_load("risk_config.yaml", "lightgbm")

        self._horizon: int = int(self._cfg_label["horizon_bars"])
        self._target_col: str = str(self._cfg_label["target_col"])
        self._rank_col: str = str(self._cfg_label["rank_col"])
        self._leakage_guard: bool = bool(self._cfg_label["leakage_guard"])
        self._drop_last_n: int = int(self._cfg_label["drop_last_n_bars"])

        self._multi_scale_windows: list[int] = list(
            self._cfg_preprocessor["multi_scale_windows"]
        )
        self._mad_constant: float = float(self._cfg_preprocessor["mad_constant"])
        self._outlier_cap_z: float = float(self._cfg_preprocessor["outlier_cap_z"])

        self._n_relevance_grades: int = int(self._cfg_lgbm["n_relevance_grades"])

        # S4-2: Dual-Source 피처 주입 설정 (risk_config.yaml dual_source 섹션)
        _cfg_ds: dict = config_load("risk_config.yaml", "dual_source") or {}
        self._ds_enabled_for_lgbm: bool = bool(_cfg_ds.get("enabled_for_lgbm", False))
        self._ds_feature_cols: list[str] = list(DUAL_SOURCE_FEATURES)

        logger.info(
            "[dataset_builder] 초기화 완료. horizon=%d bars, "
            "target_col=%s, windows=%s, grades=%d, ds_enabled_for_lgbm=%s",
            self._horizon, self._target_col,
            self._multi_scale_windows, self._n_relevance_grades,
            self._ds_enabled_for_lgbm,
        )

    # ================================================================== #
    # Public properties (외부 consumer 캡슐화 유지)
    # ================================================================== #

    @property
    def horizon_bars(self) -> int:
        """Label horizon (bars). yaml label.horizon_bars 값."""
        return self._horizon

    @property
    def target_col(self) -> str:
        """Label 컬럼명. yaml label.target_col 값."""
        return self._target_col

    @property
    def multi_scale_windows(self) -> list[int]:
        """Multi-scale rolling windows. yaml preprocessor.multi_scale_windows 값."""
        return list(self._multi_scale_windows)

    # ================================================================== #
    # Public API
    # ================================================================== #

    def build_training_frame(
        self,
        tickers: list[str],
        start_date: str,
        end_date: str,
    ):
        """tickers × [start_date, end_date] 범위 training panel DataFrame 생성.

        Returns:
            pd.DataFrame with MultiIndex (ticker, ts_close).
            label/rank NaN 행은 자동 drop.
            Cross-sectional 그룹 크기 < n_relevance_grades 인 ts_close 그룹 drop.

        Raises:
            DatasetBuildError: 전체 ticker 로드 실패 또는 최종 프레임 empty
            LabelLeakageError: leakage_guard=True 시 label 이상 패턴 감지
        """
        pd = _import_pandas()

        padded = [pad_ticker(t) for t in tickers]
        per_ticker_frames: list = []

        for ticker in padded:
            raw = self._load_ticker_bars(ticker, start_date, end_date)
            if raw is None or raw.empty:
                logger.warning(
                    "[dataset_builder] %s: 데이터 없음, skip", ticker
                )
                continue

            with_feats = self._compute_rolling_features(raw)
            with_label = self._generate_labels(with_feats)
            per_ticker_frames.append(with_label)

        if not per_ticker_frames:
            raise DatasetBuildError(
                f"모든 ticker({len(padded)}개) 데이터 로드 실패. "
                f"artifacts_dir={self._artifacts_dir} 확인."
            )

        panel = pd.concat(per_ticker_frames, axis=0)
        panel = self._cross_sectional_rank(panel)

        # S4-2: Dual-Source 5피처 join (enabled_for_lgbm=true 시)
        if self._ds_enabled_for_lgbm:
            panel = self._join_dual_source_features(panel, start_date, end_date)

        # label/rank NaN 행 drop
        before = len(panel)
        panel = panel.dropna(subset=[self._target_col, self._rank_col, "relevance"])
        logger.info(
            "[dataset_builder] 최종 panel: %d rows (pre-drop %d, "
            "drop %d NaN label/rank 행)",
            len(panel), before, before - len(panel),
        )

        if panel.empty:
            raise DatasetBuildError("label/rank 계산 후 panel empty")

        # PIT-safety 재검증 (leakage_guard)
        if self._leakage_guard:
            self._assert_no_leakage(panel)

        return panel

    # ================================================================== #
    # Step 1: raw parquet 로드
    # ================================================================== #

    def _load_ticker_bars(
        self,
        ticker: str,
        start_date: str,
        end_date: str,
    ):
        """ticker 디렉토리에서 start~end 범위 모든 parquet/jsonl 파일 로드 → DataFrame.

        Returns: pd.DataFrame | None. 컬럼: ticker, ts_close(Timestamp, tz-aware KST),
                 open, high, low, close, volume.
        """
        pd = _import_pandas()

        ticker_dir = self._artifacts_dir / ticker
        if not ticker_dir.exists():
            return None

        start = _parse_yyyymmdd(start_date)
        end = _parse_yyyymmdd(end_date)

        frames: list = []
        for file_path in sorted(ticker_dir.iterdir()):
            file_date = _extract_file_date(file_path)
            if file_date is None:
                continue
            if not (start <= file_date <= end):
                continue
            df = self._read_bar_file(file_path)
            if df is not None and not df.empty:
                frames.append(df)

        if not frames:
            return None

        combined = pd.concat(frames, axis=0, ignore_index=True)
        combined["ts_close"] = pd.to_datetime(combined["ts_close"], utc=False)

        # tz-aware KST로 정규화
        if combined["ts_close"].dt.tz is None:
            combined["ts_close"] = combined["ts_close"].dt.tz_localize(_KST)
        else:
            combined["ts_close"] = combined["ts_close"].dt.tz_convert(_KST)

        combined["ticker"] = ticker
        combined = combined.sort_values("ts_close").reset_index(drop=True)

        # 숫자 컬럼 보장
        for col in ("open", "high", "low", "close", "volume"):
            combined[col] = combined[col].astype(float)

        return combined

    def _read_bar_file(self, file_path: Path):
        """parquet 또는 jsonl 읽기. 실패 시 None 반환 (한 파일 실패가 전체를 막지 않게)."""
        pd = _import_pandas()
        try:
            if file_path.suffix == ".parquet":
                return pd.read_parquet(file_path)
            if file_path.suffix == ".jsonl":
                records = []
                with file_path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            records.append(json.loads(line))
                return pd.DataFrame(records)
        except Exception as e:
            logger.warning(
                "[dataset_builder] %s 읽기 실패: %s", file_path, e
            )
        return None

    # ================================================================== #
    # Step 2: rolling features (time-axis)
    # ================================================================== #

    def _compute_rolling_features(self, raw):
        """단일 ticker raw OHLCV DataFrame → 4 피처 컬럼 추가.

        feat_1m_close_robust_z: 60m rolling MAD robust Z of close
        feat_5m_ret: close_t / close_{t-5} - 1
        feat_30m_vol: rolling std(close_{t-29..t}) / rolling mean
        feat_60m_trend: rolling 60-bar linear slope / mean
        """
        pd = _import_pandas()
        df = raw.copy()
        closes = df["close"].to_numpy(dtype=float)
        n = len(closes)

        # multi_scale_windows: [1m, 5m, 30m, 60m]. yaml 경유 (불변 원칙 5).
        # w1은 원본(reference), w5/w30/w60이 실제 피처 window로 쓰임.
        _, w5, w30, w60 = self._multi_scale_windows

        # feat_5m_ret (multi_scale_windows[1] = 5)
        five_min_ret = np.full(n, np.nan, dtype=float)
        if n > w5:
            prev = closes[:-w5]
            curr = closes[w5:]
            with np.errstate(divide="ignore", invalid="ignore"):
                ratio = np.where(prev > 1e-8, curr / prev - 1.0, 0.0)
            five_min_ret[w5:] = ratio
        df["feat_5m_ret"] = five_min_ret

        # feat_30m_vol (multi_scale_windows[2] = 30, rolling std normalized by mean).
        # min_periods는 risk_config.yaml preprocessor.rolling_min_periods 경유.
        min_periods = int(self._cfg_preprocessor.get("rolling_min_periods", 5))
        close_series = df["close"].astype(float)
        roll_mean = close_series.rolling(window=w30, min_periods=min_periods).mean()
        roll_std = close_series.rolling(window=w30, min_periods=min_periods).std(ddof=1)
        df["feat_30m_vol"] = (roll_std / roll_mean.replace(0, np.nan)).to_numpy()

        # feat_60m_trend (multi_scale_windows[3] = 60, rolling linear slope / mean)
        df["feat_60m_trend"] = self._rolling_trend(closes, window=w60).tolist()

        # feat_1m_close_robust_z (60m rolling MAD-Z of close_t, multi_scale_windows[3])
        df["feat_1m_close_robust_z"] = self._rolling_robust_z(
            closes, window=w60
        ).tolist()

        return df

    def _rolling_trend(self, closes: np.ndarray, window: int) -> np.ndarray:
        """rolling 60-bar linear regression slope / mean. O(n*window) 단순 구현.

        PERF-TODO: Sprint 4 vectorize. 1년치 200만 rows 기준 현재 수 분 소요 예상.
        np.lib.stride_tricks 또는 numba JIT 검토. Hot Path 비영향 (offline 학습 전용).
        """
        n = len(closes)
        out = np.full(n, np.nan, dtype=float)
        if n < 2:
            return out
        x = np.arange(window, dtype=float)
        x_mean = x.mean()
        x_var = float(np.sum((x - x_mean) ** 2))
        if x_var < 1e-12:
            return out

        for i in range(window - 1, n):
            y = closes[i - window + 1:i + 1]
            y_mean = float(y.mean())
            if y_mean < 1e-8:
                continue
            cov = float(np.sum((x - x_mean) * (y - y_mean)))
            slope = cov / x_var
            out[i] = slope / y_mean
        return out

    def _rolling_robust_z(self, closes: np.ndarray, window: int) -> np.ndarray:
        """rolling MAD robust Z. (close_t - median) / (MAD * mad_constant), clip ±cap."""
        n = len(closes)
        out = np.full(n, np.nan, dtype=float)
        for i in range(window - 1, n):
            block = closes[i - window + 1:i + 1]
            med = float(np.median(block))
            mad = float(np.median(np.abs(block - med)))
            denom = mad * self._mad_constant
            if denom < 1e-8:
                denom = 1e-8
            z = (float(closes[i]) - med) / denom
            out[i] = float(np.clip(z, -self._outlier_cap_z, self._outlier_cap_z))
        return out

    # ================================================================== #
    # Step 3: label generation (PIT-safe forward return)
    # ================================================================== #

    def _generate_labels(self, df):
        """close_{t+horizon} / close_t - 1 label. 마지막 horizon bars는 NaN 후 drop.

        drop_last_n_bars (risk_config.yaml label.drop_last_n_bars) 적용.
        leakage_guard는 최종 panel 단계에서 재검증 (_assert_no_leakage).
        """
        pd = _import_pandas()

        h = self._horizon
        closes = df["close"].to_numpy(dtype=float)
        n = len(closes)

        labels = np.full(n, np.nan, dtype=float)
        if n > h:
            future = closes[h:]
            now = closes[:-h]
            with np.errstate(divide="ignore", invalid="ignore"):
                ret = np.where(now > 1e-8, future / now - 1.0, np.nan)
            labels[:-h] = ret

        out = df.copy()
        out[self._target_col] = labels

        # 마지막 horizon bars + drop_last_n_bars (추가 safety) 만큼 drop.
        # len(out) <= drop_count 이면 iloc[:-drop_count]는 empty → 그대로 반환.
        drop_count = max(h, self._drop_last_n)
        if drop_count > 0:
            out = out.iloc[:-drop_count].copy()

        return out

    # ================================================================== #
    # Step 4: cross-sectional rank + relevance grade
    # ================================================================== #

    def _cross_sectional_rank(self, panel):
        """매 ts_close 그룹 내 label_5m_ret → pct rank (0~1) → relevance (0 ~ n-1).

        그룹 크기 < n_relevance_grades 면 해당 ts_close 그룹 drop.
        """
        pd = _import_pandas()
        pd_local = pd

        target = self._target_col
        n_grades = self._n_relevance_grades

        # pct rank per ts_close
        panel = panel.copy()
        panel[self._rank_col] = panel.groupby("ts_close")[target].rank(
            method="average", pct=True
        )

        # relevance grade: pct rank을 n_grades 버킷으로
        panel["relevance"] = self._to_relevance(panel[self._rank_col], n_grades)

        # 그룹 크기 < n_grades 인 ts_close 제거
        group_sizes = panel.groupby("ts_close")["close"].transform("count")
        keep = group_sizes >= n_grades
        dropped = (~keep).sum()
        if dropped > 0:
            logger.info(
                "[dataset_builder] %d 행 drop: ts_close 그룹 크기 < %d",
                int(dropped), n_grades,
            )
        panel = panel[keep].copy()

        # MultiIndex 설정
        panel = panel.set_index(["ticker", "ts_close"]).sort_index()
        return panel

    @staticmethod
    def _to_relevance(pct_rank, n_grades: int):
        """pct_rank (0~1) → 정수 relevance grade (0 ~ n_grades-1).

        버킷 규약 (right-closed, pd.cut 스타일):
          [0, 1/n] → 0, (1/n, 2/n] → 1, ..., ((n-1)/n, 1] → n-1.
        tie 경계값(0.25, 0.5, 0.75 등)은 낮은 쪽 버킷 끝에 포함.

        edge case:
          pct_rank=0.0 → 포함시키기 위해 첫 버킷을 `include_lowest=True`로.
        """
        import pandas as pd_

        if n_grades < 1:
            raise ValueError(f"n_grades must be >= 1, got {n_grades}")

        edges = [i / n_grades for i in range(n_grades + 1)]
        labels = list(range(n_grades))
        # pd.cut: 오른쪽 경계 포함(right=True), 첫 구간에 최저값 포함
        binned = pd_.cut(
            pct_rank.astype(float),
            bins=edges,
            labels=labels,
            include_lowest=True,
            right=True,
        )
        # Categorical → float (NaN 보존)
        return binned.astype(float).rename("relevance")

    # ================================================================== #
    # Step 4b: Dual-Source 5피처 join (S4-2)
    # ================================================================== #

    def _join_dual_source_features(self, panel, start_date: str, end_date: str):
        """날짜 범위 내 Dual-Source 5피처를 panel에 left join.

        - panel: MultiIndex (ticker, ts_close) DataFrame.
        - dual_source_runner.load_latest_scores(date_str) → list[dict] 로드.
          각 dict: {"ticker": str, "news_score_t": float, ..., "community_noise_multiplier": float}.
        - 날짜 기준 join: ts_close.date() = 배치 날짜 매핑.
        - 결측 시 기본값 0.0 (PIT-safe: 배치 점수 없는 날은 중립 처리).

        PIT-Safety:
          - load_latest_scores는 YYYYMMDD 배치 파일(08:30 KST 생성)을 로드.
          - ts_close는 장중 1분봉 → 당일 배치 점수와 join 시 미래 참조 없음.
          - 단, 배치 파일 생성이 ts_close보다 이전임을 보장 (08:30 KST < 09:00 KST).

        R1-W4 벡터화: 행 단위 iloc 루프 → reset_index + merge + set_index.
        1년치 20종목 1분봉 (~700만 행) 기준 O(N) merge로 대폭 가속.
        """
        pd = _import_pandas()
        start = _parse_yyyymmdd(start_date)
        end = _parse_yyyymmdd(end_date)

        # 날짜 범위 내 모든 날짜의 Dual-Source 점수를 미리 로드
        # {date_str: {ticker: {feat: val}}}
        date_ticker_scores: dict[str, dict[str, dict[str, float]]] = {}

        current = start
        while current <= end:
            date_str = current.strftime("%Y%m%d")
            scores_list: list[dict] = load_latest_scores(date_str)
            if scores_list:
                ticker_map: dict[str, dict[str, float]] = {}
                for item in scores_list:
                    t = str(item.get("ticker", "")).zfill(6)
                    if not t or t == "000000":
                        continue
                    ticker_map[t] = {
                        feat: float(item.get(feat, 0.0))
                        for feat in self._ds_feature_cols
                    }
                date_ticker_scores[date_str] = ticker_map
            current = current + timedelta(days=1)

        if not date_ticker_scores:
            logger.warning(
                "[dataset_builder] Dual-Source 점수 파일 없음 (%s~%s). "
                "5피처 기본값 0.0 사용.",
                start_date, end_date,
            )

        # panel에 5피처 컬럼 추가 (기본값 0.0)
        panel = panel.copy()
        for feat in self._ds_feature_cols:
            panel[feat] = 0.0

        if not date_ticker_scores:
            logger.info("[dataset_builder] Dual-Source join 완료: 기본값 0.0 (파일 없음)")
            return panel

        # date_ticker_scores → DataFrame 변환 후 벡터화 merge
        # {date_str: {ticker: {feat: val}}} → flat records
        ds_records = []
        for date_str, ticker_map in date_ticker_scores.items():
            for ticker, feats in ticker_map.items():
                ds_records.append(
                    {
                        "date_str": date_str,
                        "ticker": ticker,
                        **{feat: float(feats.get(feat, 0.0)) for feat in self._ds_feature_cols},
                    }
                )

        if not ds_records:
            logger.info("[dataset_builder] Dual-Source join 완료: 기본값 0.0 (레코드 없음)")
            return panel

        ds_df = pd.DataFrame(ds_records)

        # panel을 flat으로 펼쳐 merge 후 다시 MultiIndex 복원
        panel_flat = panel.reset_index()
        panel_flat["date_str"] = panel_flat["ts_close"].dt.strftime("%Y%m%d")

        # 5피처 컬럼을 left join 전에 제거 (merge 후 _x/_y 충돌 방지)
        panel_flat_base = panel_flat.drop(columns=list(self._ds_feature_cols), errors="ignore")

        merged = panel_flat_base.merge(
            ds_df,
            on=["date_str", "ticker"],
            how="left",
        )

        # 결측 → 0.0 채우기
        for feat in self._ds_feature_cols:
            merged[feat] = merged[feat].fillna(0.0)

        # join 통계 (hit = 5피처 중 하나라도 non-zero)
        hit_mask = merged[list(self._ds_feature_cols)].any(axis=1)
        join_hit = int(hit_mask.sum())
        join_miss = len(merged) - join_hit

        # date_str 컬럼 제거 후 MultiIndex 복원
        merged = merged.drop(columns=["date_str"])
        panel = merged.set_index(["ticker", "ts_close"]).sort_index()

        total = join_hit + join_miss
        logger.info(
            "[dataset_builder] Dual-Source join 완료 (벡터화): hit=%d miss=%d (%.1f%% 적용)",
            join_hit, join_miss,
            100.0 * join_hit / max(total, 1),
        )
        return panel

    # ================================================================== #
    # PIT-safety re-assertion
    # ================================================================== #

    def _assert_no_leakage(self, panel) -> None:
        """label 생성 규약 위반 검증. build_training_frame dropna 이후 호출.

        검증 항목:
          1. NaN: dropna가 이미 제거했어야 함. 발견 시 dropna 경로 버그 의미.
             (방어적 assert, 통상 dead branch. 내부 리팩토링 회귀 탐지용.)
          2. inf: close=0 분모로 인한 ZeroDivision fallback이 inf로 남은 경우.
          3. 극단값: |label| > 1.0 (1분봉 5분 수익률 ≥ 100%는 비현실).
             Mock 데이터 또는 corrupt 입력 탐지.
        """
        for ticker, group in panel.groupby(level="ticker"):
            label_vals = group[self._target_col].to_numpy()
            if label_vals.size == 0:
                continue
            # (1) NaN 방어
            if np.isnan(label_vals).any():
                raise LabelLeakageError(
                    f"{ticker}: label {self._target_col}에 NaN 존재. "
                    "dropna 경로 회귀 의심. build_training_frame 재확인."
                )
            # (2) inf
            if np.isinf(label_vals).any():
                raise LabelLeakageError(
                    f"{ticker}: label inf 감지. close=0 분모 처리 확인."
                )
            # (3) 극단값 (label 수익률 |r| > 1.0 = 100%)
            if np.any(np.abs(label_vals) > 1.0):
                extreme_count = int(np.sum(np.abs(label_vals) > 1.0))
                raise LabelLeakageError(
                    f"{ticker}: label 극단값 {extreme_count}건 감지 (|r| > 100%). "
                    "close 데이터 corruption 가능성. PIT replay 재검증 필요."
                )


# ====================================================================== #
# 날짜 파싱 helper
# ====================================================================== #


def _parse_yyyymmdd(s: str) -> date:
    return datetime.strptime(s, "%Y%m%d").date()


def _extract_file_date(file_path: Path) -> date | None:
    """bars_1m_YYYYMMDD.{parquet,jsonl} → date."""
    stem = file_path.stem
    if not stem.startswith("bars_1m_"):
        return None
    yyyymmdd = stem[len("bars_1m_"):]
    if len(yyyymmdd) != 8 or not yyyymmdd.isdigit():
        return None
    try:
        return _parse_yyyymmdd(yyyymmdd)
    except ValueError:
        return None
