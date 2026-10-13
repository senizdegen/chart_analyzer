"""Closed vocabularies of the domain.

Every value here is a stable machine code. The client maps codes to colours, icons and
its own translated labels, so codes never change with the user's language.
"""

from enum import StrEnum


class Language(StrEnum):
    """Languages Finelo supports; the AI writes its free text in the selected one."""

    EN = "en"
    ES = "es"
    PT = "pt"
    DE = "de"
    TR = "tr"
    FR = "fr"
    IT = "it"
    ID = "id"
    FIL = "fil"

    @property
    def english_name(self) -> str:
        return _LANGUAGE_NAMES[self]


_LANGUAGE_NAMES: dict[Language, str] = {
    Language.EN: "English",
    Language.ES: "Spanish",
    Language.PT: "Portuguese",
    Language.DE: "German",
    Language.TR: "Turkish",
    Language.FR: "French",
    Language.IT: "Italian",
    Language.ID: "Indonesian",
    Language.FIL: "Filipino",
}


class AssetType(StrEnum):
    STOCK = "stock"
    CRYPTO = "crypto"


class AnalysisStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (AnalysisStatus.COMPLETED, AnalysisStatus.FAILED)

    @property
    def is_active(self) -> bool:
        return not self.is_terminal


class Stage(StrEnum):
    """Real pipeline steps, reported to the client as progress."""

    QUEUED = "queued"
    RECOGNIZING = "recognizing"
    MARKET_DATA = "market_data"
    ANALYZING = "analyzing"
    DONE = "done"


class Trend(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    SIDEWAYS = "sideways"
    NEUTRAL = "neutral"


class Volatility(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class MarketSentiment(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class Position(StrEnum):
    LONG = "long"
    SHORT = "short"


class NewsSentiment(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class AnalystConsensus(StrEnum):
    BUY = "buy"
    HOLD = "hold"
    SELL = "sell"


class ErrorCode(StrEnum):
    """Stable error codes returned to the client next to a localized message."""

    # request-level (HTTP 4xx)
    MISSING_USER_ID = "missing_user_id"
    INVALID_IMAGE = "invalid_image"
    IMAGE_TOO_LARGE = "image_too_large"
    UNSUPPORTED_LANGUAGE = "unsupported_language"
    QUOTA_EXCEEDED = "quota_exceeded"
    TOO_MANY_ACTIVE_ANALYSES = "too_many_active_analyses"
    ANALYSIS_NOT_FOUND = "analysis_not_found"
    VALIDATION_ERROR = "validation_error"
    # analysis-level (reported through the analysis status and the event stream)
    UNCLEAR_CHART = "unclear_chart"
    ASSET_NOT_FOUND = "asset_not_found"
    MARKET_DATA_UNAVAILABLE = "market_data_unavailable"
    AI_UNAVAILABLE = "ai_unavailable"
    INVALID_AI_RESPONSE = "invalid_ai_response"
    INTERNAL_ERROR = "internal_error"
