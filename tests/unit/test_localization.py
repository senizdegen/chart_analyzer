"""Error messages exist in every supported language."""

from uuid import uuid4

import pytest

from chart_analyzer.application.localization import message_for
from chart_analyzer.application.views import to_view
from chart_analyzer.domain.enums import AnalysisStatus, AssetType, ErrorCode, Language
from tests.fakes import make_analysis, make_recognition, make_snapshot


@pytest.mark.parametrize("code", list(ErrorCode))
@pytest.mark.parametrize("language", list(Language))
def test_every_error_has_a_message_in_every_language(code: ErrorCode, language: Language) -> None:
    assert message_for(code, language).strip()


def test_messages_are_actually_translated() -> None:
    texts = {message_for(ErrorCode.UNCLEAR_CHART, language) for language in Language}
    assert len(texts) == len(Language)


def test_there_are_nine_languages() -> None:
    assert {language.value for language in Language} == {
        "en", "es", "pt", "de", "tr", "fr", "it", "id", "fil",
    }  # fmt: skip


class TestAnalysisView:
    def test_failed_analysis_carries_a_localized_error(self) -> None:
        analysis = make_analysis(
            uuid4(),
            language=Language.DE,
            status=AnalysisStatus.FAILED,
            error_code=ErrorCode.UNCLEAR_CHART,
        )

        view = to_view(analysis)

        assert view.error is not None
        assert view.error.code is ErrorCode.UNCLEAR_CHART
        assert view.error.message == "Bitte lade einen klaren Trading-Chart hoch."
        assert view.result is None

    def test_asset_comes_from_market_data_when_available(self) -> None:
        analysis = make_analysis(
            uuid4(),
            recognition=make_recognition(symbol="aapl", asset_name="Apple"),
            market_snapshot=make_snapshot(),
        )

        view = to_view(analysis)

        assert view.asset is not None
        assert (view.asset.symbol, view.asset.name) == ("AAPL", "Apple Inc")

    def test_asset_falls_back_to_what_the_ai_read(self) -> None:
        analysis = make_analysis(
            uuid4(),
            recognition=make_recognition(
                symbol="BTC", asset_name="Bitcoin", asset_type=AssetType.CRYPTO
            ),
        )

        view = to_view(analysis)

        assert view.asset is not None
        assert (view.asset.symbol, view.asset.type) == ("BTC", AssetType.CRYPTO)

    def test_no_asset_before_recognition(self) -> None:
        assert to_view(make_analysis(uuid4())).asset is None
