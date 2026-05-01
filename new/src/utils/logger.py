"""[모듈명] 접두사 표준 로거. 한국어 메시지 기본."""
from __future__ import annotations

import logging


def get_logger(module_name: str) -> logging.Logger:
    """[{module_name}] 접두사 로거 반환.

    핸들러 중복 방지: logger.handlers 체크 후 미등록 시만 추가.
    한국어 메시지 권장: [quant] Hot Path 레이턴시 98ms
    """
    logger = logging.getLogger(module_name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(f"[{module_name}] %(levelname)s %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
