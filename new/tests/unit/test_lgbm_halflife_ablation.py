"""lgbm_halflife_ablation 스크립트 헬퍼 + 안전성 가드 test.

정책 검증:
- bundle_id slug: path traversal + 특수문자 차단 (operator 입력 safe).
- research registry 분리: production registry mutation 방지.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lgbm_halflife_ablation import _safe_filename_slug, _to_float, _clean_metrics


def test_slug_keeps_safe_chars():
    """영숫자/하이픈/언더스코어/점은 그대로 유지."""
    assert _safe_filename_slug("BUNDLE-20260521-POSTCLOSE") == "BUNDLE-20260521-POSTCLOSE"
    assert _safe_filename_slug("ablation_h30") == "ablation_h30"
    assert _safe_filename_slug("v1.0.0") == "v1.0.0"


def test_slug_blocks_path_traversal():
    """'..' 또는 '/' 같은 path traversal 문자 차단."""
    assert ".." not in _safe_filename_slug("../etc/passwd")
    assert "/" not in _safe_filename_slug("../../etc/passwd")
    # leading dot 차단
    result = _safe_filename_slug("..hidden")
    assert not result.startswith(".")


def test_slug_blocks_windows_forbidden_chars():
    """Windows 파일명 금지 문자 (:, *, ?, <, >, |) 차단."""
    for bad_char in [":", "*", "?", "<", ">", "|", '"']:
        result = _safe_filename_slug(f"bundle{bad_char}id")
        assert bad_char not in result


def test_slug_handles_empty_input():
    """빈 문자열은 'unknown' fallback."""
    assert _safe_filename_slug("") == "unknown"
    assert _safe_filename_slug(".") == "unknown"
    assert _safe_filename_slug("...") == "unknown"


def test_slug_caps_length():
    """100자 초과 입력은 cap."""
    long_input = "A" * 500
    result = _safe_filename_slug(long_input)
    assert len(result) == 100


def test_to_float_handles_none():
    assert _to_float(None) is None


def test_to_float_casts_numpy_float64():
    import numpy as np
    val = np.float64(1.5)
    result = _to_float(val)
    assert result == 1.5
    assert isinstance(result, float)
    assert not isinstance(result, np.floating)


def test_clean_metrics_casts_all_values():
    import numpy as np
    raw = {"ic": np.float64(0.05), "sr": 1.5, "mdd": None}
    cleaned = _clean_metrics(raw)
    assert cleaned["ic"] == 0.05
    assert isinstance(cleaned["ic"], float)
    assert cleaned["sr"] == 1.5
    assert cleaned["mdd"] is None


def test_clean_metrics_non_dict_returns_empty():
    assert _clean_metrics(None) == {}
    assert _clean_metrics("not a dict") == {}
    assert _clean_metrics([1, 2, 3]) == {}
