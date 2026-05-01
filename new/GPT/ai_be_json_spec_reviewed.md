# AI-BE JSON 스펙 — BE 팀 연습용 1페이지 요약 (검토 반영본)

> **SSOT**: `api_contracts.md` (`C4 / C5 / C8 / C9 / C10`)
> **불변 원칙**:
> - `target_weights = PPO Allocator`
> - `order_deltas = Portfolio Manager`
> - `FDA = final approval` (`can_change_weight=false`)
> **상태**: MVP freeze (`mock / paper` 연동용). 실거래 production immutable freeze는 아님.

---

## 1. 연동 범위

### BE가 우선 맞춰야 하는 핵심 3개
- `C8 PortfolioDeltaPlannerContract`
- `C9 FDADecisionContract`
- `C10 ExecutionFeedbackContract`

### 내부/감사 trail용 참조 2개
- `C4 SharedMessagePoolContract`
- `C5 AgentReportContract`

---

## 2. 장중 연동 흐름

```text
AI → BE:  portfolio_patch (C8)  →  final_decision (C9)
BE → AI:  execution_feedback (C10)
           ├─ execution_report
           └─ feedback_record
```

> 실무적으로는 `execution_report`와 `feedback_record`를 하나의 envelope로 묶어서 전달해도 된다.
> 다만 스키마 기준 SSOT는 `C10 ExecutionFeedbackContract`이다.

---

## 3. Portfolio Patch (C8)

**생성 주체**: Portfolio Manager  
**FDA 수정 가능 여부**: 불가 (`fda_may_edit=false`)

```json
{
  "portfolio_patch_id": "PP-20260410-001",
  "based_on_ts": "2026-04-10T09:01:00+09:00",
  "target_weights": {
    "005930": 0.08,
    "000660": 0.06
  },
  "order_deltas": [
    {"ticker": "005930", "side": "buy", "qty": 12, "reason": "rebalance"},
    {"ticker": "000660", "side": "sell", "qty": 5, "reason": "risk_reduce"}
  ]
}
```

검증 규칙:
- `price_gt_zero`: 가격 > 0
- `qty_gt_zero`: 수량 > 0
- `ticker_in_universe`: **현재 trade universe 내 종목코드**
- `order_value_lt_position_limit`: 주문금액 < 포지션 리밋
- `daily_pnl_check`: `daily_pnl <= -risk_config.daily_loss_threshold`

추가 규칙:
- `excluded_holding_policy`: `weight=0` 이면 자동 `sell delta` 생성
- `cold_path_exit_trigger`: `quant_anomaly`, `risk_veto`

---

## 4. Final Decision (C9)

**생성 주체**: FDA  
**중요**: `target_weights`와 `order_deltas`는 **read-only echo** 이며 FDA가 수정하면 안 된다.

```json
{
  "decision_id": "DEC-20260410-0901-001",
  "approved": true,
  "target_weights": {"005930": 0.08, "000660": 0.06},
  "order_deltas": [
    {"ticker": "005930", "side": "buy", "qty": 12, "reason": "rebalance"},
    {"ticker": "000660", "side": "sell", "qty": 5, "reason": "risk_reduce"}
  ],
  "veto_reason": null,
  "risk_overrides": [],
  "confidence": 0.77,
  "expiry": "2026-04-10T09:02:00+09:00"
}
```

규칙:
- `can_change_weight`: `false`
- `must_include_reasoning`: `true`
- `veto_if_uncertain`: `true`
- `dependency_wait`: `all active agents or timeout`

주의:
- `risk_overrides`는 **audit metadata 전용**이며, 실제 리스크 정책 변경 경로로 사용 금지
- FDA가 `order_deltas`를 수정 시도하면 `ILLEGAL_DELTA_MODIFICATION_ATTEMPT`
- 계약서에는 `must_include_reasoning=true`가 있지만, 현재 `final_decision` payload의 고정 필드에는 별도 `reasoning` 필드가 없다. 따라서 reasoning 본문은 상위 message/report trail 또는 내부 로그에서 참조될 수 있다고 이해하면 안전하다.

---

## 5. Execution Feedback (C10)

### 5-1. execution_report

```json
{
  "order_plan_id": "OP-20260410-0901-001",
  "submitted_at": "2026-04-10T09:01:05+09:00",
  "status": "partial_filled",
  "fills": [
    {"ticker": "005930", "side": "buy", "qty": 10, "avg_fill_price": 81200.0, "fill_ts": "2026-04-10T09:01:06+09:00"}
  ],
  "estimated_cost": 1350.5,
  "realized_slippage": 8.2
}
```

### 5-2. feedback_record

```json
{
  "kb_message_id": "MSG-20260410-EXEC-001",
  "pnl_contribution": 152340.0,
  "execution_shortfall": -8100.0,
  "lesson_stub": "partial fill로 execution_shortfall 발생"
}
```

mode:
- `mock`: 주문 미발송, mock 체결 생성
- `paper`: KIS 모의투자 서버
- `live`: KIS 운영 서버 (`live_enabled=true` 필수)

안전장치:
- `if approved=false`: **주문 제출 금지**
- `kill_switch`: `daily_pnl <= -daily_loss_threshold` → 전량 시장가 청산 + `EMERGENCY_HALT`
  - `risk_config.yaml`의 `daily_loss_threshold`는 **양수 절대값(0.05)**
- `reconciliation`: 매 실행 사이클 후 시스템 포지션 vs KIS 실제 잔고 비교
- `audit_log`: JSON Lines 포맷, 최소 1년 보관
- `live_enabled=false AND execution_mode=live` → `REJECTED`

---

## 6. Shared Message Pool (C4)

**전달 보장**: `at-least-once`

```json
{
  "message_id": "MSG-20260410-0901-007",
  "content": "외국인 순매도 급증",
  "cause_by": "krx_investor_flow",
  "sent_from": "RiskAgent",
  "send_to": null,
  "priority": "urgent",
  "confidence": 0.82,
  "reasoning": "foreign_net_sell_critical threshold 초과",
  "evidence_ids": ["EVT-20260410-krx-flow-005930"],
  "uncertainty": "low",
  "prediction": null,
  "risk_level": "high",
  "timestamp": "2026-04-10T09:01:03+09:00",
  "ttl": 60,
  "expires_at": "2026-04-10T09:02:03+09:00",
  "scope": "ticker:005930",
  "event_id": "EVT-20260410-krx-flow-005930",
  "supersedes": null,
  "action_type": "alert",
  "portfolio_patch_id": null
}
```

참고:
- `send_to`는 `string | list | null`
- `supersedes`로 이전 메시지 교체 가능
- `action_type` enum: `signal | alert | veto_recommendation | regime_change | resolution`

---

## 7. Agent Report (C5) — 참고용

정의된 `report_type`:
- `news_signal`
- `risk_warning`
- `quant_signal`
- `investor_flow_alert`
- `theme_score`

### risk_warning 예시
```json
{
  "report_type": "risk_warning",
  "payload": {
    "stance": "risk_reduce",
    "risk_level": "high",
    "macro_note_ref": "NOTE-MACRO-20260410-001",
    "micro_note_ref": "NOTE-MICRO-005930-20260410-001"
  }
}
```

### quant_signal 예시
```json
{
  "report_type": "quant_signal",
  "payload": {
    "scores": [{"ticker": "005930", "score": 0.81, "confidence": 0.74}],
    "anomalies": [{"ticker": "005930", "anomaly_type": "volume_spike", "score": 2.9}],
    "top10_candidates": ["005930", "000660"],
    "filter": {"min_confidence": 0.30}
  }
}
```

---

## 8. ID 형식

| ID | 형식 | 예시 |
|---|---|---|
| `portfolio_patch_id` | `PP-{yyyymmdd}-{seq}` | `PP-20260410-001` |
| `decision_id` | `DEC-{yyyymmdd}-{hhmm}-{seq}` | `DEC-20260410-0901-001` |
| `message_id` | `MSG-{yyyymmdd}-{hhmm}-{seq}` | `MSG-20260410-0901-007` |
| `order_plan_id` | `OP-{yyyymmdd}-{hhmm}-{seq}` | `OP-20260410-0901-001` |
| `event_id` | `EVT-{yyyymmdd}-{source}-{scope}` | `EVT-20260410-krx-flow-005930` |

---

## 9. BE 팀 전달 문구

> AI-BE 연습용 JSON 스펙은 `api_contracts.md`를 SSOT로 하여 `C4/C5/C8/C9/C10` 기준으로 freeze한다.  
> BE가 우선 연동할 핵심은 `C8/C9/C10`이고, 장중 주문 경로는  
> **`portfolio_patch → final_decision → execution_feedback`** 이다.  
> `target_weights=PPO`, `order_deltas=Portfolio Manager`, `final_approval=FDA` 원칙은 절대 불변이다.
