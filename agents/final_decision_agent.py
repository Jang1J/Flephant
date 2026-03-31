"""
Final Decision Agent (FDA)
- Phase 1: deterministic pre-check + LLM explanation wrapper
- 제안서 기준: constrained aggregator (can_change_weight=false)
- StrategyCard + RiskCard + COP를 받아 approve/veto 판정
- conflict case에서만 LLM 호출, 그 외 deterministic

Usage:
    from agents.final_decision_agent import FinalDecisionAgent
    fda = FinalDecisionAgent()
    fdc = fda.run(target_date, cop, risk_card, strategy_cards, portfolio_state)
"""

import json
import yaml
import pandas as pd
from datetime import datetime
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from connectors import now_kst_iso, make_snapshot_dt

_BASE_DIR = Path(__file__).resolve().parent.parent

FDC_DIR = _BASE_DIR / "artifacts" / "final_decision_card"
FDC_DIR.mkdir(parents=True, exist_ok=True)

# FDA contract 로드
CONTRACT_PATH = _BASE_DIR / "prompts" / "final_decision_contract_v0.md"


class FinalDecisionAgent:
    """
    Constrained Final Decision Agent
    - can_change_weight = false (비중 수정 불가)
    - approve 또는 veto만 가능
    - Phase 1: deterministic rules + LLM explanation
    """

    def __init__(self, use_llm: bool = True):
        self.use_llm = use_llm
        self.contract = self._load_contract()
        self._policy = self._load_policy()
        self._sector_map = self._load_sector_map()

    def _load_sector_map(self) -> dict:
        """universe_v1.csv에서 ticker→wics_sector 매핑 로드 (Rule 6b 전용)"""
        csv_path = _BASE_DIR / "config" / "universe_v1.csv"
        try:
            df = pd.read_csv(csv_path, dtype={"ticker": str})
            df["ticker"] = df["ticker"].apply(lambda x: str(x).zfill(6))
            return dict(zip(df["ticker"], df["wics_sector"]))
        except Exception as e:
            print(f"[FDA] universe_v1.csv sector 매핑 로드 실패: {e}")
            return {}

    def _load_policy(self) -> dict:
        """risk_policy_v0.yaml 로드"""
        policy_path = _BASE_DIR / "config" / "risk_policy_v0.yaml"
        try:
            with open(policy_path, encoding="utf-8") as f:
                return yaml.safe_load(f)
        except Exception as e:
            print(f"[FDA] 정책 파일 로드 실패: {e} — 기본값 사용")
            return {}

    def _load_contract(self) -> str:
        """FDA prompt contract 로드"""
        if CONTRACT_PATH.exists():
            return CONTRACT_PATH.read_text(encoding="utf-8")
        return ""

    def run(
        self,
        target_date: str,
        cop: dict,
        risk_card: dict,
        strategy_cards: list | None = None,
        portfolio_state: dict | None = None,
        backtest_summary: dict | None = None,
        snapshot_hour: int = 18,
        dmp: dict | None = None,
    ) -> dict:
        """
        FDA 실행: COP의 각 order에 대해 approve/veto 판정

        Args:
            target_date: YYYYMMDD
            cop: CandidateOrderPlan
            risk_card: RiskCard
            strategy_cards: StrategyCard 리스트 (Phase 2: conflict detection용)
            portfolio_state: PortfolioState (Phase 2: 포지션 맥락)
            backtest_summary: BacktestReport 요약 (Phase 2: 과거 성과 참조)
            snapshot_hour: snapshot 기준 시각 (기본 18, intraday 호출 시 해당 시각 전달)

        Returns:
            FinalDecisionCard dict
        """
        _hhmm = f"{snapshot_hour:02d}0000"
        snapshot_dt = make_snapshot_dt(target_date, snapshot_hour)
        regime = cop.get("regime", "green")

        decisions = []
        conflicts = []
        approved = 0
        vetoed = 0

        # SC를 ticker별로 인덱싱
        sc_map = {}
        if strategy_cards:
            sc_map = {c["ticker"]: c for c in strategy_cards}

        for order in cop.get("orders", []):
            ticker = order["ticker"]
            decision, veto_reason, is_conflict = self._evaluate_order(
                order, regime, risk_card, sc_map.get(ticker), portfolio_state,
                backtest_summary,
            )

            # warning_reason 분리: approve이면서 경고 텍스트가 있으면 warning_reason으로 이동
            warning_reason = None
            if decision == "approve" and veto_reason and veto_reason.startswith("["):
                warning_reason = veto_reason
                veto_reason = None

            if decision == "approve":
                approved += 1
            else:
                vetoed += 1

            if is_conflict:
                # conflict type 자동 분류
                if "Regime RED" in (veto_reason or ""):
                    conflict_type = "regime_conflict"
                elif "stop-loss" in (veto_reason or ""):
                    conflict_type = "stop_loss_conflict"
                elif "백테스트" in (veto_reason or ""):
                    conflict_type = "backtest_conflict"
                elif "보유" in (veto_reason or ""):
                    conflict_type = "holding_conflict"
                else:
                    conflict_type = "confidence_weight_conflict"

                sc = sc_map.get(ticker, {})
                _fda_conf = self._policy.get("fda_constraints", {}).get("confidence_threshold", 0.4)
                _order_conf = order.get("confidence", 0.0)
                _confidence_gap = max(0.0, float(_fda_conf) - float(_order_conf))
                conflicts.append({
                    "ticker": ticker,
                    "conflict_type": conflict_type,
                    "description": veto_reason,
                    "resolution": f"veto 처리 ({conflict_type})",
                    "confidence_gap": round(_confidence_gap, 4),
                })

            # SC의 uncertainty_score를 FDC에 전달 (SC → COP order에 있거나 SC map에서 추출)
            sc = sc_map.get(ticker, {})
            uq_score = order.get("uncertainty_p85") if order.get("uncertainty_p85") is not None else sc.get("uncertainty_score")

            decisions.append({
                "ticker": ticker,
                "name": order.get("name", ticker),
                "action": order["action"],
                "weight": order["weight"],  # can_change_weight = false
                "decision": decision,
                "veto_reason": veto_reason,
                "warning_reason": warning_reason,
                "evidence_ids": order.get("evidence_ids", []),
                "uncertainty_score": uq_score,
            })

        # total_approved_weight: approve된 buy/hold만 합산 (sell은 청산 예정이므로 제외)
        total_approved_weight = sum(
            d["weight"] for d in decisions
            if d["decision"] == "approve" and d.get("action") != "sell"
        )

        # conflict 항목별 LLM 갈등 분석 추가
        if conflicts and self.use_llm:
            conflicts = self._analyze_conflicts(conflicts, sc_map)

        # Multi-Agent Debate 트리거 (explanation/audit only — 판정 불변)
        if conflicts and dmp:
            from agents.debate_agent import DebateAgent
            debate_agent = DebateAgent()
            dmp_macro = dmp.get("macro_snapshot", {})
            # COP order를 ticker로 인덱싱
            order_map = {o["ticker"]: o for o in cop.get("orders", [])}
            for conflict in conflicts:
                if self._should_debate(conflict):
                    ticker = conflict.get("ticker", "")
                    order = order_map.get(ticker, {"ticker": ticker})
                    try:
                        debate_log = debate_agent.run_debate(
                            ticker=ticker,
                            order=order,
                            sc_map=sc_map,
                            risk_card=risk_card,
                            dmp_macro=dmp_macro,
                            portfolio_state=portfolio_state,
                            debate_triggered_by=conflict.get("conflict_type", "confidence_gap"),
                        )
                        conflict["debate_log"] = debate_log
                        print(f"  [FDA] [{ticker}] Debate 완료 (consensus={debate_log.get('consensus_score')})")
                    except Exception as e:
                        print(f"  [FDA] [{ticker}] Debate 실패 (skip): {e}")

        # LLM explanation 생성
        explanation = self._generate_explanation(
            target_date, regime, decisions, total_approved_weight, conflicts
        )
        fallback_used = explanation.get("fallback_used", False)

        fdc = {
            "decision_id": f"FDC-{target_date}-{_hhmm}",
            "snapshot_dt": snapshot_dt,
            "generated_at": now_kst_iso(),
            "artifact_version": "v1.0",
            "plan_id": cop["plan_id"],
            "fallback_used": fallback_used,
            "decisions": decisions,
            "conflicts": conflicts,
            "execution_summary": {
                "approved_count": approved,
                "vetoed_count": vetoed,
                "total_exposure": round(total_approved_weight, 2),
                "cash_ratio": round(100 - total_approved_weight, 2),
                "explanation": explanation["text"],
            }
        }

        # 저장
        fdc_path = FDC_DIR / f"FDC-{target_date}-{_hhmm}.json"
        with open(fdc_path, "w", encoding="utf-8") as f:
            json.dump(fdc, f, ensure_ascii=False, indent=2)

        print(f"  → 승인: {approved}, 거부: {vetoed}")
        print(f"  → 총 노출: {total_approved_weight:.1f}%, 현금: {100-total_approved_weight:.1f}%")
        print(f"  ✅ FinalDecisionCard: {fdc_path}")

        return fdc

    def _evaluate_order(
        self,
        order: dict,
        regime: str,
        risk_card: dict,
        strategy_card: dict | None,
        portfolio_state: dict | None,
        backtest_summary: dict | None = None,
    ) -> tuple:
        """
        개별 order에 대한 approve/veto 판정.
        - sell order: stop_loss/signal_sell → 기본 approve (청산 필요성 존중)
        - hold order: 기본 approve
        - buy order: 기존 규칙 적용

        Returns: (decision, veto_reason, is_conflict)
        """
        action = order.get("action", "buy")
        confidence = order.get("confidence", 0)
        weight = order.get("weight", 0)
        risk_flag = order.get("risk_flag", "pass")

        # sell/hold order는 기본 approve (can_change_weight=false 유지)
        if action == "sell":
            sell_reason = order.get("sell_reason", "signal_sell")
            # sell order는 원칙적으로 approve — RiskEngine이 이미 검증했음
            return "approve", None, False
        if action == "hold":
            return "approve", None, False

        # Rule 1: risk_flag가 reject이면 veto
        if risk_flag == "reject":
            return "veto", f"Risk Agent reject: {order.get('rationale', '')}", False

        # Rule 2: confidence가 낮고 비중이 크면 veto (과도 리스크)
        # fda_constraints.confidence_threshold (default 0.4) 이하이면서
        # fda_constraints.low_conf_weight_cap (default 10%) 초과 시 차단.
        _fda_constraints = self._policy.get("fda_constraints", {})
        _yaml_min_conf = self._policy.get("position_constraints", {}).get("min_confidence", 0.3)
        _fda_conf_threshold = max(
            _fda_constraints.get("confidence_threshold", 0.4), _yaml_min_conf
        )
        _low_conf_weight_cap = _fda_constraints.get("low_conf_weight_cap", 10)
        if confidence < _fda_conf_threshold and weight > _low_conf_weight_cap:
            return "veto", (
                f"confidence({confidence}) < FDA 임계값({_fda_conf_threshold})"
                f"이고 비중({weight}%) > {_low_conf_weight_cap}%로 리스크 과다"
            ), False

        # Rule 3: red regime인데 buy signal이면 conflict
        if regime == "red" and order.get("action") == "buy":
            return "veto", f"Regime RED에서 신규 매수 금지", True  # type: regime_conflict

        # Rule 4: PortfolioState 기반 — stop-loss hit 종목이면 veto
        if portfolio_state:
            for pos in portfolio_state.get("positions", []):
                if pos["ticker"] == order["ticker"] and pos.get("stop_loss_hit"):
                    return "veto", f"stop-loss 도달 종목 — 추가 매수 금지", True  # type: stop_loss_conflict

        # Rule 5: PortfolioState 기반 — 장기 보유 + 하락 종목 경고
        if portfolio_state:
            for pos in portfolio_state.get("positions", []):
                if (pos["ticker"] == order["ticker"]
                        and pos.get("holding_days", 0) >= 5
                        and pos.get("unrealized_pnl_pct", 0) < -3):
                    return "veto", f"보유 {pos['holding_days']}일 + 손실 {pos['unrealized_pnl_pct']:.1f}% — 추가 매수 비권장", True

        # Rule 6: BacktestReport 기반 판단 (status == "live"일 때만 활성화)
        if backtest_summary and backtest_summary.get("status") == "live":
            warnings = []

            # Rule 6a: 낮은 승률 경고 (임계값: risk_policy fda_constraints.backtest_win_rate_warn)
            _bt_warn = _fda_constraints.get("backtest_win_rate_warn", 0.35)
            win_rate = backtest_summary.get("win_rate_hint")
            if win_rate is not None and win_rate < _bt_warn:
                warnings.append(f"백테스트 승률 {win_rate:.0%} 미달 (임계값 {_bt_warn:.0%})")
                print(f"  [FDA] [{order.get('ticker', '')}] Rule 6a 경고: 승률 {win_rate:.0%} < {_bt_warn:.0%}")

            # Rule 6b: failure_tags 매칭
            # sector는 SC 스키마에 없으므로 universe_v1.csv에서 lookup한다.
            failure_tags = backtest_summary.get("failure_tags", [])
            if failure_tags:
                sc = strategy_card or {}
                ticker_key = order.get("ticker", "")
                sector = self._sector_map.get(str(ticker_key).zfill(6), "")
                signal = sc.get("signal", "")
                matched_tags = []
                if "sector_concentration" in failure_tags and sector:
                    matched_tags.append("sector_concentration")
                if "momentum_reversal" in failure_tags and signal in ["strong_buy", "buy"]:
                    matched_tags.append("momentum_reversal")
                if matched_tags:
                    warnings.append(f"failure_tags 매칭: {matched_tags}")
                    print(f"  [FDA] [{ticker_key}] Rule 6b 경고: {matched_tags}")

            # Rule 6c: Sharpe/MDD 기반 전략 신뢰도 (포트폴리오 전체 경고)
            recent_sharpe = backtest_summary.get("recent_sharpe")
            recent_mdd = backtest_summary.get("recent_mdd")
            if (recent_sharpe is not None and recent_sharpe < 0) or \
               (recent_mdd is not None and recent_mdd < -15):
                warnings.append(
                    f"전략 신뢰도 저하 (Sharpe={recent_sharpe}, MDD={recent_mdd}%)"
                )
                print(f"  [FDA] [{order.get('ticker', '')}] Rule 6c 경고: "
                      f"Sharpe={recent_sharpe}, MDD={recent_mdd}%")

            # 경고가 있으면 veto_reason에 반영 (approve이지만 explanation에 포함)
            if warnings:
                warning_text = "; ".join(warnings)
                # 승률 미달 + 고비중이면 veto (임계값: risk_policy fda_constraints)
                _bt_veto = _fda_constraints.get("backtest_win_rate_veto", 0.4)
                _bt_wt_cap = _fda_constraints.get("backtest_weight_veto_cap", 15)
                if win_rate is not None and win_rate < _bt_veto and weight > _bt_wt_cap:
                    return "veto", f"백테스트 승률 {win_rate:.0%} 미달 + 비중 {weight}% 과다", True
                # 그 외 경고는 approve이지만 경고 텍스트를 veto_reason에 저장 (explanation 반영용)
                return "approve", f"[백테스트 경고] {warning_text}", False

        return "approve", None, False

    def _should_debate(self, conflict: dict) -> bool:
        """Debate 트리거 조건: regime_conflict 또는 confidence_gap > threshold."""
        conflict_type = conflict.get("conflict_type", "")

        # regime_conflict는 항상 Debate
        if conflict_type == "regime_conflict":
            return True

        # confidence_gap 직접 읽기
        confidence_gap = conflict.get("confidence_gap", 0.0)
        threshold = self._policy.get("fda_constraints", {}).get("debate_confidence_gap_threshold", 0.3)
        if confidence_gap > threshold:
            return True

        return False

    def _analyze_conflicts(self, conflicts: list, sc_map: dict) -> list:
        """
        conflict 항목별 Kanana-o LLM 갈등 분석 추가
        각 conflict 항목에 llm_conflict_analysis 필드를 추가한다.
        LLM 실패 시 null.
        """
        try:
            from connectors.llm_router import call_llm
        except Exception as e:
            print(f"  [FDA] llm_router import 실패: {e}")
            for c in conflicts:
                c["llm_conflict_analysis"] = None
            return conflicts

        enriched = []
        for conflict in conflicts:
            ticker = conflict.get("ticker", "")
            conflict_type = conflict.get("conflict_type", "")
            description = conflict.get("description", "")
            sc = sc_map.get(ticker, {})
            signal = sc.get("signal", "N/A")
            confidence = sc.get("confidence", "N/A")
            name = sc.get("name", ticker)

            llm_conflict_analysis = None
            try:
                prompt_messages = [
                    {
                        "role": "system",
                        "content": (
                            "너는 한국 주식 투자 갈등 분석 전문가야. "
                            "투자 신호 간 갈등 상황을 분석하고 승인/거부 중 어느 쪽이 합리적인지 근거를 제시해. "
                            "단, 최종 판단은 deterministic rule이 결정하므로 분석만 제공해."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"아래 종목에서 투자 신호 간 갈등이 감지되었다:\n\n"
                            f"종목: {name} ({ticker})\n"
                            f"신호: {signal} (confidence: {confidence})\n"
                            f"갈등 유형: {conflict_type}\n"
                            f"세부: {description}\n\n"
                            "이 갈등 상황을 분석하고, 승인/거부 중 어느 쪽이 합리적인지 근거를 제시하라.\n"
                            "반드시 아래 JSON 형식으로만 답하라:\n"
                            '{"conflict_analysis": "갈등 분석 2~3문장 (한국어)", '
                            '"recommended_action": "approve" 또는 "veto" 또는 "cautious_approve", '
                            '"reasoning": "근거 요약 1문장"}'
                        ),
                    },
                ]

                llm_result = call_llm(prompt_messages, temperature=0.3, max_tokens=256)
                raw_content = llm_result.get("content", "")
                model_used = llm_result.get("model", "unknown")

                # balanced bracket 매칭 (그리디 매칭 방지)
                parsed = None
                _s = raw_content.find("{")
                if _s != -1:
                    _depth, _e = 0, _s
                    for _ci, _ch in enumerate(raw_content[_s:], _s):
                        if _ch == "{": _depth += 1
                        elif _ch == "}": _depth -= 1
                        if _depth == 0:
                            _e = _ci + 1
                            break
                    try:
                        parsed = json.loads(raw_content[_s:_e])
                    except json.JSONDecodeError:
                        parsed = None
                if parsed:
                    recommended = parsed.get("recommended_action", "veto")
                    valid_actions = ["approve", "veto", "cautious_approve"]
                    if recommended not in valid_actions:
                        recommended = "veto"
                    llm_conflict_analysis = {
                        "conflict_analysis": parsed.get("conflict_analysis", ""),
                        "recommended_action": recommended,
                        "reasoning": parsed.get("reasoning", ""),
                        "model_used": model_used,
                    }
                    print(f"  → [{ticker}] 갈등 분석 완료 (recommended={recommended})")
                else:
                    print(f"  [WARN] [{ticker}] LLM 갈등 분석 JSON 파싱 실패 → null")
            except Exception as e:
                print(f"  [FDA] [{ticker}] 갈등 분석 LLM 호출 실패 (skip): {e}")

            conflict["llm_conflict_analysis"] = llm_conflict_analysis
            enriched.append(conflict)

        return enriched

    def _generate_explanation(
        self,
        target_date: str,
        regime: str,
        decisions: list,
        total_weight: float,
        conflicts: list,
    ) -> dict:
        """LLM으로 설명 생성, 실패 시 deterministic fallback"""
        approved = sum(1 for d in decisions if d["decision"] == "approve")
        vetoed = sum(1 for d in decisions if d["decision"] == "veto")

        # conflict가 있거나 LLM 사용 설정이면 LLM 호출
        if self.use_llm and (conflicts or approved > 0):
            try:
                from connectors.llm_router import call_llm

                decision_summary = "\n".join([
                    f"- {d['name']}({d['ticker']}): {d['decision']} {d['weight']}%"
                    + (f" (사유: {d['veto_reason']})" if d['veto_reason'] else "")
                    for d in decisions
                ])

                conflict_text = ""
                if conflicts:
                    conflict_text = "\n\nConflict 발생:\n" + "\n".join(
                        f"- {c['ticker']}: [{c['conflict_type']}] {c['description']}" for c in conflicts
                    )

                # FDA prompt contract를 system prompt에 포함
                contract_prefix = ""
                if self.contract:
                    contract_prefix = (
                        "## FDA Contract (반드시 준수)\n"
                        f"{self.contract[:1000]}\n\n"  # 핵심 부분만
                        "---\n위 contract에 따라 아래 결정을 설명해줘.\n\n"
                    )

                prompt_messages = [
                    {
                        "role": "system",
                        "content": (
                            f"{contract_prefix}"
                            "너는 한국 주식 투자 Final Decision Agent야. "
                            "아래 투자 결정을 3~5문장으로 한국어로 설명해줘. "
                            "can_change_weight=false 원칙을 지키며, "
                            "왜 이 결정을 내렸는지, 시장 상황과 리스크를 고려해 간결하게 서술해."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"날짜: {target_date}\nRegime: {regime}\n"
                            f"총 노출: {total_weight:.1f}%\n현금: {100-total_weight:.1f}%\n\n"
                            f"결정 내역:\n{decision_summary}{conflict_text}"
                        ),
                    },
                ]

                llm_result = call_llm(prompt_messages, temperature=0.3, max_tokens=512)
                print(f"  → LLM explanation 생성 완료 (fallback={llm_result.get('fallback_used', False)})")
                return {
                    "text": llm_result["content"],
                    "fallback_used": llm_result.get("fallback_used", False),
                }
            except Exception as e:
                print(f"  → LLM 호출 실패: {e} — deterministic explanation 사용")

        # Deterministic fallback
        text = (
            f"[DETERMINISTIC] {target_date} 투자 결정: "
            f"{approved}종목 승인, {vetoed}종목 거부. "
            f"Regime={regime}. 총 노출={total_weight:.1f}%."
        )
        return {"text": text, "fallback_used": False}
