"""S3-6 LightGBM 야간 재학습 오케스트레이터.

S1-0 LGBMTrainer를 재사용하며 Alpha Factor Zoo의 active 팩터를 추가 feature로 포함.
sliding window retrain → v{n}.pkl → ModelRegistry 등록.

설계:
  1. FactorZoo에서 active 팩터 목록 로드 (최대 max_alpha_factors개)
  2. factor column 이름 생성 (alpha_factor_0, alpha_factor_1, ...)
  3. LGBMTrainer.feature_cols에 alpha factor 컬럼 추가
  4. LGBMTrainer.train() 호출 → 내부적으로 DatasetBuilder + WalkForwardSplitter + lgb.train 실행
  5. 결과를 v{n}.pkl로 ModelRegistry 등록

불변 원칙:
  1. 하드코딩 금지: 파라미터는 risk_config.yaml nightly_retrainer 섹션 경유
  2. PIT-Safety: DatasetBuilder/LGBMTrainer 내부 강제 적용 (미래 데이터 사용 금지)
  3. bare except 금지

Note:
  실 backfill 데이터 없으면 (S1-8 blocker) DatasetBuilder가 synthetic 데이터 사용.
  테스트도 synthetic 사용.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.mode_guard import mode_b_only

logger = get_logger("nightly_lgbm_retrainer")
_KST = ZoneInfo("Asia/Seoul")


class NightlyLGBMRetrainer:
    """LightGBM 야간 재학습 오케스트레이터.

    FactorZoo active 팩터를 augmented feature로 포함해서 LGBMTrainer.train() 호출.
    결과를 v{n}.pkl로 ModelRegistry에 등록.
    bundle_id는 Mode B Scheduler가 발급한 값 사용.

    파라미터 출처: risk_config.yaml nightly_retrainer 섹션 (불변 원칙 5).
    """

    def __init__(self) -> None:
        cfg = config_load("risk_config.yaml", "nightly_retrainer") or {}
        self._tickers: list[str] = list(cfg.get("tickers", []))
        self._lookback_days: int = int(cfg.get("lookback_days", 30))
        self._max_alpha_factors: int = int(cfg.get("max_alpha_factors", 5))
        logger.info(
            "[nightly_lgbm_retrainer] 초기화. lookback_days=%d max_alpha_factors=%d",
            self._lookback_days,
            self._max_alpha_factors,
        )

    # ================================================================== #
    # Public API
    # ================================================================== #

    @mode_b_only
    def retrain(
        self,
        bundle_id: str | None = None,
        tickers: list[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """야간 재학습 실행. 새 버전 pkl 등록 후 결과 dict 반환.

        Args:
            bundle_id:   Mode B Scheduler가 발급한 bundle_id. 메타데이터 추적용.
            tickers:     재학습 대상 ticker 목록. None이면 yaml 기본값 사용.
            start_date:  학습 시작일 "YYYY-MM-DD". None이면 end_date - lookback_days.
            end_date:    학습 종료일 "YYYY-MM-DD". None이면 오늘(KST).

        Returns:
            {
                "version": "v2",
                "model_path": "artifacts/lgbm/v2.pkl",
                "metrics": {"ic": ..., "icir": ..., "rank_ic": ..., "arr": ...,
                            "ir": ..., "mdd": ..., "sr": ...},
                "alpha_factors_used": 3,
                "bundle_id": bundle_id,
                "n_folds": int,
            }

        Raises:
            RuntimeError: LGBMTrainer.train() 내부 오류. 호출자(scheduler)가 catch.
        """
        from src.models.lgbm_trainer import LGBMTrainer
        from src.models.registry import ModelRegistry

        # 1. 다음 버전 결정
        registry = ModelRegistry()
        next_version = self._next_version(registry)

        # 2. Alpha Factor Zoo에서 active 팩터 → feature column 이름 목록
        alpha_feature_cols = self._load_alpha_factor_columns()

        # 3. 날짜 범위 결정
        resolved_tickers = tickers if tickers is not None else self._tickers
        end_dt = end_date or datetime.now(_KST).strftime("%Y-%m-%d")
        start_dt = start_date or self._compute_start_date(end_dt)

        # 4. LGBMTrainer 구성 + alpha feature 추가
        trainer = LGBMTrainer()
        if alpha_feature_cols:
            trainer.feature_cols = trainer.feature_cols + alpha_feature_cols
            logger.info(
                "[nightly_lgbm_retrainer] alpha factor 피처 %d개 추가: %s",
                len(alpha_feature_cols),
                alpha_feature_cols,
            )

        logger.info(
            "[nightly_lgbm_retrainer] 재학습 시작. version=%s tickers=%d %s~%s",
            next_version,
            len(resolved_tickers),
            start_dt,
            end_dt,
        )

        # 5. 학습 실행 (DatasetBuilder + WalkForwardSplitter + lgb.train + registry.save 포함)
        result = trainer.train(
            tickers=resolved_tickers,
            start_date=start_dt,
            end_date=end_dt,
            version=next_version,
            bundle_id=bundle_id,
        )

        result["alpha_factors_used"] = len(alpha_feature_cols)
        result["bundle_id"] = bundle_id

        logger.info(
            "[nightly_lgbm_retrainer] 재학습 완료. version=%s model_path=%s metrics=%s",
            next_version,
            result.get("model_path", ""),
            result.get("metrics", {}),
        )
        return result

    # ================================================================== #
    # Internal helpers
    # ================================================================== #

    def _next_version(self, registry) -> str:
        """현재 최신 버전 다음 버전 결정. baseline → v2 → v3 ...

        registry가 비어있으면 "v2" (baseline이 v1 역할).
        "vN" 형식 중 최대 N+1 반환.
        파싱 불가 버전이 있어도 건너뜀.
        """
        try:
            versions_meta = registry.list_versions()
            if not versions_meta:
                return "v2"
            nums: list[int] = []
            for meta in versions_meta:
                v = meta.get("version", "") if isinstance(meta, dict) else str(meta)
                m = re.match(r"v(\d+)$", str(v))
                if m:
                    nums.append(int(m.group(1)))
            if not nums:
                return "v2"
            return f"v{max(nums) + 1}"
        except Exception as e:
            logger.warning(
                "[nightly_lgbm_retrainer] 버전 결정 실패: %s. v2 사용", e
            )
            return "v2"

    def _load_alpha_factor_columns(self) -> list[str]:
        """FactorZoo active 팩터 → feature column 이름 목록 반환.

        최대 max_alpha_factors개. FactorZoo 로드 실패 시 빈 리스트 반환 (재학습 계속).
        column 이름 규칙: alpha_factor_{i} (i=0, 1, 2, ...)
        """
        try:
            from src.mode_b.alpha_factor.factor_zoo import FactorZoo

            zoo = FactorZoo()
            active = zoo.list_by_status("active")
            cols: list[str] = []
            for i, _ in enumerate(active[: self._max_alpha_factors]):
                cols.append(f"alpha_factor_{i}")
            if cols:
                logger.info(
                    "[nightly_lgbm_retrainer] active 팩터 %d개 → 컬럼 %d개 생성",
                    len(active),
                    len(cols),
                )
            return cols
        except Exception as e:
            logger.warning(
                "[nightly_lgbm_retrainer] FactorZoo 로드 실패: %s. alpha factor 없이 재학습",
                e,
            )
            return []

    def _compute_start_date(self, end_date: str) -> str:
        """end_date에서 lookback_days 이전 날짜 계산.

        PIT-Safety: end_date는 호출 시점 이전 날짜여야 한다 (LGBMTrainer 내부 검증).
        """
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        start_dt = end_dt - timedelta(days=self._lookback_days)
        return start_dt.strftime("%Y-%m-%d")
