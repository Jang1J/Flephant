---
name: paper-trending
description: "논문 트렌드 조사 스킬. 금융 ML, 퀀트 트레이딩, multi-agent 시스템 분야의 최신 논문 트렌드를 조사한다. '논문 트렌드', '최신 연구', 'paper trending', 'SOTA', '최신 논문', 'trending papers' 키워드 시 사용. 특정 주제 심층 조사는 /agent-research가 처리한다."
argument-hint: "[분야: quant | multi-agent | financial-ml | all]"
user-invocable: true
agent: analyst
allowed-tools: Read, Grep, Glob, Bash, WebSearch, WebFetch
---

# Paper Trending -- 논문 트렌드 조사 스킬

금융 ML, 퀀트 트레이딩, multi-agent 시스템 분야의 최신 논문 트렌드를 파악한다.

## 역할 구분

- **이 스킬**: 분야 전체의 트렌드 파악, 주요 논문 목록 정리
- **/agent-research**: 특정 주제에 대한 심층 조사, 프로젝트 적용 분석

## 워크플로우

### Phase 1: 분야 선택
사용자 요청에서 분야를 파악한다:

| 분야 | 키워드 | 검색 대상 |
|------|--------|----------|
| quant | 퀀트, 알고리즘 트레이딩, backtest | algorithmic trading, portfolio optimization |
| multi-agent | 멀티에이전트, LLM agent, agentic | multi-agent systems, LLM-based agents |
| financial-ml | 금융 ML, 주가 예측, 시계열 | financial ML, stock prediction, time series |
| all | 전체, 트렌드 | 위 3개 전체 |

### Phase 2: 트렌드 수집
1. arxiv 최근 3개월 키워드 검색
2. 인용 수 / 관심도 높은 논문 우선
3. 프로젝트 관련도 평가

### Phase 3: 트렌드 보고서

```markdown
## 트렌드 보고서: [분야] (YYYY-MM 기준)

### 핵심 트렌드
1. **트렌드 1**: [설명] — 대표 논문 [제목]
2. **트렌드 2**: [설명] — 대표 논문 [제목]

### 주요 논문 (최근 3개월)
| # | 제목 | 분야 | 핵심 기여 | 프로젝트 관련도 |
|---|------|------|----------|--------------|

### Elephant Lab 시사점
- 시사점 1: [현재 설계와 비교]
- 시사점 2: [개선 아이디어]
```

## 프로젝트 연관 분야

Elephant Lab과 직접 관련된 연구 키워드:
- Cross-sectional momentum / reversal in Korean market
- Chart pattern recognition with CNN
- Multi-agent trading systems
- LLM-augmented financial decision making
- Uncertainty quantification in portfolio management
- Walk-forward validation for financial ML
