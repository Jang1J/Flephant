"""S1-0 Batch B DatasetBuilder. backfill parquet → LightGBM 학습용 DataFrame.

파이프라인:
  artifacts/data/{ticker}/bars_1m_*.parquet
    → load_ticker_bars(raw OHLCV time series)
    → compute_rolling_features(multi-scale features per ticker)
    → generate_labels(PIT-safe forward return)
    → cross_sectional_rank(매 ts_close 내 20종목 pct rank → relevance grade)
    → join_dual_source_features(S4-2: Dual-Source 5피처 날짜 기준 join)
    → join_exogenous_features(C3: US overnight / investor flow / macro defaults)
    → 최종 panel DataFrame (MultiIndex ticker × ts_close)

불변 원칙 준수:
  - 모든 수치는 risk_config.yaml에서 로드 (하드코딩 금지)
  - PIT-Safety: label_5m_ret은 t+horizon close 참조 → training 전용.
    drop_last_n_bars로 마지막 horizon bar 제거.
  - 종목코드: pad_ticker() 6자리 zero-padded

S4-2 Dual-Source 확장:
  - dual_source.enabled_for_lgbm=true 시 5피처 join (날짜 기준 left join).
  - 결측 시 per-feature neutral 기본값 사용
    (community_noise_multiplier=1.0, 나머지=0.0).
  - 5피처명: news_score_t, comm_score_t_1, comm_score_t_2,
             news_comm_divergence, community_noise_multiplier.

C3 외생 피처 확장:
  - us_overnight / investor_flow / macro manifest 컬럼을 panel에 포함.
  - historical connector artifact가 아직 없으면 risk_config neutral_defaults 사용.

Batch C lgbm_trainer가 이 DataFrame을 소비.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from src.data.dual_source_runner import load_latest_scores
from src.data.exogenous_feature_store import (
    DEFAULT_EXOGENOUS_ARTIFACT_DIR,
    is_non_neutral,
    load_exogenous_scores,
)
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.safe_cast import safe_bool
from src.utils.ticker_utils import pad_ticker

# S4-2: Dual-Source 5피처 컬럼명 (SSOT). risk_config.yaml dual_source_feature_cols와 동기화.
DUAL_SOURCE_FEATURES: tuple[str, ...] = (
    "news_score_t",
    "comm_score_t_1",
    "comm_score_t_2",
    "news_comm_divergence",
    "community_noise_multiplier",
)

DUAL_SOURCE_DEFAULTS: dict[str, float] = {
    "news_score_t": 0.0,
    "comm_score_t_1": 0.0,
    "comm_score_t_2": 0.0,
    "news_comm_divergence": 0.0,
    "community_noise_multiplier": 1.0,
}

EXOGENOUS_FEATURES: tuple[str, ...] = (
    "us_sp500_change",
    "us_nasdaq_change",
    "us_vix",
    "us_soxx_change",
    "foreign_net_buy",
    "institutional_net_buy",
    "retail_net_buy",
    "interest_rate",
    "usd_krw",
)

logger = get_logger("dataset_builder")

_KST = ZoneInfo("Asia/Seoul")
_ALLOWED_DUAL_SOURCE_INPUT_MODES = {"real", "archived_raw_events"}

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
        allow_synthetic_fallback: bool = False,
        synthetic_seed: int | None = None,
    ) -> None:
        self._artifacts_dir = artifacts_dir or _ARTIFACTS_ROOT
        self._allow_synthetic_fallback = safe_bool(
            allow_synthetic_fallback,
            default=False,
        )
        self._synthetic_seed = int(synthetic_seed or 0)

        # risk_config.yaml 로드 (불변 원칙 5)
        self._cfg_label: dict = config_load("risk_config.yaml", "label")
        self._cfg_preprocessor: dict = config_load("risk_config.yaml", "preprocessor")
        self._cfg_lgbm: dict = config_load("risk_config.yaml", "lightgbm")
        self._cfg_mock: dict = (config_load("risk_config.yaml", "connector_mock") or {}).get("kis", {})
        self._cfg_wf: dict = config_load("risk_config.yaml", "walk_forward") or {}
        self._cfg_nightly: dict = config_load("risk_config.yaml", "nightly_retrainer") or {}
        self._cfg_cost_model: dict = config_load("risk_config.yaml", "execution_cost_model") or {}
        self._cfg_service_policy: dict = config_load("risk_config.yaml", "service_policy_replay") or {}
        self._cfg_cost_aware: dict = config_load("risk_config.yaml", "cost_aware_retraining") or {}

        self._horizon: int = int(self._cfg_label["horizon_bars"])
        self._aux_label_horizons: list[int] = self._load_aux_label_horizons()
        self._target_col: str = str(self._cfg_label["target_col"])
        self._rank_col: str = str(self._cfg_label["rank_col"])
        self._leakage_guard: bool = safe_bool(
            self._cfg_label["leakage_guard"],
            default=True,
        )
        self._drop_last_n: int = int(self._cfg_label["drop_last_n_bars"])
        self._label_generation_version: str = str(
            self._cfg_label.get("generation_version", "")
        )
        self._label_session_scope: str = str(
            self._cfg_label.get("session_scope", "")
        )
        cost_components = self._cfg_cost_model.get("components") or {}
        self._label_total_cost_bps: float = float(
            cost_components.get("commission_bps", 0.0)
        ) + float(cost_components.get("slippage_bps", 0.0))
        self._label_min_expected_net_bps: float = float(
            self._cfg_service_policy.get("min_expected_net_alpha_bps", 0.0)
        )

        self._multi_scale_windows: list[int] = list(
            self._cfg_preprocessor["multi_scale_windows"]
        )
        self._mad_constant: float = float(self._cfg_preprocessor["mad_constant"])
        self._outlier_cap_z: float = float(self._cfg_preprocessor["outlier_cap_z"])

        self._n_relevance_grades: int = int(self._cfg_lgbm["n_relevance_grades"])

        # S4-2: Dual-Source 피처 주입 설정 (risk_config.yaml dual_source 섹션)
        _cfg_ds: dict = config_load("risk_config.yaml", "dual_source") or {}
        self._ds_enabled_for_lgbm: bool = safe_bool(
            _cfg_ds.get("enabled_for_lgbm", False),
            default=False,
        )
        self._ds_feature_cols: list[str] = list(DUAL_SOURCE_FEATURES)
        _cfg_exog: dict = config_load("risk_config.yaml", "exogenous_features") or {}
        self._exog_enabled_for_lgbm: bool = safe_bool(
            _cfg_exog.get("enabled_for_lgbm", False),
            default=False,
        )
        self._exog_feature_cols: list[str] = list(
            self._cfg_preprocessor.get("exogenous_feature_cols", EXOGENOUS_FEATURES)
        )
        self._exog_defaults: dict[str, float] = {
            col: float((_cfg_exog.get("neutral_defaults") or {}).get(col, 0.0))
            for col in self._exog_feature_cols
        }
        self._neutral_feature_cols: list[str] = []

        logger.info(
            "[dataset_builder] 초기화 완료. horizon=%d bars, "
            "target_col=%s, windows=%s, grades=%d, ds_enabled_for_lgbm=%s, "
            "exog_enabled_for_lgbm=%s",
            self._horizon, self._target_col,
            self._multi_scale_windows, self._n_relevance_grades,
            self._ds_enabled_for_lgbm,
            self._exog_enabled_for_lgbm,
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
    def label_generation_version(self) -> str:
        """Label 생성 알고리즘 버전. yaml label.generation_version 값."""
        return self._label_generation_version

    @property
    def label_session_scope(self) -> str:
        """Label session scope. yaml label.session_scope 값."""
        return self._label_session_scope

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
        synthetic_tickers: list[str] = []
        loaded_tickers: list[str] = []
        missing_tickers: list[str] = []

        for ticker in padded:
            raw = self._load_ticker_bars(ticker, start_date, end_date)
            if raw is None or raw.empty:
                if not self._allow_synthetic_fallback:
                    logger.warning(
                        "[dataset_builder] %s: 데이터 없음, skip", ticker
                    )
                    missing_tickers.append(ticker)
                    continue
                logger.warning(
                    "[dataset_builder] %s: 데이터 없음, synthetic fallback 생성", ticker
                )
                raw = self._make_synthetic_bars(ticker, start_date, end_date)
                synthetic_tickers.append(ticker)

            with_feats = self._compute_rolling_features(raw)
            with_label = self._generate_labels(with_feats)
            if with_label.empty:
                missing_tickers.append(ticker)
                logger.warning(
                    "[dataset_builder] %s: label 생성 후 데이터 없음, skip", ticker
                )
                continue
            loaded_tickers.append(ticker)
            per_ticker_frames.append(with_label)

        if not per_ticker_frames:
            raise DatasetBuildError(
                f"모든 ticker({len(padded)}개) 데이터 로드 실패. "
                f"artifacts_dir={self._artifacts_dir} 확인."
            )

        panel_attrs = {
            "data_source": "synthetic_fallback" if synthetic_tickers else "artifact_bars",
            "requested_tickers": list(padded),
            "loaded_tickers": list(loaded_tickers),
            "missing_tickers": sorted(set(missing_tickers)),
            "synthetic_tickers": list(synthetic_tickers),
            "synthetic_fallback": bool(synthetic_tickers),
        }
        panel = pd.concat(per_ticker_frames, axis=0)
        panel.attrs.update(panel_attrs)
        panel = self._cross_sectional_rank(panel)
        panel.attrs.update(panel_attrs)

        # S4-2: Dual-Source 5피처 join (enabled_for_lgbm=true 시)
        if self._ds_enabled_for_lgbm:
            panel = self._join_dual_source_features(panel, start_date, end_date)
            panel.attrs.update(panel_attrs)

        if self._exog_enabled_for_lgbm:
            panel = self._join_exogenous_features(panel)
            panel.attrs.update(panel_attrs)

        if self._neutral_feature_cols:
            panel = self._join_neutral_feature_columns(panel)
            panel.attrs.update(panel_attrs)

        # label/rank NaN 행 drop
        before = len(panel)
        panel = panel.dropna(subset=[self._target_col, self._rank_col, "relevance"])
        panel.attrs.update(panel_attrs)
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

    def add_neutral_feature_columns(self, columns: list[str]) -> None:
        """외부 재학습 오케스트레이터가 요구하는 후보 피처를 neutral 0.0으로 추가."""
        for col in columns:
            if col not in self._neutral_feature_cols:
                self._neutral_feature_cols.append(str(col))

    def relabel_panel_for_target(self, panel, target_col: str):
        """기존 panel을 다른 label 컬럼 기준으로 재-rank한다.

        Cost-aware 실험용. 기본 학습 타깃(label_5m_ret)은 건드리지 않고,
        보조 라벨(label_session_close_net_ret 등)을 대상으로 cs_rank/relevance만
        다시 계산한다.
        """
        target = str(target_col)
        if target not in panel.columns:
            raise KeyError(f"target_col='{target}' panel에 없음")

        frame = panel.reset_index() if "ticker" in (panel.index.names or []) else panel.copy()
        frame = frame.dropna(subset=[target]).copy()
        frame[self._rank_col] = frame.groupby("ts_close")[target].rank(
            method="average",
            pct=True,
        )
        frame["relevance"] = self._to_relevance(frame[self._rank_col], self._n_relevance_grades)

        group_sizes = frame.groupby("ts_close")["close"].transform("count")
        frame = frame[group_sizes >= self._n_relevance_grades].copy()
        return frame.set_index(["ticker", "ts_close"]).sort_index()

    def _join_neutral_feature_columns(self, panel):
        panel = panel.copy()
        for col in self._neutral_feature_cols:
            panel[col] = 0.0
        logger.info(
            "[dataset_builder] neutral feature join 완료: cols=%d",
            len(self._neutral_feature_cols),
        )
        return panel

    @staticmethod
    def _parse_artifact_datetime(value: object) -> datetime | None:
        if value in (None, ""):
            return None
        try:
            dt = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=_KST)
        return dt.astimezone(_KST)

    @staticmethod
    def _market_open_ts(date_key: str) -> datetime:
        clean = str(date_key).replace("-", "")[:8]
        parsed = datetime.strptime(clean, "%Y%m%d")
        return parsed.replace(hour=9, minute=0, second=0, microsecond=0, tzinfo=_KST)

    def _dual_source_artifact_blockers(
        self,
        date_key: str,
        scores_list: list[dict],
    ) -> list[str]:
        blockers: set[str] = set()
        market_open = self._market_open_ts(date_key)
        for item in scores_list:
            source_stats = item.get("source_stats")
            if not isinstance(source_stats, dict) or not source_stats:
                blockers.add("dual_source_provenance_missing")
            else:
                input_mode = str(source_stats.get("input_mode", "")).strip().lower()
                if input_mode not in _ALLOWED_DUAL_SOURCE_INPUT_MODES:
                    blockers.add("dual_source_non_real_input_mode")
                if safe_bool(source_stats.get("neutral_rehearsal_file"), default=False):
                    blockers.add("dual_source_neutral_rehearsal_artifact")
            snapshot_dt = self._parse_artifact_datetime(
                item.get("snapshot_ts") or item.get("asof")
            )
            if snapshot_dt is not None and snapshot_dt > market_open:
                blockers.add("dual_source_snapshot_after_market_open")
        return sorted(blockers)

    def _exogenous_artifact_blockers(self, date_key: str, stats: dict[str, Any]) -> list[str]:
        blockers: set[str] = set()
        source_stats = stats.get("source_stats")
        if not isinstance(source_stats, dict) or not source_stats:
            blockers.add("exogenous_provenance_missing")
        else:
            input_mode = str(source_stats.get("input_mode", "")).strip().lower()
            if input_mode and input_mode != "real":
                blockers.add("exogenous_non_real_input_mode")
            if safe_bool(source_stats.get("neutral_rehearsal_file"), default=False):
                blockers.add("exogenous_neutral_rehearsal_artifact")
            provider_availability = source_stats.get("provider_availability")
            if isinstance(provider_availability, dict) and any(
                not bool(value) for value in provider_availability.values()
            ):
                blockers.add("exogenous_provider_unavailable")
            us_source = source_stats.get("us_market_source")
            if us_source and str(us_source) != "yfinance":
                blockers.add("exogenous_us_market_source_not_yfinance")
        snapshot_dt = self._parse_artifact_datetime(stats.get("snapshot_ts"))
        if snapshot_dt is not None and snapshot_dt > self._market_open_ts(date_key):
            blockers.add("exogenous_snapshot_after_market_open")
        return sorted(blockers)

    def _join_exogenous_features(self, panel):
        """C3 외생 feature_manifest 컬럼을 panel에 추가.

        daily exogenous artifact(new/artifacts/exogenous/YYYYMMDD.json)가 있으면
        날짜/ticker 기준으로 join한다. 파일 또는 ticker별 값이 없으면
        risk_config.yaml exogenous_features.neutral_defaults로 채운다.
        """
        panel = panel.copy()
        for col in self._exog_feature_cols:
            panel[col] = float(self._exog_defaults.get(col, 0.0))

        if panel.empty:
            panel.attrs["exogenous_join_stats"] = {
                "artifact_dir": str(DEFAULT_EXOGENOUS_ARTIFACT_DIR),
                "dates_found": 0,
                "dates_missing": 0,
                "rows_total": 0,
                "rows_non_neutral": 0,
                "non_neutral_rate": 0.0,
            }
            return panel

        panel_for_join = panel.drop(
            columns=[
                col for col in ("ticker", "ts_close")
                if col in panel.columns and col in panel.index.names
            ],
            errors="ignore",
        )
        panel_flat = panel_for_join.reset_index()
        panel_flat["date_str"] = panel_flat["ts_close"].dt.strftime("%Y%m%d")
        date_keys = sorted(panel_flat["date_str"].unique().tolist())
        date_score_maps: dict[str, dict[str, dict[str, float]]] = {}
        dates_found = 0
        dates_missing = 0

        for date_key in date_keys:
            score_map, stats = load_exogenous_scores(
                date_key,
                feature_cols=self._exog_feature_cols,
                defaults=self._exog_defaults,
                artifact_dir=DEFAULT_EXOGENOUS_ARTIFACT_DIR,
            )
            if stats["status"] == "found":
                blockers = self._exogenous_artifact_blockers(date_key, stats)
                if blockers:
                    raise DatasetBuildError(
                        "unsafe exogenous artifact "
                        f"date={date_key} blockers={','.join(blockers)}"
                    )
                dates_found += 1
                date_score_maps[date_key] = score_map
            else:
                dates_missing += 1

        rows_non_neutral = 0
        if date_score_maps:
            for idx, row in panel_flat.iterrows():
                score_map = date_score_maps.get(str(row["date_str"]), {})
                global_values = score_map.get("*", {})
                ticker_values = score_map.get(str(row["ticker"]).zfill(6), {})
                applied = {
                    col: float(
                        ticker_values.get(
                            col,
                            global_values.get(col, self._exog_defaults.get(col, 0.0)),
                        )
                    )
                    for col in self._exog_feature_cols
                }
                for col, value in applied.items():
                    panel_flat.at[idx, col] = value
                if is_non_neutral(applied, self._exog_defaults):
                    rows_non_neutral += 1

            panel = (
                panel_flat.drop(columns=["date_str"])
                .set_index(["ticker", "ts_close"])
                .sort_index()
            )
        else:
            rows_non_neutral = 0

        rows_total = int(len(panel))
        panel.attrs["exogenous_join_stats"] = {
            "artifact_dir": str(DEFAULT_EXOGENOUS_ARTIFACT_DIR),
            "dates_found": dates_found,
            "dates_missing": dates_missing,
            "rows_total": rows_total,
            "rows_non_neutral": rows_non_neutral,
            "non_neutral_rate": rows_non_neutral / max(rows_total, 1),
        }
        logger.info(
            "[dataset_builder] Exogenous feature join 완료: cols=%d dates_found=%d "
            "dates_missing=%d non_neutral_rows=%d/%d",
            len(self._exog_feature_cols),
            dates_found,
            dates_missing,
            rows_non_neutral,
            rows_total,
        )
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

    def _make_synthetic_bars(self, ticker: str, start_date: str, end_date: str):
        """KIS/backfill blocker 환경에서 학습용 mock 1분봉 생성."""
        pd = _import_pandas()
        start = _parse_yyyymmdd(start_date)
        end = _parse_yyyymmdd(end_date)
        trading_minutes = int(self._cfg_wf.get("trading_minutes_per_day", 390))
        base_price = float(self._cfg_mock.get("base_price", 50000))
        price_modulo = int(self._cfg_mock.get("price_modulo", 100000))
        volume_min = int(self._cfg_mock.get("volume_min", 1000))
        volume_max = int(self._cfg_mock.get("volume_max", 100000))
        change_min = float(self._cfg_mock.get("change_min", 0))
        change_max = float(self._cfg_mock.get("change_max", 100))
        drift_scale = float(self._cfg_nightly.get("synthetic_drift_scale", 0.00005))
        return_noise_std = float(
            self._cfg_nightly.get("synthetic_return_noise_std", 0.0015)
        )
        min_price = float(self._cfg_nightly.get("synthetic_min_price", 1000.0))
        session_start_raw = str(self._cfg_nightly.get("synthetic_session_start", "09:00"))
        try:
            session_hour, session_minute = [int(part) for part in session_start_raw.split(":", 1)]
        except ValueError as e:
            raise DatasetBuildError(
                "nightly_retrainer.synthetic_session_start 형식 오류: "
                f"{session_start_raw!r}, expected HH:MM"
            ) from e

        seed = self._synthetic_seed + sum(ord(ch) for ch in ticker)
        rng = np.random.default_rng(seed)
        ticker_offset = sum(ord(ch) for ch in ticker) % max(price_modulo, 1)
        price = base_price + float(ticker_offset)
        records: list[dict[str, object]] = []

        current = start
        while current <= end:
            if current.weekday() >= 5:
                current += timedelta(days=1)
                continue
            session_start = datetime(
                current.year,
                current.month,
                current.day,
                session_hour,
                session_minute,
                0,
                tzinfo=_KST,
            )
            for minute in range(trading_minutes):
                ts = session_start + timedelta(minutes=minute)
                drift = (ticker_offset / max(price_modulo, 1) - 0.5) * drift_scale
                shock = float(rng.normal(0.0, return_noise_std))
                open_price = price
                close = max(min_price, open_price * (1.0 + drift + shock))
                spread = abs(float(rng.normal(change_min, max(change_max, 1.0))))
                high = max(open_price, close) + spread
                low = max(1.0, min(open_price, close) - spread)
                volume = int(rng.integers(volume_min, volume_max + 1))
                records.append({
                    "ticker": ticker,
                    "ts_close": ts,
                    "open": float(open_price),
                    "high": float(high),
                    "low": float(low),
                    "close": float(close),
                    "volume": float(volume),
                })
                price = close
            current += timedelta(days=1)

        return pd.DataFrame(records)

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
        """거래일 내부에서만 close_{t+horizon} / close_t - 1 label 생성.

        ticker × KST 거래일 단위로 마지막 drop_last_n_bars를 제거한다.
        이렇게 해야 하루 마지막 bar가 다음 거래일 첫 bar를 label로 참조하지 않는다.
        """
        pd = _import_pandas()

        missing_cols = {"close", "ts_close"} - set(df.columns)
        if missing_cols:
            raise DatasetBuildError(
                "label 생성 필수 컬럼 누락: "
                f"{sorted(missing_cols)}"
            )

        out = df.copy()
        out["ts_close"] = self._normalize_ts_close_kst(out["ts_close"], pd)
        sort_cols = ["ts_close"]
        group_cols = ["_label_session"]
        if "ticker" in out.columns:
            sort_cols = ["ticker", "ts_close"]
            group_cols = ["ticker", "_label_session"]
        out = out.sort_values(sort_cols).reset_index(drop=True)

        out["_label_session"] = out["ts_close"].dt.date

        close_values = out["close"].to_numpy(dtype=float)
        grouped_positions = out.groupby(group_cols, sort=False).indices

        def forward_labels_for_horizon(horizon: int) -> np.ndarray:
            labels = np.full(len(out), np.nan, dtype=float)
            drop_count = max(horizon, self._drop_last_n)
            for positions in grouped_positions.values():
                positions_arr = np.asarray(positions, dtype=np.int64)
                valid_count = max(0, len(positions_arr) - drop_count)
                if valid_count <= 0:
                    continue
                closes = close_values[positions_arr]
                now = closes[:valid_count]
                future = closes[horizon:horizon + valid_count]
                with np.errstate(divide="ignore", invalid="ignore"):
                    labels[positions_arr[:valid_count]] = np.where(
                        now > 1e-8,
                        future / now - 1.0,
                        np.nan,
                    )
            return labels

        h = self._horizon
        labels = forward_labels_for_horizon(h)
        session_close_labels = np.full(len(out), np.nan, dtype=float)
        drop_count = max(h, self._drop_last_n)
        for positions in grouped_positions.values():
            positions = np.asarray(positions, dtype=np.int64)
            valid_count = max(0, len(positions) - drop_count)
            if valid_count <= 0:
                continue
            closes = close_values[positions]
            now = closes[:valid_count]
            session_close = float(closes[-1])
            with np.errstate(divide="ignore", invalid="ignore"):
                session_close_labels[positions[:valid_count]] = np.where(
                    now > 1e-8,
                    session_close / now - 1.0,
                    np.nan,
                )

        out = out.drop(columns=["_label_session"])
        out[self._target_col] = labels
        active_net_col = f"label_{h}m_net_bps"
        out[active_net_col] = out[self._target_col] * 10_000.0 - self._label_total_cost_bps
        out[f"label_{h}m_net_ret"] = out[active_net_col] / 10_000.0
        out[f"label_{h}m_tradeable"] = (
            out[active_net_col] >= self._label_min_expected_net_bps
        ).astype("Int64")
        for aux_horizon in self._aux_label_horizons:
            if aux_horizon == h:
                continue
            aux_label_col = f"label_{aux_horizon}m_ret"
            aux_net_col = f"label_{aux_horizon}m_net_bps"
            out[aux_label_col] = forward_labels_for_horizon(aux_horizon)
            out[aux_net_col] = out[aux_label_col] * 10_000.0 - self._label_total_cost_bps
            out[f"label_{aux_horizon}m_net_ret"] = out[aux_net_col] / 10_000.0
            out[f"label_{aux_horizon}m_tradeable"] = (
                out[aux_net_col] >= self._label_min_expected_net_bps
            ).astype("Int64")
        out["label_session_close_ret"] = session_close_labels
        out["label_session_close_net_bps"] = (
            out["label_session_close_ret"] * 10_000.0 - self._label_total_cost_bps
        )
        out["label_session_close_net_ret"] = out["label_session_close_net_bps"] / 10_000.0
        out["label_session_close_tradeable"] = (
            out["label_session_close_net_bps"] >= self._label_min_expected_net_bps
        ).astype("Int64")
        out = out.dropna(subset=[self._target_col]).copy()

        return out

    def _load_aux_label_horizons(self) -> list[int]:
        """Cost-aware integer horizon candidates whose labels should exist in panel."""
        horizons: list[int] = []
        for raw in self._cfg_cost_aware.get("horizon_candidates", []) or []:
            try:
                horizon = int(raw)
            except (TypeError, ValueError):
                continue
            if horizon > 0 and horizon not in horizons:
                horizons.append(horizon)
        try:
            min_holding_horizon = int(self._cfg_service_policy.get("min_holding_bars", 0) or 0)
        except (TypeError, ValueError):
            min_holding_horizon = 0
        if min_holding_horizon > 0 and min_holding_horizon not in horizons:
            horizons.append(min_holding_horizon)
        if self._horizon not in horizons:
            horizons.insert(0, self._horizon)
        return horizons

    @staticmethod
    def _normalize_ts_close_kst(ts_close, pd):
        """ts_close를 KST-aware pandas Series로 정규화."""
        ts = pd.to_datetime(ts_close)
        if ts.dt.tz is None:
            return ts.dt.tz_localize(_KST)
        return ts.dt.tz_convert(_KST)

    # ================================================================== #
    # Step 4: cross-sectional rank + relevance grade
    # ================================================================== #

    def _cross_sectional_rank(self, panel):
        """매 ts_close 그룹 내 label_5m_ret → pct rank (0~1) → relevance (0 ~ n-1).

        그룹 크기 < n_relevance_grades 면 해당 ts_close 그룹 drop.
        """
        _import_pandas()

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
        - 결측 시 per-feature neutral 기본값
          (community_noise_multiplier=1.0, 나머지=0.0).

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
                blockers = self._dual_source_artifact_blockers(date_str, scores_list)
                if blockers:
                    raise DatasetBuildError(
                        "unsafe dual-source artifact "
                        f"date={date_str} blockers={','.join(blockers)}"
                    )
                ticker_map: dict[str, dict[str, float]] = {}
                for item in scores_list:
                    t = str(item.get("ticker", "")).zfill(6)
                    if not t or t == "000000":
                        continue
                    ticker_map[t] = {
                        feat: float(item.get(feat, DUAL_SOURCE_DEFAULTS.get(feat, 0.0)))
                        for feat in self._ds_feature_cols
                    }
                date_ticker_scores[date_str] = ticker_map
            current = current + timedelta(days=1)

        if not date_ticker_scores:
            logger.warning(
                "[dataset_builder] Dual-Source 점수 파일 없음 (%s~%s). "
                "5피처 neutral 기본값 사용.",
                start_date, end_date,
            )

        # panel에 5피처 컬럼 추가 (per-feature neutral defaults).
        panel = panel.copy()
        for feat in self._ds_feature_cols:
            panel[feat] = float(DUAL_SOURCE_DEFAULTS.get(feat, 0.0))

        if not date_ticker_scores:
            panel.attrs["dual_source_join_stats"] = {
                "dates_found": 0,
                "dates_missing": (end - start).days + 1,
                "rows_total": int(len(panel)),
                "rows_non_neutral": 0,
                "non_neutral_rate": 0.0,
            }
            logger.info("[dataset_builder] Dual-Source join 완료: neutral 기본값 (파일 없음)")
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
                        "_dual_source_matched": True,
                        **{
                            feat: float(feats.get(feat, DUAL_SOURCE_DEFAULTS.get(feat, 0.0)))
                            for feat in self._ds_feature_cols
                        },
                    }
                )

        if not ds_records:
            panel.attrs["dual_source_join_stats"] = {
                "dates_found": len(date_ticker_scores),
                "dates_missing": max(0, (end - start).days + 1 - len(date_ticker_scores)),
                "rows_total": int(len(panel)),
                "rows_join_hit": 0,
                "rows_join_miss": int(len(panel)),
                "rows_non_neutral": 0,
                "non_neutral_rate": 0.0,
            }
            logger.info("[dataset_builder] Dual-Source join 완료: neutral 기본값 (레코드 없음)")
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

        matched_mask = merged["_dual_source_matched"].astype("boolean").fillna(False)

        # 결측 → per-feature neutral defaults 채우기.
        for feat in self._ds_feature_cols:
            merged[feat] = merged[feat].fillna(float(DUAL_SOURCE_DEFAULTS.get(feat, 0.0)))

        # join 통계 (hit = score file row matched, non-neutral = neutral default와 다름)
        join_hit = int(matched_mask.sum())
        join_miss = len(merged) - join_hit

        neutral_cmp = (
            ((merged["news_score_t"].astype(float) - DUAL_SOURCE_DEFAULTS["news_score_t"]).abs() > 1e-12)
            | ((merged["comm_score_t_1"].astype(float) - DUAL_SOURCE_DEFAULTS["comm_score_t_1"]).abs() > 1e-12)
            | ((merged["comm_score_t_2"].astype(float) - DUAL_SOURCE_DEFAULTS["comm_score_t_2"]).abs() > 1e-12)
            | ((merged["news_comm_divergence"].astype(float) - DUAL_SOURCE_DEFAULTS["news_comm_divergence"]).abs() > 1e-12)
            | ((merged["community_noise_multiplier"].astype(float) - DUAL_SOURCE_DEFAULTS["community_noise_multiplier"]).abs() > 1e-12)
        )
        non_neutral_rows = int(neutral_cmp.sum())

        # date_str 컬럼 제거 후 MultiIndex 복원
        merged = merged.drop(columns=["date_str", "_dual_source_matched"])
        panel = merged.set_index(["ticker", "ts_close"]).sort_index()

        total = join_hit + join_miss
        panel.attrs["dual_source_join_stats"] = {
            "dates_found": len(date_ticker_scores),
            "dates_missing": max(0, (end - start).days + 1 - len(date_ticker_scores)),
            "rows_total": int(len(panel)),
            "rows_with_any_non_zero": join_hit,
            "rows_non_neutral": non_neutral_rows,
            "non_neutral_rate": non_neutral_rows / max(len(panel), 1),
        }
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
