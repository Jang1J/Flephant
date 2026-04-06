# Paper 3: MetaGPT — Meta Programming for Multi-Agent Collaborative Framework

> 멀티에이전트를 조직하는 법 | ICLR 2024 | DeepWisdom

## 논문 해석

### 핵심 문제
LLM을 단순 체이닝하면 hallucination 누적 + 논리 불일치. 에이전트 간 자유 대화는 "Chinese whispers" 효과로 정보 왜곡.

### 해법
인간 조직의 SOP(Standard Operating Procedures)를 멀티에이전트에 적용. 구조화된 문서로만 소통. LLM 1개 + 역할 프롬프트 5개 = 멀티에이전트.

### 아키텍처
```
PM → PRD → Architect → System Design → Project Manager → Tasks → Engineer → Code → QA → Test

Shared Message Pool (Pub/Sub)
  ├── Publish: 에이전트가 구조화된 산출물 게시
  ├── Subscribe: 관심 있는 에이전트만 구독
  └── 의존성 기반 활성화: 선행 조건 충족 시 자동 트리거
```

### 핵심 기법 상세

#### 1. SOP + 역할 전문화
- 5개 역할: Product Manager, Architect, Project Manager, Engineer, QA
- 각 역할에 Name, Profile, Goal, Constraint 정의
- 각 역할이 표준화된 산출물 생성 (PRD, System Design, Tasks, Code, Test)

#### 2. Shared Message Pool + Pub/Sub
- 글로벌 메시지 저장소
- 메시지 메타데이터: content, instruct_content, cause_by, sent_from, send_to
- 역할 기반 필터링: Architect는 PRD만 구독, QA 결과는 무시
- 의존성 기반 활성화: 선행 의존성 모두 충족 후 행동

#### 3. 구조화된 소통 (대화 아님)
- "documents and diagrams rather than dialogue"
- 에이전트 간 자유 대화 금지 → 정보 왜곡 방지
- 각 handover에 품질 표준 적용

#### 4. Executable Feedback Loop
- 코드 실행 → 에러 시 과거 PRD/Design/Code 참조 → 수정
- 최대 3회 재시도 (bounded iteration)
- 성공 시 다음 단계, 실패 시 디버그

#### 5. Self-Improvement (Appendix A)
- Handover Feedback: 프로젝트 종료 시 경험 정리 → long-term memory
- React Action: 새 프로젝트 시작 시 과거 피드백 리뷰 → constraint prompt 자기 갱신
- Self-referential: 에이전트가 관찰한 정보로 자기 constraint 수정

#### 6. Economy of Minds
- 에이전트 기여도를 경제 원리로 측정
- 좋은 기여 → 높은 영향력, 나쁜 기여 → 낮은 영향력

### 성과 수치
- HumanEval 85.9%, MBPP 87.7% (Pass@1 SOTA)
- Executability 3.75/4.0 vs ChatDev 2.25
- Human Revision 0.83 vs ChatDev 2.5 (3배 감소)
- 토큰 60% 더 쓰지만 품질 대폭 향상
- Ablation: 1역할 Exec 1.0 → 5역할 Exec 4.0

## KOSPI 1분봉 활용 방안

### 1. Message Pool 도입
- 스크립트 순차 호출 → 에이전트 자율 Pub/Sub
- Risk Agent "US 급락" publish → FDA subscribe → 즉시 반응

### 2. Agent Profile 정의
```python
RiskAgent = {
    "name": "Risk Agent",
    "profile": "시장 리스크 감시 전문가",
    "goal": "퀀트가 못 잡는 리스크 감지",
    "constraint": "정량은 퀀트에 위임, 정성만",
    "subscribes_to": ["us_market", "news", "dart"],
    "publishes": ["risk_warning", "veto"]
}
```

### 3. 구조화된 메시지 프로토콜
```json
{
  "content": "삼성전자 리스크",
  "cause_by": "NewsAnalysis",
  "sent_from": "RiskAgent",
  "send_to": "FDA",
  "priority": "urgent",
  "confidence": 0.85,
  "reasoning": "DART 영업이익 -15%",
  "uncertainty": "시장 반응 미확인"
}
```

### 4. Executable Feedback Loop
- 판단→실행→결과→수정 (max 3회)
- 1분봉에서는 이벤트 시에만 반복, 평상시 1회

### 5. Self-Improvement (장마감 후)
- Handover: 오늘 판단+결과 정리 → Memory 저장
- React: 다음 날 08:30 과거 피드백 리뷰 → constraint 갱신

### 6. 의존성 기반 병렬
- News/Risk/Quant Agent 병렬 실행
- FDA는 모두 완료 후 활성화

### 7. Ablation 필수
- "Risk Agent 빼면 Sharpe 얼마나 떨어지나?"
- 각 에이전트의 가치를 정량 증명
