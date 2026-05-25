#!/usr/bin/env python
"""LightGBM baseline vs Committee (AlphaGAT Stage II) ablation runner.

기존 `CommitteeModel`은 fit() 내부에서 tree_only_sr / committee_sr /
delta_sharpe를 산출하지만 production/job 코드에서 호출되는 곳이 없어 실험 결과가
없었다. 본 스크립트는 운영 LightGBM 모델(BUNDLE-20260521-POSTCLOSE)과 동일한
4개월 윈도우 / 30종목(active+pending) / 14피처(dual_source community 제외) panel을
빌드해 Committee를 1회 학습시키고 {tree_only_sr, committee_sr, delta_sharpe,
improved} 결과를 JSON으로 저장한다.

실행 (Mode B 시간창 18-22 KST 내 실행):
    python new/scripts/run_committee_ablation.py

기본값으로 실행하면 BUNDLE-20260521-POSTCLOSE paper baseline과 동일 조건으로 ablation.

Mode B 가드:
    CommitteeModel.fit()은 @mode_b_only 데코레이터로 ELEPHANT_MODE=mode_b와
    18-22 KST 시간창을 요구한다. 본 스크립트는 ELEPHANT_MODE를 set하고 시간창은
    그대로 따른다.

산출물:
    artifacts/committee/v{n}/                  — Committee 학습 산출 (committee.py 내부)
    artifacts/committee_ablation/*.json        — 본 스크립트 결과 리포트
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parents[2]
_NEW_SRC = _ROOT / "new"
if str(_NEW_SRC) not in sys.path:
    sys.path.insert(0, str(_NEW_SRC))

os.environ.setdefault("ELEPHANT_MODE", "mode_b")

from src.data.dataset_builder import DatasetBuilder  # noqa: E402
from src.models.committee import CommitteeModel  # noqa: E402
from src.models.lgbm_trainer import _load_feature_cols as _load_lgbm_feature_cols  # noqa: E402
from src.utils.config_loader import load as config_load  # noqa: E402
from src.utils.logger import get_logger  # noqa: E402
from src.utils.ticker_utils import pad_ticker  # noqa: E402

logger = get_logger("committee_ablation")
_KST = ZoneInfo("Asia/Seoul")

# dual_source 5피처 중 community 4개 제외 (운영 모델 BUNDLE-20260521-POSTCLOSE 설계).
# 즉 news_score_t 1개만 사용.
_COMMUNITY_FEATURES_EXCLUDED: frozenset[str] = frozenset({
    "comm_score_t_1",
    "comm_score_t_2",
    "news_comm_divergence",
    "community_noise_multiplier",
})


def _load_universe_tickers(max_tickers: int) -> list[str]:
    """universe_config.yaml에서 active + pending 종목 모두 로드 (하드코딩 금지).

    BUNDLE-20260521-POSTCLOSE는 30종목 (active 20 + pending 10) 학습.
    """
    cfg = config_load("universe_config.yaml") or {}
    tickers: list[str] = []
    allowed_statuses = {"active", "pending"}
    for sector in (cfg.get("sectors") or {}).values():
        for stock in sector.get("stocks", []):
            status = str(stock.get("status", "")).lower()
            if status in allowed_statuses and stock.get("ticker"):
                tickers.append(pad_ticker(str(stock["ticker"])))
    return tickers[:max_tickers]


def _default_max_tickers() -> int:
    """paper_auto_trading.max_tickers에서 기본 universe cap 로드."""
    cfg = config_load("risk_config.yaml", "paper_auto_trading") or {}
    if "max_tickers" not in cfg:
        raise RuntimeError("risk_config.yaml paper_auto_trading.max_tickers 설정 누락")
    return int(cfg["max_tickers"])


def _build_feature_cols() -> list[str]:
    """기존 SSOT에서 feature_cols 조합. 하드코딩 금지.

      - OHLCV: LGBMTrainer feature loader
      - Dual-Source: LGBMTrainer feature loader 결과에서 community 4개 제외
      - Exogenous: LGBMTrainer feature loader

    결과: 4 + 1 + 9 = 14 (BUNDLE-20260521-POSTCLOSE 운영 모델과 동일).
    """
    return [
        col for col in _load_lgbm_feature_cols(
            include_dual_source=True,
            include_exogenous=True,
        )
        if col not in _COMMUNITY_FEATURES_EXCLUDED
    ]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--start-date", default="20260116",
        help="train 시작일 YYYYMMDD (dataset_builder 표준)",
    )
    p.add_argument(
        "--end-date", default="20260515",
        help="train 종료일 YYYYMMDD",
    )
    p.add_argument(
        "--max-tickers", type=int, default=None,
        help="universe_config.yaml active+pending 종목 중 상위 N개 (default: risk_config paper_auto_trading.max_tickers)",
    )
    p.add_argument(
        "--tickers", nargs="*", default=None,
        help="ticker 직접 지정 (default: universe_config.yaml active+pending 상위 max-tickers)",
    )
    p.add_argument(
        "--label-col", default="relevance",
        help="Committee label_col (LGBM ranking + Sharpe 산출용)",
    )
    p.add_argument(
        "--output-dir", default="artifacts/committee_ablation",
        help="결과 JSON 저장 디렉토리",
    )
    p.add_argument(
        "--version", default=None,
        help="Committee artifact 버전명 (default: 자동 v{n})",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    max_tickers = int(args.max_tickers) if args.max_tickers is not None else _default_max_tickers()
    tickers = (
        list(args.tickers) if args.tickers
        else _load_universe_tickers(max_tickers)
    )
    feature_cols = _build_feature_cols()

    started_at = datetime.now(_KST)
    logger.info(
        "[위원회절제] 시작. window=%s~%s tickers=%d features=%d label_col=%s",
        args.start_date, args.end_date, len(tickers), len(feature_cols),
        args.label_col,
    )

    builder = DatasetBuilder(
        dual_source_enabled_for_lgbm=True,
        exogenous_enabled_for_lgbm=True,
    )
    panel = builder.build_training_frame(
        tickers=tickers,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    logger.info(
        "[위원회절제] panel 빌드 완료. rows=%d cols=%d loaded_tickers=%s",
        len(panel), len(panel.columns),
        panel.attrs.get("loaded_tickers", []),
    )

    missing = [c for c in feature_cols if c not in panel.columns]
    if missing:
        raise RuntimeError(
            f"[위원회절제] panel에 누락된 feature_cols={missing}. "
            "dataset_builder dual_source/exogenous join 결과 확인."
        )

    committee = CommitteeModel()
    result = committee.fit(
        panel=panel,
        feature_cols=feature_cols,
        label_col=args.label_col,
        version=args.version,
    )
    finished_at = datetime.now(_KST)

    output_dir = _ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "elapsed_sec": (finished_at - started_at).total_seconds(),
        "args": {
            "start_date": args.start_date,
            "end_date": args.end_date,
            "max_tickers": max_tickers,
            "n_tickers": len(tickers),
            "tickers": list(tickers),
            "label_col": args.label_col,
            "feature_cols": feature_cols,
            "feature_policy_source": "src.models.lgbm_trainer._load_feature_cols",
            "feature_cols_excluded_community": sorted(_COMMUNITY_FEATURES_EXCLUDED),
        },
        "panel": {
            "rows": int(len(panel)),
            "cols": int(len(panel.columns)),
            "loaded_tickers": panel.attrs.get("loaded_tickers", []),
            "missing_tickers": panel.attrs.get("missing_tickers", []),
            "synthetic_tickers": panel.attrs.get("synthetic_tickers", []),
        },
        "committee_result": result.to_dict(),
    }
    report_path = output_dir / (
        f"committee_ablation_{started_at.strftime('%Y%m%d_%H%M%S')}.json"
    )
    with report_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    print()
    print("=" * 60)
    print("Committee Ablation 결과")
    print("=" * 60)
    print(f"  window           : {args.start_date} ~ {args.end_date}")
    print(f"  n_tickers loaded : {len(panel.attrs.get('loaded_tickers', []))}")
    print(f"  n_features       : {len(feature_cols)}")
    print(f"  panel rows       : {len(panel):,}")
    print(f"  n_folds          : {result.n_folds}")
    print()
    print(f"  tree_only_sr     : {result.tree_only_sr:+.4f}")
    print(f"  committee_sr     : {result.committee_sr:+.4f}")
    print(f"  delta_sharpe     : {result.delta_sharpe:+.4f}")
    print(f"  improved         : {result.improved}")
    print()
    print(f"  artifact_dir     : {result.artifact_dir}")
    print(f"  report           : {report_path}")
    print(f"  elapsed          : {report['elapsed_sec']:.1f}s")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
