# Preference Resolver 설계서 v1

> 기준일: 2026-03-29
> 작성: AI #1 (장재원)
> 상태: DRAFT — vnext 설계 문서

---

## 1. 핵심 원칙

### 성향은 model feature가 아니라 전략 profile 라우터다

사용자 설문 응답값은 어떠한 경우에도 LightGBM ranker의 입력 feature로 사용하지 않는다.
설문 응답은 개인마다 다르고 시점마다 달라질 수 있으므로, 이를 모델 feature로 넣으면 학습/검증의 일관성이 무너진다.

대신, 설문 응답은 **"어떤 전략 profile을 이 사용자에게 연결할 것인가"** 를 결정하는 라우팅 입력으로만 사용한다.
즉, 성향 데이터는 모델 내부로 들어가지 않고, **모델 선택 단계**에서 소비된다.

### 리스크 정책은 절대 건드리지 않는다

사용자의 성향이 무엇이든 `config/risk_policy_v0.yaml`에 정의된 stop-loss, position cap, turnover cap, cash ratio는 고정이다.
Preference Resolver는 전략 profile을 라우팅할 뿐이며, 리스크 파라미터를 사용자별로 변경하는 기능을 제공하지 않는다.

---

## 2. 설정 파일 구조

### 2.1 strategy_profiles.yaml (Generator Catalog 전용)

```yaml
# config/strategy_profiles.yaml
# 역할: 전략 생성기(generator)의 카탈로그. 새 전략을 추가할 때만 수정.
# 이 파일에 사용자 성향 매핑 로직을 넣지 않는다.

profiles:
  momentum:
    description: "추세추종 전략 — 상승 모멘텀 종목 중심"
    generator: build_strategy_card_momentum
    artifact_prefix: "SC-momentum"

  rebound:
    description: "반등포착 전략 — 과매도 이후 기술적 반등 중심"
    generator: build_strategy_card_rebound
    artifact_prefix: "SC-rebound"
```

strategy_profiles.yaml은 어떤 전략 generator가 존재하는지를 카탈로그 형태로 관리한다.
사용자 성향 로직은 이 파일에 포함되지 않는다.

### 2.2 preference_resolver.yaml (Resolver Config 전용)

```yaml
# config/preference_resolver.yaml
# 역할: 설문 5축을 전략 profile로 매핑하는 라우팅 규칙.
# strategy_profiles.yaml과 분리하여 관리.

axes:
  trend_following:      # 추세추종 선호 (0~10)
    weight: 0.30
  rebound_seeking:      # 반등포착 선호 (0~10)
    weight: 0.30
  trade_frequency:      # 잦은 매매 허용도 (0~10, 높을수록 빈번 허용)
    weight: 0.15
  loss_aversion:        # 손실 회피 (0~10, 높을수록 보수적)
    weight: 0.15
  explanation_preference: # 설명 선호 (0~10, 높을수록 자세한 설명 요구)
    weight: 0.10

routing:
  momentum_threshold: 5.5   # style_score >= 이 값이면 momentum 선택
  rebound_threshold: 4.5    # style_score < 이 값이면 rebound 선택
  # style_score가 두 threshold 사이이면 system_recommended 사용
```

---

## 3. 설문 5축 정의

| 축 | 질문 예시 | 범위 | 해석 |
|----|---------|------|------|
| **추세추종 선호** | "이미 오르고 있는 종목에 올라타는 전략을 선호한다" | 0~10 | 높을수록 momentum 친화 |
| **반등포착 선호** | "많이 빠진 종목이 반등할 때를 노리는 전략을 선호한다" | 0~10 | 높을수록 rebound 친화 |
| **잦은 매매 허용도** | "포트폴리오가 자주 바뀌어도 괜찮다" | 0~10 | 높을수록 turnover 수용 |
| **손실 회피** | "수익이 적더라도 손실을 최소화하는 게 중요하다" | 0~10 | 높을수록 보수적 성향 |
| **설명 선호** | "왜 이 종목을 추천하는지 자세히 알고 싶다" | 0~10 | 높을수록 상세 narrative 생성 |

---

## 4. style_score 계산 및 profile 매핑 규칙

### 4.1 style_score 계산

```python
# jobs/run_preference_resolver.py
def compute_style_score(survey: dict, resolver_config: dict) -> float:
    axes = resolver_config["axes"]
    score = 0.0
    score += survey["trend_following"]  * axes["trend_following"]["weight"]
    score += survey["rebound_seeking"]  * axes["rebound_seeking"]["weight"]
    score += survey["trade_frequency"]  * axes["trade_frequency"]["weight"]
    score -= survey["loss_aversion"]    * axes["loss_aversion"]["weight"]  # 음수 방향
    # explanation_preference는 profile 선택이 아닌 서비스 파라미터에 반영
    return round(score, 3)
```

loss_aversion은 보수적 성향을 나타내므로 style_score에 음수로 반영한다.
explanation_preference는 profile 선택에 영향을 주지 않고, 서비스 레이어에서 narrative 길이/상세도 조절에만 사용된다.

### 4.2 selected_profile 매핑

```
style_score >= momentum_threshold(5.5)         → selected_profile = "momentum"
style_score <  rebound_threshold(4.5)          → selected_profile = "rebound"
4.5 <= style_score < 5.5 (중간 구간)            → selected_profile = system_recommended
```

**system_recommended**: Preference Resolver가 최종 해석하는 meta-state다.
사용자 성향이 중간 구간에 해당하면, Resolver는 당일 시장 regime(DMP.kopen_summary의 spillover_grade 등)을 참조하여 그날에 더 적합한 전략을 자동 선택한다.
이 선택은 사용자에게 노출되지 않으며, 서비스에서는 단순히 선택된 profile 이름만 보여준다.

---

## 5. UserPreferenceProfile Artifact

```json
{
  "artifact_id": "UPP-{user_id}-{YYYYMMDD}",
  "user_id": "u_001",
  "survey_date": "2026-03-29",
  "survey_responses": {
    "trend_following": 7,
    "rebound_seeking": 3,
    "trade_frequency": 6,
    "loss_aversion": 4,
    "explanation_preference": 8
  },
  "style_score": 5.75,
  "selected_profile": "momentum",
  "system_recommended": null,
  "explanation_detail_level": "high",
  "resolver_version": "v1"
}
```

`system_recommended` 필드는 style_score가 중간 구간일 때만 값이 채워진다.
그 외의 경우 null이다.

---

## 6. publish vs serve 구분

### 데모용 (단일 publish)

```bash
python jobs/publish_strategy_variant.py --profile momentum --date 20260329
```

데모 환경에서는 단일 profile을 publish하여 SC-momentum-20260329.json 하나를 생성한다.
사용자 성향 설문 없이 profile을 직접 지정한다.

### 웹 서비스용 (둘 다 생성, serve만 다르게)

```bash
python jobs/build_strategy_card_momentum.py 20260329   # SC-momentum 생성
python jobs/build_strategy_card_rebound.py 20260329    # SC-rebound 생성
python jobs/run_preference_resolver.py --user u_001 --date 20260329  # 라우팅 결정
python jobs/serve_strategy_for_user.py --user u_001 --date 20260329  # 해당 SC serve
```

웹 서비스에서는 모든 profile의 StrategyCard를 미리 생성해두고, Preference Resolver가 사용자별로 어떤 SC를 serve할지 결정한다. 전략 생성 비용은 사용자 수에 무관하게 1회만 발생한다.

---

## 7. 금지 항목

- 설문 응답값을 LightGBM 또는 CNN 모델의 입력 feature로 사용 금지
- risk_policy_v0.yaml의 파라미터를 사용자 성향에 따라 변경 금지
- preference_resolver.yaml의 threshold를 코드에 하드코딩 금지
- 설문 없이 system_recommended를 강제 활성화 금지

---

## 8. 변경 이력

| 버전 | 날짜 | 내용 |
|------|------|------|
| v1.0 | 2026-03-29 | 최초 작성 (vnext 설계 초안) |
