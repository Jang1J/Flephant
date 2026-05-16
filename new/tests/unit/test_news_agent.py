"""NewsAgent 단위 테스트. S2-7 실구현 기준."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.agents.cold.news import NewsAgent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_llm(success=True, content="삼성전자 buy 매수 추천"):
    """LLMRouter mock 생성. LLMCallResult 유사 객체 반환."""
    llm = MagicMock()
    result = MagicMock()
    result.success = success
    result.content = content if success else None
    result.error = None if success else "LLM 호출 실패"
    llm.call.return_value = result
    return llm


def _make_agent(llm=None, memory_root=None):
    """NewsAgent 기본 인스턴스."""
    if llm is None:
        llm = _make_llm()
    return NewsAgent(llm_router=llm, memory_root=memory_root)


class _DictCache:
    news_ttl = 300

    def __init__(self) -> None:
        self.items = {}

    def get(self, key):
        return self.items.get(key)

    def set(self, key, value, ttl_seconds=None):
        self.items[key] = value


# ---------------------------------------------------------------------------
# 인스턴스화
# ---------------------------------------------------------------------------

class TestNewsAgentInstantiation:
    def test_news_agent_instantiation(self):
        """LLMRouter 주입 후 인스턴스화 가능."""
        llm = _make_llm()
        agent = NewsAgent(llm_router=llm)
        assert isinstance(agent, NewsAgent)

    def test_news_agent_llm_router_stored(self):
        """주입된 llm_router가 _llm_router에 저장된다."""
        llm = _make_llm()
        agent = NewsAgent(llm_router=llm)
        assert agent._llm_router is llm

    def test_news_agent_default_memory_root_is_path(self):
        """memory_root 기본값이 Path 타입."""
        from pathlib import Path
        agent = _make_agent()
        assert isinstance(agent._memory_root, Path)


# ---------------------------------------------------------------------------
# ALLOWED_PUBLISH_CHANNELS
# ---------------------------------------------------------------------------

class TestAllowedPublishChannels:
    def test_news_agent_allowed_publish_channels(self):
        """ALLOWED_PUBLISH_CHANNELS = {"news_signal", "dart_alert"}."""
        agent = _make_agent()
        assert agent.ALLOWED_PUBLISH_CHANNELS == frozenset({"news_signal", "dart_alert"})

    def test_news_agent_allowed_channels_subset_of_valid(self):
        """ALLOWED_PUBLISH_CHANNELS는 VALID_PUBLISH_CHANNELS의 subset."""
        agent = _make_agent()
        assert agent.ALLOWED_PUBLISH_CHANNELS.issubset(agent.VALID_PUBLISH_CHANNELS)

    def test_news_agent_publish_news_signal(self):
        """news_signal 채널 publish 성공."""
        agent = _make_agent()
        result = agent.publish("news_signal", {"ticker": "005930", "score": 0.8})
        assert result["channel"] == "news_signal"
        assert result["agent"] == "NewsAgent"

    def test_news_agent_publish_dart_alert(self):
        """dart_alert 채널 publish 성공."""
        agent = _make_agent()
        result = agent.publish("dart_alert", {"ticker": "005930", "type": "공시"})
        assert result["channel"] == "dart_alert"

    def test_news_agent_publish_invalid_channel_raises(self):
        """허용되지 않은 채널 publish 시 ValueError."""
        agent = _make_agent()
        with pytest.raises(ValueError):
            agent.publish("quant_signal", {})


# ---------------------------------------------------------------------------
# report() 검증
# ---------------------------------------------------------------------------

class TestReport:
    def test_report_valid_news_signal_payload(self):
        """유효한 payload → report dict 반환."""
        agent = _make_agent()
        rpt = agent.report(
            "news_signal",
            {
                "stance": "buy",
                "impacted_tickers": ["005930"],
                "impacted_sectors": ["반도체"],
                "narrative": "삼성전자 실적 호조로 매수 의견",
            },
        )
        assert rpt["payload"]["stance"] == "buy"
        assert rpt["payload"]["impacted_tickers"] == ["005930"]

    def test_report_invalid_stance_raises_valueerror(self):
        """stance 범위 이탈 시 ValueError."""
        agent = _make_agent()
        with pytest.raises(ValueError, match="stance"):
            agent.report(
                "news_signal",
                {
                    "stance": "strong_buy",
                    "impacted_tickers": [],
                    "impacted_sectors": [],
                    "narrative": "test",
                },
            )

    def test_report_missing_narrative_raises(self):
        """narrative 누락 시 ValueError."""
        agent = _make_agent()
        with pytest.raises(ValueError, match="narrative"):
            agent.report(
                "news_signal",
                {
                    "stance": "neutral",
                    "impacted_tickers": [],
                    "impacted_sectors": [],
                },
            )

    def test_report_empty_narrative_raises(self):
        """narrative 빈 문자열도 ValueError."""
        agent = _make_agent()
        with pytest.raises(ValueError, match="narrative"):
            agent.report(
                "news_signal",
                {
                    "stance": "neutral",
                    "impacted_tickers": [],
                    "impacted_sectors": [],
                    "narrative": "",
                },
            )

    def test_report_returns_expected_structure(self):
        """반환 dict에 report_type / payload / agent / ts 키 존재."""
        agent = _make_agent()
        rpt = agent.report(
            "news_signal",
            {
                "stance": "neutral",
                "impacted_tickers": [],
                "impacted_sectors": [],
                "narrative": "중립 의견",
            },
        )
        for key in ("report_type", "payload", "agent", "ts"):
            assert key in rpt, f"key {key!r} 누락"
        assert rpt["agent"] == "NewsAgent"
        assert rpt["report_type"] == "news_signal"


# ---------------------------------------------------------------------------
# analyze() 이벤트 분기
# ---------------------------------------------------------------------------

class TestAnalyze:
    def test_analyze_news_event_calls_llm(self):
        """analyze 호출 시 llm.call이 mode='cold', caller='news_agent' 로 호출된다."""
        llm = _make_llm()
        agent = NewsAgent(llm_router=llm)
        event = {
            "event_type": "news",
            "ticker": "005930",
            "title": "삼성 실적 호조",
            "summary": "영업이익 증가",
            "event_id": "E-001",
        }
        agent.analyze(event)
        llm.call.assert_called_once()
        _, kwargs = llm.call.call_args
        assert kwargs.get("mode") == "cold" or llm.call.call_args[0][1] == "cold"
        assert kwargs.get("caller") == "news_agent" or llm.call.call_args[0][2] == "news_agent"
        prompt = llm.call.call_args[0][0]
        assert '"confidence": 0.0' in prompt

    def test_analyze_news_event_returns_news_signal_channel(self):
        """news event_type → channel=news_signal."""
        agent = _make_agent()
        result = agent.analyze({
            "event_type": "news",
            "ticker": "005930",
            "title": "test",
            "summary": "test",
            "event_id": "E-1",
            "occurred_at": "2026-04-20T09:30:00+09:00",
            "asof": "2026-04-20T09:31:00+09:00",
        })
        assert result["channel"] == "news_signal"
        for key in ("cause_by", "sent_from", "priority", "action_type", "timestamp"):
            assert key in result
        assert result["scope"] == "ticker:005930"
        assert result["payload"]["confidence"] == pytest.approx(0.5)
        assert result["payload"]["scope"] == "ticker:005930"
        assert result["payload"]["ticker"] == "005930"
        assert result["payload"]["event_id"] == "E-1"
        assert result["payload"]["occurred_at"] == "2026-04-20T09:30:00+09:00"
        assert result["payload"]["asof"] == "2026-04-20T09:31:00+09:00"
        assert result["message"]["event_id"] == "E-1"
        assert result["message"]["occurred_at"] == "2026-04-20T09:30:00+09:00"
        assert result["message"]["asof"] == "2026-04-20T09:31:00+09:00"

    def test_analyze_tickers_string_uses_full_code(self):
        """event.tickers가 문자열이어도 첫 글자만 쓰지 않고 6자리 코드를 유지한다."""
        agent = _make_agent()
        result = agent.analyze({
            "event_type": "news",
            "tickers": "005930",
            "title": "test",
            "summary": "test",
            "event_id": "E-string-ticker",
        })
        assert result["scope"] == "ticker:005930"

    def test_analyze_cache_hit_republishes_message(self):
        """cache hit이어도 pubsub가 주입된 direct path에서는 message를 다시 publish한다."""
        cache = _DictCache()
        pubsub = MagicMock()
        pubsub.publish.return_value = "MSG-CACHED"
        agent = NewsAgent(llm_router=_make_llm(), pubsub=pubsub, cache=cache)
        event = {
            "event_type": "news",
            "ticker": "005930",
            "title": "test",
            "summary": "test",
            "event_id": "E-cache",
            "occurred_at": "2026-04-20T09:30:00+09:00",
            "asof": "2026-04-20T09:31:00+09:00",
        }

        first = agent.analyze(event)
        second = agent.analyze(event)

        assert first["message"]["scope"] == "ticker:005930"
        assert first["message"]["event_id"] == "E-cache"
        assert second["republished_from_cache"] is True
        assert second["message_id"] == "MSG-CACHED"
        assert second["message"]["event_id"] == "E-cache"
        assert second["message"]["occurred_at"] == "2026-04-20T09:30:00+09:00"
        assert second["message"]["asof"] == "2026-04-20T09:31:00+09:00"
        assert pubsub.publish.call_count == 2

    def test_analyze_cache_hit_treats_string_false_fallback_as_false(self):
        """캐시 payload의 llm_fallback 문자열 false도 재게시 가능 상태로 해석한다."""
        cache = _DictCache()
        pubsub = MagicMock()
        pubsub.publish.return_value = "MSG-CACHED"
        agent = NewsAgent(llm_router=_make_llm(), pubsub=pubsub, cache=cache)
        event = {
            "event_type": "news",
            "ticker": "005930",
            "title": "test",
            "summary": "test",
            "event_id": "E-cache-string-false",
        }

        first = agent.analyze(event)
        cached = dict(first)
        cached["llm_fallback"] = "false"
        cache.set("news:005930:E-cache-string-false", cached)
        second = agent.analyze(event)

        assert second["republished_from_cache"] is True
        assert second["message_id"] == "MSG-CACHED"

    def test_analyze_dart_event_returns_dart_alert_channel(self):
        """dart event_type → channel=dart_alert."""
        agent = _make_agent()
        result = agent.analyze({
            "event_type": "dart",
            "ticker": "005930",
            "title": "대규모 공시",
            "summary": "유상증자 결정",
            "event_id": "D-1",
        })
        assert result["channel"] == "dart_alert"

    def test_analyze_community_event_returns_news_signal_channel(self):
        """community event_type → channel=news_signal."""
        agent = _make_agent()
        result = agent.analyze({
            "event_type": "community",
            "ticker": "035720",
            "title": "카카오 이슈",
            "summary": "카카오 매도 의견 다수",
            "event_id": "C-1",
        })
        assert result["channel"] == "news_signal"

    def test_analyze_unknown_event_type_raises(self):
        """알 수 없는 event_type → ValueError."""
        agent = _make_agent()
        with pytest.raises(ValueError, match="event_type"):
            agent.analyze({
                "event_type": "unknown_type",
                "ticker": "005930",
                "title": "test",
                "summary": "test",
                "event_id": "X-1",
            })

    def test_analyze_llm_failure_stance_neutral_fallback(self):
        """LLMCallResult(success=False) → stance=neutral, llm_fallback=True."""
        llm = _make_llm(success=False)
        agent = NewsAgent(llm_router=llm)
        result = agent.analyze({
            "event_type": "news",
            "ticker": "005930",
            "title": "test",
            "summary": "test",
            "event_id": "E-fail",
        })
        assert result["payload"]["stance"] == "neutral"
        assert result["llm_fallback"] is True

    def test_analyze_result_has_required_keys(self):
        """analyze 반환값에 channel/payload/report_type/agent/ts/llm_fallback 존재."""
        agent = _make_agent()
        result = agent.analyze({
            "event_type": "news",
            "ticker": "005930",
            "title": "test",
            "summary": "test",
            "event_id": "E-2",
        })
        for key in ("channel", "payload", "report_type", "agent", "ts", "llm_fallback"):
            assert key in result, f"key {key!r} 누락"


# ---------------------------------------------------------------------------
# _parse_llm_content() heuristic
# ---------------------------------------------------------------------------

class TestParseLlmContent:
    def test_parse_llm_content_buy_keyword(self):
        """buy 키워드 포함 → stance=buy."""
        agent = _make_agent()
        parsed = agent._parse_llm_content("이 뉴스는 buy 의견입니다.")
        assert parsed["stance"] == "buy"

    def test_parse_llm_content_maesoo_keyword(self):
        """'매수' 키워드 → stance=buy."""
        agent = _make_agent()
        parsed = agent._parse_llm_content("삼성전자 매수 추천.")
        assert parsed["stance"] == "buy"

    def test_parse_llm_content_sell_keyword(self):
        """sell 키워드 포함 → stance=sell."""
        agent = _make_agent()
        parsed = agent._parse_llm_content("현재 주가 고평가. sell 권고.")
        assert parsed["stance"] == "sell"

    def test_parse_llm_content_maedo_keyword(self):
        """'매도' 키워드 → stance=sell."""
        agent = _make_agent()
        parsed = agent._parse_llm_content("카카오 매도 시점.")
        assert parsed["stance"] == "sell"

    def test_parse_llm_content_default_neutral(self):
        """매수/매도 키워드 없음 → stance=neutral."""
        agent = _make_agent()
        parsed = agent._parse_llm_content("특별한 변화 없음. 관망 권고.")
        assert parsed["stance"] == "neutral"

    def test_parse_llm_content_narrative_max_200(self):
        """narrative 길이는 news_filter.yaml text_pack_settings.narrative_max_chars SSOT 따름 (기본 200, yaml 변경 시 자동 적응)."""
        agent = _make_agent()
        long_content = "A" * (agent._narrative_max_chars + 100)
        parsed = agent._parse_llm_content(long_content)
        assert len(parsed["narrative"]) == agent._narrative_max_chars

    def test_parse_llm_content_returns_list_fields(self):
        """impacted_tickers / impacted_sectors는 list."""
        agent = _make_agent()
        parsed = agent._parse_llm_content("neutral content")
        assert isinstance(parsed["impacted_tickers"], list)
        assert isinstance(parsed["impacted_sectors"], list)

    def test_parse_llm_json_array_falls_back_without_crash(self):
        """JSON array 응답은 object parser 오류 후 heuristic fallback으로 처리된다."""
        agent = _make_agent()
        parsed = agent._parse_llm_content("[]")
        assert parsed["stance"] == "neutral"
        assert parsed["confidence"] == pytest.approx(0.5)

    def test_parse_llm_content_preserves_json_confidence(self):
        """LLM JSON confidence는 C5 payload까지 보존한다."""
        agent = _make_agent()
        parsed = agent._parse_llm_content(
            '{"stance":"buy","impacted_tickers":["005930"],'
            '"impacted_sectors":["반도체"],"narrative":"호재",'
            '"confidence":0.82}'
        )
        rpt = agent.report("news_signal", parsed)
        assert parsed["confidence"] == pytest.approx(0.82)
        assert rpt["payload"]["confidence"] == pytest.approx(0.82)

    def test_parse_confidence_clamps_invalid_values(self):
        """confidence는 finite 0.0~1.0 값으로 정규화된다."""
        assert NewsAgent._parse_confidence(1.7) == pytest.approx(1.0)
        assert NewsAgent._parse_confidence(-0.2) == pytest.approx(0.0)
        assert NewsAgent._parse_confidence("nan") == pytest.approx(0.5)
        assert NewsAgent._parse_confidence("bad") == pytest.approx(0.5)

    def test_publish_confidence_is_safe_clamped(self):
        """C4 publish confidence도 비수치/범위초과 값을 안전하게 정규화한다."""
        agent = _make_agent()
        assert agent.publish("news_signal", {"confidence": "high"})["confidence"] == pytest.approx(0.5)
        assert agent.publish("news_signal", {"confidence": 2.0})["confidence"] == pytest.approx(1.0)
        assert agent.publish("news_signal", {"confidence": float("nan")})["confidence"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# _save_memory() JSONL
# ---------------------------------------------------------------------------

class TestSaveMemory:
    def test_save_memory_micro_creates_jsonl(self, tmp_path):
        """micro 저장 시 JSONL 파일 생성."""
        import json
        agent = _make_agent(memory_root=tmp_path)
        agent._save_memory("005930", "micro", {"stance": "buy", "event_id": "E-1"})
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
        path = tmp_path / "news_agent" / "005930" / f"{today}.jsonl"
        assert path.exists()
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["note_type"] == "micro"

    def test_save_memory_macro_creates_jsonl(self, tmp_path):
        """macro 저장 시 macro/{YYYYMMDD}.jsonl 생성."""
        import json
        from datetime import datetime
        from zoneinfo import ZoneInfo
        agent = _make_agent(memory_root=tmp_path)
        agent._save_memory("005930", "macro", {"macro_key": "gdp"})
        today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
        path = tmp_path / "macro" / f"{today}.jsonl"
        assert path.exists()
        rec = json.loads(path.read_text(encoding="utf-8").strip())
        assert rec["note_type"] == "macro"

    def test_save_memory_append_preserves_existing(self, tmp_path):
        """2회 호출 → 2줄 JSONL."""
        agent = _make_agent(memory_root=tmp_path)
        agent._save_memory("005930", "micro", {"call": 1})
        agent._save_memory("005930", "micro", {"call": 2})
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
        path = tmp_path / "news_agent" / "005930" / f"{today}.jsonl"
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2

    def test_save_memory_ticker_zfill(self, tmp_path):
        """ticker가 6자리 미만이면 zero-padded 경로 사용."""
        agent = _make_agent(memory_root=tmp_path)
        agent._save_memory("5930", "micro", {"test": True})
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
        path = tmp_path / "news_agent" / "005930" / f"{today}.jsonl"
        assert path.exists()


# ---------------------------------------------------------------------------
# consume_text_pack()
# ---------------------------------------------------------------------------

class TestConsumeTextPack:
    def test_consume_text_pack_delegates_to_analyze(self):
        """consume_text_pack은 analyze를 호출한다."""
        llm = _make_llm()
        agent = NewsAgent(llm_router=llm)
        result = agent.consume_text_pack(
            "005930",
            "삼성전자 30분 텍스트팩",
            context={"event_id": "TP-1", "event_type": "news", "title": "실적 발표"},
        )
        # LLM이 호출됐다면 analyze가 위임된 것
        llm.call.assert_called_once()
        assert result is not None

    def test_consume_text_pack_no_context(self):
        """context 없이도 동작."""
        agent = _make_agent()
        result = agent.consume_text_pack("005930", "텍스트팩 내용")
        assert result is not None

    def test_consume_text_pack_returns_channel(self):
        """반환값에 channel 포함."""
        agent = _make_agent()
        result = agent.consume_text_pack(
            "005930",
            "내용",
            context={"event_type": "news"},
        )
        assert "channel" in result


# ---------------------------------------------------------------------------
# attach_to_gateway()
# ---------------------------------------------------------------------------

class TestAttachToGateway:
    def test_attach_to_gateway_registers_3_event_types(self):
        """attach_to_gateway → gateway.register_handler 3회 호출."""
        agent = _make_agent()
        gateway = MagicMock()
        agent.attach_to_gateway(gateway)
        assert gateway.register_handler.call_count == 3

    def test_attach_to_gateway_news_channel(self):
        """'news' event_type은 news_signal 채널로 등록."""
        agent = _make_agent()
        gateway = MagicMock()
        agent.attach_to_gateway(gateway)
        calls = gateway.register_handler.call_args_list
        news_calls = [c for c in calls if c[0][0] == "news"]
        assert len(news_calls) == 1
        assert news_calls[0][1]["publish_channel"] == "news_signal"

    def test_attach_to_gateway_dart_channel(self):
        """'dart' event_type은 dart_alert 채널로 등록."""
        agent = _make_agent()
        gateway = MagicMock()
        agent.attach_to_gateway(gateway)
        calls = gateway.register_handler.call_args_list
        dart_calls = [c for c in calls if c[0][0] == "dart"]
        assert len(dart_calls) == 1
        assert dart_calls[0][1]["publish_channel"] == "dart_alert"

    def test_attach_to_gateway_community_channel(self):
        """'community' event_type은 news_signal 채널로 등록."""
        agent = _make_agent()
        gateway = MagicMock()
        agent.attach_to_gateway(gateway)
        calls = gateway.register_handler.call_args_list
        community_calls = [c for c in calls if c[0][0] == "community"]
        assert len(community_calls) == 1
        assert community_calls[0][1]["publish_channel"] == "news_signal"
