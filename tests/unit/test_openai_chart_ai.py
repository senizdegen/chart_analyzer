"""The OpenAI adapter against a mocked HTTP transport: real SDK, real requests, no network."""

import base64
import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI
from structlog.testing import capture_logs

from chart_analyzer.application.ports.services import ChartImage
from chart_analyzer.domain.enums import (
    AssetType,
    ErrorCode,
    Language,
    MarketSentiment,
    NewsSentiment,
    Position,
    Trend,
    Volatility,
)
from chart_analyzer.domain.errors import TransientFailure
from chart_analyzer.infrastructure.ai.openai_chart_ai import OpenAIChartAI
from chart_analyzer.infrastructure.ai.schemas import ANALYSIS_FORMAT, RECOGNITION_FORMAT
from tests.fakes import make_insights, make_recognition, make_snapshot

API_KEY = "test-secret-key"
IMAGE = ChartImage(data=b"\x89PNG fake chart bytes", content_type="image/png")
IMAGE_URL = "data:image/png;base64," + base64.b64encode(IMAGE.data).decode()

Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    """Mock transport handler that keeps the requests it answered."""

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    @property
    def body(self) -> dict[str, Any]:
        assert len(self.requests) == 1, "the adapter must not retry"
        body: dict[str, Any] = json.loads(self.requests[0].content)
        return body


def completion(
    content: str | None,
    *,
    finish_reason: str = "stop",
    refusal: str | None = None,
    total_tokens: int | None = 1_234,
) -> httpx.Response:
    body: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1_760_000_000,
        "model": "served-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
            }
        ],
    }
    if total_tokens is not None:
        body["usage"] = {
            "prompt_tokens": total_tokens - 100,
            "completion_tokens": 100,
            "total_tokens": total_tokens,
        }
    return httpx.Response(200, json=body)


def answering(response: httpx.Response) -> Recorder:
    return Recorder(lambda request: response)


def make_ai(handler: Handler, *, max_news: int = 3) -> OpenAIChartAI:
    client = AsyncOpenAI(
        api_key=API_KEY,
        base_url="http://test/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_retries=0,
    )
    return OpenAIChartAI(
        client,
        recognition_model="vision-mini",
        analysis_model="vision-large",
        recognition_max_tokens=300,
        analysis_max_tokens=2_000,
        max_news=max_news,
    )


def image_urls(body: dict[str, Any]) -> list[str]:
    return [
        part["image_url"]["url"]
        for message in body["messages"]
        if isinstance(message["content"], list)
        for part in message["content"]
        if part["type"] == "image_url"
    ]


def prompt_text(body: dict[str, Any]) -> str:
    """All text the model receives, system and user, without the image."""
    texts: list[str] = []
    for message in body["messages"]:
        if isinstance(message["content"], str):
            texts.append(message["content"])
        else:
            texts.extend(part["text"] for part in message["content"] if part["type"] == "text")
    return "\n".join(texts)


# --- recognition ------------------------------------------------------------------------------


async def test_recognize_sends_image_schema_and_limit_and_parses_the_answer() -> None:
    expected = make_recognition()
    recorder = answering(completion(expected.model_dump_json(), total_tokens=1_200))

    answer = await make_ai(recorder).recognize(IMAGE)

    assert answer.value == expected
    assert answer.tokens == 1_200

    request = recorder.requests[0]
    assert request.method == "POST"
    assert request.url == "http://test/v1/chat/completions"
    body = recorder.body
    assert body["model"] == "vision-mini"
    assert body["max_completion_tokens"] == 300
    assert "max_tokens" not in body
    assert "temperature" not in body
    assert image_urls(body) == [IMAGE_URL]
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "chart_recognition",
            "strict": True,
            "schema": RECOGNITION_FORMAT.schema,
        },
    }


async def test_recognize_parses_a_non_chart() -> None:
    content = json.dumps(
        {"is_chart": False, "symbol": None, "asset_name": None, "asset_type": None}
    )

    answer = await make_ai(answering(completion(content))).recognize(IMAGE)

    assert answer.value == make_recognition(
        is_chart=False, symbol=None, asset_name=None, asset_type=None
    )


async def test_recognize_parses_a_chart_of_an_unsupported_asset() -> None:
    expected = make_recognition(symbol="EURUSD", asset_name="Euro / US Dollar", asset_type=None)

    answer = await make_ai(answering(completion(expected.model_dump_json()))).recognize(IMAGE)

    assert answer.value == expected


# --- analysis ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "name"), [(Language.TR, "Turkish"), (Language.FIL, "Filipino")]
)
async def test_analyze_sends_language_and_market_data_and_parses_the_answer(
    language: Language, name: str
) -> None:
    expected = make_insights()
    snapshot = make_snapshot()
    recorder = answering(completion(expected.model_dump_json(), total_tokens=6_800))

    answer = await make_ai(recorder, max_news=2).analyze(IMAGE, language, snapshot)

    assert answer.value == expected
    assert answer.tokens == 6_800

    body = recorder.body
    assert body["model"] == "vision-large"
    assert body["max_completion_tokens"] == 2_000
    assert "temperature" not in body
    assert image_urls(body) == [IMAGE_URL]
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "chart_insights", "strict": True, "schema": ANALYSIS_FORMAT.schema},
    }

    system = body["messages"][0]
    assert name in system["content"]
    assert "at most 2" in system["content"]
    text = prompt_text(body)
    assert name in text
    assert "340.97" in text
    assert '"symbol":"AAPL"' in text
    assert '"consensus":"buy"' in text
    for article in snapshot.news:
        assert f'"id":"{article.id}"' in text
        assert article.headline in text
        assert article.url not in text


async def test_analyze_without_analyst_review_and_news_still_builds_a_request() -> None:
    expected = make_insights(news=[])
    snapshot = make_snapshot(analyst_review=None, news=[])
    recorder = answering(completion(expected.model_dump_json()))

    answer = await make_ai(recorder).analyze(IMAGE, Language.EN, snapshot)

    assert answer.value == expected
    text = prompt_text(recorder.body)
    assert '"analyst_review":null' in text
    assert '"candidate_news":[]' in text
    assert "English" in text


async def test_analyze_keeps_non_ascii_market_data_readable() -> None:
    article = make_snapshot().news[0].model_copy(update={"headline": "Apple'ın geliri arttı"})
    recorder = answering(completion(make_insights().model_dump_json()))

    await make_ai(recorder).analyze(IMAGE, Language.TR, make_snapshot(news=[article]))

    assert "Apple'ın geliri arttı" in prompt_text(recorder.body)


# --- provider errors --------------------------------------------------------------------------


def _raise(error: Exception) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    return handler


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(
            lambda r: httpx.Response(429, json={"error": {"message": "slow down"}}), id="429"
        ),
        pytest.param(lambda r: httpx.Response(500, json={"error": {"message": "boom"}}), id="500"),
        pytest.param(lambda r: httpx.Response(502, text="<html>bad gateway</html>"), id="502-html"),
        pytest.param(lambda r: httpx.Response(400, json={"error": {"message": "bad"}}), id="400"),
        pytest.param(lambda r: httpx.Response(401, json={"error": {"message": "key"}}), id="401"),
        pytest.param(lambda r: httpx.Response(403, json={"error": {"message": "no"}}), id="403"),
        pytest.param(lambda r: httpx.Response(404, json={"error": {"message": "model"}}), id="404"),
        pytest.param(_raise(httpx.ReadTimeout("timed out")), id="timeout"),
        pytest.param(_raise(httpx.ConnectError("refused")), id="connection"),
    ],
)
async def test_provider_errors_are_transient_ai_unavailable(handler: Handler) -> None:
    recorder = Recorder(handler)
    ai = make_ai(recorder)

    with pytest.raises(TransientFailure) as recognize_error:
        await ai.recognize(IMAGE)
    with pytest.raises(TransientFailure) as analyze_error:
        await ai.analyze(IMAGE, Language.EN, make_snapshot())

    assert recognize_error.value.code is ErrorCode.AI_UNAVAILABLE
    assert analyze_error.value.code is ErrorCode.AI_UNAVAILABLE
    assert len(recorder.requests) == 2, "one request per call: the adapter must not retry"


# --- unusable answers -------------------------------------------------------------------------


def _insights_with(path: tuple[str, ...], value: object) -> str:
    data = make_insights().model_dump(mode="json")
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return json.dumps(data)


def _insights_without(key: str) -> str:
    data = make_insights().model_dump(mode="json")
    del data[key]
    return json.dumps(data)


UNUSABLE_ANALYSES = [
    pytest.param(completion(None, refusal="I can't help with that."), id="refusal"),
    pytest.param(completion(None), id="null-content"),
    pytest.param(completion(""), id="empty-content"),
    pytest.param(
        completion(make_insights().model_dump_json()[:200], finish_reason="length"),
        id="truncated",
    ),
    pytest.param(completion(None, finish_reason="content_filter"), id="content-filter"),
    pytest.param(completion("Sure! Here is the analysis:"), id="invalid-json"),
    pytest.param(completion(_insights_without("trading_signals")), id="missing-field"),
    pytest.param(completion(_insights_without("news")), id="missing-news"),
    pytest.param(
        completion(_insights_with(("trading_signals", "start_level"), "high")), id="wrong-type"
    ),
    pytest.param(completion(_insights_with(("extra",), 1)), id="extra-field"),
    pytest.param(
        completion(_insights_with(("key_insights", "trend", "value"), "moon")),
        id="unknown-trend",
    ),
    pytest.param(
        completion(_insights_with(("trading_signals", "position"), "hold")),
        id="unknown-position",
    ),
    pytest.param(httpx.Response(200, text="<html>login</html>"), id="200-not-json"),
    pytest.param(httpx.Response(200, json={"id": "x", "choices": []}), id="no-choices"),
]


@pytest.mark.parametrize("response", UNUSABLE_ANALYSES)
async def test_unusable_analysis_is_transient_invalid_ai_response(
    response: httpx.Response,
) -> None:
    with pytest.raises(TransientFailure) as error:
        await make_ai(answering(response)).analyze(IMAGE, Language.EN, make_snapshot())

    assert error.value.code is ErrorCode.INVALID_AI_RESPONSE


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(completion(None, refusal="No."), id="refusal"),
        pytest.param(completion('{"is_chart": tr', finish_reason="length"), id="truncated"),
        pytest.param(completion("not json"), id="invalid-json"),
        pytest.param(completion('{"is_chart": true}'), id="missing-fields"),
        pytest.param(
            completion(
                json.dumps(
                    {
                        "is_chart": True,
                        "symbol": "EURUSD",
                        "asset_name": None,
                        "asset_type": "forex",
                    }
                )
            ),
            id="unknown-asset-type",
        ),
    ],
)
async def test_unusable_recognition_is_transient_invalid_ai_response(
    response: httpx.Response,
) -> None:
    with pytest.raises(TransientFailure) as error:
        await make_ai(answering(response)).recognize(IMAGE)

    assert error.value.code is ErrorCode.INVALID_AI_RESPONSE


# --- tokens and logs --------------------------------------------------------------------------


async def test_missing_usage_counts_zero_tokens_and_warns() -> None:
    response = completion(make_recognition().model_dump_json(), total_tokens=None)

    with capture_logs() as logs:
        answer = await make_ai(answering(response)).recognize(IMAGE)

    assert answer.value == make_recognition()
    assert answer.tokens == 0
    assert [entry["log_level"] for entry in logs if entry["event"] == "chart_ai.usage_missing"] == [
        "warning"
    ]


async def test_failures_are_logged_without_secrets_or_payload() -> None:
    secret_headline = "Headline that must stay out of the logs"
    article = make_snapshot().news[0].model_copy(update={"headline": secret_headline})
    snapshot = make_snapshot(news=[article])
    rejected = httpx.Response(401, json={"error": {"message": f"bad key {API_KEY}"}})
    mismatch = completion(_insights_with(("summary",), 5))

    with capture_logs() as logs:
        with pytest.raises(TransientFailure) as unavailable:
            await make_ai(answering(rejected)).analyze(IMAGE, Language.EN, snapshot)
        with pytest.raises(TransientFailure) as invalid:
            await make_ai(answering(mismatch)).analyze(IMAGE, Language.EN, snapshot)

    failed, unusable = logs
    assert failed["event"] == "chart_ai.request_failed"
    assert failed["model"] == "vision-large"
    assert failed["error_type"] == "AuthenticationError"
    assert failed["status_code"] == 401
    assert unusable["event"] == "chart_ai.invalid_response"
    assert unusable["model"] == "vision-large"
    assert unusable["reason"] == "schema_mismatch"
    assert unusable["fields"] == ["summary"]

    exposed = repr(logs) + unavailable.value.detail + invalid.value.detail
    assert API_KEY not in exposed
    assert IMAGE_URL not in exposed
    assert secret_headline not in exposed


# --- schema strictness ------------------------------------------------------------------------


def _objects(node: object) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for value in node.values():
            yield from _objects(value)
    elif isinstance(node, list):
        for item in node:
            yield from _objects(item)


@pytest.mark.parametrize(
    ("schema", "object_count"),
    [
        pytest.param(RECOGNITION_FORMAT.schema, 1, id="recognition"),
        pytest.param(ANALYSIS_FORMAT.schema, 7, id="analysis"),
    ],
)
def test_schemas_are_strict(schema: dict[str, Any], object_count: int) -> None:
    objects = list(_objects(schema))

    assert len(objects) == object_count
    for node in objects:
        assert node["additionalProperties"] is False
        assert node["required"] == list(node["properties"])
    serialized = json.dumps(schema)
    assert "$ref" not in serialized
    assert "$defs" not in serialized
    assert '"default"' not in serialized


def test_recognition_schema_matches_the_domain() -> None:
    properties: dict[str, Any] = RECOGNITION_FORMAT.schema["properties"]  # type: ignore[assignment]

    assert list(properties) == ["is_chart", "symbol", "asset_name", "asset_type"]
    assert properties["is_chart"]["type"] == "boolean"
    for nullable in ("symbol", "asset_name", "asset_type"):
        assert {"type": "null"} in properties[nullable]["anyOf"]
    assert properties["asset_type"]["anyOf"][0]["enum"] == [item.value for item in AssetType]


def test_analysis_schema_enums_equal_the_domain_enums() -> None:
    properties: dict[str, Any] = ANALYSIS_FORMAT.schema["properties"]  # type: ignore[assignment]
    insights = properties["key_insights"]["properties"]
    signals = properties["trading_signals"]["properties"]
    news = properties["news"]["items"]["properties"]

    def codes(node: dict[str, Any]) -> list[str]:
        enum: list[str] = node["enum"]
        return enum

    assert codes(insights["trend"]["properties"]["value"]) == [item.value for item in Trend]
    assert codes(insights["volatility"]["properties"]["value"]) == [
        item.value for item in Volatility
    ]
    assert codes(insights["market_sentiment"]["properties"]["value"]) == [
        item.value for item in MarketSentiment
    ]
    assert codes(signals["position"]) == [item.value for item in Position]
    assert codes(news["sentiment"]) == [item.value for item in NewsSentiment]
    assert list(properties) == ["summary", "key_insights", "trading_signals", "news"]
    assert list(signals) == [
        "position",
        "start_level",
        "safety_stop",
        "projected_exit_1",
        "projected_exit_2",
        "support_level",
        "resistance_level",
        "rationale",
    ]
