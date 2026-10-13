"""The seeded demo analyses obey the same schemas and rules as real ones."""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from chart_analyzer.domain.analysis_rules import validate_trading_signals
from chart_analyzer.domain.enums import ErrorCode, Language
from chart_analyzer.domain.models import AnalysisResult, ChartRecognition, MarketSnapshot

ROOT = Path(__file__).resolve().parents[2]


def _load_migration() -> ModuleType:
    path = ROOT / "migrations" / "versions" / "0002_demo_data.py"
    spec = importlib.util.spec_from_file_location("demo_data_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DEMOS: list[dict[str, Any]] = _load_migration().DEMO_ANALYSES
COMPLETED = [demo for demo in DEMOS if demo["status"] == "completed"]


@pytest.mark.parametrize("demo", DEMOS, ids=lambda demo: demo["id"][-4:])
def test_checkpoints_match_the_domain_schemas(demo: dict[str, Any]) -> None:
    ChartRecognition.model_validate(demo["recognition"])
    if demo["market_snapshot"] is not None:
        MarketSnapshot.model_validate(demo["market_snapshot"])
    assert Language(demo["language"])
    assert (ROOT / "samples" / demo["sample"]).exists()


@pytest.mark.parametrize("demo", COMPLETED, ids=lambda demo: demo["id"][-4:])
def test_completed_results_are_valid(demo: dict[str, Any]) -> None:
    result = AnalysisResult.model_validate(demo["result"])

    validate_trading_signals(result.trading_signals)
    assert result.language.value == demo["language"]
    assert result.asset.symbol == demo["market_snapshot"]["asset"]["symbol"]


def test_failed_demo_has_an_error_and_no_result() -> None:
    failed = [demo for demo in DEMOS if demo["status"] == "failed"]

    assert len(failed) == 1
    assert ErrorCode(failed[0]["error_code"]) is ErrorCode.UNCLEAR_CHART
    assert failed[0]["result"] is None


def test_crypto_demo_has_no_analyst_review() -> None:
    crypto = next(demo for demo in COMPLETED if demo["result"]["asset"]["type"] == "crypto")

    assert crypto["result"]["analyst_review"] is None


def test_quota_demo_user_cannot_start_another_analysis() -> None:
    spent = sum(
        demo["tokens_used"] for demo in COMPLETED if demo["user_id"] == "demo-user-no-quota"
    )

    assert spent + 20_000 > 500_000
