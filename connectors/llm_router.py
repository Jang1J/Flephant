"""
LLM Router — Kanana-o (primary) / GPT-4o (fallback)
- Final Decision Agent가 사용하는 LLM 호출 래퍼
- Kanana-o 429/timeout 시 자동으로 GPT-4o fallback
- Circuit breaker: 연속 N회 실패 시 일정 시간 Kanana skip
"""

import httpx
from dotenv import load_dotenv
import os
import json
import time
from datetime import datetime
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

KANANA_API_KEY = os.getenv("KANANA_API_KEY", "")
KANANA_BASE_URL = os.getenv("KANANA_BASE_URL", "https://api.kanana.ai/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# ── Circuit Breaker State ──
_circuit_breaker = {
    "consecutive_failures": 0,
    "threshold": 3,              # 3회 연속 실패 시 circuit open
    "cooldown_seconds": 300,     # 5분간 Kanana 건너뜀
    "last_failure_time": 0,
}


def _is_circuit_open() -> bool:
    """Kanana circuit breaker가 열려있는지 확인"""
    cb = _circuit_breaker
    if cb["consecutive_failures"] >= cb["threshold"]:
        elapsed = time.time() - cb["last_failure_time"]
        if elapsed < cb["cooldown_seconds"]:
            return True
        # cooldown 지나면 half-open → 재시도 허용
        cb["consecutive_failures"] = 0
    return False


def _record_kanana_success():
    _circuit_breaker["consecutive_failures"] = 0


def _record_kanana_failure():
    _circuit_breaker["consecutive_failures"] += 1
    _circuit_breaker["last_failure_time"] = time.time()


def call_kanana(
    messages: list[dict],
    model: str = "kanana-o",
    temperature: float = 0.0,
    max_tokens: int = 2048,
    timeout: float = 30.0,
) -> dict:
    """
    Kanana-o API 호출 (OpenAI-compatible endpoint)

    Returns:
        {"content": str, "model": str, "fallback_used": False}
    """
    headers = {
        "Authorization": f"Bearer {KANANA_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    resp = httpx.post(
        f"{KANANA_BASE_URL}/chat/completions",
        headers=headers,
        json=payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()

    return {
        "content": data["choices"][0]["message"]["content"],
        "model": data.get("model", model),
        "fallback_used": False,
    }


def call_gpt4o(
    messages: list[dict],
    temperature: float = 0.0,
    max_tokens: int = 2048,
) -> dict:
    """
    GPT-4o fallback 호출

    Returns:
        {"content": str, "model": str, "fallback_used": True}
    """
    import openai
    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=30,
    )

    return {
        "content": resp.choices[0].message.content,
        "model": resp.model,
        "fallback_used": True,
    }


_IGNORE_PATTERNS = [
    "카카오에서 만든",
    "AI 어시스턴트",
    "무엇을 도와드릴까요",
    "어떤 도움이 필요하신가요",
]


def _is_valid_response(response_text: str) -> bool:
    """LLM 응답이 시스템 프롬프트를 이행했는지 검증."""
    if not response_text or len(response_text.strip()) < 5:
        return False
    for pattern in _IGNORE_PATTERNS:
        if pattern in response_text:
            return False
    return True


def call_llm(
    messages: list[dict],
    temperature: float = 0.0,
    max_tokens: int = 2048,
) -> dict:
    """
    Primary: Kanana-o → Fallback: GPT-4o
    자동 전환 로직 포함.

    Returns:
        {"content": str, "model": str, "fallback_used": bool}
    """
    errors = []

    # 1) Kanana-o 시도 (최대 2회 retry, circuit breaker 적용)
    if KANANA_API_KEY and not _is_circuit_open():
        for attempt in range(2):
            try:
                result = call_kanana(messages, temperature=temperature, max_tokens=max_tokens)
                if not _is_valid_response(result["content"]):
                    err_msg = "Kanana 응답 품질 검증 실패 (자기소개/무관 응답)"
                    errors.append(err_msg)
                    _record_kanana_failure()
                    print(f"[LLM Router] 응답 품질 검증 실패, fallback 전환")
                    break
                _record_kanana_success()
                result["kanana_errors"] = []
                print(f"[LLM Router] Kanana-o 성공 (model={result['model']}, attempt={attempt+1})")
                return result
            except httpx.HTTPStatusError as e:
                err_msg = f"Kanana HTTP {e.response.status_code}"
                errors.append(err_msg)
                _record_kanana_failure()
                if e.response.status_code == 429:
                    wait = 2 ** attempt
                    print(f"[LLM Router] Kanana-o 429 rate limit → {wait}초 대기 후 재시도")
                    time.sleep(wait)
                    continue
                print(f"[LLM Router] {err_msg} → fallback")
                break
            except httpx.TimeoutException:
                errors.append("Kanana timeout")
                _record_kanana_failure()
                print(f"[LLM Router] Kanana-o timeout (attempt {attempt+1}) → {'재시도' if attempt == 0 else 'fallback'}")
                if attempt == 0:
                    time.sleep(1)
                    continue
                break
            except Exception as e:
                errors.append(f"Kanana error: {e}")
                _record_kanana_failure()
                print(f"[LLM Router] Kanana-o 실패: {e} → fallback")
                break
    elif _is_circuit_open():
        errors.append("Kanana circuit breaker OPEN (연속 실패 → 5분 cooldown)")
        print(f"[LLM Router] Kanana circuit breaker OPEN → GPT-4o 직행")

    # 2) GPT-4o fallback
    if OPENAI_API_KEY:
        try:
            result = call_gpt4o(messages, temperature=temperature, max_tokens=max_tokens)
            result["kanana_errors"] = errors
            print(f"[LLM Router] GPT-4o fallback 성공 (model={result['model']})")
            return result
        except Exception as e:
            errors.append(f"GPT-4o error: {e}")
            print(f"[LLM Router] GPT-4o도 실패: {e}")

    raise RuntimeError(
        f"[LLM Router] 모든 LLM 사용 불가. errors={errors}. "
        "API 키를 확인하거나 네트워크 상태를 점검하세요."
    )


# ── smoke test ──
if __name__ == "__main__":
    print("=== LLM Router Smoke Test ===\n")

    test_messages = [
        {"role": "system", "content": "너는 한국 금융 시장 전문가야. 간단히 답해."},
        {"role": "user", "content": "KOSPI가 뭔지 한 문장으로 설명해줘."},
    ]

    print("[1] Kanana-o 직접 호출 테스트")
    if KANANA_API_KEY:
        try:
            result = call_kanana(test_messages)
            print(f"  모델: {result['model']}")
            print(f"  응답: {result['content'][:200]}")
        except Exception as e:
            print(f"  실패: {e}")
    else:
        print("  ⚠️ KANANA_API_KEY 없음 — 스킵")
    print()

    print("[2] GPT-4o 직접 호출 테스트")
    if OPENAI_API_KEY:
        try:
            result = call_gpt4o(test_messages)
            print(f"  모델: {result['model']}")
            print(f"  응답: {result['content'][:200]}")
        except Exception as e:
            print(f"  실패: {e}")
    else:
        print("  ⚠️ OPENAI_API_KEY 없음 — 스킵")
    print()

    print("[3] 자동 라우팅 테스트 (Kanana → GPT-4o fallback)")
    result = call_llm(test_messages)
    print(f"  모델: {result['model']}")
    print(f"  fallback: {result['fallback_used']}")
    print(f"  응답: {result['content'][:200]}")

    print("\n✅ LLM Router smoke test 완료!")
