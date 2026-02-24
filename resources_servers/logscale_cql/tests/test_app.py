# SPDX-FileCopyrightText: Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
import pytest

from nemo_gym.openai_utils import (
    NeMoGymResponse,
    NeMoGymResponseOutputMessage,
    NeMoGymResponseOutputText,
)
from resources_servers.logscale_cql.app import (
    extract_cql_from_response,
    _parse_semantic_judge,
    _parse_execution_judge,
    compute_reward,
)
from resources_servers.logscale_cql.container_engine import LogScaleContainerEngine


def _make_response(text: str) -> NeMoGymResponse:
    out = NeMoGymResponseOutputMessage(
        id="msg-1",
        type="message",
        content=[NeMoGymResponseOutputText(annotations=[], text=text)],
    )
    return NeMoGymResponse(output=[out])


# ── CQL extraction ────────────────────────────────────────────────────

class TestExtractCqlFromResponse:
    def test_extract_from_message_output_text(self):
        response = _make_response("#event_simpleName=ProcessRollup2\n| groupBy(ImageFileName, function=count())")
        result = extract_cql_from_response(response)
        assert "ProcessRollup2" in result
        assert "groupBy" in result

    def test_extract_strips_markdown_fence(self):
        response = _make_response("```\n#event_simpleName=DnsRequest\n| head(10)\n```")
        result = extract_cql_from_response(response)
        assert "DnsRequest" in result
        assert "head" in result
        assert result.strip() == "#event_simpleName=DnsRequest\n| head(10)"

    def test_empty_response(self):
        response = NeMoGymResponse(output=[])
        result = extract_cql_from_response(response)
        assert result == ""

    def test_no_message_content(self):
        out = NeMoGymResponseOutputMessage(id="msg-1", type="message", content=[])
        response = NeMoGymResponse(output=[out])
        result = extract_cql_from_response(response)
        assert result == ""


# ── Judge parsers ─────────────────────────────────────────────────────

class TestParseSemanticJudge:
    def test_parse_full(self):
        raw = "SEMANTIC_SCORE: 4\nSEMANTIC_REASONING: Query captures intent well."
        r = _parse_semantic_judge(raw)
        assert r["semantic_score"] == 4
        assert "intent" in r["semantic_reasoning"]

    def test_parse_missing_score(self):
        raw = "SEMANTIC_REASONING: Something happened."
        r = _parse_semantic_judge(raw)
        assert r["semantic_score"] is None


class TestParseExecutionJudge:
    def test_parse_full(self):
        raw = "EXECUTION_SCORE: 5\nEXECUTION_REASONING: Results are perfect."
        r = _parse_execution_judge(raw)
        assert r["execution_score"] == 5
        assert "perfect" in r["execution_reasoning"]

    def test_parse_missing_score(self):
        raw = "EXECUTION_REASONING: Empty results."
        r = _parse_execution_judge(raw)
        assert r["execution_score"] is None


# ── Reward computation ────────────────────────────────────────────────

class TestComputeReward:
    def test_perfect_scores(self):
        assert compute_reward(1, 5, 5) == pytest.approx(1.0)

    def test_all_zeros(self):
        assert compute_reward(0, None, None) == pytest.approx(0.0)

    def test_valid_but_no_judge(self):
        # validity=1, no semantic, no execution
        assert compute_reward(1, None, None) == pytest.approx(1 / 3)

    def test_mixed(self):
        # validity=1, semantic=3/5, execution=4/5
        expected = (1.0 + 3 / 5 + 4 / 5) / 3
        assert compute_reward(1, 3, 4) == pytest.approx(expected)

    def test_invalid_query(self):
        # validity=0, semantic=2, execution=1
        expected = (0.0 + 2 / 5 + 1 / 5) / 3
        assert compute_reward(0, 2, 1) == pytest.approx(expected)


# ── Container engine (unit tests for static helpers) ──────────────────

class TestContainerEngineFormatPreview:
    def test_empty_events(self):
        assert LogScaleContainerEngine._format_preview([]) == "(no results)"

    def test_skips_internal_fields(self):
        events = [{"ComputerName": "SRV-01", "@rawstring": "raw", "#repo": "sandbox", "aid": "abc"}]
        preview = LogScaleContainerEngine._format_preview(events)
        assert "SRV-01" in preview
        assert "@rawstring" not in preview
        assert "#repo" not in preview
        assert "aid" in preview

    def test_limits_rows(self):
        events = [{"x": str(i)} for i in range(100)]
        preview = LogScaleContainerEngine._format_preview(events, max_rows=3)
        lines = [l for l in preview.strip().split("\n") if l.strip()]
        assert len(lines) == 4  # header + 3 data rows
