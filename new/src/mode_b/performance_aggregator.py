"""Mode B Performance Aggregator v3 S2-10 실구현.

C18 AgentPerformanceContract 집계 주체. 장마감 18:00 KST 자동 실행.

- audit_log.jsonl (C18 18 필드) 전수 읽기
- L2 9 지표 집계 (prediction_accuracy, realized_pnl_contribution 등)
- 8차원 성과 벡터 proxy 계산 (Sprint 3에서 L1 MetricsBundle 완전 연동)
- artifacts/metrics/agent_performance_{YYYY-MM-DD}.json 저장
- knowledge_base/agent_performance_history.jsonl append

PIT-Safety (불변 원칙 1):
  label_t5_ret / price_t5_snapshot post-hoc 지표는 18:00 KST 이후만 집계.
  장중 실시간 집계 금지. pit_guard.is_pit_safe() 확인.

C14 Mode B Scheduler stage_1_data_rollup이 호출.
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.utils.config_loader import load as config_load
from src.utils.id_factory import generate_agent_performance_id
from src.utils.label_meta import is_label_backfill_pit_safe
from src.utils.logger import get_logger
from src.utils.pit_guard import PITViolationError

logger = get_logger("performance_aggregator")

_KST = ZoneInfo("Asia/Seoul")
_DEFAULT_LOG_PATH = (
    Path(__file__).resolve().parents[3] / "artifacts" / "audit_log.jsonl"
)
_DEFAULT_METRICS_DIR = (
    Path(__file__).resolve().parents[3] / "artifacts" / "metrics"
)
_DEFAULT_KB_PATH = (
    Path(__file__).resolve().parents[3] / "knowledge_base" / "agent_performance_history.jsonl"
)

# C18 20 필드
_C18_FIELDS = (
    "ts", "decision_id", "agent", "event_type", "ticker", "reason_code",
    "signal_score", "anomaly_flag", "target_weight", "actual_weight",
    "fill_price", "snapshot_vwap", "slippage_bps", "sector",
    "llm_called", "llm_model", "label_t5_ret", "price_t5_snapshot",
    "label_backfilled_at", "label_backfill_source",
)


class ModeBPerformanceAggregator:
    """C18 집계 주체. audit_log.jsonl → L2 9지표 + 8d 벡터.

    Mode B stage_1_data_rollup에서 호출. 18:00 KST 이후만 실행.
    """

    def __init__(
        self,
        log_path: Path | str | None = None,
        metrics_dir: Path | str | None = None,
        kb_path: Path | str | None = None,
    ) -> None:
        self._log_path = Path(log_path) if log_path else _DEFAULT_LOG_PATH
        self._metrics_dir = Path(metrics_dir) if metrics_dir else _DEFAULT_METRICS_DIR
        self._kb_path = Path(kb_path) if kb_path else _DEFAULT_KB_PATH
        self._cfg = self._load_config()

    def _load_config(self) -> dict[str, Any]:
        """risk_config.yaml agent_performance 섹션 로드."""
        try:
            return config_load("risk_config.yaml", "agent_performance") or {}
        except Exception as e:
            logger.warning("[performance_aggregator] config 로드 실패, 기본값 사용: %s", e)
            return {}

    # ------------------------------------------------------------------
    # 메인 진입점
    # ------------------------------------------------------------------

    def aggregate(self, date_str: str) -> dict[str, Any]:
        """일별 L2 9지표 + 8d 벡터 집계.

        Args:
            date_str: YYYY-MM-DD (집계 대상 영업일)

        Returns:
            {
                "agent_performance_id": "APM-...",
                "rollup_date": "YYYY-MM-DD",
                "metrics": {prediction_accuracy: float, ...},
                "performance_vector_8d": [f1, f2, ..., f8],
                "record_count": int,
                "post_hoc_count": int,
            }

        Raises:
            PITViolationError: 18:00 KST 이전 호출 시
        """
        # PIT-Safety: 오늘 날짜이고 snapshot_hour(yaml) 이전이면 거부 (불변 원칙 1)
        now_kst = datetime.now(_KST)
        snapshot_hour = int(
            (config_load("risk_config.yaml", "pit_safety") or {}).get("snapshot_hour", 18)
        )
        target = now_kst.strftime("%Y-%m-%d")
        if date_str > target:
            raise PITViolationError(
                f"[performance_aggregator] 미래 날짜 집계 금지: "
                f"date_str={date_str} > today={target}. 불변 원칙 1."
            )
        if date_str >= target and now_kst.hour < snapshot_hour:
            raise PITViolationError(
                f"[performance_aggregator] L2 집계는 {snapshot_hour:02d}:00 KST 이후만 허용. "
                f"현재 {now_kst.strftime('%H:%M')} KST"
            )

        # audit_log 로드 + 날짜 필터
        records = self._load_day_records(date_str)
        logger.info(
            "[performance_aggregator] %s 레코드 %d건 로드", date_str, len(records)
        )

        if not records:
            logger.warning(
                "[performance_aggregator] %s 레코드 없음. 빈 집계 반환.", date_str
            )

        # L2 9지표 계산
        metrics = self._compute_metrics(records)

        # 8d 벡터 (proxy: IC proxy + placeholder)
        perf_vector = self._compute_performance_vector_8d(records, metrics)

        apm_id = generate_agent_performance_id()
        result: dict[str, Any] = {
            "agent_performance_id": apm_id,
            "rollup_date": date_str,
            "record_count": len(records),
            "post_hoc_count": len(self._labelled_records(records)),
            "pit_label_violation_count": self._pit_label_violation_count(records),
            "metrics": metrics,
            "performance_vector_8d": perf_vector,
            "aggregated_at": datetime.now(_KST).isoformat(),
        }

        # 저장
        self._save_daily_rollup(date_str, result)
        self._append_to_kb(result)

        logger.info(
            "[performance_aggregator] 집계 완료: %s apm_id=%s metrics=%d종",
            date_str, apm_id, len(metrics),
        )
        return result

    # ------------------------------------------------------------------
    # 데이터 로드
    # ------------------------------------------------------------------

    def _load_day_records(self, date_str: str) -> list[dict[str, Any]]:
        """audit_log.jsonl에서 date_str 날짜의 레코드만 필터링."""
        if not self._log_path.exists():
            return []

        day_prefix = date_str  # "YYYY-MM-DD"
        records: list[dict[str, Any]] = []
        with self._log_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts = str(rec.get("ts", ""))
                    if ts.startswith(day_prefix) or (
                        "T" in ts and ts.split("T")[0] == day_prefix
                    ):
                        records.append(rec)
                except json.JSONDecodeError as e:
                    logger.warning("[performance_aggregator] JSONL 파싱 실패: %s", e)
        return records

    # ------------------------------------------------------------------
    # L2 9지표 계산
    # ------------------------------------------------------------------

    def _compute_metrics(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """C18 L2 9지표 전체 계산."""
        metrics: dict[str, Any] = {}

        # post-hoc 가능 레코드: label과 C18 backfill metadata가 PIT-safe인 것만.
        labelled = self._labelled_records(records)

        metrics["prediction_accuracy"] = self._calc_prediction_accuracy(labelled)
        metrics["realized_pnl_contribution"] = self._calc_realized_pnl(records)
        metrics["slippage_execution_shortfall_bps"] = self._calc_slippage(records)
        metrics["anomaly_detection_precision"] = self._calc_anomaly_precision(labelled)
        metrics["anomaly_detection_recall"] = self._calc_anomaly_recall(labelled)
        metrics["sector_exposure_tracking_error"] = self._calc_sector_tracking_error(records)
        metrics["veto_precision"] = self._calc_veto_precision(labelled)
        metrics["veto_recall"] = self._calc_veto_recall(labelled)
        metrics["false_positive_event_trigger_rate"] = self._calc_false_positive_rate(records)

        return metrics

    def _labelled_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """C18 post-hoc label metadata까지 검증한 labelled records."""
        return [
            r for r in records
            if r.get("label_t5_ret") is not None and is_label_backfill_pit_safe(r)
        ]

    def _pit_label_violation_count(self, records: list[dict[str, Any]]) -> int:
        """label은 있으나 backfill metadata가 안전하지 않은 레코드 수."""
        return sum(
            1 for r in records
            if r.get("label_t5_ret") is not None and not is_label_backfill_pit_safe(r)
        )

    def _calc_prediction_accuracy(self, labelled: list[dict]) -> float | None:
        """count(signal_score > 0 AND label_t5_ret > 0) / count(non-null label)."""
        quant_recs = [
            r for r in labelled
            if r.get("agent") == "quant"
            and r.get("signal_score") is not None
        ]
        if not quant_recs:
            return None
        correct = sum(
            1 for r in quant_recs
            if float(r.get("signal_score", 0)) > 0
            and float(r.get("label_t5_ret", 0)) > 0
        )
        return round(correct / len(quant_recs), 4)

    def _calc_realized_pnl(self, records: list[dict]) -> dict[str, float]:
        """에이전트별 realized_pnl proxy.

        단순화: fill_price 기반 daily PnL contribution.
        실 leave-one-out ablation은 Sprint 3 S3-9에서 구현.
        """
        agent_pnl: dict[str, list[float]] = {}
        for r in records:
            agent = str(r.get("agent", "unknown"))
            fill = r.get("fill_price")
            vwap = r.get("snapshot_vwap")
            if fill is not None and vwap is not None and float(vwap) > 0:
                contrib = (float(fill) - float(vwap)) / float(vwap)
                agent_pnl.setdefault(agent, []).append(contrib)
        return {
            agent: round(sum(vals) / len(vals), 6) if vals else 0.0
            for agent, vals in agent_pnl.items()
        }

    def _calc_slippage(self, records: list[dict]) -> float | None:
        """mean( (fill_price - snapshot_vwap) / snapshot_vwap * 10000 ) bps."""
        eg_recs = [
            r for r in records
            if r.get("agent") == "execution_gw"
            and r.get("fill_price") is not None
            and r.get("snapshot_vwap") is not None
        ]
        if not eg_recs:
            return None
        bps_vals = []
        for r in eg_recs:
            fill = float(r.get("fill_price", 0))
            vwap = float(r.get("snapshot_vwap", 0))
            if vwap > 0:
                bps_vals.append((fill - vwap) / vwap * 10000)
        if not bps_vals:
            return None
        return round(statistics.mean(bps_vals), 2)

    def _calc_anomaly_precision(self, labelled: list[dict]) -> float | None:
        """count(anomaly_flag=true AND label < threshold) / count(anomaly_flag=true)."""
        threshold = float(self._cfg.get("anomaly_threshold_pct", -0.01))
        flagged = [r for r in labelled if r.get("anomaly_flag") is True]
        if not flagged:
            return None
        true_pos = sum(
            1 for r in flagged
            if float(r.get("label_t5_ret", 0)) < threshold
        )
        return round(true_pos / len(flagged), 4)

    def _calc_anomaly_recall(self, labelled: list[dict]) -> float | None:
        """count(anomaly_flag=true AND label < threshold) / count(label < threshold)."""
        threshold = float(self._cfg.get("anomaly_threshold_pct", -0.01))
        ground_truth = [
            r for r in labelled
            if float(r.get("label_t5_ret", 0)) < threshold
        ]
        if not ground_truth:
            return None
        detected = sum(1 for r in ground_truth if r.get("anomaly_flag") is True)
        return round(detected / len(ground_truth), 4)

    def _calc_sector_tracking_error(self, records: list[dict]) -> float | None:
        """std(w_sector_actual - w_sector_target) across sectors."""
        pm_recs = [
            r for r in records
            if r.get("agent") == "pm"
            and r.get("sector") is not None
            and r.get("actual_weight") is not None
            and r.get("target_weight") is not None
        ]
        if not pm_recs:
            return None
        diffs = [
            float(r.get("actual_weight", 0)) - float(r.get("target_weight", 0))
            for r in pm_recs
        ]
        if len(diffs) < 2:
            return None
        return round(statistics.stdev(diffs), 6)

    def _calc_veto_precision(self, labelled: list[dict]) -> float | None:
        """count(veto AND label < threshold) / count(veto)."""
        threshold = float(self._cfg.get("veto_loss_threshold_pct", -0.005))
        fda_recs = [r for r in labelled if r.get("agent") == "fda"]
        veto_recs = [r for r in fda_recs if r.get("event_type") == "veto"]
        if not veto_recs:
            return None
        correct_veto = sum(
            1 for r in veto_recs
            if float(r.get("label_t5_ret", 0)) < threshold
        )
        return round(correct_veto / len(veto_recs), 4)

    def _calc_veto_recall(self, labelled: list[dict]) -> float | None:
        """count(veto AND label < threshold) / count(label < threshold)."""
        threshold = float(self._cfg.get("veto_loss_threshold_pct", -0.005))
        fda_recs = [r for r in labelled if r.get("agent") == "fda"]
        loss_events = [
            r for r in fda_recs
            if float(r.get("label_t5_ret", 0)) < threshold
        ]
        if not loss_events:
            return None
        vetoed = sum(1 for r in loss_events if r.get("event_type") == "veto")
        return round(vetoed / len(loss_events), 4)

    def _calc_false_positive_rate(self, records: list[dict]) -> float | None:
        """count(llm_called=true AND reason_code=NORMAL_APPROVE) / count(llm_called=true)."""
        llm_recs = [r for r in records if r.get("llm_called") is True]
        if not llm_recs:
            return None
        fp = sum(
            1 for r in llm_recs
            if r.get("reason_code") == "NORMAL_APPROVE"
        )
        return round(fp / len(llm_recs), 4)

    # ------------------------------------------------------------------
    # 8차원 성과 벡터 (proxy)
    # ------------------------------------------------------------------

    def _compute_performance_vector_8d(
        self,
        records: list[dict[str, Any]],
        metrics: dict[str, Any],
    ) -> list[float]:
        """8차원 성과 벡터 proxy 계산.

        [IC_proxy, ICIR_proxy, RankIC_proxy, Rank(ICIR)_proxy, ARR_proxy, IR_proxy, -MDD_proxy, SR_proxy]

        실 L1 MetricsBundle 연동은 Sprint 3 S3-9에서. 현재 signal-label 상관계수 proxy.
        """
        labelled = [
            r for r in records
            if r.get("agent") == "quant"
            and r.get("signal_score") is not None
            and r.get("label_t5_ret") is not None
        ]

        if len(labelled) < 5:
            return [0.0] * 8

        scores = [float(r.get("signal_score", 0)) for r in labelled]
        labels = [float(r.get("label_t5_ret", 0)) for r in labelled]

        # IC proxy: Pearson correlation
        ic_proxy = self._pearson_corr(scores, labels)

        # pnl_series: simple top-score strategy
        mean_score = statistics.mean(scores) if scores else 0.0
        pnl_series = [
            lbl for sc, lbl in zip(scores, labels)
            if sc > mean_score
        ]

        arr_proxy = statistics.mean(pnl_series) if pnl_series else 0.0
        pnl_std = statistics.stdev(pnl_series) if len(pnl_series) >= 2 else 1.0
        ir_proxy = arr_proxy / pnl_std if pnl_std > 0 else 0.0

        # MDD proxy: max drawdown of cumulative pnl
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnl_series:
            cumulative += p
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd
        mdd_proxy = -max_dd

        # SR proxy: same as IR (risk-free=0)
        sr_proxy = ir_proxy

        # clip all to [-1, 1] range
        def clip(v: float) -> float:
            if not math.isfinite(v):
                return 0.0
            return max(-1.0, min(1.0, round(v, 4)))

        return [
            clip(ic_proxy),   # IC
            clip(ic_proxy),   # ICIR proxy (same as IC until Sprint 3)
            clip(ic_proxy),   # RankIC proxy
            clip(ic_proxy),   # Rank(ICIR) proxy
            clip(arr_proxy),  # ARR
            clip(ir_proxy),   # IR
            clip(mdd_proxy),  # -MDD
            clip(sr_proxy),   # SR
        ]

    @staticmethod
    def _pearson_corr(x: list[float], y: list[float]) -> float:
        """Pearson 상관계수. 분모 0이면 0.0 반환."""
        if len(x) < 2:
            return 0.0
        n = len(x)
        mx, my = sum(x) / n, sum(y) / n
        num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
        denom_x = math.sqrt(sum((xi - mx) ** 2 for xi in x))
        denom_y = math.sqrt(sum((yi - my) ** 2 for yi in y))
        denom = denom_x * denom_y
        return round(num / denom, 4) if denom > 1e-9 else 0.0

    # ------------------------------------------------------------------
    # 저장
    # ------------------------------------------------------------------

    def _save_daily_rollup(self, date_str: str, result: dict[str, Any]) -> None:
        """artifacts/metrics/agent_performance_{date}.json 저장."""
        self._metrics_dir.mkdir(parents=True, exist_ok=True)
        safe_date = date_str.replace("-", "")
        out_path = self._metrics_dir / f"agent_performance_{safe_date}.json"
        with out_path.open("w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        logger.info("[performance_aggregator] rollup 저장: %s", out_path)

    def _append_to_kb(self, result: dict[str, Any]) -> None:
        """knowledge_base/agent_performance_history.jsonl append."""
        self._kb_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(_KST).isoformat(),
            "agent_performance_id": result.get("agent_performance_id"),
            "rollup_date": result.get("rollup_date"),
            "record_count": result.get("record_count"),
            "post_hoc_count": result.get("post_hoc_count"),
            "metrics_summary": {
                k: v for k, v in result.get("metrics", {}).items()
                if isinstance(v, (int, float, type(None)))
            },
        }
        with self._kb_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("[performance_aggregator] KB append: %s", self._kb_path)
