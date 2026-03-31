"""
DebateAgent — FDA Multi-Agent Debate (explanation/audit layer).

3 페르소나(Foreigner / Institution / Retail)가 동일 종목에 대해 서로 다른 관점으로 분석한 뒤,
Moderator가 3자 의견을 종합하여 debate_log를 생성한다.

핵심 제약:
- audit/explanation only — 거래 판정(approve/veto)은 변경하지 않는다.
- can_change_weight=false — 비중 수정 절대 불가.
- PIT-Safe — 미래 데이터 사용 금지.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class DebateAgent:
    """FDA Multi-Agent Debate (explanation/audit layer).

    3 페르소나가 동일 종목에 대해 서로 다른 관점으로 분석:
    - Foreigner: DMP macro (usd_krw, vix_proxy, treasury_3y)에 민감
    - Institution: SC rationale (DART 공시 기반) + sector 집중도에 민감
    - Retail: SC rationale (뉴스 헤드라인 기반) + quant_score에 민감

    Moderator가 3자 의견을 종합하여 debate_log 생성.
    판정 변경 없음 (audit only).
    """

    PERSONAS = ["foreigner", "institution", "retail"]

    def run_debate(
        self,
        ticker: str,
        order: dict,
        sc_map: dict,
        risk_card: dict,
        dmp_macro: dict,
        portfolio_state: dict | None,
        debate_triggered_by: str = "confidence_gap",
    ) -> dict:
        """3-페르소나 Debate 실행 후 debate_log 반환.

        Args:
            ticker: 종목코드 (6자리 zfill)
            order: COP order 딕셔너리
            sc_map: {ticker: StrategyCard} 전체 맵
            risk_card: RiskCard 딕셔너리
            dmp_macro: DMP의 macro_snapshot 딕셔너리
            portfolio_state: PortfolioState (섹터 집중도 계산용, None 허용)
            debate_triggered_by: 트리거 사유 문자열

        Returns:
            debate_log dict
        """
        ticker = str(ticker).zfill(6)

        # 페르소나별 context 분리
        foreigner_ctx = self._build_foreigner_context(ticker, order, dmp_macro)
        institution_ctx = self._build_institution_context(ticker, order, sc_map, risk_card, portfolio_state)
        retail_ctx = self._build_retail_context(ticker, order, sc_map)

        # 각 페르소나 argument 수집
        arguments = {}
        models_used = []

        for persona, ctx in [
            ("foreigner", foreigner_ctx),
            ("institution", institution_ctx),
            ("retail", retail_ctx),
        ]:
            messages = self._build_persona_prompt(persona, ctx)
            arg = self._run_persona(persona, messages)
            arguments[persona] = arg
            if arg.get("model_used"):
                m = arg["model_used"]
                if m not in models_used:
                    models_used.append(m)

        # Moderator 종합
        moderator_result = self._moderate(ticker, order, arguments)

        consensus_score = moderator_result.get("consensus_score", 0.0)
        return {
            "ticker": ticker,
            "personas": {
                "foreigner": {
                    "argument": arguments["foreigner"].get("argument", ""),
                    "stance": arguments["foreigner"].get("stance", "neutral"),
                    "key_factors": arguments["foreigner"].get("key_factors", []),
                },
                "institution": {
                    "argument": arguments["institution"].get("argument", ""),
                    "stance": arguments["institution"].get("stance", "neutral"),
                    "key_factors": arguments["institution"].get("key_factors", []),
                },
                "retail": {
                    "argument": arguments["retail"].get("argument", ""),
                    "stance": arguments["retail"].get("stance", "neutral"),
                    "key_factors": arguments["retail"].get("key_factors", []),
                },
            },
            "moderator_summary": moderator_result.get("summary", ""),
            "consensus_score": consensus_score,
            "moderator_confidence": consensus_score,
            "emergency_flag": consensus_score < 0.33,
            "models_used": models_used,
            "debate_triggered_by": debate_triggered_by,
        }

    # ------------------------------------------------------------------
    # Context 빌더 (페르소나별 관련 context만 분리 주입)
    # ------------------------------------------------------------------

    def _build_foreigner_context(self, ticker: str, order: dict, dmp_macro: dict) -> dict:
        """Foreigner 페르소나용 context — macro 지표 중심."""
        return {
            "ticker": ticker,
            "name": order.get("name", ticker),
            "action": order.get("action", "buy"),
            "confidence": order.get("confidence"),
            "weight": order.get("weight"),
            "usd_krw": dmp_macro.get("usd_krw"),
            "vix_proxy": dmp_macro.get("vix_proxy"),
            "treasury_3y": dmp_macro.get("treasury_3y"),
            "market_regime": dmp_macro.get("market_regime"),
        }

    def _build_institution_context(
        self,
        ticker: str,
        order: dict,
        sc_map: dict,
        risk_card: dict,
        portfolio_state: dict | None,
    ) -> dict:
        """Institution 페르소나용 context — DART 공시 + 섹터 집중도 중심."""
        sc = sc_map.get(ticker, {})
        # rationale / evidence에서 DART 관련 항목 추출
        rationale = sc.get("rationale", "")
        evidence_ids = sc.get("evidence_ids", [])
        dart_evidence = [eid for eid in evidence_ids if "DART" in str(eid).upper()]

        # 섹터 집중도 — portfolio_state positions에서 계산
        sector_concentration = None
        if portfolio_state:
            positions = portfolio_state.get("positions", [])
            if positions:
                # 전체 포지션 수 대비 같은 섹터 비율 (간단 추정)
                total = len(positions)
                sc_sector = sc.get("wics_sector", "")
                same_sector = sum(1 for p in positions if p.get("sector", "") == sc_sector)
                sector_concentration = round(same_sector / total, 2) if total > 0 else 0.0

        return {
            "ticker": ticker,
            "name": order.get("name", ticker),
            "action": order.get("action", "buy"),
            "confidence": order.get("confidence"),
            "weight": order.get("weight"),
            "signal": sc.get("signal"),
            "rationale": rationale,
            "dart_evidence_count": len(dart_evidence),
            "sector": sc.get("wics_sector"),
            "sector_concentration": sector_concentration,
            "risk_flag": order.get("risk_flag"),
        }

    def _build_retail_context(self, ticker: str, order: dict, sc_map: dict) -> dict:
        """Retail 페르소나용 context — 뉴스 헤드라인 + quant_score 중심."""
        sc = sc_map.get(ticker, {})
        return {
            "ticker": ticker,
            "name": order.get("name", ticker),
            "action": order.get("action", "buy"),
            "confidence": order.get("confidence"),
            "quant_score": sc.get("quant_score"),
            "signal": sc.get("signal"),
            "rationale": sc.get("rationale", ""),
            "news_sentiment": sc.get("news_sentiment"),
        }

    # ------------------------------------------------------------------
    # 프롬프트 빌더
    # ------------------------------------------------------------------

    def _build_persona_prompt(self, persona: str, ctx: dict) -> list:
        """페르소나별 시스템/유저 메시지 구성."""
        ticker = ctx.get("ticker", "")
        name = ctx.get("name", ticker)
        action = ctx.get("action", "buy")
        confidence = ctx.get("confidence", "N/A")
        weight = ctx.get("weight", "N/A")

        if persona == "foreigner":
            system_msg = (
                "너는 외국인 기관투자자 관점의 한국 주식 분석가야. "
                "환율(USD/KRW), 글로벌 금리(국채 3년), VIX(변동성 지수)에 민감하게 반응한다. "
                "거시 환경이 불안정하면 보수적으로 판단하고, 안정적이면 긍정적으로 본다."
            )
            detail = (
                f"USD/KRW: {ctx.get('usd_krw', 'N/A')}\n"
                f"VIX proxy: {ctx.get('vix_proxy', 'N/A')}\n"
                f"국채 3년: {ctx.get('treasury_3y', 'N/A')}\n"
                f"시장 regime: {ctx.get('market_regime', 'N/A')}"
            )

        elif persona == "institution":
            system_msg = (
                "너는 국내 기관투자자(연기금/자산운용사) 관점의 한국 주식 분석가야. "
                "DART 공시, 재무 fundamentals, 섹터 집중도 리스크에 집중한다. "
                "섹터 집중도가 높거나 공시 정보가 불충분하면 신중하게 접근한다."
            )
            detail = (
                f"신호: {ctx.get('signal', 'N/A')} (confidence: {confidence})\n"
                f"섹터: {ctx.get('sector', 'N/A')}\n"
                f"섹터 집중도: {ctx.get('sector_concentration', 'N/A')}\n"
                f"DART 근거 수: {ctx.get('dart_evidence_count', 0)}\n"
                f"risk_flag: {ctx.get('risk_flag', 'N/A')}\n"
                f"rationale: {ctx.get('rationale', 'N/A')}"
            )

        else:  # retail
            system_msg = (
                "너는 국내 개인투자자(소매투자자) 관점의 한국 주식 분석가야. "
                "뉴스 감성, 모멘텀, quant_score에 민감하게 반응한다. "
                "최근 뉴스가 긍정적이고 quant_score가 높으면 낙관적으로 본다."
            )
            detail = (
                f"신호: {ctx.get('signal', 'N/A')} (confidence: {confidence})\n"
                f"quant_score: {ctx.get('quant_score', 'N/A')}\n"
                f"뉴스 감성: {ctx.get('news_sentiment', 'N/A')}\n"
                f"rationale: {ctx.get('rationale', 'N/A')}"
            )

        user_content = (
            f"종목: {name} ({ticker})\n"
            f"제안 액션: {action} ({weight}%)\n\n"
            f"관련 정보:\n{detail}\n\n"
            "위 정보를 바탕으로 이 종목에 대한 너의 관점을 밝혀라.\n"
            "반드시 아래 JSON 형식으로만 답하라 (200자 이내):\n"
            '{"argument": "분석 의견 (한국어, 2문장 이내)", '
            '"stance": "bullish" 또는 "bearish" 또는 "neutral", '
            '"key_factors": ["핵심요인1", "핵심요인2"]}'
        )

        return [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_content},
        ]

    # ------------------------------------------------------------------
    # LLM 호출
    # ------------------------------------------------------------------

    def _run_persona(self, persona: str, messages: list) -> dict:
        """페르소나 LLM 호출. 실패 시 deterministic fallback 반환."""
        try:
            from connectors.llm_router import call_llm
            result = call_llm(messages, temperature=0.3, max_tokens=200)
            raw = result.get("content", "")
            model_used = result.get("model", "unknown")
            parsed = self._parse_json_safe(raw)
            if parsed:
                stance = parsed.get("stance", "neutral")
                if stance not in ("bullish", "bearish", "neutral"):
                    stance = "neutral"
                return {
                    "argument": parsed.get("argument", ""),
                    "stance": stance,
                    "key_factors": parsed.get("key_factors", []),
                    "model_used": model_used,
                }
            print(f"[DebateAgent] {persona} JSON 파싱 실패 → fallback")
        except Exception as e:
            print(f"[DebateAgent] {persona} LLM 호출 실패: {e}")

        return {
            "argument": f"{persona} 분석 불가 (LLM 호출 실패)",
            "stance": "neutral",
            "key_factors": [],
            "model_used": None,
        }

    def _moderate(self, ticker: str, order: dict, arguments: dict) -> dict:
        """Moderator: 3 argument 종합, consensus_score 계산."""
        stances = [arguments[p].get("stance", "neutral") for p in self.PERSONAS]

        # consensus_score: 완전 합의=1, 완전 불일치=0
        # 간단 계산: 같은 stance끼리 pair 수 / 총 pair 수
        total_pairs = 3  # C(3,2)
        matched = sum(
            1
            for i in range(len(stances))
            for j in range(i + 1, len(stances))
            if stances[i] == stances[j]
        )
        consensus_score = round(matched / total_pairs, 2)

        # LLM Moderator 호출
        name = order.get("name", ticker)
        action = order.get("action", "buy")
        args_text = "\n".join(
            f"- {p.capitalize()}: [{arguments[p].get('stance', 'neutral')}] {arguments[p].get('argument', '')}"
            for p in self.PERSONAS
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "너는 투자 심의 Moderator야. "
                    "외국인/기관/개인 세 관점의 의견을 종합하여 객관적인 결론을 도출한다. "
                    "최종 거래 판정은 변경하지 않으며 분석 요약만 제공한다."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"종목: {name} ({ticker}), 제안 액션: {action}\n\n"
                    f"3자 의견:\n{args_text}\n\n"
                    "위 의견을 종합하여 주요 합의점과 이견을 요약하라 (300자 이내, 한국어).\n"
                    "JSON 형식 없이 평문으로 답하라."
                ),
            },
        ]

        summary = ""
        try:
            from connectors.llm_router import call_llm
            result = call_llm(messages, temperature=0.3, max_tokens=300)
            summary = result.get("content", "").strip()
            print(f"[DebateAgent] {ticker} Moderator 종합 완료 (consensus={consensus_score})")
        except Exception as e:
            print(f"[DebateAgent] {ticker} Moderator LLM 실패: {e}")
            bullish = stances.count("bullish")
            bearish = stances.count("bearish")
            if bullish > bearish:
                summary = f"3자 중 {bullish}명 강세. consensus_score={consensus_score}."
            elif bearish > bullish:
                summary = f"3자 중 {bearish}명 약세. consensus_score={consensus_score}."
            else:
                summary = f"3자 의견 혼재. consensus_score={consensus_score}."

        return {"summary": summary, "consensus_score": consensus_score}

    # ------------------------------------------------------------------
    # 유틸
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json_safe(text: str) -> dict | None:
        """텍스트에서 첫 번째 JSON 객체를 balanced bracket 방식으로 추출."""
        s = text.find("{")
        if s == -1:
            return None
        depth, e = 0, s
        for ci, ch in enumerate(text[s:], s):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            if depth == 0:
                e = ci + 1
                break
        try:
            return json.loads(text[s:e])
        except json.JSONDecodeError:
            return None
