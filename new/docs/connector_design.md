# v3 Data Layer 커넥터 설계 — 방법론 10개

> 공식 API 5개 직접 연동. MCP 커뮤니티 서버 사용 안 함.
> Sprint 0에서 data-engineer가 이 문서 기준으로 구현.

## 공식 API 소스

| # | 소스 | API | 주기 | 계약서 |
|---|------|-----|------|--------|
| 1 | KIS | openapivts (모의) / openapi (실전).koreainvestment.com | 1분 (WS) + 주문 (REST) | C1 |
| 2 | DART | opendart.fss.or.kr | 1분 폴링 | C2 (dart) |
| 3 | KRX | openapi.krx.co.kr | 1분 폴링 | C2 (investor_flow) |
| 4 | ECOS | ecos.bok.or.kr | 일별 | C2 (macro) |
| 5 | Naver | developers.naver.com | 1분 폴링 | C2 (news) |
| + | 커뮤니티 | 크롤링 | 5분 폴링 | C2 (community) |
| + | US Market | yfinance / KIS | 08:30 1회 | C2 (us_market) |

## 방법론 1: 중앙 인증 (auth.py)

모든 API 인증을 1개 모듈로 통합.

```python
# auth.py
class AuthManager:
    def get_kis_token(self, mode="paper"):  # "paper" or "live"
        # OAuth: AppKey + AppSecret → access_token
        # 만료 15분 전 자동 refresh
        # 토큰 발급 1분당 1회 제한 준수

    def get_dart_key(self):
        # 현재 프로세스 환경변수에서 DART_API_KEY 로드 (40자리)

    def get_naver_keys(self):
        # 현재 프로세스 환경변수에서 NAVER_CLIENT_ID + SECRET 로드
```

- API 키는 현재 프로세스 환경변수에서만 로드. `.env` 파일 직접 읽기와 코드 하드코딩 금지.
- KIS 토큰은 메모리 보관 (파일 저장 금지, C1 auth 규칙).

## 방법론 2: REST/WebSocket 파일 분리

```
new/connectors/
├── kis_ws.py        # WebSocket: 1분봉 실시간 시세 구독
├── kis_rest.py      # REST: 주문/잔고/분봉조회
├── dart_rest.py     # REST: 공시검색/재무제표
├── krx_rest.py      # REST: 투자자별 수급
├── ecos_rest.py     # REST: 거시지표
├── naver_rest.py    # REST: 뉴스 검색
├── community.py     # 크롤링: 종토방
├── us_market.py     # REST: yfinance/KIS
└── auth.py          # 중앙 인증
```

- WebSocket(`*_ws.py`)과 REST(`*_rest.py`)를 물리적으로 분리.
- Hot Path는 `kis_ws.py`만 사용. Cold Path는 `*_rest.py` 사용.

## 방법론 3: Rate Limiter

```python
# rate_limiter.py
class RateLimiter:
    def __init__(self, config_path="new/config/risk_config.yaml"):
        # yaml에서 소스별 한도 로드 (하드코딩 금지)
        # KIS: 초당 N건
        # DART: 일 20,000건
        # Naver: 일 25,000건
        # Kanana-o: 100회/일

    def check(self, source: str) -> bool:
        # Token bucket 방식
        # 한도 초과 시 False → 큐에 대기 or 스킵
```

## 방법론 4: 모의투자 ↔ 실전 1줄 전환

```yaml
# new/config/risk_config.yaml
trading:
  mode: "paper"  # "paper" or "live"
```

```python
if config["trading"]["mode"] == "paper":
    base_url = "https://openapivts.koreainvestment.com"   # 모의
else:
    base_url = "https://openapi.koreainvestment.com"       # 실전
```

- yaml 1줄 변경으로 전환. 코드 수정 불필요.

## 방법론 5: 대용량 DART 공시 분할

```python
def parse_disclosure(raw_doc):
    if len(raw_doc) < 1_000_000:  # 1MB 미만
        return parse_full(raw_doc)
    else:                          # 1MB 이상
        toc = extract_toc(raw_doc)
        key_sections = select_key_sections(toc)
        return parse_sections(raw_doc, key_sections)
```

- 1MB 미만: 전체 파싱 → News Agent로 전달
- 1MB 이상: 목차(TOC) 추출 → 핵심 섹션만 파싱

## 방법론 6: 데이터 정규화 (C2 EventNormalizeContract)

모든 소스의 raw 데이터를 하나의 형식으로 통일:

```python
normalized_event = {
    "event_id": "EVT-{yyyymmdd}-{source}-{scope}",
    "source": "dart",                      # 7개 소스 enum
    "event_type": "dart",                  # news|dart|macro|us_market|community|regime|investor_flow
    "scope": "ticker:005930",              # ticker:{code}|sector:{name}|market
    "title": "삼성전자 분기보고서",
    "summary": "...",
    "occurred_at": "ISO8601",
    "ingest_ts": "ISO8601",                # PIT-Safety 필수
    "priority": "normal",                  # urgent|normal|low
    "llm_required": True,
    "ttl": 3600,
    "expires_at": "ISO8601"
}
```

## 방법론 7: 에러 복구 3단계

```
1차: Retry (3회, exponential backoff: 1s → 2s → 4s)
  ↓ 실패
2차: Fallback (캐시된 이전 데이터 사용)
  ↓ 실패
3차: Circuit Breaker (연속 3회 실패 → 5분 cooldown → 리더에게 경고)
```

DART 에러코드 매핑:
- 000: 정상
- 010: 미등록 키 → .env 확인 안내
- 013: 데이터 없음 → 빈 결과 반환
- 020: 요청 제한 초과 → Rate Limiter 강화
- 100: 부적절한 값 → 파라미터 검증

## 방법론 8: PIT-Safety 강제

```python
def is_pit_safe(data_ts: str, snapshot_ts: str = "18:00") -> bool:
    """모든 커넥터 출력에서 호출. 미래 데이터 차단."""
    return parse_time(data_ts) <= parse_time(snapshot_ts)
```

- 모든 커넥터 출력에 `ingest_ts` 필수 기록.
- snapshot 기준 18:00 KST 이후 데이터는 다음 날 처리.
- 불변 원칙 #1. 위반 시 즉시 거부.

## 방법론 9: Dual-Source 점수화 (C3A)

```python
# 08:00 batch에서 실행
dual_source = {
    "news_score_t": score_news(today_news),           # 빠른 decay (λ=0.8)
    "comm_score_t_1": score_community(yesterday),      # 느린 decay (λ=0.4)
    "comm_score_t_2": score_community(day_before),     # peak_lag=2d
    "news_comm_divergence": abs(news - comm),          # 방향 불일치 = uncertainty
    "community_noise_multiplier": calc_noise(volume)   # 게시량 급증 시 감쇠
}
```

- 뉴스: FinBERT/로컬 분류기 기반 점수
- 커뮤니티: spam/manipulation/sentiment_dict 기반 점수
- decay 파라미터는 `new/config/dual_source.yaml`에서 로드

## 방법론 10: 3단계 텍스트 필터

```
Raw 텍스트 (하루 수천 건)
  ↓
1차: 규칙 매칭 (ms 단위, LLM 미호출)
  - news_filter.yaml 키워드 매칭
  - spam_rules.yaml 스팸 필터
  - manipulation_rules.yaml 시세조작 탐지
  → 대부분 여기서 걸러짐
  ↓
2차: 통계 집계 (ms 단위, LLM 미호출)
  - sentiment_dict.yaml 기반 감성 점수
  - 종목별 언급 빈도 집계
  - 이상치 탐지 (z-score)
  → 정량 판단 가능한 것은 여기서 처리
  ↓
3차: Kanana-o 해석 (이벤트 시만, 100회/일 예산)
  - 1차+2차 통과한 ~50건만 LLM이 읽음
  - CoT reasoning으로 투자 영향 판단
  - News Agent / Risk Agent가 소비
```

- Kanana-o 100회/일 예산 보호가 핵심 목적.
- 1차+2차 규칙은 `new/config/*.yaml` 10개에서 로드.
