"""S1-0 Batch C LGBMTrainer. DatasetBuilder → Splitter → lgb.train → Registry 통합.

학습 전략:
  1. DatasetBuilder로 panel 생성 (raw parquet → 피처 + label + cs_rank + relevance)
  2. WalkForwardSplitter로 fold 분할 (purge/embargo 자동 적용)
  3. 각 fold에서 lgb.train → val 메트릭 수집
  4. 최종 모델: 마지막 fold의 train set로 학습한 booster
  5. 평균 메트릭 + 최종 booster → ModelRegistry.save()

S3-6 야간 재학습도 동일 LGBMTrainer 재사용 (version 파라미터만 다르게).

CLI:
  python -m src.models.lgbm_trainer --tickers 005930,000660 \
    --start 20260101 --end 20260419 --version baseline
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from typing import Any

import numpy as np

from src.data.dataset_builder import DatasetBuilder
from src.models.metrics import MetricsBundle
from src.models.ranking_loss import (
    get_lightgbm,
    build_lgbm_params,
    get_training_control,
    make_lgbm_dataset,
)
from src.models.registry import ModelRegistry
from src.models.splitter import WalkForwardSplitter
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.safe_cast import safe_bool

logger = get_logger("lgbm_trainer")


def _load_feature_cols() -> list[str]:
    """risk_config.yaml preprocessor.feature_cols에서 로드 (불변 원칙 5).

    S4-2: dual_source.enabled_for_lgbm=true 시 dual_source_feature_cols도 병합.
    DatasetBuilder._compute_rolling_features 출력 + _join_dual_source_features 출력과 1:1 대응.
    피처 추가/제거 시 yaml + dataset_builder 양쪽 동시 수정 필수.
    """
    cfg = config_load("risk_config.yaml", "preprocessor")
    base_cols: list[str] = list(cfg["feature_cols"])

    # S4-2: Dual-Source 5피처 병합 (enabled_for_lgbm 플래그)
    ds_cfg = config_load("risk_config.yaml", "dual_source") or {}
    if safe_bool(ds_cfg.get("enabled_for_lgbm", False), default=False):
        ds_cols: list[str] = list(cfg.get("dual_source_feature_cols", []))
        # 중복 방지: base에 없는 피처만 추가
        for col in ds_cols:
            if col not in base_cols:
                base_cols.append(col)

    exog_cfg = config_load("risk_config.yaml", "exogenous_features") or {}
    if safe_bool(exog_cfg.get("enabled_for_lgbm", False), default=False):
        exog_cols: list[str] = list(cfg.get("exogenous_feature_cols", []))
        for col in exog_cols:
            if col not in base_cols:
                base_cols.append(col)

    return base_cols


class LGBMTrainer:
    """S1-0 Batch C 학습 orchestrator.

    모든 파라미터는 risk_config.yaml에서 로드 (불변 원칙 5).
    """

    def __init__(
        self,
        dataset_builder: DatasetBuilder | None = None,
        splitter: WalkForwardSplitter | None = None,
        registry: ModelRegistry | None = None,
    ) -> None:
        self.builder = dataset_builder or DatasetBuilder()
        self.splitter = splitter or WalkForwardSplitter()
        self.registry = registry or ModelRegistry()

        self.feature_cols: list[str] = _load_feature_cols()
        # target_col / top_k_fraction은 yaml 경유 (불변 원칙 5).
        self.target_col: str = str(config_load("risk_config.yaml", "label")["target_col"])
        self.top_k_fraction: float = float(
            config_load("risk_config.yaml", "evaluation")["top_k_fraction"]
        )

    # ================================================================== #
    # Public
    # ================================================================== #

    def train(
        self,
        tickers: list[str],
        start_date: str,
        end_date: str,
        version: str = "baseline",
        bundle_id: str | None = None,
        is_latest: bool = True,
        target_col_override: str | None = None,
    ) -> dict[str, Any]:
        """전체 파이프라인 실행. 학습된 booster를 registry에 save.

        Returns:
          {
            "version": "baseline",
            "model_path": "artifacts/lgbm/baseline.pkl",
            "metrics": {...7 fields},
            "n_folds": int,
            "fold_metrics": [{...}, ...],
            "n_train_rows": int,
            "n_val_rows": int,
          }
        """
        resolved_bundle_id = str(bundle_id).strip() if bundle_id is not None else None
        resolved_bundle_id = resolved_bundle_id or None
        effective_is_latest = bool(is_latest)
        if resolved_bundle_id and effective_is_latest:
            logger.warning(
                "[lgbm_trainer] bundle_id=%s 후보는 deploy gate 전 active/latest로 저장하지 않음.",
                resolved_bundle_id,
            )
            effective_is_latest = False

        start_date_norm = self._normalize_yyyymmdd(start_date)
        end_date_norm = self._normalize_yyyymmdd(end_date)

        effective_target_col = str(target_col_override or self.target_col)

        # 1. Panel 생성
        logger.info(
            "[lgbm_trainer] panel 생성 시작: tickers=%d, %s~%s",
            len(tickers), start_date_norm, end_date_norm,
        )
        panel = self.builder.build_training_frame(tickers, start_date_norm, end_date_norm)
        if effective_target_col != self.target_col:
            panel = self.builder.relabel_panel_for_target(panel, effective_target_col)
            if panel.empty:
                raise RuntimeError(
                    "target_col_override 적용 후 panel empty: "
                    f"target_col={effective_target_col}"
                )
        logger.info("[lgbm_trainer] panel rows=%d", len(panel))
        data_source = dict(getattr(panel, "attrs", {}) or {})
        self._assert_requested_tickers_present(panel, tickers, data_source)
        self._assert_feature_manifest(panel)

        # 2. Fold 분할
        folds = list(self.splitter.split(panel))
        if not folds:
            raise RuntimeError(
                "walk-forward fold 0개. "
                "train+test+embargo 대비 데이터 부족. "
                f"panel rows={len(panel)}, splitter={self.splitter}"
            )
        logger.info("[lgbm_trainer] %d fold 생성", len(folds))

        # 3. fold별 학습
        # lightgbm lazy import: 데이터/manifest/fold guard를 먼저 통과한 뒤 실제 학습 직전에만 로드.
        lgb = get_lightgbm()
        params = build_lgbm_params()
        tc = get_training_control()
        fold_metrics: list[dict[str, float]] = []
        last_booster = None
        last_train_panel = None
        last_val_panel = None

        for fold_idx, (train_idx, val_idx) in enumerate(folds):
            train_panel = panel.iloc[train_idx]
            val_panel = panel.iloc[val_idx]

            # ts_close 그룹이 n_relevance_grades 이상인지 DatasetBuilder가 보장.
            train_panel = train_panel.sort_index(level="ts_close")
            val_panel = val_panel.sort_index(level="ts_close")

            booster, metrics = self._train_fold(
                train_panel,
                val_panel,
                params,
                tc,
                lgb,
                target_col=effective_target_col,
            )
            fold_metrics.append({"fold": fold_idx, **metrics})
            logger.info(
                "[lgbm_trainer] fold %d: IC=%.4f, RankIC=%.4f, SR=%.4f, MDD=%.4f",
                fold_idx, metrics["ic"], metrics["rank_ic"],
                metrics["sr"], metrics["mdd"],
            )
            last_booster = booster
            last_train_panel = train_panel
            last_val_panel = val_panel

        # 4. 평균 metrics 계산 (fold별 평균)
        avg_metrics = self._aggregate_fold_metrics(fold_metrics)
        metric_scope = {
            "scope": "trainer_validation_proxy",
            "deploy_quality": False,
            "reason": "Trainer fold metrics are diagnostic only; C12 real backtest is required before deploy.",
        }

        # 5. Registry 저장
        if last_booster is None:
            raise RuntimeError("학습 실패: last_booster None")

        preprocessor_cfg = config_load("risk_config.yaml", "preprocessor")
        feature_set = set(self.feature_cols)
        metadata = {
            "version": version,
            "bundle_id": resolved_bundle_id,
            "train_start": self._yyyymmdd_to_iso(start_date_norm),
            "train_end": self._yyyymmdd_to_iso(end_date_norm),
            "feature_cols": self.feature_cols,
            "feature_manifest": {
                "feature_cols": list(self.feature_cols),
                "panel_columns_checked": True,
                "requires_dual_source": bool(
                    feature_set
                    & set(preprocessor_cfg.get("dual_source_feature_cols", []))
                ),
                "requires_exogenous": bool(
                    feature_set
                    & set(preprocessor_cfg.get("exogenous_feature_cols", []))
                ),
            },
            "label_horizon_bars": self.builder.horizon_bars,
            "label_generation_version": self.builder.label_generation_version,
            "label_session_scope": self.builder.label_session_scope,
            "target_col": effective_target_col,
            "metrics": avg_metrics,
            "data_version": self._compute_data_version(tickers, start_date_norm, end_date_norm),
            "data_source": data_source.get("data_source", "artifact_bars"),
            "synthetic_fallback": safe_bool(data_source.get("synthetic_fallback", False), default=False),
            "requested_tickers": list(data_source.get("requested_tickers", tickers)),
            "loaded_tickers": list(data_source.get("loaded_tickers", [])),
            "missing_tickers": list(data_source.get("missing_tickers", [])),
            "synthetic_tickers": list(data_source.get("synthetic_tickers", [])),
            "n_folds": len(folds),
            "n_train_rows": int(len(last_train_panel)) if last_train_panel is not None else 0,
            "n_val_rows": int(len(last_val_panel)) if last_val_panel is not None else 0,
            "fold_metrics": fold_metrics,
            "n_tickers": len(tickers),
            "lgbm_params": params,
            "training_control": tc,
            "metric_scope": metric_scope,
        }
        pkl_path = self.registry.save(
            last_booster,
            metadata,
            is_latest=effective_is_latest,
        )

        logger.info(
            "[lgbm_trainer] 학습 완료. version=%s, pkl=%s, avg_IC=%.4f",
            version, pkl_path, avg_metrics["ic"],
        )

        return {
            "version": version,
            "model_path": str(pkl_path),
            "metrics": avg_metrics,
            "n_folds": len(folds),
            "fold_metrics": fold_metrics,
            "n_train_rows": int(len(last_train_panel)) if last_train_panel is not None else 0,
            "n_val_rows": int(len(last_val_panel)) if last_val_panel is not None else 0,
            "is_latest": effective_is_latest,
            "data_source": data_source.get("data_source", "artifact_bars"),
            "synthetic_fallback": safe_bool(data_source.get("synthetic_fallback", False), default=False),
            "missing_tickers": list(data_source.get("missing_tickers", [])),
            "label_generation_version": self.builder.label_generation_version,
            "label_session_scope": self.builder.label_session_scope,
            "target_col": effective_target_col,
            "metric_scope": metric_scope,
        }

    # ================================================================== #
    # Internal
    # ================================================================== #

    def _train_fold(
        self,
        train_panel,
        val_panel,
        params: dict[str, Any],
        tc: dict[str, int],
        lgb,
        target_col: str,
    ):
        """단일 fold 학습 + validation 메트릭 계산."""
        train_ds = make_lgbm_dataset(train_panel, feature_cols=self.feature_cols)
        val_ds = make_lgbm_dataset(val_panel, feature_cols=self.feature_cols)

        # lgb.train valid_sets는 train_ds 먼저, val_ds 두번째.
        booster = lgb.train(
            params,
            train_ds,
            num_boost_round=tc["n_estimators"],
            valid_sets=[train_ds, val_ds],
            valid_names=["train", "val"],
            callbacks=[
                lgb.early_stopping(stopping_rounds=tc["early_stopping_rounds"], verbose=False),
                lgb.log_evaluation(period=0),
            ],
        )

        # val 세트 예측 + metrics
        val_X = val_panel[self.feature_cols].to_numpy(dtype=float)
        val_pred = booster.predict(val_X)

        # 메트릭 계산용 그룹핑 (target_col / top_k_fraction 모두 yaml 경유)
        y_true_by_group, y_pred_by_group, daily_pnl = self._group_for_metrics(
            val_panel, val_pred,
            target_col=target_col,
            top_k_fraction=self.top_k_fraction,
        )

        bundle = MetricsBundle.compute(
            y_true_by_group=y_true_by_group,
            y_pred_by_group=y_pred_by_group,
            daily_pnl=daily_pnl,
        )
        return booster, bundle.to_dict()

    def _assert_feature_manifest(self, panel) -> None:
        """훈련 feature manifest와 실제 panel columns를 강제 일치시킨다."""
        panel_cols = set(panel.columns)
        missing = [col for col in self.feature_cols if col not in panel_cols]
        if missing:
            raise RuntimeError(
                "feature manifest mismatch: DatasetBuilder panel에 학습 feature가 없음. "
                f"missing={missing}"
            )

    @staticmethod
    def _assert_requested_tickers_present(
        panel,
        requested_tickers: list[str],
        data_source: dict[str, Any],
    ) -> None:
        """요청한 ticker 일부가 조용히 skip된 모델 학습을 차단한다."""
        requested = {str(ticker).zfill(6) for ticker in requested_tickers}
        present = {
            str(ticker).zfill(6)
            for ticker in panel.index.get_level_values("ticker").unique().tolist()
        }
        missing = sorted(requested - present)
        if missing:
            data_source["missing_tickers"] = sorted(
                set(data_source.get("missing_tickers", [])) | set(missing)
            )
        if data_source.get("synthetic_fallback"):
            return
        if data_source.get("missing_tickers"):
            raise RuntimeError(
                "missing requested ticker data: "
                f"{sorted(set(data_source['missing_tickers']))}. "
                "실데이터 학습은 요청 universe 전 종목 artifact가 필요하다."
            )

    @staticmethod
    def _normalize_yyyymmdd(value: str) -> str:
        """YYYYMMDD / YYYY-MM-DD 입력을 DatasetBuilder 표준 YYYYMMDD로 정규화."""
        raw = str(value)
        for fmt in ("%Y%m%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt).strftime("%Y%m%d")
            except ValueError:
                continue
        raise ValueError(f"날짜 형식 오류: {value!r} (expected YYYYMMDD or YYYY-MM-DD)")

    @staticmethod
    def _yyyymmdd_to_iso(value: str) -> str:
        """YYYYMMDD를 C17 metadata 표준 YYYY-MM-DD로 변환."""
        return datetime.strptime(str(value), "%Y%m%d").date().isoformat()

    @staticmethod
    def _group_for_metrics(
        val_panel,
        val_pred,
        target_col: str,
        top_k_fraction: float,
    ):
        """panel + prediction → (ts_close별 y_true dict, y_pred dict, daily_pnl array).

        daily_pnl: 단순 strategy simulation (S1-3 Portfolio Manager 완성 전 임시).
        매 ts_close에서 pred 상위 Top-K long 포지션 가정 → label 평균.
        k = max(1, int(len(group) * top_k_fraction)).

        Args:
            target_col: label 컬럼명 (yaml label.target_col에서 전달).
            top_k_fraction: Top-K 비율 (yaml evaluation.top_k_fraction에서 전달).
        """
        import pandas as pd_

        y_true_by_group: dict[Any, np.ndarray] = {}
        y_pred_by_group: dict[Any, np.ndarray] = {}

        # val_panel: MultiIndex (ticker, ts_close)
        labels = val_panel[target_col].to_numpy(dtype=float)
        ts_level = val_panel.index.get_level_values("ts_close")

        df = pd_.DataFrame(
            {
                "ts_close": ts_level,
                "label": labels,
                "pred": val_pred,
            }
        )

        daily_pnl_records: dict[Any, list[float]] = {}

        for ts, group in df.groupby("ts_close"):
            y_true_by_group[ts] = group["label"].to_numpy(dtype=float)
            y_pred_by_group[ts] = group["pred"].to_numpy(dtype=float)

            # Top-K long strategy (yaml top_k_fraction 기반)
            n = len(group)
            k = max(1, int(n * top_k_fraction))
            top_labels = group.nlargest(k, "pred")["label"]
            pnl_at_ts = float(top_labels.mean())
            day_key = ts.date() if hasattr(ts, "date") else ts
            daily_pnl_records.setdefault(day_key, []).append(pnl_at_ts)

        daily_pnl = np.asarray(
            [float(np.mean(v)) for v in daily_pnl_records.values()],
            dtype=float,
        )
        return y_true_by_group, y_pred_by_group, daily_pnl

    @staticmethod
    def _aggregate_fold_metrics(fold_metrics: list[dict[str, float]]) -> dict[str, float]:
        """fold별 메트릭 평균. (단순 산술 평균, fold 가중치 1.)"""
        keys = ("ic", "icir", "rank_ic", "arr", "ir", "mdd", "sr")
        out: dict[str, float] = {}
        for k in keys:
            vals = [float(m[k]) for m in fold_metrics if k in m]
            out[k] = float(np.mean(vals)) if vals else 0.0
        return out

    @staticmethod
    def _compute_data_version(
        tickers: list[str],
        start_date: str,
        end_date: str,
    ) -> str:
        """간단 data version. ticker count + date range 기반 hash-like."""
        return f"n{len(tickers)}_{start_date}_{end_date}"


# ====================================================================== #
# CLI entry
# ====================================================================== #


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="lgbm_trainer",
        description="S1-0 Batch C LightGBM baseline 학습",
    )
    p.add_argument(
        "--tickers",
        type=str,
        required=True,
        help="콤마 구분 ticker 목록 (예: 005930,000660,035420)",
    )
    p.add_argument(
        "--start",
        type=str,
        required=True,
        help="YYYYMMDD 학습 시작일",
    )
    p.add_argument(
        "--end",
        type=str,
        required=True,
        help="YYYYMMDD 학습 종료일",
    )
    p.add_argument(
        "--version",
        type=str,
        default="baseline",
        help="Registry 버전명 (기본 'baseline', 재학습 시 'v2', 'v3' 등)",
    )
    p.add_argument(
        "--bundle-id",
        type=str,
        default=None,
        help="C12 bundle_id (S3-6 재학습 시)",
    )
    p.add_argument(
        "--target-col-override",
        type=str,
        default=None,
        help="실험용 label 컬럼 override (예: label_session_close_net_ret)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    trainer = LGBMTrainer()
    try:
        result = trainer.train(
            tickers=tickers,
            start_date=args.start,
            end_date=args.end,
            version=args.version,
            bundle_id=args.bundle_id,
            target_col_override=args.target_col_override,
        )
    except Exception as e:
        logger.error("[lgbm_trainer] 학습 실패: %s", e)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
