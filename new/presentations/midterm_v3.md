# KOSPI 1분봉 연구형 Decision OS: 중간발표 패키지 v3

> 파일 경로: `new/presentations/midterm_v3.md`
> 작성일: 2026-04-12 (D-5)
> 발표일: 2026-04-17 (금) / 제출 마감: 2026-04-16 (목) 17:00

---

## 섹션 1: 메타 & 현재 상태

### 발표 정보

| 항목 | 내용 |
|---|---|
| 발표 제목 (AI 파트) | KOSPI 1분봉 연구형 Decision OS |
| 부제 | Event-aware Multi-Agent System for Korean Equity |
| 발표 시간 | 10분 전체 / AI 파트 약 6분 / Q&A 2분 |
| 발표일 | 2026-04-17 (금) |
| 제출 마감 | 2026-04-16 (목) 17:00 (마감 이후 수정 = 감점) |
| 평가 기준 | 발표 기술, 슬라이드 레이아웃, 시간 준수, Peer review (최고/최저 제외 평균) |

### 현재 상태 (정직 프레이밍)

| 영역 | 상태 | 수치/규모 |
|---|---|---|
| 설계 문서 | 완비 | `architecture.md` 99KB + `architecture_visual.md` 90KB |
| API 계약서 | 완비 | `api_contracts.md` 37KB, C1~C16 총 16개 계약 |
| Config | 완비 | yaml 10개 (risk / dual_source / universe / dart_rules / news_filter / sentiment / spam / manipulation 등) |
| 논문 분석 | 완비 | 6편 (AAPM / AlphaGAT / MetaGPT / RD-Agent / TradeXpert / AlphaAgent) |
| 하네스 | 완비 | 에이전트 10개 + 스킬 23개 + preamble 10섹션 + rules 3개 |
| 코드 구현 | 설계 단계 완료, Sprint 0 착수 예정 | .py 파일 0개 |

**프레이밍 원칙 (발표자 필독)**: 코드 0줄은 숨길 일이 아니다. 12주 종설 6주 시점에서 설계 완료 + Sprint 0 착수 준비 = 정상 진행. 발표 초점은 "무엇을 만들었냐"가 아니라 "왜 이 구조인가". 설계가 잘못되면 코드는 기술 부채가 된다. 이 순서가 맞다.

### 교수님 피드백 1:1 매핑 (2026-04-03)

| 피드백 | 반영 위치 | 설계 근거 |
|---|---|---|
| 1. 아이디어/전략 좋다 | 방향 유지 | v3 재설계 전면 반영 |
| 2. 데이터 주기를 분단위로 | Hot Path 매 1분, `risk_config.yaml: hot_path_interval_sec: 60` | KIS WebSocket 1분봉, 30개 종목 × 0.3ms = 9ms |
| 3. 즉시 반응 구조 | Hot Path <100ms + Cold Path 이벤트 트리거 | LightGBM 단독, LLM 미호출 |
| 4. 멀티에이전트 강점 | News / Risk / Debate / FDA 역할 분리 + Blackboard 통신 | MetaGPT SOP 기반 |
| 5. cause 중심 발표 | FDA `reason_code` 필드, KP-1~7 킬러 문장 | `api_contracts.md` C9 FDADecisionContract |

---

## 섹션 2: 스토리라인 (cause-driven 8단계)

이 8단계는 발표 흐름의 뼈대다. 각 단계마다 "왜 말하는가"를 명시했다.

### Stage 1: 문제 정의 (Why now)

**말할 것**: "KOSPI 30종목을 1분봉으로 운영하면 두 가지 문제가 동시에 온다. 첫째, 정량 모델은 빠르지만 뉴스 이벤트에 맹목적이다. 둘째, 뉴스 기반 판단은 이유가 있지만 느리고 설명이 없다. 기존 ML 트레이딩 서비스는 둘 중 하나만 한다."

**왜 말하는가**: 청중이 "왜 복잡한 구조가 필요한가"를 먼저 납득해야 이후 설계가 당연해진다.

### Stage 2: 우리의 답 (The OS metaphor)

**말할 것**: "우리 시스템은 설계 철학이 아니라 실제 제어 흐름이다. 빠른 경로(Hot Path)와 이벤트 경로(Cold Path)를 나누고, 매일 밤 스스로 모델을 바꾼다. 이름이 'OS'인 이유는, 에이전트가 스케줄러/메모리/승인/로그 같은 OS 역할을 맡기 때문이다."

**왜 말하는가**: KP-2 "논문 조립형이 아니라 구현 가능한 OS"를 직접 제시. 청중이 "OS라는 비유가 왜 정확한가"를 이해하게 한다.

### Stage 3: 전체 구조 (2-mode, 3-path)

**말할 것**: "Mode A는 장중(09:00~15:30), Mode B는 장마감(18:00~22:00). Mode A 안에는 두 경로. Hot Path는 1분마다 LightGBM이 30종목 중 상위 10개를 추리고 PPO Allocator가 비중을 정한다. Cold Path는 뉴스/공시/수급 이벤트 발생 시만 작동한다."

**왜 말하는가**: 교수님 피드백 3번(즉시 반응)과 4번(멀티에이전트 강점)을 동시에 커버. "모든 판단을 LLM이 하지 않는다"는 점을 명확히 한다.

### Stage 4: Hot Path 상세 (속도의 근거)

**말할 것**: "LightGBM 한 종목당 0.3ms. 30종목 = 9ms. PPO Allocator 비중 계산 ~5ms. FDA approve/veto ~1ms. 합계 <20ms. 100ms 기준 80ms 여유. LLM 호출 없이 이게 가능하다."

**왜 말하는가**: "즉시 반응"이 구체적인 수치로 뒷받침된다는 것을 보여준다. 추상 주장 아니고 계산 근거.

### Stage 5: Cold Path + 멀티에이전트 협업 (why multi-agent)

**말할 것**: "이벤트가 오면 세 에이전트가 병렬로 분석한다. News Agent는 텍스트 감성, Risk Agent는 수급/변동성, Debate Agent는 두 시각 충돌을 정리한다. FDA가 세 에이전트의 결과를 받아 approve/veto를 낸다. 판단 근거는 `reason_code` 필드에 남는다."

**왜 말하는가**: 교수님 피드백 4번(멀티에이전트 강점) + 5번(cause 중심). FDA reason_code가 "cause 시각화"의 실체다.

### Stage 6: Dual-Source (차별점 2번)

**말할 것**: "뉴스와 커뮤니티가 같은 방향이면 신호 강도 올린다. 방향이 반대면 불확실성 신호. 이 상황에서 포지션을 자동 축소한다. 커뮤니티는 뉴스보다 1~2일 lagged 반영이라 동일 가중치 금지."

**왜 말하는가**: 국장 네이티브 차별점. 국내 커뮤니티 데이터를 체계적으로 쓰는 구조는 흔치 않다.

### Stage 7: Mode B 자동 진화 (차별점 3번)

**말할 것**: "장마감 후 Alpha Factor Engine이 새 factor를 만들고, Co-STEER가 LightGBM을 재학습하고, Backtest Agent가 회귀 위험을 차단한다. 이 세 단계를 통과한 것만 22:00에 배포 게이트를 넘는다. KP-7: 내일의 시스템은 오늘과 다르다."

**왜 말하는가**: "변화 = 개선이 아니라 변화 + 검증 = 개선"이라는 차별점 3번을 설명. RD-Agent 논문 기반 Co-STEER가 이 파트의 핵심.

### Stage 8: 설계 자산 + Sprint 계획 (현실 앵커)

**말할 것**: "설계 자산: architecture.md 99KB, api_contracts.md 37KB, yaml 10개, 논문 6편, 에이전트 10개. 코드는 Sprint 0부터 시작. 기말발표까지 MVP = Sprint 0~2 완성. Sprint 3~5는 Phase 2. 구현 순서도 설계 근거가 있다."

**왜 말하는가**: 투명성. 코드 0줄을 숨기지 않되, 설계 완비 = 탄탄한 착지점임을 보여준다.

---

## 섹션 3: 슬라이드 세부 (15장)

> 기준: AI 파트 6분 = 360초. 슬라이드 1장당 평균 24초. 여유 포함 15장.
> 각 슬라이드: 제목 / 핵심 bullet 3개 / 발표 대본 2~3 문장 / 시각자료 힌트.
> [발표자 노트] 안 시간 배분은 기준점이고, 실제 발표에서 +-5초는 무방.

---

### S01: 타이틀 슬라이드

**제목**: KOSPI 1분봉 연구형 Decision OS

**핵심 bullet**:
- Event-aware Multi-Agent System for Korean Equity
- 1분봉 + 이벤트 트리거 + 매일 밤 자동 진화
- 팀원 이름 + 지도교수 + 2026-04-17

**발표 대본**: "오늘 발표할 시스템 이름은 'KOSPI 1분봉 연구형 Decision OS'입니다. OS라는 이름을 붙인 이유는 발표 중에 설명하겠습니다."

**시각자료 힌트**: 배경 심플. 제목 크게. 부제 작게.

[발표자 노트] 10초 이내. 팀 소개는 10초 안에 끝낸다.

---

### S02: 문제 정의

**제목**: 두 가지 문제, 동시에

**핵심 bullet**:
- 정량 모델: 빠르지만 이벤트에 맹목적 (속도 O, 맥락 X)
- 뉴스 기반 판단: 이유 있지만 느리고 설명 없음 (맥락 O, 속도 X)
- 기존 ML 트레이딩: 둘 중 하나만 해결

**발표 대본**: "KOSPI 30종목을 1분봉으로 운영하면 두 가지 문제가 동시에 온다. 빠른 모델은 공시 하나에 멍하고, 뉴스를 읽는 시스템은 100ms 안에 판단을 못 낸다. 둘을 동시에 푸는 구조가 우리 설계의 출발점이다."

**시각자료 힌트**: 좌우 2분할. 왼쪽 "정량 모델 (빠름 / 맹목)", 오른쪽 "뉴스 판단 (느림 / 이유 있음)". 가운데 "?" 또는 갈등 표시.

[발표자 노트] 문제 정의 0:40 중 40초. 짧고 날카롭게.

---

### S03: 우리의 답 (OS 비유)

**제목**: 설계 철학이 아니라 실제 제어 흐름

**핵심 bullet**:
- Hot Path: 매 1분, LLM 없이 <100ms
- Cold Path: 이벤트 시만 LLM 개입 (10~30초)
- Mode B: 매일 밤 자동 진화

**발표 대본**: "우리 시스템을 OS라고 부르는 이유는, 에이전트가 스케줄러/승인/로그 같은 OS 역할을 나눠 맡기 때문이다. 설계 문서가 아니라 실제 제어 흐름이 있다. 논문 조립형이 아니라 구현 가능한 OS다."

**시각자료 힌트**: `new/visuals/01_overall_architecture.svg` (신규 생성 필요, 섹션 6 VA-01 참고). 또는 간단한 3-box (Hot / Cold / Mode B) 수평 배치.

[발표자 노트] 전체 구조 1:10 중 30초.

---

### S04: 전체 아키텍처 (1장)

**제목**: 2 Mode × 5 Layer, 3 Path, 6 Agent

**핵심 bullet**:
- Mode A 장중: Hot Path (매 1분) + Cold Path (이벤트)
- Mode B 장마감: Alpha Factor Engine + Co-STEER + Backtest Gate
- 공통: FDA가 모든 경로의 approve/veto 담당

**발표 대본**: "전체 구조를 한 장으로 보면 이렇다. Mode A는 장중, Mode B는 장마감 후. 에이전트 6개가 경로별 역할을 분리한다. 모든 경로의 최종 판단은 FDA를 거친다."

**시각자료 힌트**: `new/visuals/01_overall_architecture.svg` (신규 생성 필요, 섹션 6 VA-01 참고). Mermaid graph TD로도 가능.

[발표자 노트] 전체 구조 1:10 중 약 40초. 구조 전체를 스캔시키는 슬라이드.

---

### S05: Hot Path 상세

**제목**: Hot Path: 1분봉, 30종목, <100ms

**핵심 bullet**:
- LightGBM: 종목당 0.3ms, 30종목 = 9ms
- PPO Allocator: 상위 10 종목 비중 결정 ~5ms
- FDA approve/veto: ~1ms, 합계 <20ms (LLM 0건)

**발표 대본**: "Hot Path는 LLM을 한 번도 호출하지 않는다. LightGBM 단독으로 30종목을 9ms에 스코어링하고, PPO가 비중을 잡고, FDA가 승인하면 끝이다. 교수님께서 요청하신 즉시 반응 구조가 이것이다."

**시각자료 힌트**: 수평 파이프라인 다이어그램. `1분봉 → LightGBM (9ms) → PPO (5ms) → PM → FDA (1ms) → 주문`. 각 박스 아래 ms 표기.

[발표자 노트] 핵심 기술 2:00 중 약 25초.

---

### S06: Hot Path 피처 설계

**제목**: 피처 42개, 감으로 잡은 값 0개

**핵심 bullet**:
- 가격 8개 (KIS), 다중스케일 20개 (AlphaGAT 논문), 수급 3개 (교수님 요구)
- 미국 4개 + 거시 2개 + Dual-Source 5개
- Alpha Factor Engine: 매일 밤 10~50개 자동 추가 (Mode B)

**발표 대본**: "피처 42개는 감이 아니라 논문과 교수님 피드백, 도메인 관행에서 각각 출처가 있다. 감으로 잡은 임계값은 0개다. 수급 3개는 교수님께서 직접 요청하신 항목이다."

**시각자료 힌트**: 표 또는 도넛 차트. 카테고리별 피처 수 + 출처 (AlphaGAT / 교수님 / KIS 등).

[발표자 노트] 약 15초. 피처 설명은 짧게.

---

### S07: Cold Path + 멀티에이전트 협업

**제목**: Cold Path: 이벤트가 왔을 때만

**핵심 bullet**:
- 이벤트 감지 → News / Risk / Debate Agent 병렬 분석
- FDA가 세 에이전트 결과 수신 후 approve/veto
- 판단 근거: `reason_code` 필드 (cause 시각화)

**발표 대본**: "이벤트가 오면 세 에이전트가 병렬로 분석하고 FDA에 넘긴다. 중요한 건 FDA가 비중을 직접 바꾸지 않는다는 것. FDA는 approve/veto만 낸다. 판단 근거는 reason_code에 남긴다. 이게 교수님께서 요청하신 'cause 중심' 설계다."

**시각자료 힌트**: `new/visuals/02_dual_source.svg` (신규 생성 필요, 섹션 6 VA-02 참고). 이벤트 발생에서 FDA까지 수직 플로우. 병렬 3-agent 분기 강조.

[발표자 노트] 핵심 기술 2:00 중 약 35초.

---

### S08: Dual-Source 차별점

**제목**: 뉴스와 커뮤니티가 반대로 말할 때

**핵심 bullet**:
- 뉴스: 즉시 반영, decay λ=0.8
- 커뮤니티: 1~2일 지연 반영, decay λ=0.4, peak_lag=2
- 방향 불일치 = Uncertainty Signal = 포지션 자동 축소

**발표 대본**: "뉴스와 커뮤니티가 같은 방향이면 신호를 강화한다. 반대면 포지션을 자동으로 줄인다. 커뮤니티는 뉴스보다 lag이 있어서 가중치를 낮게 설계했다. 국내 데이터 소스를 이렇게 나눠서 쓰는 구조는 기존 서비스에 없다."

**시각자료 힌트**: `new/visuals/02_dual_source.svg` (신규 생성 필요, 섹션 6 VA-02 참고). 뉴스/커뮤니티 두 스트림이 수렴/발산하는 다이어그램. 포지션 축소 효과 화살표.

[발표자 노트] 약 25초.

---

### S09: 차별점 4개 (Tier 1)

**제목**: 우리 시스템이 다른 4가지 이유

**핵심 bullet (표 형태 권장)**:

| # | 차별점 | 핵심 |
|---|---|---|
| 1 | 저지연 이중 경로 | Hot <100ms LLM 0건 + Cold 이벤트 시만 |
| 2 | Dual-Source | 뉴스 vs 커뮤니티 방향 불일치 = 포지션 축소 |
| 3 | 검증된 진화 | Backtest Gate 통과한 것만 배포 |
| 4 | 국장 네이티브 | 7개 한국 소스 직접 설계 (KIS/KRX/DART/Naver/ECOS/커뮤니티/US) |

**발표 대본**: "기존 ML 트레이딩 서비스가 '실시간 AI'를 말할 때, 우리는 몇 ms인지, 뭘 검증했는지, 왜 그 판단인지를 답할 수 있는 구조를 만들었다."

**시각자료 힌트**: 2x2 또는 세로 4행 표. 각 행에 아이콘 또는 색상 구분.

[발표자 노트] 약 20초. 표 하나로 끝낸다.

---

### S10: Mode B 자동 진화

**제목**: 내일의 시스템은 오늘과 다르다

**핵심 bullet**:
- Alpha Factor Engine: LLM이 새 factor 생성 (Mode B 전용)
- Co-STEER: LightGBM 재학습 (RD-Agent 논문 기반)
- Backtest Gate: 회귀 위험 차단 후 22:00 배포

**발표 대본**: "Alpha Factor Engine이 새 factor를 만들고, Co-STEER가 모델을 재학습하고, Backtest Agent가 회귀 위험을 차단한 뒤에야 내일의 시스템이 배포된다. 변화 = 개선이 아니다. 변화 + 검증 = 개선이다."

**시각자료 힌트**: 신규 생성 필요. `new/visuals/` 하위에 Mode B 진화 루프 다이어그램 (섹션 6 시각자료 plan 참조).

[발표자 노트] 약 30초.

---

### S11: 설계 자산 수치

**제목**: 설계 완비: 수치로 본다

**핵심 bullet**:
- 문서: `architecture.md` 99KB + `api_contracts.md` 37KB (C1~C16)
- Config: yaml 10개 + 논문 6편 분석 완료
- 하네스: 에이전트 10개 + 스킬 23개 + preamble 10섹션

**발표 대본**: "코드는 Sprint 0부터 시작한다. 그러나 설계 자산은 이미 완비다. 잘못된 설계 위에 코드를 먼저 쌓으면 기술 부채가 된다. 이 순서가 맞다."

**시각자료 힌트**: 아이콘 + 수치 나열. 문서 아이콘 / Config 아이콘 / 에이전트 아이콘 3열.

[발표자 노트] 약 20초.

---

### S12: 불변 5원칙

**제목**: 시스템이 지키는 규칙 5개

**핵심 bullet (5행)**:
- PIT-Safety: 미래 데이터 사용 금지 (snapshot 18:00 KST 이전)
- FDA can_change_weight = false: 비중은 PPO, order는 PM
- Backtest Agent Mode B 전용: 장중 개입 0
- LLM 예산: Hot Path 0회 + Cold Path Kanana-o 100회/일 + Mode B GPT-4o 전용
- 하드코딩 금지: 모든 임계값은 `risk_config.yaml`에서 로드

**발표 대본**: "이 5개 원칙은 어느 에이전트도 위반할 수 없다. 위반 시 시스템이 STOP한다. 설계 문서에 박아 놓은 것이 아니라 API 계약서 C1~C16에 필드 수준으로 반영되어 있다."

**시각자료 힌트**: 5행 표. 각 행: 원칙 이름 + 한 줄 설명 + 반영 위치.

[발표자 노트] 약 20초. 빠르게.

---

### S13: Sprint 로드맵

**제목**: Sprint 0~5: 기말까지 MVP

**핵심 bullet**:
- Sprint 0~2 (기말 MVP): 인프라 + Hot Path + Cold Path
- Sprint 3~4 (Phase 2): Mode B + 통합 최적화
- Sprint 5 (동적 유니버스): KOSPI200 watch + 후보 편입

**발표 대본**: "기말발표 목표는 Sprint 0~2 완성이다. Hot Path와 Cold Path가 작동하는 MVP. Mode B와 동적 유니버스는 Phase 2다. 전체 25개 태스크 중 현재 0%이고, 오늘부터 Sprint 0를 시작한다."

**시각자료 힌트**: 수평 타임라인 표. Sprint 0~5 각 셀에 주요 산출물. MVP 구간 색상 강조.

| Sprint | 태스크 수 | 산출물 | 상태 |
|---|---|---|---|
| 0 | 8 | `new/src/` + KIS/DART/KRX 커넥터 + rate limit | not_started |
| 1 | 4 | Quant Agent + PPO + PM + FDA (비LLM) | not_started |
| 2 | 5 | Blackboard + News/Risk/Debate Agent + FDA (LLM) | not_started |
| 3 | 3 | Alpha Factor Engine + Co-STEER + Backtest Agent | Phase 2 |
| 4 | 3 | Dual-Source + E2E + 성능 최적화 | Phase 2 |
| 5 | 2 | KOSPI200 watch + 동적 유니버스 | Phase 2 |

[발표자 노트] 약 25초.

---

### S14: 팀 합병 + 시너지

**제목**: 팀 합병: 6건 흡수, 4건 거부

**핵심 bullet**:
- 흡수 6건: Dual-Source / 커뮤니티 lag 설계 / 수급 피처 3개 / 동적 유니버스 Sprint 5 등
- 거부 4건: 설계 원칙 충돌 (PIT-Safety / FDA 역할 혼용 등)
- Sprint 5: KOSPI200 watch pool (팀 합병 최대 기여)

**발표 대본**: "팀 합병 결과 6건을 흡수하고 4건은 설계 원칙 충돌로 거부했다. 거부한 이유도 api_contracts.md에 근거가 있다. 흡수한 것 중 가장 큰 기여는 Sprint 5 동적 유니버스다."

**시각자료 힌트**: 흡수 O / 거부 X 2열 표.

[발표자 노트] Sprint+마무리 1:10 중 약 15초.

---

### S15: 마무리 + 다음

**제목**: 기말발표에서 보여줄 것

**핵심 bullet**:
- Sprint 0~2 동작 데모 (Hot Path 실시간 시그널)
- Backtest 결과 Before/After (Mode B 진화 증거)
- FDA reason_code 시각화 (cause 중심 달성)

**발표 대본**: "기말발표에서는 코드가 작동하는 것을 보여줄 것이다. Hot Path 실시간 시그널, FDA reason_code, Mode B 진화 결과. 설계가 맞았는지는 그때 검증된다."

**시각자료 힌트**: 세 항목을 아이콘 + 한 줄씩. 배경 심플.

[발표자 노트] 약 10초. 질문 받을 준비 완료.

---

## 섹션 4: Q&A 13개 전체

> 발표 후 backup 슬라이드로 준비. 최소 Q1 / Q4 / Q6는 슬라이드에 포함 권장.

---

**Q1: 유니버스 구조가 어떻게 되나?**

짧은 답변: 목표 30종목. active 20 확정, pending 10.

심화 답변: "매매 대상은 매분 LightGBM ranking 상위 10종목이다. pending 10은 Sprint 5 동적 유니버스에서 KOSPI200 watch pool에서 이벤트 편입 기준으로 채운다. 현재 active 20의 선정 기준은 `universe_config.yaml`에 정의되어 있다."

---

**Q2: 매분 Top-10은 어떻게 선정하나?**

짧은 답변: 30종목 전부 0.3ms × 30 = 9ms에 추론 후 상위 10 선택.

심화 답변: "LightGBM은 30종목 전부 매분 스코어링한다. 9ms이면 충분하다. 상위 10에 PPO Allocator가 비중을 배분하고, PM이 order_deltas를 결정한다. FDA가 최종 approve하면 주문이 나간다."

---

**Q3: 뉴스와 커뮤니티 처리를 왜 다르게 하나?**

짧은 답변: Dual-Source. 뉴스는 즉시 반영(λ=0.8), 커뮤니티는 1~2일 지연(λ=0.4, peak_lag=2). 방향 불일치 시 포지션 축소.

심화 답변: "커뮤니티 반응은 뉴스보다 평균 1~2일 늦게 피크를 찍는다. 같은 가중치를 주면 이미 반영된 정보를 두 번 쓰는 꼴이다. 방향이 반대면 불확실성 신호로 처리하고 포지션을 자동 축소한다. 이 설계는 팀 합병 시 흡수한 항목이다."

---

**Q4: LightGBM이 텍스트도 입력받나?**

짧은 답변: 아니다. 텍스트를 감성 점수(숫자)로 변환 후 피처로 입력. LightGBM은 숫자만 받는다.

심화 답변: "뉴스 텍스트는 Sentiment Agent가 점수화한다. 그 점수가 LightGBM 피처 42개 중 5개(Dual-Source 피처)로 들어간다. LightGBM에 텍스트를 직접 넣는 구조는 Hot Path <100ms와 충돌한다."

---

**Q5: 이벤트 발생 시 흐름을 설명하면?**

짧은 답변: 1차 규칙 매칭(ms) → 2차 통계 집계(LLM 없음) → 3차 Kanana-o 개입(이벤트 시만) → FDA 판단.

심화 답변: "이벤트 게이트웨이가 먼저 28개 rule-based 규칙으로 필터링한다. 대부분 여기서 처리된다. 3단계까지 가는 건 실제 중요 이벤트만이다. Kanana-o 호출은 100회/일 예산이 있어서 무분별하게 쓸 수 없다."

---

**Q6: 피처가 몇 개이고 근거가 뭔가?**

짧은 답변: 고정 42개 + Alpha Factor 10~50개 (Mode B 자동 생성).

심화 답변: "가격 8개(KIS), 다중스케일 20개(AlphaGAT 논문), 수급 3개(교수님 요구), 미국 4개, 거시 2개, Dual-Source 5개. 감으로 잡은 수치 0개. 각 피처의 출처가 설계 문서에 명시되어 있다."

---

**Q7: 피처 설계 근거는?**

짧은 답변: 가격 8 + 다중스케일 20 + 수급 3 + 미국 4 + 거시 2 + Dual-Source 5. 각 그룹 출처 별도.

심화 답변: "다중스케일 20개는 AlphaGAT 논문의 multi-scale temporal feature 방법론에서 가져왔다. 수급 3개(외국인/기관/개인 순매수)는 교수님께서 직접 요청한 항목이다. Dual-Source 5개는 팀 합병 시 추가됐다."

---

**Q8: 수급 데이터를 어디에 쓰나?**

짧은 답변: LightGBM 피처 3개(외국인/기관/개인 순매수) + Risk Fast trigger(임계값 초과 시 경고).

심화 답변: "수급 데이터는 두 경로에서 쓰인다. Hot Path에서는 피처로, Cold Path에서는 Risk Fast Agent가 임계값 초과 시 이벤트로 처리한다. 임계값은 `risk_config.yaml`에서 로드한다."

---

**Q9: 호가를 왜 안 쓰나?**

짧은 답변: Mock 환경이라 호가 의미 없음. 1분봉에서 호가는 노이즈. 실거래 전환 시 도입.

심화 답변: "현재는 paper trading이고, 1분봉 기반이다. 호가 스프레드는 틱 단위 전략에서 의미 있고, 1분봉에서는 노이즈 비율이 높다. Phase 2 실거래 전환 시 도입할 예정이고, KIS WebSocket으로 호가 수신은 기술적으로 가능하다."

---

**Q10: 규칙이 몇 개인가?**

짧은 답변: 28개 rule-based.

심화 답변: "Hot Path 3개 + 텍스트 필터 6개 + 이벤트 관리 3개 + trigger 5개 + 동적 3개 + Mode B 4개 + 안전장치 4개. 각 규칙의 임계값은 전부 `risk_config.yaml`에서 로드한다. 하드코딩 0개."

---

**Q11: 임계값 근거가 있나, 감으로 잡은 건가?**

짧은 답변: 감으로 잡은 값 0개. 논문 + 통계 표준 + 도메인 관행 + 시스템 제약 4개 출처.

심화 답변: "AlphaGAT/AlphaAgent 논문에서 가져온 것, z-score 2.5σ/3σ 같은 통계 표준, VIX 백분위 같은 도메인 관행, KIS rate limit 같은 시스템 제약. 네 출처 중 하나에 속하지 않는 임계값은 설계에 없다."

---

**Q12: 장중에 LLM을 어떻게 활용하나?**

짧은 답변: LLM이 밤에 만든 yaml 규칙 6종이 rule-based로 장중 작동. 장중 직접 호출은 Cold Path Kanana-o뿐.

심화 답변: "Mode B에서 GPT-4o가 생성한 6종의 yaml 규칙이 다음 날 장중에 rule-based로 실행된다. LLM 자체는 장중에 호출되지 않는다. Cold Path에서만 Kanana-o가 100회/일 예산 안에서 호출된다. Mode B 예산은 Cold Path 100회/일과 별개이며, GPT-4o는 Mode B 전용이다."

---

**Q13: 규칙이 고정인가, 적응하나?**

짧은 답변: 자동 갱신 6개 + 백테스트 후 수동 조정 12개 + pilot 후 확정 6개 + 고정 4개.

심화 답변: "총 28개 중 진짜 고정은 4개(안전장치)뿐이다. 나머지 24개는 Mode B 또는 backtesting 결과에 따라 주기적으로 바뀐다. 이것이 '내일의 시스템은 오늘과 다르다'는 의미다."

---

## 섹션 5: 킬러 문장 모음

### KP-1~7 + KAIDRA 대응

| 코드 | 문장 | 사용 슬라이드 |
|---|---|---|
| KP-1 | "설계 철학이 아니라 실제 제어 흐름." | S03 |
| KP-2 | "논문 조립형이 아니라 구현 가능한 OS." | S03 |
| KP-3 | "양을 늘린 강화가 아니라 자리를 바로잡은 강화." | Q&A (RL 질문 시) |
| KP-4 | "연구형 event-aware multi-agent Decision OS." | S01 타이틀 |
| KP-5 | "prompt chaining이 아니라 운영 가능한 blackboard system." | Q&A (통신 구조 질문 시) |
| KP-6 | "기여도를 측정하고 다음 날 행동 규칙을 바꾸는 적응형 MAS." | S10 Mode B |
| KP-7 | "내일의 시스템은 오늘과 다르다." | S10 Mode B |
| KP-7 확장형 | "Alpha Factor Engine이 새 factor를 만들고, Co-STEER가 모델을 재학습하고, Backtest Agent가 회귀 위험을 차단한 뒤에야 내일의 시스템이 배포된다." | S10 대본 |
| KAIDRA 대응 | "기존 서비스가 '실시간 AI'를 말할 때, 우리는 몇 ms인지, 뭘 검증했는지, 왜 그 판단인지를 답할 수 있는 구조를 만들었다." | S09 |

### 3개 킬러 답변 (Q&A 예상 공격 질문 대비)

**"멀티에이전트라면 기존 서비스랑 다른 게 뭔가?"**

"멀티에이전트라는 단어로 차별화하지 않는다. 차이는 세 가지다. 첫째, Hot Path에서 에이전트가 LLM 없이 <100ms로 작동한다. 둘째, Cold Path에서 에이전트 세 개가 병렬 분석 후 Blackboard (Shared Message Pool + Pub/Sub, MetaGPT 설계 원칙 기반) 를 통해 합의한다. 셋째, FDA가 판단 근거를 reason_code로 남겨서 cause를 추적할 수 있다. 에이전트 수가 아니라 구조의 차이다."

**"왜 LightGBM인가? 딥러닝 안 쓰나?"**

"1분봉 Hot Path에서 딥러닝을 쓰면 100ms를 맞추기 어렵다. LightGBM 종목당 0.3ms는 측정 가능한 수치다. 딥러닝은 Mode B Alpha Factor에서 feature extraction 보조로 쓴다. 속도와 정확도의 역할 분리가 이유다."

**"실거래로 이어질 수 있나?"**

"KIS WebSocket API로 실계좌 연동이 기술적으로 가능하다. 안전장치는 6개 설계되어 있다. 논문 단계 시스템이라 현재는 paper trading이지만, Phase 2에서 실거래 전환 로드맵이 설계 문서에 있다."

---

## 섹션 6: 시각자료 plan

> 저장 위치: `new/visuals/` (디렉토리만 존재, 실제 SVG 파일 0개)
> 기존 자료: 없음. 아래 4개 전부 신규 생성 필요 (D-4 / D-3 작업).

### 필요한 시각자료 4개

---

**VA-01: 전체 아키텍처 다이어그램**

- 파일명: `new/visuals/01_overall_architecture.svg` (신규 생성 필요, 현재 `new/visuals/` 미존재)
- 보여줄 것: Mode A (Hot Path + Cold Path) + Mode B + 에이전트 6개 + Blackboard 통신 + FDA 위치
- 포맷: SVG 권장 (슬라이드 embed 가능). 대안: Mermaid `graph TD`
- 색상 구분: Hot Path = 파란색, Cold Path = 주황색, Mode B = 초록색, FDA = 빨간색 테두리
- 슬라이드 사용: S04

```
[레이아웃 스케치]
+-----------------------------------+
|         Mode A (장중)              |
|  +------+    +------------------+  |
|  | Hot  | -> | LightGBM -> PPO  |  |
|  | Path |    | -> PM -> FDA     |  |
|  +------+    +------------------+  |
|  +------+    +------------------+  |
|  | Cold | -> | News/Risk/Debate  |  |
|  | Path |    | -> FDA           |  |
|  +------+    +------------------+  |
+-----------------------------------+
+-----------------------------------+
|         Mode B (장마감)            |
|  Alpha Factor -> Co-STEER ->      |
|  Backtest Gate -> 22:00 배포      |
+-----------------------------------+
```

---

**VA-02: Dual-Source 흐름 다이어그램**

- 파일명: `new/visuals/02_dual_source.svg` (신규 생성 필요, 현재 `new/visuals/` 미존재)
- 보여줄 것: 뉴스 스트림(λ=0.8) + 커뮤니티 스트림(λ=0.4, peak_lag=2) + 수렴/발산 + 포지션 효과
- 포맷: SVG. 두 스트림이 시간축(x)으로 흐르다가 만나는 구조
- 슬라이드 사용: S08

```
[레이아웃 스케치]
뉴스 -----(λ=0.8)-----> + consensus -> 신호 강화
                           |
커뮤니티 (peak_lag=2) ----> |
       (λ=0.4)             divergence -> 포지션 축소
```

---

**VA-03: Mode B 진화 루프 다이어그램**

- 파일명: `new/visuals/05_mode_b_evolution_loop.svg` (신규 생성)
- 보여줄 것: Alpha Factor Engine -> Co-STEER -> Backtest Agent -> 배포 게이트 -> 다음 날 Hot Path 순환 구조
- 포맷: SVG 순환 화살표 (사이클 다이어그램)
- 강조 포인트: Backtest Gate = 필터. 통과 = 배포, 실패 = 롤백
- 슬라이드 사용: S10

```
[레이아웃 스케치]
         18:00
          |
          v
  Alpha Factor Engine
  (새 factor 생성)
          |
          v
       Co-STEER
  (LightGBM 재학습)
          |
          v
  Backtest Agent
  (회귀 위험 검사)
          |
    +-----+-----+
    |           |
  PASS        FAIL
    |           |
    v           v
22:00 배포   롤백 (오늘 모델 유지)
    |
    v
  내일 Hot Path
```

---

**VA-04: Sprint 로드맵 타임라인**

- 파일명: `new/visuals/06_sprint_roadmap.svg` (신규 생성)
- 보여줄 것: Sprint 0~5, 주차 기준 타임라인, MVP 구간 표시, 기말발표 마감선
- 포맷: SVG 가로 타임라인 또는 표
- 강조 포인트: Sprint 0~2 = MVP (기말 목표), Sprint 3~5 = Phase 2
- 슬라이드 사용: S13

```
[레이아웃 스케치]
주차:  1-2      3-4      5-6      7-8      9-10    11-12
      [SP 0]  [SP 1]  [SP 2]  [SP 3]  [SP 4]  [SP 5]
       인프라   Hot     Cold   Mode B   통합    동적UNV
      |<---- MVP (기말 목표) ---->|<---- Phase 2 ---->|
```

---

## 섹션 7: 준비 체크리스트 (D-5 ~ D-0)

### D-5 (오늘, 2026-04-12 일요일)

- [ ] `new/presentations/midterm_v3.md` 생성 완료 (이 파일)
- [ ] 슬라이드 툴 결정 (Google Slides / PowerPoint / Canva)
- [ ] S01~S15 슬라이드 빈 프레임 생성

### D-4 (2026-04-13 월요일)

- [ ] S01~S08 슬라이드 내용 채우기 (텍스트 + bullet)
- [ ] VA-01 (`01_overall_architecture.svg`) 신규 생성 (`new/visuals/` 폴더 없음, 같이 생성)
- [ ] VA-02 (`02_dual_source.svg`) 신규 생성
- [ ] 발표 대본 S01~S08 소리 내서 읽기 (1회)

### D-3 (2026-04-14 화요일)

- [ ] S09~S15 슬라이드 내용 채우기
- [ ] VA-03 (`05_mode_b_evolution_loop.svg`) 신규 생성
- [ ] VA-04 (`06_sprint_roadmap.svg`) 신규 생성
- [ ] 전체 슬라이드 1회 통독: 흐름 확인
- [ ] Q&A 13개 답변 소리 내서 읽기 (1회)
- [ ] **제출 마감 D-1 = 모레 (4/16 목) 17:00. 오늘 초안 완성이 목표.**

### D-2 (2026-04-15 수요일) [최종 검토]

- [ ] 제출 전 em dash 검색: 슬라이드 전체에서 U+2014 문자 0개 확인
- [ ] 제출 전 AI-sounding 어휘 검색: `.claude/rules/voice.md` Section 2 리스트 기준 0개 확인
- [ ] 시간 측정: 6분 +-30초 범위 내 확인
- [ ] Peer review 대비: Q&A backup 슬라이드 3개 (Q1, Q4, Q6) 확인
- [ ] 슬라이드 최종 통독 + 발표 흐름 확인

### D-1 (2026-04-16 목요일) [마감 당일 + 리허설]

- [ ] **17:00 이전 제출 완료** (마감 당일)
- [ ] 전체 발표 리허설 (실시간 타이머)
- [ ] Q&A 13개 중 예상 어려운 5개 집중 연습
- [ ] 슬라이드 화면 전환 확인 (발표 환경 맞추기)
- [ ] 킬러 문장 KP-1, KP-2, KP-7, KAIDRA 대응 암기

### D-0 (2026-04-17 금, 발표 당일)

- [ ] 발표 30분 전: 슬라이드 열기 + 음소거 해제 확인
- [ ] 첫 슬라이드 S01에서 출발 준비
- [ ] Q&A 2분: 모르는 건 "설계 문서에 있고, 발표 후 확인하겠습니다"로 마무리

---

## 섹션 8: 에러 방지 ("하지 말 것" 목록)

### 절대 금지

| 금지 항목 | 이유 | 대안 |
|---|---|---|
| 수익률 수치 정면 비교 (예: "우리 MDD 20% vs 타 시스템 40%") | 환경/기간/종목이 달라서 apples-to-apples 아님 | 구조적 차이를 설명 (why, not how much) |
| "멀티에이전트로 차별화" 단독 주장 | 기존 서비스도 동일 주장. 차별화 근거 없음 | 에이전트 구조가 만드는 실제 효과(속도/cause/검증)로 설명 |
| 코드 없음 방어적 포장 ("아직 구현 중이라서...") | 청중에게 약점 인식 + 발표 흐름 끊김 | "Sprint 0 시작. 기말발표에서 코드 보여준다" 단 한 문장 |
| 기존 서비스 실명 언급 (특정 서비스명) | 불필요한 비교 + 비교 근거 없으면 역공 위험 | "기존 ML 트레이딩 서비스" 또는 "기존 서비스" |
| em dash 문자 (U+2014) 사용 | voice 규칙 위반 | 콜론, 마침표, 쉼표 |
| AI-sounding 영어 어휘 사용 | voice.md Section 2 금지 리스트 20개 | 직접적 한국어 또는 구체 수치 |
| voice.md Section 3 금지 프레이즈 리스트 계열 filler | 내용 없는 filler | 바로 본론으로 |
| 임계값 수치 근거 없이 제시 | Q11 공격에 즉시 무너짐 | 출처 4개 중 하나 명시 (논문/통계/도메인/시스템 제약) |
| 슬라이드에 긴 문장 가득 채우기 | 발표자가 읽으면 청중은 안 듣는다 | bullet 3개 이하, 나머지는 대본으로 |
| 시간 초과 (6분 30초 이상) | 평가 기준 "시간 준수" 직접 감점 | D-1에 타이머 리허설 필수 |

### 주의 사항

- **RL(강화학습) 언급 시**: "RL로 전부 학습"이 아니라 KP-3 쓸 것. "PPO는 allocator, scheduler에 한정. 모델 전체를 RL로 돌리는 구조 아님."
- **Dual-Source 설명 시**: λ 수치는 발표에서 빠르게 지나가도 된다. 핵심은 "방향 불일치 = 포지션 축소" 효과.
- **Mode B 설명 시**: Co-STEER가 뭔지 교수님이 모를 수 있다. "RD-Agent 논문의 반복 학습 방법론"으로 한 줄 설명 추가.
- **"왜 Kanana-o인가" 질문 시**: "국내 모델 중 한국어 감성 분류 성능 가장 안정적. API 접근 가능. GPT-4o 대비 비용 효율."

---

## 부록: 발표 6분 배분 요약

| 파트 | 슬라이드 | 시간 | 핵심 메시지 |
|---|---|---|---|
| 타이틀 | S01 | 0:00~0:10 | 이름 + 팀 + 날짜 |
| 문제 정의 | S02 | 0:10~0:50 | 두 가지 문제, 동시에 |
| 전체 구조 | S03, S04 | 0:50~2:00 | OS 비유 + 2-mode 3-path |
| 핵심 기술 | S05~S09 | 2:00~4:00 | Hot Path + Dual-Source + 차별점 4개 |
| Mode B + 설계 자산 | S10, S11 | 4:00~4:50 | 자동 진화 + 수치 |
| Sprint 계획 + 마무리 | S12~S15 | 4:50~6:00 | 기말 MVP 목표 |

**총 6분 (360초). 슬라이드 15장. 평균 24초/장. 이 테이블이 발표자 노트 시간 SSOT.**
슬라이드 S06 (피처 설계)는 시간 압박 시 S05와 합쳐서 1장으로 줄일 수 있다.

---

## 부록: 빠른 참조 파일 경로

| 파일 | 용도 |
|---|---|
| `new/docs/architecture.md` | 전체 아키텍처 상세 (99KB) |
| `new/specs/api_contracts.md` | C1~C16 API 계약서 (37KB, SSOT) |
| `new/config/risk_config.yaml` | 임계값 출처 확인 |
| `new/config/dual_source_config.yaml` | λ=0.8, λ=0.4, peak_lag=2 확인 |
| `new/visuals/01_overall_architecture.svg` | 전체 아키텍처 시각자료 |
| `new/visuals/02_dual_source.svg` | Dual-Source 시각자료 |
| `.claude/memory/project_midterm_qa_prep.md` | Q&A 예상 질문 원본 |
