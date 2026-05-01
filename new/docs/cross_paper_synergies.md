# 논문 간 교차 활용 시너지

> 6개 논문을 교차 분석하여 발견한 미활용 시너지 기법 5가지

## 1. AlphaAgent AST + RD-Agent Knowledge Base 통합

**출처**: AlphaAgent(AST 독창성) × RD-Agent(Knowledge Base)

현재 별개 시스템이지만 통합 가능:

```
Knowledge Base 저장 시:
  (hypothesis, code, result, AST) ← AST 추가
  → 새 팩터 생성 시 KB 내 모든 AST와 자동 비교
  → 중복(유사도 ≥ 0.99)이면 바로 거부
  → 별도 Factor Zoo 유지 불필요 — KB가 곧 Factor Zoo
```

**효과**: 팩터 저장소와 독창성 검증이 하나로 통합. 유지보수 단순화.

**Sprint**: Sprint 3 S3-11

## 2. AAPM 반복 정제 + TradeXpert 종목 비교 결합

**출처**: AAPM(N-round refinement, +3.8% SR) × TradeXpert(Relaxed Sort, RankIC 0.12)

```
FDA 판단을 3라운드 정제 루프:
  Round 1: 각 에이전트 보고서 종합 → 1차 판단
  Round 2: RAG로 과거 유사 상황 검색 → 판단 보정
  Round 3: 종목 간 pairwise 비교 (Relaxed Sort) → 최종 Top-K

  max 3라운드 bounded (MetaGPT)
```

**효과**: FDA의 판단 품질이 매 라운드 개선. 종목 선정과 판단 정제가 통합.

**Sprint**: Sprint 2 S2-9 + Sprint 3 S3-3

## 3. MetaGPT 메시지 형식으로 RD-Agent 경험 저장

**출처**: MetaGPT(구조화된 메시지) × RD-Agent(Knowledge Base 삼중체)

```json
{
  "content": "반도체 -2% 시 SELL이 정답이었다",
  "cause_by": "EvalAgent_backtest",
  "sent_from": "EvalAgent",
  "result": "AR +2.3%",
  "hypothesis": "가격수축+거래량감소=반등전조",
  "ast_hash": "abc123",
  "timestamp": "2026-04-05T21:00:00"
}
```

**효과**: 에이전트 간 실시간 소통(Message Pool)과 장기 지식 축적(KB)이 같은 형식 → 통일된 데이터 구조로 검색/활용 용이.

**Sprint**: Sprint 2 S2-1 (완료)

## 4. AlphaGAT Cross-Asset Attention × Multi-scale 계층화

**출처**: AlphaGAT(Cross-Asset MHA) × AlphaGAT+MSNet(Multi-scale decomposition)

```
현재 계획: Cross-Asset Attention을 단일 스케일에서 적용
개선: 스케일별로 따로 적용 → 계층적 통합

1분 스케일:  종목 간 미시 연동 (급락 전파, 틱 동조)
5분 스케일:  섹터 내 단기 추세 연동 (반도체 섹터 동반 하락)
30분 스케일: 섹터 간 리스크 연동 (금융→산업재 전이)
60분 스케일: 시장 전체 regime 연동 (전체 하방 압력)

→ 각 스케일의 Cross-Asset 시그널을 Hierarchical Fusion
→ 미시 시그널은 빠른 반응, 거시 시그널은 큰 방향
```

**효과**: 단일 스케일보다 풍부한 종목 간 관계 포착. 1분봉의 노이즈를 다중 스케일에서 필터링.

**Sprint**: Sprint 4+ (backlog, R4 STGNN과 연계)

## 5. 동적 LLM 라우팅 (TradeXpert MoE + RD-Agent Bandit)

**출처**: TradeXpert(전문 LLM 분리) × RD-Agent(Thompson Sampling)

```
현재 계획: 이벤트 시에만 LLM 호출 (on/off 이진)
개선: Thompson Sampling이 "어떤 LLM을, 언제 호출할지" 적응적으로 결정

x_t = [시장 상태, 이벤트 유형, 시간대, 변동성]
A = {Kanana-o만, GPT-4o만, 둘 다, 호출 안 함}

예시:
  한국어 뉴스 감지 → Kanana-o (한국어 전문)
  팩터 이상 감지  → GPT-4o (추론/분석)
  급락 + 뉴스 동시 → 둘 다 호출
  평상시          → 호출 안 함 (퀀트만)
  
Bayesian posterior로 각 선택지의 성과를 추적 → 점점 최적화
```

**효과**: LLM 호출 비용 최적화 + 상황별 최적 LLM 자동 선택. on/off보다 세밀한 제어.

**Sprint**: Sprint 2 S2-2 (완료)
