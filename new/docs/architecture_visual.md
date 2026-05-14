# KOSPI Decision OS v3: Architecture Visualization

> KOSPI 1분봉을 위한 연구형/모의운용형 event-aware multi-agent Decision OS
> v2.1 GPT Pro 검증: 평균 8.6/10 (2026-04-05)
> v2.2 업데이트 (2026-04-09): Backtest Agent 추가, Hot/Cold Path 분기 시각화 보강, Mode B 타임라인 시각화 신규
> v3.0 추가 (2026-04-10): Dual-Source Temporal Signal 즉시 흡수 + Sprint 5 Dynamic Event Universe Blueprint 추가
> v3.0.1 수정 (2026-04-12): FDA reason_code 필드 추가 반영 (api_contracts.md C9 + architecture.md 동기화)
> v3.0.2 추가 (2026-04-20, S1-0 Batch B+C): C17 ModelRegistryContract 신설 반영, §8.3 Eval Agent 메트릭에 ICIR/SR 추가, Mode B stage_4 타임라인에 LGBMTrainer → registry.save(v{n}.pkl) 흐름 명시 (상세 다이어그램은 presenter 별도 작업 defer)
> v3.0.4 (2026-04-21): C18 AgentPerformanceContract + C9 uncertainty_score extension + MSG/APM/DEC/OP/PP/BUNDLE/BT/RPT/FCC/RGC UUID8 전수 정정 + Cold Path 디스패치 흐름 반영.
> v3.0.3 (2026-04-21): C18 AgentPerformanceContract 신설 반영. §20 Evaluation Matrix 3-Layer 추가.
> v3.0.5 (2026-04-25): S2-6 NewsFilter + TextPack 13 템플릿 + S2-7 NewsAgent 실구현 (Kanana-o CoT + consume_text_pack + C5 news_signal/dart_alert publish)
> v3.0.6 (2026-04-26): Sprint 2 완료 반영. S2-8 RiskAgentFast/Slow Cold Path + S2-9 DebateAgent/FDA Cold Path + S2-10 ModeBPerformanceAggregator + S2-11 BaseConnector. reason_code 7종 최종 확정. Cold Path e2e 루프 닫힘.
> v3.0.7 (2026-05-01): Sprint 3 완료 반영. ModeBScheduler 7-stage cron + ModeBDeployer atomic swap + KnowledgeBase Layer 5 + Committee (AlphaGAT Stage II) + Validation Tools 3 component. 4축 정합 일괄 수정 포함.
> v3.0.8 (2026-05-02): Sprint 4 문서 fix. §7.1 8-stage + stage_0 DQR 박스 추가 / Layer 5 Persistent Caching S4-7 SQLite 표기 / §7.6 Bootstrap 다이어그램 신설.
> v3.0.9 (2026-05-02): 전수 리뷰 fix. §3.0 6단계→8단계 + stage_0 DQR 박스 추가 / §14 active 20 표기 / §19 active 20 표기 / §20.3 RISK_BREACH→RISK_FAST_TRIGGER / Layer 3 n_est=500 정정.
> v3.1.0 cleanup (2026-05-04): Sprint 0 검증 사후 cleanup. §3.1 Backtest Agent C12 실구현 반영 + §6 S4 모듈 5건 추가 (DQR Runner/DualSourceScorer/PersistentCache/HotPathProfiler/Memory Restorer) + C2 pit_safe/payload 계약서 등재.

## v2.2 변경점 요약

| # | 위치 | 변경 |
|---|------|------|
| V1 | §2 장중 루프 | Hot Path vs Cold Path 분기 명확화 (Hot: quant+비LLM FDA, Cold: LLM 에이전트 활성) |
| V2 | §3 Mode B | 18:00~22:00 6단계 타임라인 + Backtest Agent 게이트 시각화 신규 |
| (추가) | §3 | Backtest Agent가 배포 게이트 직전에 추가됨 |
| (추가) | §4 | Backtest Agent는 Shared Message Pool에 장중에 publish하지 않음 명시 |
| v3.8 | §1 Layer 2, §2.1 | Layer 2에 Watch Universe Feed 표기 + §2.1 Dynamic Overlay 진입 구조 도식 신규 (Sprint 5 진입, 2026-05-03) |
| v3.1.0 cleanup (2026-05-04) | §3.1 Backtest Agent C12 실구현 반영 + §6 S4 모듈 5건 (DQR/DualSource/Cache/Profiler/MemoryRestorer) 추가 + C2 pit_safe/payload 계약서 등재 (S0 검증 사후 cleanup) |

## 1. 전체 시스템 구조도

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        KOSPI Decision OS v3                              │
│                                                                          │
│  ┌─── Mode A: 장중 실시간 (09:00~15:30) ──┐  ┌── Mode B: 장마감 진화 ──┐│
│  │         매 1분 판단 루프                 │  │    18:00~22:00 진화+검증 ││
│  └─────────────────────────────────────────┘  └────────────────────────┘│
│                                                                          │
│ ╔══════════════════════════════════════════════════════════════════════╗  │
│ ║  Layer 5: KNOWLEDGE                                                 ║  │
│ ║  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌─────────┐  ║  │
│ ║  │ Macro    │ │ Micro    │ │Knowledge │ │Knowledge │ │Failure  │  ║  │
│ ║  │ Notes    │ │ Notes    │ │  Base    │ │ Forest   │ │Category │  ║  │
│ ║  │(거시경제) │ │(종목별)  │ │(상황,판단│ │(가설 트리)│ │(실패 분류)│ ║  │
│ ║  └──────────┘ └──────────┘ │,결과)    │ └──────────┘ └─────────┘  ║  │
│ ║       ↑ 감쇠 커널            └────┬─────┘       ↑                    ║  │
│ ║       │ η∈(0.9,1.0)              │ RAG 검색    │ AST dedup          ║  │
│ ║       │                          ↓             │                    ║  │
│ ║  ┌──────────┐           ┌──────────────┐  ┌──────────┐             ║  │
│ ║  │Persistent│           │  Vector DB   │  │Factor Zoo│             ║  │
│ ║  │ Caching  │           │(similarity)  │  │(AST 저장)│             ║  │
│ ║  │(S4-7     │           │              │  │          │             ║  │
│ ║  │ SQLite)  │           │              │  │          │             ║  │
│ ║  └──────────┘           └──────────────┘  └──────────┘             ║  │
│ ╚══════════════════════════════════════════════════════════════════════╝  │
│       ↑↓                          ↑↓                    ↑↓               │
│ ╔══════════════════════════════════════════════════════════════════════╗  │
│ ║  Layer 4: AGENTS                                                    ║  │
│ ║                                                                     ║  │
│ ║  ┌─────────────────────────────────────────────────────────────┐    ║  │
│ ║  │                    Shared Message Pool                       │    ║  │
│ ║  │  {content, cause_by, sent_from, send_to, priority,          │    ║  │
│ ║  │   confidence, reasoning, evidence_ids, uncertainty}          │    ║  │
│ ║  └──────┬──────────┬──────────┬──────────┬──────────┬─────────┘    ║  │
│ ║         │subscribe │subscribe │subscribe │subscribe │subscribe     ║  │
│ ║         ↓          ↓          ↓          ↓          ↓              ║  │
│ ║  ┌──────────┐┌──────────┐┌──────────┐┌──────────┐┌──────────┐    ║  │
│ ║  │  News    ││  Risk    ││  Quant   ││ Debate   ││   FDA    │    ║  │
│ ║  │  Agent   ││  Agent   ││  Agent   ││  Agent   ││(Orchest.)│    ║  │
│ ║  │          ││          ││          ││          ││          │    ║  │
│ ║  │- 뉴스    ││- US 야간 ││- 모델래핑 ││- 갈등해소 ││- 종합판단 │    ║  │
│ ║  │- 공시    ││- 거시경제 ││- 시그널   ││- 근거제시 ││- 최종결정 │    ║  │
│ ║  │- 커뮤니티+테마││- 지정학  ││- 이상탐지 ││          ││- CoT     │    ║  │
│ ║  │          ││          ││          ││          ││          │    ║  │
│ ║  │[Kanana-o]││[Kanana-o]││[No LLM]  ││[Kanana-o]││[Kanana-o]│    ║  │
│ ║  │ CoT+Skip ││ CoT+Macro││ Fast     ││ CoT     ││ CoT+Bank │    ║  │
│ ║  │ Memory   ││ Memory   ││          ││ Memory   ││ Memory   │    ║  │
│ ║  └────┬─────┘└────┬─────┘└────┬─────┘└────┬─────┘└────┬─────┘    ║  │
│ ║       │publish    │publish    │publish    │publish    │decision   ║  │
│ ║       └───────────┴───────────┴───────────┴───────────┘           ║  │
│ ║                                                                     ║  │
│ ║  ┌─────────────────────────────────────────┐                       ║  │
│ ║  │  Thompson Sampling (에이전트 신뢰도)      │                       ║  │
│ ║  │  x_t = [변동성, 거래량, 뉴스, 시간대]     │                       ║  │
│ ║  │  A = {Quant우선, Risk우선, 균등}          │                       ║  │
│ ║  │  → Bayesian posterior 갱신               │                       ║  │
│ ║  └─────────────────────────────────────────┘                       ║  │
│ ║                                                                     ║  │
│ ║  ┌─────────────────────────────────────────┐                       ║  │
│ ║  │  Backtest Agent (Mode B 전용, v2.2)     │                       ║  │
│ ║  │  - 장중 경로(Hot/Cold)에 절대 미개입       │                       ║  │
│ ║  │  - Shared Message Pool과 무관             │                       ║  │
│ ║  │  - 21:00~21:30 시스템 검증 게이트         │                       ║  │
│ ║  │  - 상세 위치 → §3 Mode B 다이어그램 참조  │                       ║  │
│ ║  └─────────────────────────────────────────┘                       ║  │
│ ╚══════════════════════════════════════════════════════════════════════╝  │
│       ↑↓                                                                 │
│ ╔══════════════════════════════════════════════════════════════════════╗  │
│ ║  Layer 3: MODEL                                                     ║  │
│ ║                                                                     ║  │
│ ║  ┌─────────────────────────────────────────────────────────────┐    ║  │
│ ║  │              Quant Model: LightGBM (확정)                    │    ║  │
│ ║  │                                                              │    ║  │
│ ║  │  Alpha Factors + Dual-Source 5피처 ──→ LightGBM ──→ active 20종목 예측 시그널           │    ║  │
│ ║  │  (LLM 자동생성)    (n_est=500        + confidence              │    ║  │
│ ║  │  + Multi-scale      depth=4,        (추론 0.3ms)             │    ║  │
│ ║  │  + Cross-Asset피처  risk_config SSOT)                         │    ║  │
│ ║  └─────────────────────────────────────────────────────────────┘    ║  │
│ ║                                                                     ║  │
│ ║  ┌─────────────────────────────────────────────────────────────┐    ║  │
│ ║  │              PPO Allocator (확정)                             │    ║  │
│ ║  │                                                              │    ║  │
│ ║  │  LightGBM 시그널 ──→ PPO ──→ 종목별 비중                    │    ║  │
│ ║  │  + 현재 포지션         (stable-baselines3)                   │    ║  │
│ ║  │  + 시장 상태           reward = 수익 - 거래비용              │    ║  │
│ ║  │                        상한: 단일≤20%, 섹터≤40%, 현금≥10%    │    ║  │
│ ║  └─────────────────────────────────────────────────────────────┘    ║  │
│ ║                                                                     ║  │
│ ║  ┌─────────────────────────────────────────────────────────────┐    ║  │
│ ║  │              Alpha Factor Engine (장마감 진화)                │    ║  │
│ ║  │                                                              │    ║  │
│ ║  │  ┌────────┐   ┌────────┐   ┌────────┐                      │    ║  │
│ ║  │  │ Idea   │──→│ Factor │──→│  Eval  │──→ feedback ──┐      │    ║  │
│ ║  │  │ Agent  │   │ Agent  │   │ Agent  │               │      │    ║  │
│ ║  │  │4요소가설│   │AST구현 │   │3차원평가│               │      │    ║  │
│ ║  │  └────────┘   └────────┘   └────────┘               │      │    ║  │
│ ║  │       ↑                                              │      │    ║  │
│ ║  │       └──────────────────────────────────────────────┘      │    ║  │
│ ║  │                                                              │    ║  │
│ ║  │  3중 정규화: ①AST 독창성 ②가설 정합 ③복잡도 제어            │    ║  │
│ ║  │  Operator Library: rolling, SMA, EMA, RSI, conditional...   │    ║  │
│ ║  │  Cov Regularization: 팩터 간 다양성 강제                     │    ║  │
│ ║  │  Loss: L(예측력) + λR_g(정규화) 교대 최적화                  │    ║  │
│ ║  │  거래비용 reward 내장: r_t = (r̃+1)(1-c_t) - 1               │    ║  │
│ ║  └─────────────────────────────────────────────────────────────┘    ║  │
│ ╚══════════════════════════════════════════════════════════════════════╝  │
│       ↑↓                                                                 │
│ ╔══════════════════════════════════════════════════════════════════════╗  │
│ ║  Layer 2: DATA                                                      ║  │
│ ║                                                                     ║  │
│ ║  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐              ║  │
│ ║  │ KIS API  │ │  Naver   │ │   DART   │ │ US Mkt   │              ║  │
│ ║  │ 1분봉    │ │  News    │ │  공시    │ │ 야간     │              ║  │
│ ║  │ OHLCV+3  │ │          │ │          │ │ S&P/VIX  │              ║  │
│ ║  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘              ║  │
│ ║       │            │            │            │  ┌──────────┐       ║  │
│ ║       ↓            ↓            ↓            ↓  │  ECOS    │       ║  │
│ ║  ┌─────────────────────────────────────────────┐│  거시    │       ║  │
│ ║  │           Preprocessing Pipeline            │└──────────┘       ║  │
│ ║  │  ① Robust Z-score (MAD)                     │┌──────────┐       ║  │
│ ║  │  ② Forward-fill + Cross-sectional mean      ││ 커뮤니티 │       ║  │
│ ║  │  ③ Multi-scale 분해 (1m/5m/30m/60m)         ││ Dual-Src │       ║  │
│ ║  │  ④ TSFresh 통계 → 자연어 변환               ││ 3-stage  │       ║  │
│ ║  │  ⑤ PIT-Safety: LLM에 raw data 비노출        │└──────────┘       ║  │
│ ║  └─────────────────────────────────────────────┘                   ║  │
│ ║                                                                     ║  │
│ ║  ─ Watch Universe Feed (Sprint 5, S5-1):                            ║  │
│ ║    KOSPI200 200종목 × KIS REST get_price_snapshot() 60초 polling     ║  │
│ ║    → SnapshotStore (artifacts/watch_snapshots/) → EventGateway      ║  │
│ ║    forbidden: trade_universe_mutation, lightgbm_inference            ║  │
│ ╚══════════════════════════════════════════════════════════════════════╝  │
│       ↑↓                                                                 │
│ ╔══════════════════════════════════════════════════════════════════════╗  │
│ ║  Layer 1: EXECUTION                                                 ║  │
│ ║                                                                     ║  │
│ ║  ┌──────────┐ ┌──────────────┐ ┌────────────┐ ┌────────────────┐  ║  │
│ ║  │ KIS API  │ │  Portfolio   │ │   Risk     │ │   Feedback     │  ║  │
│ ║  │ 주문실행  │ │  Management  │ │ Constraints│ │    Loop        │  ║  │
│ ║  │          │ │              │ │            │ │ 실행→결과→학습  │  ║  │
│ ║  │ REST/WS  │ │ 포지션 관리  │ │ 제약 조건  │ │ bounded(max3) │  ║  │
│ ║  └──────────┘ └──────────────┘ └────────────┘ └────────────────┘  ║  │
│ ║                                                                     ║  │
│ ║  파이프라인: LightGBM→Top10→[충돌시]Debate→PPO→PM→FDA→실행        ║  │
│ ║  보유종목: PPO weight=0 할당 or Cold Path 청산 트리거              ║  │
│ ║  FDA: approve/veto only (order_deltas 검토만, 생성 불가)           ║  │
│ ║  Bounded Execution: 에이전트별 시간 제한 (10~30초)                  ║  │
│ ║  OMS 한계(DEFER): 부분체결, 정정/취소, 분할, Queue, WS failover    ║  │
│ ╚══════════════════════════════════════════════════════════════════════╝  │
└──────────────────────────────────────────────────────────────────────────┘
```

## 2. 장중 실시간 매매 루프 (Mode A)

```
  ┌─────────────────────────────────────────────────────────────┐
  │                    매 1분 사이클                              │
  │                                                              │
  │  ┌──────────┐     ┌──────────┐     ┌──────────┐            │
  │  │  1분봉   │────→│ 전처리   │────→│  퀀트    │            │
  │  │  수집    │     │ Z-score  │     │  모델    │            │
  │  │ active 20   │     │ 다중스케일│     │  추론    │            │
  │  └──────────┘     └──────────┘     └────┬─────┘            │
  │                                         │                   │
  │                                    시그널 publish            │
  │                                         │                   │
  │                                         ↓                   │
  │                              ┌─────────────────┐            │
  │                              │  Message Pool   │            │
  │                              └──┬──┬──┬──┬──┬──┘            │
  │                                 │  │  │  │  │               │
  │    ┌────────────────────────────┘  │  │  │  │               │
  │    │              ┌────────────────┘  │  │  │               │
  │    │              │      ┌────────────┘  │  │               │
  │    ↓              ↓      ↓               │  │               │
  │  ┌──────┐  ┌──────┐  ┌──────┐           │  │               │
  │  │ News │  │ Risk │  │Quant │           │  │               │
  │  │Agent │  │Agent │  │Agent │           │  │               │
  │  └──┬───┘  └──┬───┘  └──┬───┘           │  │               │
  │     │publish  │publish  │publish         │  │               │
  │     └─────────┴─────────┘                │  │               │
  │                │                          │  │               │
  │         ┌──────┴──────┐                   │  │               │
  │         │ 충돌 감지?  │──Yes──→┌──────┐   │  │               │
  │         └──────┬──────┘        │Debate│   │  │               │
  │                │No             │Agent │   │  │               │
  │                │               └──┬───┘   │  │               │
  │                │                  │publish │  │               │
  │                │                  │        │  │               │
  │                ↓                  ↓        ↓  │               │
  │           ┌────────────────────────────────┐  │               │
  │           │           FDA                  │  │               │
  │           │  Thompson Sampling 신뢰도 반영  │  │               │
  │           │  + 종합 판단 + CoT reasoning    │  │               │
  │           └──────────┬─────────────────────┘  │               │
  │                      │ decision                │               │
  │                      ↓                         │               │
  │               ┌──────────┐                     │               │
  │               │ 주문실행  │                     │               │
  │               │ KIS API  │                     │               │
  │               └────┬─────┘                     │               │
  │                    │ result                     │               │
  │                    ↓                            │               │
  │           ┌──────────────┐                      │               │
  │           │ Knowledge    │←─────────────────────┘               │
  │           │ Base 저장    │                                      │
  │           │(상황,판단,결과)│                                     │
  │           └──────────────┘                                      │
  └─────────────────────────────────────────────────────────────────┘

  ※ 2스트림 병렬: Stream 1(퀀트, 매 1분) + Stream 2(이벤트, 상시)
  ※ Hot Path: 퀀트만 (<100ms). Debate 미호출
  ※ Cold Path: 이벤트 감지 + 충돌 시 Debate 활성화 (10~30초)
  ※ Hot Path (평상시): Quant Agent만 활성 (추론 <100ms, LLM 미호출)
  ※ Cold Path (이벤트 시): News/Risk 분석 → 충돌 감지 시 Debate Agent 활성 (LLM 호출, 10~30초)
     트리거: 뉴스감지 | 급등락 | regime변화 | anomaly
  ※ FDA 출력: 8필드: approved, target_weights(RO), order_deltas(RO), veto_reason, reason_code, risk_overrides(audit), confidence, expiry (BUY/HOLD/SELL 아님)
  ※ 현재 적용 범위: 연구형/모의운용형 (실계좌 자동매매 아직 부적합)
```

## 2.1 Hot Path vs Cold Path 분기 (v2.2 시각화 보강)

> 매 1분 판단 지점에서 이벤트 admission 결과에 따라 두 경로가 갈라진다.
> **Hot Path**는 LLM을 한 번도 호출하지 않으며 <100ms 안에 FDA 최종 승인까지 완료된다.
> **Cold Path**는 이벤트가 있을 때만 LLM 에이전트가 활성화되어 10~30초의 reasoning 경로를 탄다.

```
                        ┌────────────────────────┐
                        │  매 1분 tick (t)        │
                        │  KIS 1분봉 + 이벤트 큐  │
                        └───────────┬────────────┘
                                    │
                        ┌───────────▼────────────┐
                        │  Event Admission (C11)  │
                        │  - priority/ttl/supersedes
                        │  - backlog overflow ↴    │
                        │     → dead_letter_log   │
                        └────┬──────────────┬─────┘
               이벤트 없음    │              │  이벤트 admitted
                              ↓              ↓
          ┌───────────────────────┐   ┌────────────────────────────┐
          │   HOT PATH (<100ms)    │   │   COLD PATH (10~30초)      │
          │   LLM 미호출           │   │   LLM 활성 (Kanana-o)      │
          │                       │   │                            │
          │ ① Quant Agent          │   │ ① News Agent (병렬)        │
          │    LightGBM 0.3ms     │   │    뉴스/공시/커뮤니티       │
          │    (숫자 anomaly)      │   │ ② Risk Agent Slow (병렬)   │
          │                       │   │    US야간/거시/수급 해석     │
          │ ��' Risk Fast sidecar  │   │    (Kanana-o LLM)           │
          │    (v3) rule-based     │   │ ③ Quant Agent (병렬)       │
          │    comm/수급 이상 감지  │   │    anomaly_detected 보고    │
          │    <50ms, LLM 미호출   │   │ ④ [충돌 시만] Debate        │
          │    ※ 주 코어 block X   │   │    pairwise 45회 재랭킹     │
          │                       │   │                            │
          │ ② Top-10 필터          │   │ ⑤ FDA (LLM)                │
          │    (퀀트 점수 순)      │   │    CoT + Thompson Sampling │
          │                       │   │    approve / veto           │
          │ ③ PPO Allocator        │   │                            │
          │    target_weights     │   └──────────┬─────────────────┘
          │                       │
          │ ④ Portfolio Manager    │
          │    order_deltas 생성  │
          │                       │
          │ ⑤ FDA (비LLM)          │
          │    규칙 7체크리스트:   │
          │    Regime Gate         │              │
          │    Kill Switch         │              │  approve/veto
          │    Position Limit      │              │
          │    Sector Limit        │              │
          │    Cash Minimum        │              │
          │    Turnover Cap        │              │
          │    Min Confidence     │              │
          │    → approve / veto    │              │
          └──────────┬─────────────┘              │
                     │                             │
                     │  approve/veto               │
                     └──────────┬──────────────────┘
                                │
                                ↓
                      ┌─────────────────┐
                      │ Execution (mock)│
                      │ → KB 저장        │
                      │ → memory 갱신    │
                      └─────────────────┘

  ※ FDA는 Hot/Cold 모두에서 최종 게이트.
  ※ Hot에서는 "비LLM deterministic validator" (규칙 체크리스트).
  ※ Cold에서는 "LLM reasoning + CoT + Thompson Sampling".
  ※ "Hot에서 FDA 없음"이 아니라 "Hot에서 FDA가 LLM을 쓰지 않음".
  ※ Backtest Agent는 이 그림에 없음. Mode B 전용이며 장중 경로에 절대 개입하지 않음 (§3 참조).
```

### Cold Path 진입 구조 (S2-1, S2-6, S2-7)

```
┌─────────────────────────────────────────────────────────┐
│  6 Connector (Naver/DART/Community/ECOS/US Market/KRX)   │
│     ↓ raw_event                                          │
│  [EventGateway.ingest()]                                 │
│     ↓ C2 정규화                                          │
│  [EventAdmission] 3 필터                                 │
│    ├─ dedupe (event_id + supersedes TTL 300s)            │
│    ├─ stale_drop (expires_at < now KST)                  │
│    └─ backlog 3건 cap + jobs/min 10 cap                 │
│     ↓ priority 정렬 (priority > trigger > scope > recency)│
│  [EventGateway.dispatch_next()]                          │
│     ↓ handler() (Direct Dispatch)                        │
│                                                          │
│  [S2-6] NewsFilter.filter() (ticker/sector/market 3-level)│
│    + TextPackBuilder.build() (TSFresh 30분 통계 → 자연어, │
│      13 템플릿, news_filter.yaml text_pack_templates SSOT)│
│     ↓ text_pack                                          │
│  [S2-7] NewsAgent (실구현: Kanana-o CoT)                 │
│    ├─ analyze() (event_type 3종: news/dart/community 분기) │
│    ├─ consume_text_pack() (TextPack 위임)                 │
│    ├─ _parse_llm_content() (heuristic)                   │
│    └─ _save_memory() (micro/macro JSONL)                 │
│     ↓ LLMRouter.call(mode='cold', caller='news_agent') │
│       Kanana-o 30회/일 → GPT-4o fallback (429/timeout)   │
│                                                          │
│  Risk Slow / Debate Agent                                │
│     ↓ PubSubBroker.publish() (채널 라우팅)               │
│  [MessagePool (C4)] -- 저장 + 구독자 fan-out             │
│     ↓ C5 news_signal / dart_alert publish                │
│  FDA Cold Path (register_dependency news+risk+quant → callback 활성화)│
└─────────────────────────────────────────────────────────┘

Risk Fast sidecar 예외: Hot Path bar_buffer 직접 감지, EventGateway bypass.
```

### Dynamic Overlay 진입 구조 (Sprint 5, S5-1~S5-4)

```
  ┌──────────────────────────────────────────────────────────────────┐
  │ Watch Universe (KOSPI200 200종목)                                 │
  │   └─ KIS REST get_price_snapshot() 60s polling                    │
  └────────────┬─────────────────────────────────────────────────────┘
               ↓
  ┌──────────────────────────────────────────────────────────────────┐
  │ EventGateway → AdmissionEngine                                    │
  │   trigger_catalog match: price_spike_admission ±5%                │
  │                          dart_hot_ticker_admission                 │
  │   → admission_event (ADM-yyyymmdd-UUID8)                          │
  └────────────┬─────────────────────────────────────────────────────┘
               ↓ gate.is_enabled() == true 일 때만
  ┌──────────────────────────────────────────────────────────────────┐
  │ Candidate Pool (max 10)  →  Holdings Manager (max 5)              │
  │   per_stock_max_weight: 0.03   total_max: 0.10                    │
  │   allocator: fixed_rule_only (PPO 금지)                            │
  └────────────┬─────────────────────────────────────────────────────┘
               ↓
  ┌──────────────────────────────────────────────────────────────────┐
  │ Exit Engine (4조건)                                                │
  │   market_close (15:30 KST) | ttl_expiry (1800s)                   │
  │   stop_loss (-2%)          | spike_resolved                       │
  │   → exit_event (EXT-yyyymmdd-UUID8)                                │
  └──────────────────────────────────────────────────────────────────┘
               ↓
  비동기 채널 dynamic_overlay_update → PortfolioManager 다음 1분 틱 반영
  Hot Path (LightGBM/PPO/PM/FDA) 격리 유지. trade universe 불변.
```

## 3. 장마감 자동 진화 루프 (Mode B)

### 3.0 Mode B 타임라인 (v2.2 시각화 신규)

> Mode B는 18:00~22:00 사이에 8단계 (stage_0 DQR + stage_1~7)로 직렬 진행된다. 각 단계 사이에는 명확한 artifact 전달이 있고,
> **21:00~21:30 Backtest Agent 게이트** 는 v2.2에서 신설된 시스템 레벨 검증 지점이다.
>
> **주체**: 모든 단계 호출은 **Mode B Scheduler (C14)** 가 cron-style로 수행한다.
> 배포 실행자는 **Mode B Deployer** (Scheduler sub-component).
> Runtime State는 `MODE_B_IDLE → MODE_B_EVOLVING → MODE_B_BACKTEST → (MODE_B_DEPLOY | MODE_B_OPERATOR_REVIEW | MODE_B_BLOCKED) → MODE_B_IDLE`.

```
시간          단계                           산출물                         다음 단계 트리거
──────        ───────────                    ──────                         ──────────────
  18:00  ┌──────────────────────────┐   → dqr_report                    → §8.1 (CRITICAL 시 차단)
         │ stage_0 DQR              │      (outlier_rate, null_rate,
         │ Data Quality Review      │       ticker 커버리지)
         │ (2분 SLA)                │
         └───────────┬──────────────┘
                     │
  18:00  ┌───────────▼──────────────┐   → performance_vector            → §8.2
         │ §8.1 성과 분석           │      8차원 [IC,ICIR,RankIC,
         │ x_t = 8차원 성과 벡터    │       ARR,IR,-MDD,SR, ...]
         └───────────┬──────────────┘
                     │
  18:30  ┌───────────▼──────────────┐   → direction ∈ {factor, model}   → §8.3 또는 §8.4
         │ §8.2 방향 결정            │      (Thompson Sampling posterior)
         │ Thompson Sampling        │
         │ A = {factor 개선, model 개선}│
         └───────────┬──────────────┘
                     │
  19:00  ┌───────────▼──────────────┐   → factor_candidate              → §8.4
         │ §8.3 팩터 진화            │     (AST + 3중 정규화 통과)
         │ Alpha Factor Engine      │
         │ Idea → Factor → Eval     │
         │ (GPT-4o)                 │
         └───────────┬──────────────┘
                     │
  20:00  ┌───────────▼──────────────┐   → model_candidate               → §8.5
         │ §8.4 모델 진화            │     (Co-STEER retrain 결과)
         │ Co-STEER                  │
         │ + §8.4.1 재학습 데이터    │
         │ (한국 today + 미국 d-1)   │
         └───────────┬──────────────┘
                     │
  20:30  ┌───────────▼──────────────┐   → agent_constraint_update       → §8.5.2
         │ §8.5 에이전트 자기 개선  │     (Handover Feedback +
         │ MetaGPT Handover/React    │      Memory 순환)
         │ (8차원 벡터 기반 기여도)  │
         └───────────┬──────────────┘
                     │
  21:00  ┏━━━━━━━━━━━▼━━━━━━━━━━━━━┓   ←─────  v2.2 신규 게이트  ─────→
         ┃ §8.5.2 Backtest Agent   ┃   → backtest_report               →
         ┃ (C12, S3-9 실구현 SHIP) ┃     {verdict, regression_risk,       
         ┃  GPT-4o + C13 tools    ┃      diagnostic_notes,              
         ┃ ① BacktestEngine        ┃      deploy_recommendation}         
         ┃   walk-forward          ┃                                     
         ┃ ② ReplayRunner          ┃   ┌─ verdict == pass AND             
         ┃   deterministic replay  ┃   │   regression_risk == false       
         ┃ ③ PerformanceAnalyzer   ┃   │    → §8.6 배포 진행              
         ┃   regime breakdown      ┃   ├─ verdict == warn                 
         ┃   + ablation            ┃   │    → operator 확인               
         ┃   + baseline 비교       ┃   │      (human_approval=true)       
         ┃                         ┃   └─ verdict == fail OR              
         ┃ LLM reasoning (GPT-4o): ┃       regression_risk == true         
         ┃  diagnostic_notes 생성   ┃        → 배포 차단 + dead_letter     
         ┗━━━━━━━━━━━┳━━━━━━━━━━━━━┛      + baseline 유지

         > 판정 임계값 SSOT (SHIP-fix NEW-4, 2026-05-06):
         >   verdict 4 임계값 + severity 3 임계값 = risk_config.yaml backtest_agent.deploy_decision_gate 참조.
         >   pass_sr/pass_ic/warn_sr/warn_ic + severity_none/low/medium_sr_threshold.
                     │
  21:30  ┌───────────▼──────────────┐   → updated keyword lists         → §8.6
         │ §8.5.1 뉴스 필터 + 규칙  │     (news_filter.yaml 등 5종
         │  갱신                    │      + trigger_catalog v3)
         │ 키워드 갱신              │
         │ (prefilter_drop_log 분석)│
         └───────────┬──────────────┘
                     │
  22:00  ┌───────────▼──────────────┐   → deployed bundle               → 다음 날 08:30
         │ §8.6 배포                 │      (factor + model + allocator
         │ - Backtest verdict 확인   │       + agent constraint)
         │ - sanity check            │
         │ - model_registry 교체     │
         │ - audit_log 기록          │
         └───────────────────────────┘

  ※ v2.2 핵심: Backtest Agent(21:00~21:30) 게이트를 통과해야만 §8.6 배포 단계로 진행.
  ※ Backtest Agent는 LLM reasoning만 수행, 계산은 C13 ValidationTools(BT Engine/Replay/Perf)가 담당.
  ※ GPT-4o만 사용. Kanana-o 일일 100회 예산은 장중(§9.4) 보존.
```

### 3.1 Mode B 상세 구조 (v2.1에서 계속)

```
  ┌──────────────────────────────────────────────────────────┐
  │                  매일 밤 진화 사이클                       │
  │                                                           │
  │  ┌───────────┐                                           │
  │  │ 오늘 성과  │                                           │
  │  │ 8차원 벡터 │                                           │
  │  │ [IC,ICIR, │                                           │
  │  │  RankIC,  │                                           │
  │  │  ARR,IR,  │                                           │
  │  │  -MDD,SR] │                                           │
  │  └─────┬─────┘                                           │
  │        │                                                  │
  │        ↓                                                  │
  │  ┌──────────────────┐                                    │
  │  │Thompson Sampling │                                    │
  │  │ "팩터? 모델?"    │                                    │
  │  └────┬────────┬────┘                                    │
  │       │        │                                          │
  │   팩터 개선  모델 개선                                     │
  │       │        │                                          │
  │       ↓        ↓                                          │
  │  ┌─────────────────────────────────────────────────┐     │
  │  │              Alpha Factor Engine                 │     │
  │  │                                                  │     │
  │  │  ┌────────┐        ┌────────┐      ┌────────┐  │     │
  │  │  │  Idea  │───────→│ Factor │─────→│  Eval  │  │     │
  │  │  │ Agent  │        │ Agent  │      │ Agent  │  │     │
  │  │  └────────┘        └────────┘      └───┬────┘  │     │
  │  │    ↑ 4요소 가설      │ AST 구현          │       │     │
  │  │    │ observation     │ Operator Lib     │       │     │
  │  │    │ knowledge       │                  │       │     │
  │  │    │ justification   │ 3중 정규화       │       │     │
  │  │    │ specification   │ ①독창성(AST)     │       │     │
  │  │    │                 │ ②정합(LLM)       │       │     │
  │  │    │                 │ ③복잡도(SL,PC)    │       │     │
  │  │    │                 │                  │       │     │
  │  │    │   feedback      │                  │       │     │
  │  │    └─────────────────┴──────────────────┘       │     │
  │  │                                                  │     │
  │  │  ┌─────────────────────────────────────────┐    │     │
  │  │  │ Alpha Decay Monitor                      │    │     │
  │  │  │ - 팩터별 IC 추이 추적                    │    │     │
  │  │  │ - IC 하락 감지 → 자동 은퇴               │    │     │
  │  │  │ - Factor Zoo 갱신                        │    │     │
  │  │  └─────────────────────────────────────────┘    │     │
  │  └─────────────────────────────────────────────────┘     │
  │                                                           │
  │  ┌─────────────────────────────────────────────────┐     │
  │  │           Agent Self-Improvement                 │     │
  │  │                                                  │     │
  │  │  ┌────────────────┐    ┌────────────────┐       │     │
  │  │  │   Handover     │    │  React Action  │       │     │
  │  │  │   Feedback     │───→│  (다음 날 08:30)│       │     │
  │  │  │ (장마감 정리)   │    │  constraint    │       │     │
  │  │  │                │    │  prompt 갱신    │       │     │
  │  │  │ - 판단+결과정리 │    │                │       │     │
  │  │  │ - 기여도 측정  │    │ "어제 Risk가   │       │     │
  │  │  │ - Memory 저장  │    │  veto 3건 실패  │       │     │
  │  │  └────────────────┘    │  → 임계값 조정" │       │     │
  │  │                        └────────────────┘       │     │
  │  └─────────────────────────────────────────────────┘     │
  │                                                           │
  │  ┌─────────────────────────────────────────────────┐     │
  │  │      Backtest Agent Gate (C12, S3-9 실구현 SHIP) │     │
  │  │      [21:00~21:30] BacktestEngine 직접 호출       │     │
  │  │                                                   │     │
  │  │  candidate_bundle = {factor, model, allocator}    │     │
  │  │                    ↓                              │     │
  │  │  C13 Tools (결정론적 계산):                         │     │
  │  │   ① BacktestEngine → IC/IR/MDD/SR                 │     │
  │  │   ② ReplayRunner → replay_trace, latency         │     │
  │  │   ③ PerformanceAnalyzer → regime + ablation       │     │
  │  │                    ↓                              │     │
  │  │  GPT-4o reasoning → diagnostic_notes              │     │
  │  │                    ↓                              │     │
  │  │  verdict: pass | warn | fail                      │     │
  │  │  regression_risk: {flagged, severity}             │     │
  │  │                                                   │     │
  │  │  pass + no regression → §8.6 배포 진행             │     │
  │  │  warn → operator 확인                              │     │
  │  │  fail → 배포 차단 + baseline 유지                  │     │
  │  └─────────────────────────────────────────────────┘     │
  │                                                           │
  │  [22:00] 배포: Backtest 통과 시 개선된 팩터+모델+에이전트 │
  │          → 다음 날. Backtest 차단 시 baseline 유지       │
  └──────────────────────────────────────────────────────────┘
```

## 4. 에이전트 통신 구조

```
  ┌──────────────────────────────────────────────────────┐
  │         Shared Message Pool (Blackboard Pattern)      │
  │                                                       │
  │  ┌─────────────────────────────────────────────────┐ │
  │  │ {message_id, content, cause_by, sent_from, send_to, │ │
  │  │  priority, confidence, reasoning, evidence_ids,  │ │
  │  │  uncertainty, timestamp,                          │ │
  │  │  prediction, risk_level,                          │ │
  │  │  ttl, expires_at, scope, event_id,               │ │
  │  │  supersedes, action_type, portfolio_patch_id}     │ │
  │  └─────────────────────────────────────────────────┘ │
  │                                                       │
  │        publish ↑     ↑ publish    ↑ publish           │
  │                │     │            │                    │
  │  ┌─────────┐  │  ┌─────────┐  ┌─────────┐           │
  │  │  News   │──┘  │  Risk   │──┘  │ Quant  │──┘       │
  │  │  Agent  │     │  Agent  │     │ Agent  │           │
  │  └─────────┘     └─────────┘     └─────────┘          │
  │                                                       │
  │  subscribe ↓     ↓ subscribe  ↓ subscribe             │
  │            │     │            │                        │
  │       ┌────┴─────┴────────────┘                       │
  │       ↓                                                │
  │  ┌──────────────────────┐                              │
  │  │        FDA           │                              │
  │  │  subscribes_to:      │                              │
  │  │  [news_signal,       │        ┌──────────┐         │
  │  │   risk_warning,      │───────→│ Debate   │         │
  │  │   quant_signal,      │충돌 시  │ Agent    │         │
  │  │   regime_change,     │←───────│          │         │
  │  │   debate_resolution] │        └──────────┘         │
  │  └──────────────────────┘                              │
  └──────────────────────────────────────────────────────┘

  의존성 기반 실행:
  ┌──────┐  ┌──────┐  ┌──────┐
  │ News │  │ Risk │  │Quant │  ← 병렬 (의존성 없음)
  └──┬───┘  └──┬───┘  └──┬───┘
     └─────────┴─────────┘
               │
          모두 완료 후
               ↓
          ┌────────┐
          │  FDA   │  ← 의존성 충족 후 활성화
          └────────┘
```

## 5. LLM 역할 분배도

```
┌─ LLM Router (v3 S2-2) ────────────────────────────────┐
│ call(prompt, mode, caller, structured_schema)          │
│   ↓ mode='hot' → RuntimeError (불변 원칙 4)             │
│   ↓ mode='mode_b' → caller 화이트리스트 검증 → GPT-4o   │
│   ↓ mode='cold':                                        │
│     _BudgetTracker.can_call(caller)                     │
│       ├─ PASS → _CircuitBreaker(kanana).can_attempt()   │
│       │          ├─ PASS → _call_kanana()               │
│       │          │   ├─ 성공 → record_success           │
│       │          │   └─ 실패 → record_failure + fallback│
│       │          └─ OPEN → fallback to GPT-4o           │
│       └─ 예산 초과 → fallback to GPT-4o                 │
│                                                         │
│ caller allocation (100회/일 총합):                      │
│   news_agent=30, dart=3, risk=15, community=5           │
│   fda_cold=12, debate=5, buffer=30                      │
└────────────────────────────────────────────────────────┘
```

## 6. 데이터 흐름 전체도

```
  [외부 데이터]
  KIS(1분봉) ─────────┐
  Naver News ─────────┤
  DART 공시 ──────────┤
  US Market ──────────┤
  ECOS 거시 ──────────┘
           │
           ↓
  ┌──── Layer 2: 전처리 ────┐
  │ Z-score → Multi-scale  │
  │ → TSFresh → PIT-safe   │
  └──────────┬─────────────┘
             │
      ┌──────┴──────┐
      ↓              ↓
  ┌────────┐   ┌──────────┐
  │ 정량    │   │  정성     │
  │ 데이터  │   │  데이터   │
  │(1분봉)  │   │(뉴스/공시)│
  └───┬────┘   └────┬─────┘
      │              │
      ↓              ↓
  ┌────────┐   ┌──────────────────────────┐
  │Layer 3 │   │       Layer 4            │
  │ 퀀트   │   │  News/Risk/Debate Agent  │
  │ 모델   │   │  (LLM 기반 정성 판단)     │
  └───┬────┘   └──────────┬───────────────┘
      │                   │
      │ quant_signal      │ agent reports
      │                   │
      └───────┬───────────┘
              │
              ↓
        ┌───────────┐
        │    FDA    │
        │ 종합 판단  │
        │ + CoT     │
        └─────┬─────┘
              │
              ↓
        ┌───────────┐      ┌───────────┐
        │ Layer 1   │─────→│ Layer 5   │
        │ 주문 실행  │result│ Knowledge │
        │ KIS API   │─────→│ Base 저장  │
        └───────────┘      └───────────┘

  [Sprint 4 인프라 모듈 (Mode A/B 공통)]
  ┌──────────────────────────────────────────────────────────┐
  │  DQR Runner (S4-5, Mode B stage_0)                       │
  │  8개 커넥터 × 5 메트릭 자동 품질 검사, CRITICAL 시 차단  │
  ├──────────────────────────────────────────────────────────┤
  │  DualSourceScorer (S4-1, FinBERT + 3-yaml + decay)       │
  │  뉴스 / 커뮤니티 divergence 5피처 → uncertainty 신호      │
  │  08:00 KST 배치 (dual_source_runner.py)                  │
  ├──────────────────────────────────────────────────────────┤
  │  PersistentCache (S4-7, SQLite TTL)                      │
  │  Cold Path 레이턴시 최적화, key=prompt_hash, TTL=config  │
  ├──────────────────────────────────────────────────────────┤
  │  HotPathProfiler (S4-4, 6단계 레이턴시 측정)             │
  │  p50/p95/p99 + SLA 100ms alert (ops/profiler.py)         │
  ├──────────────────────────────────────────────────────────┤
  │  Memory Restorer (S4-8, KB 5종 → agent 복원)             │
  │  시스템 시작 시 Bootstrap (상세: §7.6)                    │
  └──────────────────────────────────────────────────────────┘
```

## 7. 논문 매핑도

```
  ┌─────────────────────────────────────────────────────────┐
  │                  논문 → 아키텍처 매핑                     │
  │                                                          │
  │  AAPM ──────→ 에이전트 내부 품질                         │
  │               (기억, 반복정제, RAG, 자율판단, CoT)        │
  │                                                          │
  │  AlphaGAT ──→ Model Layer                                │
  │               (alpha factor, cross-asset, multi-scale)   │
  │                                                          │
  │  MetaGPT ───→ Agent Layer 조직                           │
  │               (Message Pool, Pub/Sub, Profile, Feedback) │
  │                                                          │
  │  RD-Agent ──→ 진화 루프 + 장중 자동화                     │
  │               (factor-model 공동최적화, bandit, KB)       │
  │                                                          │
  │  TradeXpert → 에이전트 전문화 + LLM 분배                  │
  │               (MoE, 프롬프트 전문화, Reprogramming)       │
  │                                                          │
  │  AlphaAgent → Alpha Decay 방지                           │
  │               (AST 독창성, 가설정합, 복잡도, 3중 정규화)  │
  └─────────────────────────────────────────────────────────┘
```

## 8. Agent Profile 상세 (MetaGPT)

```
  ┌───────────────────────────────────────────────────────────────────┐
  │                    Agent Profile 정의                              │
  │                                                                    │
  │  ┌─────────────────────────────────────────────────────────────┐  │
  │  │  News Agent (S2-7 실구현)                                    │  │
  │  │  Profile : 한국 주식 뉴스/공시 분석 전문가                   │  │
  │  │  Goal    : 뉴스/공시 투자 영향 판단 + CoT reasoning         │  │
  │  │  Constraint: 무관 뉴스 skip (NewsFilter 3-level), raw data 접근 금지 │  │
  │  │  Subscribes: [naver_news, dart_disclosure, community]        │  │
  │  │  Publishes : C5 [news_signal, dart_alert] (community → news_signal)  │  │
  │  │  LLM     : LLMRouter.call(mode='cold', caller='news_agent') │  │
  │  │             Kanana-o CoT → GPT-4o fallback                  │  │
  │  │  TextPack: consume_text_pack() ← TextPackBuilder (13 템플릿) │  │
  │  │  Memory  : micro_notes (종목별 JSONL) / macro_notes (거시 JSONL) │  │
  │  └─────────────────────────────────────────────────────────────┘  │
  │                                                                    │
  │  ┌─────────────────────────────────────────────────────────────┐  │
  │  │  Risk Agent                                                  │  │
  │  │  Profile : 시장 리스크 감시 전문가                           │  │
  │  │  Goal    : 퀀트가 못 잡는 리스크 감지 + 경고                │  │
  │  │  Constraint: 정량은 퀀트에 위임, 정성적 판단만               │  │
  │  │  Subscribes: [us_market, ecos_macro, news_signal, quant_alert]│ │
  │  │  Publishes : [risk_warning, regime_change, veto_recommendation]│  │
  │  │  Memory  : Macro Notes (거시경제 누적, 감쇠 η∈0.95~0.99)    │  │
  │  └─────────────────────────────────────────────────────────────┘  │
  │                                                                    │
  │  ┌─────────────────────────────────────────────────────────────┐  │
  │  │  Quant Agent                                                 │  │
  │  │  Profile : 퀀트 모델 래퍼 + 시그널 생성기                   │  │
  │  │  Goal    : 1분봉 예측 시그널 + 이상 탐지                    │  │
  │  │  Constraint: 모델 추론만, 정성 판단 위임, 속도 최우선        │  │
  │  │  Subscribes: [kis_1min_bar]                                  │  │
  │  │  Publishes : [quant_signal, quant_alert, anomaly_detected]   │  │
  │  │  Memory  : 예측 이력 + 정확도 추적                          │  │
  │  └─────────────────────────────────────────────────────────────┘  │
  │                                                                    │
  │  ┌─────────────────────────────────────────────────────────────┐  │
  │  │  Debate Agent                                                │  │
  │  │  Profile : 의견 충돌 해소 + 종목 pairwise 비교 전문가       │  │
  │  │  Goal    : 양측 근거 분석 + Top-10 pairwise 랭킹            │  │
  │  │  Constraint: 충돌 감지 또는 pairwise 요청 시 활성화          │  │
  │  │  Subscribes: [conflict_detected, pairwise_request]           │  │
  │  │  Publishes : [debate_resolution, pairwise_ranking]           │  │
  │  │  Memory  : 논쟁 이력 + pairwise 정확도                      │  │
  │  └─────────────────────────────────────────────────────────────┘  │
  │                                                                    │
  │  ┌─────────────────────────────────────────────────────────────┐  │
  │  │  FDA (Orchestrator)                                          │  │
  │  │  Profile : 최종 투자 판단 오케스트레이터                     │  │
  │  │  Goal    : 모든 보고서 종합 → approve/veto + CoT            │  │
  │  │  Constraint: 모든 에이전트 수신 후 판단, 불확실하면 veto     │  │
  │  │  Action  : {approved, target_weights(읽기만), order_deltas,  │  │
  │  │            veto_reason, reason_code, risk_overrides,          │  │
  │  │            confidence, expiry}                                │  │
  │  │  Subscribes: [news_signal, risk_warning, quant_signal,       │  │
  │  │              debate_resolution, regime_change, dart_alert]    │  │
  │  │  Publishes : [final_decision]                                │  │
  │  │  Memory  : 판단 이력 + 결과 피드백                          │  │
  │  └─────────────────────────────────────────────────────────────┘  │
  └───────────────────────────────────────────────────────────────────┘
```

## 9. FDA 반복 정제 3라운드 (AAPM + TradeXpert)

```
  ┌─────────────────────────────────────────────────────────┐
  │              FDA 판단 루프 (bounded, max 3)               │
  │                                                          │
  │  ┌──────────────────────────────────────────────┐       │
  │  │ Round 1: 에이전트 보고서 종합                  │       │
  │  │                                               │       │
  │  │  News Report ──┐                              │       │
  │  │  Risk Report ──┼──→ FDA 1차 판단              │       │
  │  │  Quant Signal ─┘                              │       │
  │  └──────────────────────┬───────────────────────┘       │
  │                         ↓                                │
  │  ┌──────────────────────────────────────────────┐       │
  │  │ Round 2: RAG 검색 + 판단 보정 (AAPM)          │       │
  │  │                                               │       │
  │  │  1차 판단 ──→ Vector DB에서 유사 상황 검색    │       │
  │  │              "과거에 이런 상황에서 뭘 했나?"   │       │
  │  │              ──→ 판단 보정                     │       │
  │  └──────────────────────┬───────────────────────┘       │
  │                         ↓                                │
  │  ┌──────────────────────────────────────────────┐       │
  │  │ Round 3: [충돌 감지 시만] Debate pairwise       │       │
  │  │                                               │       │
  │  │  충돌 시: Debate 10종목 pairwise (45회)        │       │
  │  │           win count → Top-K 재랭킹            │       │
  │  │  비충돌: 퀀트 점수 순 유지, Debate 미호출       │       │
  │  │  ※ LLM comparator 비이행적 → 모든 쌍 비교     │       │
  │  └──────────────────────┬───────────────────────┘       │
  │                         ↓                                │
  │          PPO Allocator → 비중 결정 → FDA approve/veto    │
  └─────────────────────────────────────────────────────────┘
```

## 10. 동적 LLM 라우팅 (TradeXpert MoE + RD-Agent Bandit)

```
  ┌────────────────────────────────────────────────────────┐
  │              동적 LLM 라우팅 (Thompson Sampling)         │
  │                                                         │
  │  입력: x_t = [시장 상태, 이벤트 유형, 시간대, 변동성]    │
  │                                                         │
  │  선택지 A = {Kanana-o만, GPT-4o만, 둘 다, 호출 안 함}   │
  │                                                         │
  │  ┌────────────────────────────────────────────────┐    │
  │  │  상황              →  LLM 선택                  │    │
  │  │  ─────────────────────────────────────────────  │    │
  │  │  한국어 뉴스 감지   →  Kanana-o만               │    │
  │  │  팩터 이상 감지     →  GPT-4o만 (추론)          │    │
  │  │  급락 + 뉴스 동시   →  둘 다 호출               │    │
  │  │  평상시             →  호출 안 함 (퀀트만)      │    │
  │  └────────────────────────────────────────────────┘    │
  │                                                         │
  │  과거 각 선택의 성과를 Bayesian posterior 갱신            │
  │  → 점점 최적 LLM 조합 학습                              │
  └────────────────────────────────────────────────────────┘
```

## 11. 1분봉 병목 해결 전략

```
  ┌────────────────────────────────────────────────────────┐
  │           1분봉 병목 문제 + 해결 전략                     │
  │                                                         │
  │  문제: active 20 × LLM 4.7초/종목 = 141초 > 60초 (1분)     │
  │                                                         │
  │  ┌──────────────────────────────────────────────────┐  │
  │  │  해결 1: 평상시 퀀트만 (LLM 미호출)               │  │
  │  │                                                   │  │
  │  │  매 1분: 퀀트 모델 추론 (수 ms) → 판단 → 실행    │  │
  │  │          LLM 에이전트: 호출 안 함                 │  │
  │  │          → 1분 안에 충분                          │  │
  │  └──────────────────────────────────────────────────┘  │
  │                                                         │
  │  ┌──────────────────────────────────────────────────┐  │
  │  │  해결 2: 이벤트 시 선택적 LLM 호출                │  │
  │  │                                                   │  │
  │  │  트리거: 뉴스감지 | 급등락 | regime변화 | anomaly │  │
  │  │          ↓                                        │  │
  │  │  active 20 전부 X → 변화 감지 종목만 LLM 처리       │  │
  │  └──────────────────────────────────────────────────┘  │
  │                                                         │
  │  ┌──────────────────────────────────────────────────┐  │
  │  │  해결 3: 캐싱 (TTL 기반)                          │  │
  │  │                                                   │  │
  │  │  뉴스 분석 결과  → 캐싱 (TTL: 장중)              │  │
  │  │  에이전트 보고서  → 캐싱 (TTL: 5분 or 이벤트)    │  │
  │  │  팩터 IC 결과    → 캐싱 (TTL: 1일)               │  │
  │  │  → 같은 뉴스 재분석 안 함                        │  │
  │  └──────────────────────────────────────────────────┘  │
  │                                                         │
  │  ┌──────────────────────────────────────────────────┐  │
  │  │  해결 4: 2단계 필터링                             │  │
  │  │                                                   │  │
  │  │  Step 1: 퀀트 모델 → Top 10 필터링 (빠름)       │  │
  │  │  Step 2: LLM → 10종목만 pairwise (45회)         │  │
  │  │          30종목 전체 비교(435회)는 비용 과다      │  │
  │  └──────────────────────────────────────────────────┘  │
  └────────────────────────────────────────────────────────┘
```

## 12. Knowledge Base 통합 형식 (MetaGPT 메시지 + RD-Agent KB)

```
  ┌────────────────────────────────────────────────────────┐
  │          KB 통합 저장 형식 (교차 시너지 #1, #3)          │
  │                                                         │
  │  에이전트 소통(Message Pool)과 지식 축적(KB)을           │
  │  동일 JSON 형식으로 통합                                 │
  │                                                         │
  │  ┌──────────────────────────────────────────────────┐  │
  │  │  {                                               │  │
  │  │    "message_id": "KB-20260405-001",              │  │
  │  │    "content": "반도체 -2% 시 SELL 정답",         │  │
  │  │    "cause_by": "EvalAgent_backtest",             │  │
  │  │    "sent_from": "EvalAgent",                     │  │
  │  │    "situation": "반도체 -2% + VIX↑ + 외국인매도", │  │
  │  │    "decision": "SK하이닉스 HOLD",                │  │
  │  │    "outcome": "-1.5% 추가 손실",                 │  │
  │  │    "lesson": "이 패턴 = 즉시 SELL",              │  │
  │  │    "hypothesis": "관련 팩터 가설",                │  │
  │  │    "ast_hash": "abc123",                         │  │
  │  │    "timestamp": "2026-04-05T10:30:00",            │  │
  │  │    "ttl": 300,                                    │  │
  │  │    "scope": "ticker:000660",                      │  │
  │  │    "event_id": "EVT-20260405-VIX",                │  │
  │  │    "supersedes": null,                            │  │
  │  │    "action_type": "signal",                       │  │
  │  │    "portfolio_patch_id": "PP-20260405-002"        │  │
  │  │  }                                               │  │
  │  └──────────────────────────────────────────────────┘  │
  │                                                         │
  │  → Vector DB에 저장 (similarity 검색용)                 │
  │  → RAG: 유사 상황 Top-K 검색 (AAPM)                    │
  │  → 에이전트 소통과 지식 축적이 같은 형식                 │
  │  → v2.1 추가 7필드: ttl, expires_at, scope, event_id,   │
  │    supersedes, action_type, portfolio_patch_id            │
  └────────────────────────────────────────────────────────┘
```

## 13. 실패 카테고리별 학습 (AlphaAgent)

```
  ┌────────────────────────────────────────────────────────┐
  │               실패 카테고리 분류 저장                     │
  │                                                         │
  │  ┌──────────────────────┐  ┌──────────────────────┐   │
  │  │ hypothesis_           │  │ complexity_           │   │
  │  │ misalignment          │  │ violation             │   │
  │  │                       │  │                       │   │
  │  │ "가설: 유동성 포착    │  │ "AST 노드 50개 초과  │   │
  │  │  수식: volume 없음"   │  │  파라미터 8개 초과"   │   │
  │  └──────────────────────┘  └──────────────────────┘   │
  │                                                         │
  │  ┌──────────────────────┐  ┌──────────────────────┐   │
  │  │ decay_detected        │  │ crowding_risk         │   │
  │  │                       │  │                       │   │
  │  │ "초기 IC 0.03 →      │  │ "Alpha158의 RSI와   │   │
  │  │  3개월 후 IC 0.005"   │  │  AST 유사도 0.95"    │   │
  │  └──────────────────────┘  └──────────────────────┘   │
  │                                                         │
  │  ┌──────────────────────┐                              │
  │  │ execution_failure     │                              │
  │  │                       │  → Factor Agent가 실패     │
  │  │ "코드 실행 에러      │    카테고리 참조 후 회피    │
  │  │  ZeroDivisionError"   │  → 같은 유형 반복 방지     │
  │  └──────────────────────┘                              │
  └────────────────────────────────────────────────────────┘
```

## 14. Cross-Asset Attention 계층화 (AlphaGAT × Multi-scale)

```
  ┌────────────────────────────────────────────────────────┐
  │        Multi-scale Cross-Asset Attention                 │
  │                                                         │
  │  1분봉 Raw Data (active 20 × 8 features)                 │
  │            ↓ Multi-scale Decomposition                   │
  │                                                         │
  │  ┌──────────────────────────────────────────────────┐  │
  │  │  1분 스케일   → Cross-Asset Attention (MHA)       │  │
  │  │  의미: 종목 간 미시 연동                          │  │
  │  │  예시: 삼성전자 급락 → SK하이닉스 틱 동조         │  │
  │  └──────────────────────┬───────────────────────────┘  │
  │                         ↓                                │
  │  ┌──────────────────────────────────────────────────┐  │
  │  │  5분 스케일   → Cross-Asset Attention (MHA)       │  │
  │  │  의미: 섹터 내 단기 추세 연동                     │  │
  │  │  예시: 반도체 섹터 전체 동반 하락                  │  │
  │  └──────────────────────┬───────────────────────────┘  │
  │                         ↓                                │
  │  ┌──────────────────────────────────────────────────┐  │
  │  │  30분 스케일  → Cross-Asset Attention (MHA)       │  │
  │  │  의미: 섹터 간 리스크 연동                        │  │
  │  │  예시: 금융 섹터 약세 → 산업재 전이               │  │
  │  └──────────────────────┬───────────────────────────┘  │
  │                         ↓                                │
  │  ┌──────────────────────────────────────────────────┐  │
  │  │  60분 스케일  → Cross-Asset Attention (MHA)       │  │
  │  │  의미: 시장 전체 regime 연동                      │  │
  │  │  예시: KOSPI 전체 하방 압력                       │  │
  │  └──────────────────────┬───────────────────────────┘  │
  │                         ↓                                │
  │              Hierarchical Fusion                         │
  │         (4 스케일 시그널 계층적 통합)                     │
  │         미시 = 빠른 반응, 거시 = 큰 방향                 │
  └────────────────────────────────────────────────────────┘
```

## 15. Economy of Minds (MetaGPT)

```
  ┌────────────────────────────────────────────────────────┐
  │            에이전트 기여도 동적 조정                      │
  │                                                         │
  │  ┌──────────────────────────────────────────────────┐  │
  │  │  매 판단 후 기여도 측정:                          │  │
  │  │                                                   │  │
  │  │  Risk Agent veto → 실제 -3% 하락 방지            │  │
  │  │  → Risk Agent 영향력 + (기여 인정)               │  │
  │  │                                                   │  │
  │  │  News Agent 경고 → 실제로는 반등                  │  │
  │  │  → News Agent 영향력 - (오판)                    │  │
  │  │                                                   │  │
  │  │  Quant Signal BUY → 실제 +2% 수익               │  │
  │  │  → Quant Agent 영향력 + (정확)                   │  │
  │  └──────────────────────────────────────────────────┘  │
  │                         ↓                                │
  │  ┌──────────────────────────────────────────────────┐  │
  │  │  FDA가 다음 판단 시 가중치 반영:                  │  │
  │  │                                                   │  │
  │  │  영향력 높은 에이전트 의견 → 가중치 ↑             │  │
  │  │  영향력 낮은 에이전트 의견 → 가중치 ↓             │  │
  │  │                                                   │  │
  │  │  Thompson Sampling 신뢰도 bandit과 연동          │  │
  │  └──────────────────────────────────────────────────┘  │
  └────────────────────────────────────────────────────────┘
```

## 16. 교차 시너지 5가지 매핑

```
  ┌────────────────────────────────────────────────────────┐
  │              논문 간 교차 시너지 매핑                     │
  │                                                         │
  │  #1  AlphaAgent AST + RD-Agent KB 통합                  │
  │      → KB에 AST 포함 저장, 별도 Factor Zoo 불필요       │
  │      적용: Layer 5 Knowledge Base                       │
  │                                                         │
  │  #2  AAPM 반복 정제 + TradeXpert pairwise 비교          │
  │      → FDA Round 1(종합) → 2(RAG) → 3([충돌시] Debate)  │
  │      적용: Layer 4 FDA 판단 루프                        │
  │                                                         │
  │  #3  MetaGPT 메시지 형식 + RD-Agent KB 저장             │
  │      → 에이전트 소통과 지식 축적이 같은 JSON 형식       │
  │      적용: Layer 4 + Layer 5 통합 형식                  │
  │                                                         │
  │  #4  AlphaGAT Cross-Asset × Multi-scale 계층화          │
  │      → 1분/5분/30분/60분 스케일별 Cross-Asset → Fusion  │
  │      적용: Layer 3 퀀트 모델                            │
  │                                                         │
  │  #5  TradeXpert MoE + RD-Agent Bandit = 동적 LLM 라우팅 │
  │      → Thompson Sampling이 LLM 호출 여부+선택 결정      │
  │      적용: Layer 4 LLM Router                           │
  └────────────────────────────────────────────────────────┘
```

## 17. 자율 판단 Skip/Analyze (AAPM)

```
  ┌────────────────────────────────────────────────────────┐
  │           News Agent 자율 판단 로직                      │
  │                                                         │
  │  뉴스/공시 수신                                         │
  │       ↓                                                  │
  │  ┌────────────────────────────┐                         │
  │  │ "이 뉴스가 투자에           │                         │
  │  │  관련 있는가?" (LLM 판단)   │                         │
  │  └──────┬──────────┬──────────┘                         │
  │         │          │                                     │
  │       관련 있음   무관                                   │
  │         │          │                                     │
  │         ↓          ↓                                     │
  │  ┌──────────┐ ┌──────────┐                              │
  │  │ Analyze  │ │  Skip    │                              │
  │  │ 분석 수행 │ │ LLM 호출 │                              │
  │  │ → publish│ │ 안 함    │                              │
  │  └──────────┘ └──────────┘                              │
  │                                                         │
  │  효과: LLM 호출 비용 절감 + 노이즈 감소                 │
  │  기준: 종목 관련성, 투자 영향도, 시급성                  │
  └────────────────────────────────────────────────────────┘
```

## 18. 장중↔장마감 Memory 순환 연결 (GPT Pro #6)

```
  ┌── 장중 (09:00~15:30) ──────────────────────────────────┐
  │                                                         │
  │  에이전트별 memory 실시간 축적                           │
  │  + performance_metrics 7항목 갱신                        │
  │  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐        │
  │  │News  │ │Risk  │ │Quant │ │Debate│ │ FDA  │        │
  │  │micro │ │macro │ │pred  │ │pair  │ │decis │        │
  │  │notes │ │notes │ │hist  │ │hist  │ │hist  │        │
  │  └──┬───┘ └──┬───┘ └──┬───┘ └──┬───┘ └──┬───┘        │
  └─────┴────────┴────────┴────────┴────────┴──────────────┘
                          │ 장마감 (15:30)
                          ↓
  ┌── 자동 집계 (18:00) ───────────────────────────────────┐
  │                                                         │
  │  performance_metrics → 8차원 벡터 변환                   │
  │  v = [pred_acc, pnl, veto, slip, anomaly, sector, fp,   │
  │       overall]                                           │
  │                                                         │
  │  에이전트 기여도 분해 (ablation)                         │
  │  → Knowledge Base에 벡터 저장                            │
  │  → 과거 유사 시장 상태에서 최적 에이전트 조합 검색 가능  │
  └──────────────────────┬──────────────────────────────────┘
                          │
                          ↓
  ┌── 다음 날 반영 (08:30) ────────────────────────────────┐
  │                                                         │
  │  Thompson Sampling posterior 업데이트                     │
  │  저성과 에이전트 → 신뢰도 ↓                              │
  │  고성과 에이전트 → 신뢰도 ↑                              │
  │  Constraint Prompt 자기 갱신                              │
  └─────────────────────────────────────────────────────────┘
```

## 19. 확정 파이프라인 순서 (GPT Pro #2)

```
  LightGBM (active 20 시그널, 0.3ms)
       ↓
  Top-10 필터링 (퀀트 점수 순)
       ↓
  [충돌 시] Debate Agent pairwise → 재랭킹 (평상시: 퀀트 점수 순 유지)
       ↓
  PPO Allocator (종목별 비중 학습, stable-baselines3)
       ↓
  상한 규칙 적용 (단일≤20%, 섹터≤40%, 현금≥10%, default)
       ↓
  Portfolio Manager → order_deltas 생성 (현재 포지션 vs target_weights 차분)
       ↓
  FDA approve/veto (에이전트 보고서 있으면 참고, order_deltas 검토만, 생성/수정 불가)
       ↓
  KIS API 주문 실행

  ※ API Contracts 18개 (C1~C18) 명세: new/specs/api_contracts.md
  ※ 구현 순서: C1→C4→C5→C8→C9→C10 → inference → 검증 → Mode B
```


## 20. Evaluation Matrix 3-Layer (기말발표 킬러)

> "기존 퀀트는 '얼마 벌었나'만 본다.
> 우리는 '누가, 왜, 언제 결정했고, 그게 맞았나'를 매트릭으로 측정한다."

구현 상태 (2026-04-21 기준):
- L1: Sprint 1 구현 완료 (`new/src/models/metrics.py`, C12/C13)
- L2: Sprint 2~3 구축 예정 (C18 AgentPerformanceContract 신설됨, 2026-04-21)
- L3: Sprint 4 구축 예정 (설계 완료)

상세 스펙: `new/docs/evaluation_metrics.md` 참조.

### 20.1 L1 Model Layer: 8차원 성과 벡터

구현: `new/src/models/metrics.py::MetricsBundle.to_performance_vector()`
SSOT: architecture.md §8.1

8축 방사형 차트 구조:

| 축 | 지표 | 정상 범위 |
|---|---|---|
| 1 | IC | >= 0.02 |
| 2 | ICIR | >= 0.3 |
| 3 | Rank(IC) | 30일 rolling 정규화 0~1 |
| 4 | Rank(ICIR) | 동일 |
| 5 | ARR (연환산) | >= 8% |
| 6 | IR | >= 0.5 |
| 7 | -MDD (역부호) | >= -20%, 높을수록 좋음 |
| 8 | SR (Sharpe) | >= 1.0 |

```
ASCII 방사형 (8각형 스케치):

           IC
           |
   Rank(IC)+      +ICIR
          /|\    /|\
         / | \  / | \
SR ------*  |  *  |  *------ Rank(ICIR)
         \ | /  \ | /
          \|/    \|/
    -MDD---+      +IR
           |
           ARR
```

정규화: min-max rolling 30일, clip [0.01, 0.99].
config: `risk_config.yaml evaluation.performance_vector_rolling_window: 30`.

### 20.2 L2 Agent Contribution: 에이전트별 marginal PnL

구현 예정: Sprint 2 S2-0 AuditLogger 확장 -> 18:00 KST 배치 집계.
SSOT: api_contracts.md C18 AgentPerformanceContract.

7 에이전트 marginal PnL 막대그래프 mock (2026-03 FOMC 시나리오 가정):

| Agent | marginal PnL 기여 | 상태 |
|---|---|---|
| Quant | +0.31% | Sprint 2 설계 완료 |
| News Agent | +0.18% | Sprint 2 설계 완료 |
| Risk (Fast) | +0.09% | Sprint 2 설계 완료 |
| Risk (Slow) | +0.03% | Sprint 2 설계 완료 |
| FDA | 0.00% (판단만, 비중 미개입) | Sprint 2 설계 완료 |
| Debate | -0.02% | Sprint 2 설계 완료 |
| Execution GW | -0.04% (slippage) | Sprint 2 설계 완료 |

```
ASCII 막대그래프:

Quant     |================+0.31%
News      |=========+0.18%
Risk(F)   |====+0.09%
Risk(S)   |==+0.03%
FDA       | 0.00%
Debate    |-0.02%
Exec GW   |--0.04% (slippage)
          +------------------+
          -0.1%   0%   +0.3%
```

수치는 시나리오 예시. Sprint 2 구현 후 실 audit_log.jsonl 집계로 대체.

### 20.3 L3 System OS Metrics

구현 예정: Sprint 4 통합 Dashboard. `hot_path_latency_p95_ms` 만 Sprint 1 구현 완료.

#### Cause Attribution 파이 차트 (FDA reason_code 분포 mock)

```
FDA reason_code 분포 (2026-03-20 FOMC 이벤트 시나리오):

        NEWS_DIVERGENCE
           [========] 72%
       /
      +  DEBATE_CONFLICT [==] 18%
       \
        RISK_FAST_TRIGGER [=] 10%
```

| reason_code | 비율 | 의미 |
|---|---|---|
| NEWS_DIVERGENCE | 72% | 뉴스 vs 커뮤니티 방향 불일치 -> FDA 개입 |
| DEBATE_CONFLICT | 18% | 에이전트 간 의견 충돌 -> FDA 중재 |
| RISK_FAST_TRIGGER | 10% | 리스크 규칙 트리거 -> FDA veto |

Cause Attribution Accuracy: 64% (사후 일치 비율, Sprint 4 측정 후 확정).

#### Hot Path Latency SLA 히트맵

| 종목 | p50 (ms) | p95 (ms) | p99 (ms) | SLA |
|---|---|---|---|---|
| 005930 (삼성전자) | 11 | 38 | 51 | OK |
| 000660 (SK하이닉스) | 12 | 41 | 58 | OK |
| 035720 (카카오) | 14 | 44 | 63 | OK |
| 유니버스 전체 (20종목) | 13 | 87 | 97 | OK |
| SLA 기준 | - | <100ms | - | - |

구현: `ops/monitor.py` (Sprint 1 완료). SSOT: `risk_config.yaml quant_agent.latency_p95_target_ms: 100`.

#### Self-Evolution Gain 시계열 mock

```
Sharpe lift (Mode B 야간 재학습 전후):

1.50 +
     |   v1 (기준)
1.58 +   [====] +0.08 (Day 1 Mode B 후)
1.63 +   [========] +0.13 (Day 5)
1.71 +   [============] +0.21 (Day 10)
     |
     +----------------
     D0   D1   D5  D10
```

구현 예정: `ModelRegistry.compare_versions()` 이미 Sprint 1 선행 구현 완료.
실 적용은 Sprint 3 야간 재학습 + Sprint 4 Dashboard 통합 시.
임계: `risk_config.yaml system_os_metrics.self_evolution_gain_threshold: 0.05`.

### 20.4 발표 킬러 문장 (4 레벨)

- **총괄**: "기존 퀀트는 '얼마 벌었나'만 본다. 우리는 '누가, 왜, 언제 결정했고, 그게 맞았나'를 매트릭으로 측정한다."
- **L1**: "walk-forward 8 fold 기준 SR 1.84, IC 0.031, MDD -12.3%. 업계 표준 퀀트 모델이 하는 것."
- **L2**: "News Agent가 3월 FOMC에서 +0.18% 기여. audit_log.jsonl 14:31:07 엔트리에 남아 있음."
- **L3**: "FDA 한 달 누적: 전체 판단의 58%가 NEWS_DIVERGENCE, 그 중 71%가 실제 가격 하락. '왜 거부했는가'에 숫자 답 가능한 시스템."

### 20.5 발표용 구현 상태 3 Tier

| 상태 | 포함 지표 | 슬라이드 표기 |
|---|---|---|
| 구현 완료 (Sprint 1) | L1 전체, Hot Path Latency, ModelRegistry.compare_versions | 실수치 직접 제시 |
| 구현 완료 (W2 P1, 2026-05-09 SHIP) | **L3 cause_attribution_accuracy + reason_code_distribution** | `new/src/eval/cause_attribution.py`, `new/src/eval/reason_code_stats.py`, `new/src/eval/synth_audit_log.py`, 15 unit test PASS, accuracy 0.755 / Top-3 coverage 0.869 (synthetic) |
| 구현 진행 (Sprint 2) | L2 7종 (precision/recall 쌍 포함하여 9 key) | "Sprint 2 구현 중, 설계 완료" 뱃지 |
| 설계 완료 (Sprint 4+, defer) | L3 self_evolution_gain, dual_source_lead_time, regime_agent_contribution (`new/src/analytics/` 미구현) | "Sprint 4 예정, 스키마 확정" 뱃지 |

mock 숨기지 않음. 설계 완료된 mock과 근거 없는 mock을 구별. SSOT 근거: C18 신설 (api_contracts.md v3.2), architecture.md §8.1 + §4.2 + evaluation_metrics.md v1.1 (2026-05-09).

---

## 5. v3 즉시 반영: Dual-Source Temporal Signal

```
뉴스/공시 (당일, 빠른 decay) ──→ news_score_t ────────────────┐
                                                              │
커뮤니티 (전일/전전일, 느린 decay) ─→ comm_score_t-1/t-2 ─────┼─→ LightGBM feature pack
                                                              │
게시량 급증 z-score ───────────────→ community_noise_multiplier ─┤
                                                              │
news_score_t vs comm_score_t-1 ─→ |difference| = divergence ───┘
                                                 │
                                                 ├─ Hot: Risk Fast sidecar → uncertainty penalty
                                                 └─ Cold: Risk Slow → "왜 불일치인가" CoT 해석
```

핵심 메시지: **같은 텍스트라도 뉴스와 커뮤니티는 반영 속도와 신뢰도가 다르다.** v3는 이 차이를 감성 평균으로 뭉개지 않고, **소스 간 방향 불일치 자체를 불확실성 신호**로 쓴다.

## 6. Sprint 5 확장: Dynamic Event Universe (설계도)

```
Trade Universe (active 20)                      Watch Universe (KOSPI200)
┌──────────────────────────┐                    ┌──────────────────────────────┐
│ LightGBM + PPO + PM      │                    │ 뉴스/공시/커뮤니티/수급/가격   │
│ Hot Path core            │                    │ snapshot 감시                 │
└─────────────┬────────────┘                    └──────────────┬───────────────┘
              │                                               │
              │                                        Risk Fast rule match
              │                                               │
              │                                  candidate_pool (max 10)
              │                                               │
              │                                   dynamic holdings (3~5)
              │                                               │
              └────────────────────────────── FDA / Execution overlay ─────┘
```

원칙:
- trade universe와 watch universe는 분리
- 매분 교체형 랭킹이 아니라 **이벤트 드리븐 overlay**
- PPO 미관여, 소형 고정 비중 규칙 적용
- 장마감/TTL/stop-loss 기반 당일 청산

---

## 7. Sprint 3 추가 다이어그램

### 7.1 ModeBScheduler 8-stage Cron Flow

```
┌─────────────────────────────────────────────────────────────┐
│  ModeBScheduler (C14) — 18:00~22:00 KST 8-stage cron       │
│                                                             │
│  18:00 stage_0 (120s SLA) → DQR (CRITICAL alert 시 파이프라인 차단)│
│  18:02 stage_1 (30s SLA)  → performance_vector (8d)        │
│  18:30 stage_2 (60s SLA)  → direction ∈ {factor, model}    │
│  19:00 stage_3 (3600s)    → factor_candidate (Alpha Engine) │
│  20:00 stage_4 (1800s)    → model_candidate (Co-STEER)      │
│  20:30 stage_5 (1800s)    → agent_constraint_update         │
│  21:00 stage_6 (1800s)    → backtest_report (Backtest Agent)│
│  22:00 stage_7 (900s)     → deployed bundle or hold         │
│                                                             │
│  실패 시: on_retry_fail = baseline_hold                     │
│  감사 로그: artifacts/mode_b_audit_log.jsonl                │
└─────────────────────────────────────────────────────────────┘
```

### 7.2 ModeBDeployer Atomic Swap 6-step

```
┌───────────────────────────────────────────────────────┐
│  ModeBDeployer (C14 sub-component)                    │
│                                                       │
│  Step 1: backtest_report.verdict == pass 확인         │
│  Step 2: sanity_check() — NaN/Inf/shape 검증          │
│  Step 3: candidate 검증                               │
│          artifacts/bundles/{bundle_id}/ required 4종  │
│  Step 4: backup live → artifacts/backup/{deploy_id}/ │
│  Step 5: atomic swap candidate → live artifacts/      │
│          factor_zoo + lgbm + committee + ppo          │
│  Step 6: audit_log 기록 (DEPLOY-{yyyymmdd}-{UUID8})   │
│                                                       │
│  실패 시: 롤백 + dead_letter_log + baseline 유지       │
└───────────────────────────────────────────────────────┘
```

### 7.2.1 EvalRunner (C14 sub-component, 2026-05-09 W2 P1 SHIP)

```
┌───────────────────────────────────────────────────────┐
│  EvalRunner (C14 sub-component)                       │
│  Trigger: stage_1 직후 (18:02 KST batch)              │
│  PIT-Safety: snapshot_hour 18 KST 이후만 valid 산출   │
│                                                       │
│  L3 metrics (artifacts/metrics/):                     │
│  ├─ reason_code_distribution_{yyyymmdd}.json          │
│  │    Top-3 coverage threshold 0.80                   │
│  └─ cause_attribution_{yyyymmdd}.json                 │
│       Accuracy threshold 0.60 (발표 킬러 지표)        │
│                                                       │
│  입력: artifacts/audit_log.jsonl (C18 20 필드)        │
│        label_t5_ret + label_backfilled_at             │
│        + label_backfill_source (PIT-Safety meta)      │
│                                                       │
│  실패 시: PASS=false 메트릭 → operator alert          │
└───────────────────────────────────────────────────────┘
```

### 7.3 KnowledgeBase 6 Storage Types (Layer 5)

```
┌─────────────────────────────────────────────────────────┐
│  KnowledgeBase (S3-11) — Layer 5                        │
│                                                         │
│  write(msg: Message) → KB-{yyyymmdd}-{UUID8}            │
│    └─ timestamp <= now() PIT-Safety 검증                │
│    └─ required_fields 검증 (content/sent_from/timestamp)│
│                                                         │
│  storage_types (6):                                     │
│  ┌─────────────────────────────────────────────────┐    │
│  │ micro_notes      │ macro_notes                  │    │
│  │ debate_history   │ decision_history             │    │
│  │ backtest_history │ factor_zoo                   │    │
│  └─────────────────────────────────────────────────┘    │
│                                                         │
│  search(query, top_k=5) → recency_boost exp(-λ*days)   │
│  read(kb_id) → Message                                  │
└─────────────────────────────────────────────────────────┘
```

### 7.4 Committee (AlphaGAT Stage II) 앙상블

```
┌─────────────────────────────────────────────────────────┐
│  Committee (S3-8) — AlphaGAT Stage II                   │
│                                                         │
│  ┌────────────────┐                                     │
│  │  tree_core     │ LightGBM (hot path 모델)             │
│  │  (base)        │ → signal_proba                      │
│  └───────┬────────┘                                     │
│          │                                              │
│  ┌───────▼────────┐  ┌─────────────────┐               │
│  │  CNN           │  │  MetaFuser       │               │
│  │  confirmatory  │  │  LogisticReg OOF │               │
│  │  (Conv1d)      │→─│  → ensemble_pred │               │
│  └────────────────┘  └────────┬────────┘               │
│                               │                         │
│  Sharpe(committee) > Sharpe(tree) → deploy              │
│  threshold: sharpe_improvement_threshold = 0.0          │
└─────────────────────────────────────────────────────────┘
```

### 7.5 Validation Tools 3 Component (Mode B)

```
┌──────────────────────────────────────────────────────────┐
│  Validation Tools (C13) — Backtest Agent이 호출           │
│                                                          │
│  ① BacktestEngine                                        │
│     walk-forward (purge=60bars, embargo=78bars)          │
│     → IC / IR / MDD / SR                                 │
│     SLA: max_runtime_sec=1800                            │
│                                                          │
│  ② ReplayRunner                                          │
│     deterministic_replay (분봉 1m)                       │
│     event_sources: 6개 커넥터                             │
│     → replay_trace_ref (RPT-{yyyymmdd}-{UUID8})          │
│     SLA: max_runtime_sec=2400                            │
│                                                          │
│  ③ PerformanceAnalyzer                                   │
│     regime breakdown (bull/bear/sideways/volatile)       │
│     + ablation (factor/model/allocator/dual_source)      │
│     verdict: pass | warn | fail                          │
│     SLA: max_runtime_sec=600                             │
│                                                          │
│  result → KB TTL=30days (Sprint 4 KB.write 예정)         │
└──────────────────────────────────────────────────────────┘
```

### 7.6 Bootstrap 단계 (시스템 시작 시 Memory 복원)

```
┌──────────────────────────────────────────────────────────┐
│  Bootstrap (시스템 시작 → HOT_RUNNING 진입 전)            │
│                                                          │
│  AgentMemoryRestorer.restore_all()   (S4-8)              │
│    │                                                     │
│    ├─ KB storage 읽기 (5종, factor_zoo 제외):             │
│    │    micro_notes / macro_notes / debate_history       │
│    │    decision_history / backtest_history              │
│    │    (factor_zoo: Mode B Scheduler 전용, restorer 제외)  │
│    │                                                     │
│    ├─ 에이전트 인스턴스별 inject:                         │
│    │    NewsAgent      ← micro_notes + macro_notes       │
│    │    RiskAgent      ← macro_notes                     │
│    │    DebateAgent    ← debate_history                  │
│    │    FDA            ← decision_history                │
│    │    BacktestAgent  ← backtest_history                │
│    │                                                     │
│    └─ 부트 완료 → HOT_RUNNING 진입                       │
│                                                          │
│  실패 시: 빈 memory로 cold start (warn 로그 출력)         │
└──────────────────────────────────────────────────────────┘
```
