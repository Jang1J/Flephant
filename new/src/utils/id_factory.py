"""ID 생성 팩토리. architecture.md §14 ID Convention + api_contracts.md identity 블록.

Prefix 리스트 (SSOT):
  EVT:    event_id (일반 이벤트)
  MSG:    message_id (Blackboard 메시지)
  DEC:    decision_id (FDA 결정)
  PP:     portfolio_patch_id (C8)
  BUNDLE: mode_b_bundle_id (Mode B 배포 번들)
  BT:     backtest_id (C12)
  RPT:    agent_report_id (C5) + replay_trace_ref (C13 ReplayRunner)
  FCC:    failure_case_card_id (C12 FailureCase)
  RGC:    regression_case_id (C12 v3 신규, RegressionCase)
  OP:     order_plan_id (C10)

포맷: {PREFIX}-{YYYYMMDD}-{UUID8}
  예: EVT-20260419-A8F3D91C
"""
from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")


def _today_str() -> str:
    return datetime.now(_KST).strftime("%Y%m%d")


def _uuid8() -> str:
    return uuid.uuid4().hex[:8].upper()


def _make_id(prefix: str) -> str:
    return f"{prefix}-{_today_str()}-{_uuid8()}"


def generate_event_id() -> str:
    """이벤트 ID. 예: EVT-20260419-A8F3D91C"""
    return _make_id("EVT")


def generate_message_id() -> str:
    """블랙보드 메시지 ID. 예: MSG-20260419-A8F3D91C"""
    return _make_id("MSG")


def generate_decision_id() -> str:
    """FDA 결정 ID. 예: DEC-20260419-A8F3D91C (C9)."""
    return _make_id("DEC")


def generate_portfolio_patch_id() -> str:
    """포트폴리오 패치 ID. 예: PP-20260419-A8F3D91C (C8)."""
    return _make_id("PP")


def generate_bundle_id() -> str:
    """Mode B 배포 번들 ID. 예: BUNDLE-20260419-A8F3D91C."""
    return _make_id("BUNDLE")


def generate_backtest_id() -> str:
    """백테스트 실행 ID. 예: BT-20260419-A8F3D91C (C12)."""
    return _make_id("BT")


def generate_report_id() -> str:
    """에이전트 리포트 ID. 예: RPT-20260419-A8F3D91C (C5)."""
    return _make_id("RPT")


def generate_failure_case_card_id() -> str:
    """실패 케이스 카드 ID. 예: FCC-20260419-A8F3D91C (C12 FailureCase)."""
    return _make_id("FCC")


def generate_regression_case_id() -> str:
    """회귀 케이스 ID. 예: RGC-20260419-A8F3D91C (C12 RegressionCase, v3 신규).

    architecture.md §14: RGC-{yyyymmdd}-{UUID8}. id_factory._make_id("RGC") SSOT.
    """
    return _make_id("RGC")


def generate_order_plan_id() -> str:
    """주문 계획 ID. 예: OP-20260419-A8F3D91C (C10).

    architecture.md §14: OP-{yyyymmdd}-{UUID8}. id_factory._make_id("OP") SSOT.
    """
    return _make_id("OP")


def generate_agent_performance_id() -> str:
    """에이전트 성능 롤업 ID. 예: APM-20260421-A8F3D91C (C18).

    api_contracts.md identity 블록 SSOT: APM-{yyyymmdd}-{UUID8}.
    2026-04-21 S2-1 감사 반영: seq 충돌 방지를 위해 UUID8 기반으로 확정.
    """
    return _make_id("APM")


def generate_replay_id() -> str:
    """ReplayRunner 실행 ID. 예: RPT-20260501-A8F3D91C (C13 ReplayRunner).

    api_contracts.md identity 블록 SSOT: RPT-{yyyymmdd}-{UUID8}.
    replay_trace_ref 필드에 사용. S3-9 ReplayRunner 실구현 신설.
    2026-05-01 S3 Tier 1 수정: RPT → RPT (spec 불일치 정정).
    """
    return _make_id("RPT")


def generate_pa_id() -> str:
    """PerformanceAnalyzer 실행 ID. 예: PA-20260501-A8F3D91C (C13 PerformanceAnalyzer).

    api_contracts.md §14 ID Convention: PA-{yyyymmdd}-{UUID8}.
    S3-9 PerformanceAnalyzer 실구현 신설.
    """
    return _make_id("PA")


def generate_deploy_id() -> str:
    """배포 실행 ID. 예: DEPLOY-20260501-A8F3D91C (C14 ModeBDeployer).

    api_contracts.md §14 ID Convention: DEPLOY-{yyyymmdd}-{UUID8}.
    S3-10 ModeBDeployer 실구현 신설.
    """
    return _make_id("DEPLOY")


def generate_kb_id() -> str:
    """Knowledge Base 항목 ID. 예: KB-20260501-A8F3D91C (S3-11 KnowledgeBase Layer 5).

    architecture.md §14 ID Convention: KB-{yyyymmdd}-{UUID8}.
    """
    return _make_id("KB")
