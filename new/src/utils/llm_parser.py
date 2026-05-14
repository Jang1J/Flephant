"""LLM 응답 파싱 공통 유틸.

news.py / risk_slow.py / debate.py / fda.py 4곳의 중복 로직을 여기로 통일.
markdown fence 제거 + JSON parse. 파싱 실패 시 json.JSONDecodeError 전파.

사용법:
    from src.utils.llm_parser import parse_llm_json

    parsed = parse_llm_json(llm_result.content)
"""
from __future__ import annotations

import json
import re


def parse_llm_json(content: str) -> dict:
    """LLM 응답에서 markdown fence를 제거하고 JSON을 파싱한다.

    LLM이 응답 앞뒤에 ```json ... ``` 또는 ``` ... ``` 블록을 붙이는 경우 제거.

    Args:
        content: LLM 원본 응답 문자열.

    Returns:
        파싱된 dict.

    Raises:
        json.JSONDecodeError: JSON 파싱 실패 시.
    """
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    return json.loads(cleaned)
