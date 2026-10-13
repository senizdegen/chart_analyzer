"""``ChartAI`` on top of the Chat Completions API of OpenAI or a compatible proxy.

Only the lowest common denominator of that API is used (``chat.completions.create`` with
an explicit JSON schema, parsed here), because the deployment talks to a LiteLLM proxy and
SDK conveniences such as ``parse()`` are not guaranteed to survive one.

Every failure is a ``TransientFailure``: the adapter never retries, the task queue does.
The adapter only checks the shape of an answer; whether its content makes sense is decided
by the domain.
"""

import base64

import structlog
from openai import AsyncOpenAI, OpenAIError
from openai.types.chat import ChatCompletion
from pydantic import ValidationError

from chart_analyzer.application.ports.services import AIAnswer, ChartImage
from chart_analyzer.domain.enums import ErrorCode, Language
from chart_analyzer.domain.errors import TransientFailure
from chart_analyzer.domain.models import ChartInsights, ChartRecognition, MarketSnapshot
from chart_analyzer.infrastructure.ai.prompts import (
    RECOGNITION_SYSTEM_PROMPT,
    RECOGNITION_USER_PROMPT,
    analysis_system_prompt,
    analysis_user_prompt,
)
from chart_analyzer.infrastructure.ai.schemas import (
    ANALYSIS_FORMAT,
    RECOGNITION_FORMAT,
    AnswerFormat,
)

logger = structlog.get_logger(__name__)


class OpenAIChartAI:
    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        recognition_model: str,
        analysis_model: str,
        recognition_max_tokens: int,
        analysis_max_tokens: int,
        max_news: int,
    ) -> None:
        self._client = client
        self._recognition_model = recognition_model
        self._analysis_model = analysis_model
        self._recognition_max_tokens = recognition_max_tokens
        self._analysis_max_tokens = analysis_max_tokens
        self._max_news = max_news

    async def recognize(self, image: ChartImage) -> AIAnswer[ChartRecognition]:
        return await self._ask(
            operation="recognize",
            model=self._recognition_model,
            max_tokens=self._recognition_max_tokens,
            system_prompt=RECOGNITION_SYSTEM_PROMPT,
            user_prompt=RECOGNITION_USER_PROMPT,
            image=image,
            answer_format=RECOGNITION_FORMAT,
        )

    async def analyze(
        self, image: ChartImage, language: Language, snapshot: MarketSnapshot
    ) -> AIAnswer[ChartInsights]:
        return await self._ask(
            operation="analyze",
            model=self._analysis_model,
            max_tokens=self._analysis_max_tokens,
            system_prompt=analysis_system_prompt(language, max_news=self._max_news),
            user_prompt=analysis_user_prompt(language, snapshot),
            image=image,
            answer_format=ANALYSIS_FORMAT,
        )

    async def _ask[T](
        self,
        *,
        operation: str,
        model: str,
        max_tokens: int,
        system_prompt: str,
        user_prompt: str,
        image: ChartImage,
        answer_format: AnswerFormat[T],
    ) -> AIAnswer[T]:
        log = logger.bind(operation=operation, model=model)
        try:
            response = await self._client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_prompt},
                            {"type": "image_url", "image_url": {"url": _data_url(image)}},
                        ],
                    },
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": answer_format.name,
                        "strict": True,
                        "schema": answer_format.schema,
                    },
                },
                max_completion_tokens=max_tokens,
            )
        except OpenAIError as error:
            # The error message is left out on purpose: providers echo parts of the request.
            error_type = type(error).__name__
            log.warning(
                "chart_ai.request_failed",
                error_type=error_type,
                status_code=getattr(error, "status_code", None),
                provider_code=getattr(error, "code", None),
            )
            raise TransientFailure(
                ErrorCode.AI_UNAVAILABLE, f"{operation}: {error_type}"
            ) from error

        # The SDK does not validate responses: a proxy answering 200 with a non-JSON body
        # (an HTML error page) comes back as a plain string, and fields may be missing.
        if not isinstance(response, ChatCompletion) or not response.choices:
            raise _invalid_response(log, operation, "no_completion")
        tokens = _total_tokens(response, log)
        log = log.bind(tokens=tokens)

        choice = response.choices[0]
        if choice.finish_reason in ("length", "content_filter"):
            raise _invalid_response(log, operation, f"finish_reason_{choice.finish_reason}")
        if choice.message.refusal:
            raise _invalid_response(log, operation, "refusal")
        if not choice.message.content:
            raise _invalid_response(log, operation, "empty_content")

        try:
            value = answer_format.parse(choice.message.content)
        except ValidationError as error:
            # Only the locations: the messages of pydantic quote the model's text.
            fields = sorted({".".join(str(part) for part in e["loc"]) for e in error.errors()})
            raise _invalid_response(log, operation, "schema_mismatch", fields=fields) from error

        log.info("chart_ai.answered")
        return AIAnswer(value=value, tokens=tokens)


def _data_url(image: ChartImage) -> str:
    return f"data:{image.content_type};base64,{base64.b64encode(image.data).decode('ascii')}"


def _total_tokens(response: ChatCompletion, log: structlog.stdlib.BoundLogger) -> int:
    tokens = getattr(response.usage, "total_tokens", None)
    if not isinstance(tokens, int):
        # Without usage the call is free for the quota; make that visible.
        log.warning("chart_ai.usage_missing")
        return 0
    return tokens


def _invalid_response(
    log: structlog.stdlib.BoundLogger, operation: str, reason: str, **details: object
) -> TransientFailure:
    log.warning("chart_ai.invalid_response", reason=reason, **details)
    return TransientFailure(ErrorCode.INVALID_AI_RESPONSE, f"{operation}: {reason}")
