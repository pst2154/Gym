# SPDX-FileCopyrightText: Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""
LogScale (CQL) NL-to-query resources server for NeMo Gym.

Scoring rubric (3 components):
  1. Validity    – is the CQL syntactically valid?        (0 or 1, from LogScale validateQuery)
  2. Semantic    – does the query capture user intent?     (1-5, LLM judge)
  3. Execution   – do the results look correct?            (1-5, LLM judge)

  reward = (validity + semantic/5 + execution/5) / 3      (0.0 – 1.0)

Quick start:
    bash setup.sh          # start container, ingest data
    python app.py          # start the resources server
"""

import re
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import ConfigDict

from nemo_gym.base_resources_server import (
    BaseResourcesServerConfig,
    BaseVerifyRequest,
    BaseVerifyResponse,
    SimpleResourcesServer,
)
from nemo_gym.config_types import ModelServerRef
from nemo_gym.openai_utils import (
    NeMoGymEasyInputMessage,
    NeMoGymResponse,
    NeMoGymResponseCreateParamsNonStreaming,
)
from nemo_gym.server_utils import get_response_json, raise_for_status

from container_engine import LogScaleContainerEngine

# ---------------------------------------------------------------------------
# Judge prompts (separate for semantic and execution)
# ---------------------------------------------------------------------------

SEMANTIC_JUDGE_PROMPT = """\
You are evaluating whether a CQL query captures the user's intent.

Question: {question}
Generated CQL: {query}

Score the SEMANTIC correctness on a 1-5 scale:
- 5 = Query perfectly captures the user's intent (correct fields, filters, aggregations, ordering)
- 4 = Minor deviation that would still give useful results
- 3 = Partially correct — misses an important aspect of the question
- 2 = Mostly wrong — fundamentally misunderstands the question
- 1 = Completely wrong or unrelated

Respond EXACTLY in this format:
SEMANTIC_SCORE: <1-5>
SEMANTIC_REASONING: <one sentence>"""

EXECUTION_JUDGE_PROMPT = """\
You are evaluating whether a CQL query produced correct results.

Question: {question}
Generated CQL: {query}
Query Results:
{results}

Score the EXECUTION correctness on a 1-5 scale:
- 5 = Results are exactly what you'd expect for this question
- 4 = Results are mostly correct with minor issues (e.g. slightly off sort order)
- 3 = Results are partially correct but have notable issues
- 2 = Results are mostly wrong or empty when they shouldn't be
- 1 = Complete failure — error, empty when data should exist, or nonsensical output

Respond EXACTLY in this format:
EXECUTION_SCORE: <1-5>
EXECUTION_REASONING: <one sentence>"""


# ---------------------------------------------------------------------------
# CQL extraction
# ---------------------------------------------------------------------------

def extract_cql_from_response(response: NeMoGymResponse) -> str:
    """Extract CQL from model output (strip markdown, take first block or full text)."""
    texts = []
    for o in getattr(response, "output", []) or []:
        if getattr(o, "type", None) != "message":
            continue
        for c in getattr(o, "content", []) or []:
            if getattr(c, "type", None) == "output_text":
                texts.append(getattr(c, "text", "") or "")
    raw = "".join(texts).strip()
    if not raw:
        return ""
    if raw.startswith("```"):
        raw = "\n".join(line for line in raw.split("\n") if not line.strip().startswith("```")).strip()
    return raw.strip()


# ---------------------------------------------------------------------------
# Query execution helpers
# ---------------------------------------------------------------------------

def _execute_container(engine: LogScaleContainerEngine, cql: str) -> dict:
    return engine.execute(cql)


def _execute_local(engine, cql: str) -> dict:
    import time
    t0 = time.time()
    try:
        result_df = engine.execute(cql)
        return {
            "success": True,
            "n_rows": len(result_df),
            "preview": result_df.head(15).to_string(index=False),
        }
    except Exception as e:
        return {
            "success": False,
            "n_rows": 0,
            "preview": "",
            "error": str(e),
        }


# ---------------------------------------------------------------------------
# Judge response parsers
# ---------------------------------------------------------------------------

def _parse_semantic_judge(raw: str) -> dict:
    sm = re.search(r"SEMANTIC_SCORE:\s*(\d)", raw)
    rm = re.search(r"SEMANTIC_REASONING:\s*(.+)", raw)
    return {
        "semantic_score": int(sm.group(1)) if sm else None,
        "semantic_reasoning": rm.group(1).strip() if rm else "",
    }


def _parse_execution_judge(raw: str) -> dict:
    sm = re.search(r"EXECUTION_SCORE:\s*(\d)", raw)
    rm = re.search(r"EXECUTION_REASONING:\s*(.+)", raw)
    return {
        "execution_score": int(sm.group(1)) if sm else None,
        "execution_reasoning": rm.group(1).strip() if rm else "",
    }


def compute_reward(validity: int, semantic: int | None, execution: int | None) -> float:
    """Combine the three rubric components into a single 0-1 reward.

    reward = (validity + semantic/5 + execution/5) / 3
    Falls back gracefully when judge scores are unavailable.
    """
    sem = (semantic / 5.0) if semantic is not None else 0.0
    exe = (execution / 5.0) if execution is not None else 0.0
    return (float(validity) + sem + exe) / 3.0


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class LogScaleCQLResourcesServerConfig(BaseResourcesServerConfig):
    name: str = "logscale_cql"

    engine: str = "container"
    logscale_url: str = "http://localhost:8080"
    repository: Optional[str] = None

    csv_path: Optional[str] = None
    judge_model_server: Optional[ModelServerRef] = None
    judge_responses_create_params: Optional[NeMoGymResponseCreateParamsNonStreaming] = None


class LogScaleCQLVerifyRequest(BaseVerifyRequest):
    model_config = ConfigDict(extra="allow")
    question: str = ""
    metadata: Optional[dict[str, Any]] = None


class LogScaleCQLVerifyResponse(BaseVerifyResponse):
    model_config = ConfigDict(extra="allow")
    question: str = ""
    extracted_cql: Optional[str] = None

    validity_score: int = 0
    validity_diagnostics: Optional[list[dict]] = None

    semantic_score: Optional[int] = None
    semantic_reasoning: Optional[str] = None

    execution_score: Optional[int] = None
    execution_reasoning: Optional[str] = None
    exec_success: bool = False


# ---------------------------------------------------------------------------
# Resources server
# ---------------------------------------------------------------------------

class LogScaleCQLResourcesServer(SimpleResourcesServer):
    config: LogScaleCQLResourcesServerConfig

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.config.engine == "container":
            self._container_engine = LogScaleContainerEngine(
                base_url=self.config.logscale_url,
                repository=self.config.repository,
            )
            self._local_engine = None
        else:
            self._container_engine = None
            self._init_local_engine()

    def _init_local_engine(self):
        import pandas as pd
        import sys

        _REPO_ROOT = Path(__file__).resolve().parent.parent.parent
        if str(_REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(_REPO_ROOT))

        from logscale_evaluations.cql_engine import CQLEngine

        csv_path = self.config.csv_path
        if not csv_path:
            csv_path = Path(__file__).resolve().parent / "data" / "synthetic_falcon_events.csv"
        csv_path = Path(csv_path)
        if not csv_path.is_file():
            raise FileNotFoundError(f"CSV not found (needed for engine=local): {csv_path}")

        df = pd.read_csv(csv_path, dtype=str)
        self._local_engine = CQLEngine(df)

    def _execute(self, cql: str) -> dict:
        if self._container_engine is not None:
            return _execute_container(self._container_engine, cql)
        return _execute_local(self._local_engine, cql)

    def _validate(self, cql: str) -> dict:
        """Score 1: is the query syntactically valid? Returns {is_valid, diagnostics}."""
        if self._container_engine is not None:
            return self._container_engine.validate_query(cql)
        # Local mode: no validation available, assume valid if it parses
        return {"is_valid": True, "diagnostics": []}

    async def _judge_call(self, prompt_text: str) -> str:
        """Send a prompt to the judge LLM and return the raw text response."""
        judge_params = self.config.judge_responses_create_params.model_copy(deep=True)
        judge_params.input = [NeMoGymEasyInputMessage(role="user", content=prompt_text)]
        response = await self.server_client.post(
            server_name=self.config.judge_model_server.name,
            url_path="/v1/responses",
            json=judge_params.model_dump(exclude_none=True),
        )
        await raise_for_status(response)
        judge_json = await get_response_json(response)
        judge_response = NeMoGymResponse.model_validate(judge_json)
        text = ""
        for o in getattr(judge_response, "output", []) or []:
            if getattr(o, "type", None) != "message":
                continue
            for c in getattr(o, "content", []) or []:
                if getattr(c, "type", None) == "output_text":
                    text += getattr(c, "text", "") or ""
        return text

    def setup_webserver(self) -> FastAPI:
        return super().setup_webserver()

    async def verify(self, body: LogScaleCQLVerifyRequest) -> LogScaleCQLVerifyResponse:
        question = (body.question or "").strip()
        if not question:
            return LogScaleCQLVerifyResponse(
                **body.model_dump(exclude={"question"}), reward=0.0, question=question,
            )

        cql = extract_cql_from_response(body.response)
        if not cql:
            return LogScaleCQLVerifyResponse(
                **body.model_dump(exclude={"question"}), reward=0.0, question=question,
                extracted_cql=None, validity_score=0,
                semantic_reasoning="No CQL extracted from model output.",
            )

        # -- Score 1: Validity (binary 0/1) --------------------------------
        validation = self._validate(cql)
        validity_score = 1 if validation["is_valid"] else 0

        # -- Execute the query (needed for score 3) -------------------------
        exec_result = self._execute(cql)
        exec_success = exec_result.get("success", False)
        results_str = exec_result["preview"] if exec_success else f"ERROR: {exec_result.get('error', 'unknown')}"

        # -- Scores 2 & 3: LLM judge (if configured) -----------------------
        semantic_score = None
        semantic_reasoning = ""
        execution_score = None
        execution_reasoning = ""

        has_judge = self.config.judge_model_server and self.config.judge_responses_create_params
        if has_judge:
            try:
                sem_raw = await self._judge_call(
                    SEMANTIC_JUDGE_PROMPT.format(question=question, query=cql)
                )
                parsed_sem = _parse_semantic_judge(sem_raw)
                semantic_score = parsed_sem["semantic_score"]
                semantic_reasoning = parsed_sem["semantic_reasoning"]
            except Exception as e:
                semantic_reasoning = f"Semantic judge error: {e}"

            try:
                exe_raw = await self._judge_call(
                    EXECUTION_JUDGE_PROMPT.format(question=question, query=cql, results=results_str)
                )
                parsed_exe = _parse_execution_judge(exe_raw)
                execution_score = parsed_exe["execution_score"]
                execution_reasoning = parsed_exe["execution_reasoning"]
            except Exception as e:
                execution_reasoning = f"Execution judge error: {e}"
        else:
            execution_score = 5 if exec_success else 1
            execution_reasoning = "Execution success" if exec_success else "Execution failed"

        reward = compute_reward(validity_score, semantic_score, execution_score)

        return LogScaleCQLVerifyResponse(
            **body.model_dump(exclude={"question"}),
            reward=reward,
            question=question,
            extracted_cql=cql,
            validity_score=validity_score,
            validity_diagnostics=validation.get("diagnostics"),
            semantic_score=semantic_score,
            semantic_reasoning=semantic_reasoning,
            execution_score=execution_score,
            execution_reasoning=execution_reasoning,
            exec_success=exec_success,
        )


if __name__ == "__main__":
    LogScaleCQLResourcesServer.run_webserver()
