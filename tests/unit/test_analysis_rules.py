"""Analysis logic: what we accept from the AI and how results are assembled."""

from datetime import date

import pytest

from chart_analyzer.domain.analysis_rules import (
    assemble_result,
    build_analyst_review,
    normalize_symbol,
    resolve_recognized_asset,
    validate_insights,
    validate_price_scale,
    validate_trading_signals,
)
from chart_analyzer.domain.enums import (
    AnalystConsensus,
    AssetType,
    ErrorCode,
    Language,
    NewsSentiment,
    Position,
)
from chart_analyzer.domain.errors import AnalysisFailure, TransientFailure
from chart_analyzer.domain.models import ChartInsights, CurrentPrice, NewsAssessment
from tests.fakes import NOW, make_insights, make_recognition, make_signals, make_snapshot


class TestNormalizeSymbol:
    @pytest.mark.parametrize(
        ("raw", "asset_type", "expected"),
        [
            ("aapl", AssetType.STOCK, "AAPL"),
            (" NASDAQ:AAPL ", AssetType.STOCK, "AAPL"),
            ("BRK.B", AssetType.STOCK, "BRK.B"),
            ("BTCUSDT", AssetType.CRYPTO, "BTC"),
            ("BINANCE:ETHUSDT", AssetType.CRYPTO, "ETH"),
            ("btc/usd", AssetType.CRYPTO, "BTC"),
            ("SOL-USD", AssetType.CRYPTO, "SOL"),
            ("BTC", AssetType.CRYPTO, "BTC"),
            ("USDT", AssetType.CRYPTO, "USDT"),
        ],
    )
    def test_reduces_chart_labels_to_base_ticker(
        self, raw: str, asset_type: AssetType, expected: str
    ) -> None:
        assert normalize_symbol(raw, asset_type) == expected

    def test_stock_ticker_ending_in_usd_is_not_truncated(self) -> None:
        assert normalize_symbol("MUSD", AssetType.STOCK) == "MUSD"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("BTCUSDTPERP", "BTC"),
            ("BINANCE:BTCUSDT.P", "BTC"),
            ("BTCUSDT.P", "BTC"),
            ("ethusd.p", "ETH"),
            ("BTC-PERP", "BTC"),
            ("BTCPERP", "BTC"),
            ("BYBIT:SOLUSDC.P", "SOL"),
        ],
    )
    def test_perpetual_futures_labels_reduce_to_the_base_ticker(
        self, raw: str, expected: str
    ) -> None:
        assert normalize_symbol(raw, AssetType.CRYPTO) == expected

    @pytest.mark.parametrize(
        ("raw", "asset_type", "expected"),
        [
            ("PERP", AssetType.CRYPTO, "PERP"),  # the Perpetual Protocol coin itself
            ("PERPUSDT", AssetType.CRYPTO, "PERP"),
            ("XRP", AssetType.CRYPTO, "XRP"),
            ("XRPUSDT", AssetType.CRYPTO, "XRP"),
            ("BRK.P", AssetType.STOCK, "BRK.P"),  # a stock share class is not a marker
            ("PERP", AssetType.STOCK, "PERP"),
        ],
    )
    def test_perpetual_markers_are_not_cut_out_of_other_symbols(
        self, raw: str, asset_type: AssetType, expected: str
    ) -> None:
        assert normalize_symbol(raw, asset_type) == expected


class TestResolveRecognizedAsset:
    def test_returns_symbol_and_type(self) -> None:
        recognition = make_recognition(symbol="NASDAQ:AAPL")
        assert resolve_recognized_asset(recognition) == ("AAPL", AssetType.STOCK)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"is_chart": False, "symbol": None, "asset_type": None},
            {"symbol": None},
            {"symbol": "   "},
            {"asset_type": None},
            {"symbol": "not a ticker at all!"},
        ],
    )
    def test_unusable_recognition_is_a_permanent_failure(
        self, overrides: dict[str, object]
    ) -> None:
        with pytest.raises(AnalysisFailure) as error:
            resolve_recognized_asset(make_recognition(**overrides))
        assert error.value.code is ErrorCode.UNCLEAR_CHART


class TestBuildAnalystReview:
    def test_computes_shares_and_buy_consensus(self) -> None:
        review = build_analyst_review(
            date(2026, 9, 1), strong_buy=12, buy=22, hold=15, sell=3, strong_sell=1
        )
        assert review is not None
        assert review.total_analysts == 53
        assert review.consensus is AnalystConsensus.BUY
        assert review.strong_buy.percent == 22.6
        assert review.buy.percent == 41.5
        shares = (review.strong_buy, review.buy, review.hold, review.sell, review.strong_sell)
        assert sum(share.percent for share in shares) == pytest.approx(100, abs=0.3)

    def test_sell_consensus(self) -> None:
        review = build_analyst_review(
            date(2026, 9, 1), strong_buy=1, buy=2, hold=4, sell=6, strong_sell=5
        )
        assert review is not None and review.consensus is AnalystConsensus.SELL

    def test_tie_resolves_to_hold(self) -> None:
        review = build_analyst_review(
            date(2026, 9, 1), strong_buy=2, buy=3, hold=5, sell=3, strong_sell=2
        )
        assert review is not None and review.consensus is AnalystConsensus.HOLD

    def test_no_coverage_gives_no_review(self) -> None:
        assert (
            build_analyst_review(
                date(2026, 9, 1), strong_buy=0, buy=0, hold=0, sell=0, strong_sell=0
            )
            is None
        )

    def test_negative_counts_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            build_analyst_review(
                date(2026, 9, 1), strong_buy=-1, buy=0, hold=0, sell=0, strong_sell=0
            )


class TestValidateTradingSignals:
    def test_consistent_long_passes(self) -> None:
        validate_trading_signals(make_signals())

    def test_consistent_short_passes(self) -> None:
        validate_trading_signals(
            make_signals(
                position=Position.SHORT,
                start_level=340.0,
                safety_stop=356.0,
                projected_exit_1=320.0,
                projected_exit_2=305.0,
            )
        )

    @pytest.mark.parametrize(
        "overrides",
        [
            {"safety_stop": 350.0},  # stop above entry on a long
            {"projected_exit_1": 330.0},  # target below entry on a long
            {"projected_exit_2": 360.0},  # second target before the first
            {"position": Position.SHORT},  # long levels labelled as short
            {"start_level": 0.0},
            {"safety_stop": -5.0},
            {"projected_exit_2": float("inf")},
            {"support_level": float("nan")},
            {"support_level": 400.0},  # support above resistance
        ],
    )
    def test_inconsistent_levels_are_rejected_as_retryable(
        self, overrides: dict[str, object]
    ) -> None:
        with pytest.raises(TransientFailure) as error:
            validate_trading_signals(make_signals(**overrides))
        assert error.value.code is ErrorCode.INVALID_AI_RESPONSE


class TestAssembleResult:
    def test_merges_insights_with_market_data(self) -> None:
        snapshot, insights = make_snapshot(), make_insights()

        result = assemble_result(Language.ES, snapshot, insights, max_news=3)

        assert result.language is Language.ES
        assert result.asset == snapshot.asset
        assert result.current_price == snapshot.price
        assert result.analyst_review == snapshot.analyst_review
        assert result.trading_signals == insights.trading_signals
        assert [item.headline for item in result.latest_news] == [
            "Apple posts record results",
            "Regulators probe Apple",
        ]

    def test_news_facts_come_from_the_provider_not_the_model(self) -> None:
        result = assemble_result(Language.EN, make_snapshot(), make_insights(), max_news=3)

        first = result.latest_news[0]
        assert first.original_headline == "Original headline 1"
        assert first.source == "Yahoo"
        assert first.url == "https://example.com/1"
        assert first.sentiment is NewsSentiment.POSITIVE

    def test_drops_invented_and_duplicate_news_ids(self) -> None:
        insights = make_insights(
            news=[
                NewsAssessment(id="n1", headline="Real", sentiment="positive"),
                NewsAssessment(id="made-up", headline="Invented", sentiment="negative"),
                NewsAssessment(id="n1", headline="Repeated", sentiment="neutral"),
            ]
        )

        result = assemble_result(Language.EN, make_snapshot(), insights, max_news=3)

        assert [item.headline for item in result.latest_news] == ["Real"]

    def test_caps_news_at_max_news(self) -> None:
        insights = make_insights(
            news=[
                NewsAssessment(id=f"n{index}", headline=f"H{index}", sentiment="neutral")
                for index in (1, 2, 3)
            ]
        )

        result = assemble_result(Language.EN, make_snapshot(), insights, max_news=2)

        assert len(result.latest_news) == 2

    def test_blank_translated_headline_falls_back_to_original(self) -> None:
        insights = make_insights(news=[NewsAssessment(id="n2", headline="  ", sentiment="neutral")])

        result = assemble_result(Language.EN, make_snapshot(), insights, max_news=3)

        assert result.latest_news[0].headline == "Original headline 2"

    def test_asset_without_analyst_coverage_keeps_the_block_empty(self) -> None:
        snapshot = make_snapshot(analyst_review=None)

        result = assemble_result(Language.EN, snapshot, make_insights(), max_news=3)

        assert result.analyst_review is None

    def test_invalid_signals_abort_assembly(self) -> None:
        insights = make_insights(trading_signals=make_signals(safety_stop=999.0))
        with pytest.raises(TransientFailure):
            assemble_result(Language.EN, make_snapshot(), insights, max_news=3)

    def test_empty_summary_is_rejected(self) -> None:
        with pytest.raises(TransientFailure):
            assemble_result(Language.EN, make_snapshot(), make_insights(summary="  "), max_news=3)


def _with_explanation(insight: str, text: str) -> ChartInsights:
    insights = make_insights()
    blanked = getattr(insights.key_insights, insight).model_copy(update={"explanation": text})
    key_insights = insights.key_insights.model_copy(update={insight: blanked})
    return insights.model_copy(update={"key_insights": key_insights})


class TestValidateInsights:
    def test_complete_answer_passes(self) -> None:
        validate_insights(make_insights())

    @pytest.mark.parametrize("insight", ["trend", "volatility", "market_sentiment"])
    @pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
    def test_blank_explanation_is_rejected_as_retryable(self, insight: str, blank: str) -> None:
        with pytest.raises(TransientFailure) as error:
            validate_insights(_with_explanation(insight, blank))
        assert error.value.code is ErrorCode.INVALID_AI_RESPONSE

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_rationale_is_rejected_as_retryable(self, blank: str) -> None:
        with pytest.raises(TransientFailure) as error:
            validate_insights(make_insights(trading_signals=make_signals(rationale=blank)))
        assert error.value.code is ErrorCode.INVALID_AI_RESPONSE

    def test_blank_texts_abort_assembly(self) -> None:
        for insights in (
            _with_explanation("volatility", " "),
            make_insights(trading_signals=make_signals(rationale=" ")),
        ):
            with pytest.raises(TransientFailure):
                assemble_result(Language.EN, make_snapshot(), insights, max_news=3)


def _scaled_signals(factor: float) -> object:
    """The sample long setup (consistent in itself) on another price scale."""
    return make_signals(
        start_level=340.0 * factor,
        safety_stop=323.0 * factor,
        projected_exit_1=365.0 * factor,
        projected_exit_2=380.0 * factor,
        support_level=325.0 * factor,
        resistance_level=366.0 * factor,
    )


class TestPriceScale:
    """Levels must be on the scale of the real price (340.97 in the sample snapshot)."""

    @pytest.mark.parametrize("factor", [100.0, 0.01, 10.0, 0.1])
    def test_levels_far_from_the_current_price_abort_assembly(self, factor: float) -> None:
        insights = make_insights(trading_signals=_scaled_signals(factor))
        validate_insights(insights)  # nothing else is wrong with the answer

        with pytest.raises(TransientFailure) as error:
            assemble_result(Language.EN, make_snapshot(), insights, max_news=3)

        assert error.value.code is ErrorCode.INVALID_AI_RESPONSE

    @pytest.mark.parametrize("factor", [1.0, 0.5, 2.0])
    def test_levels_within_the_band_are_accepted(self, factor: float) -> None:
        # an old chart: the price has halved or doubled since
        insights = make_insights(trading_signals=_scaled_signals(factor))

        result = assemble_result(Language.EN, make_snapshot(), insights, max_news=3)

        assert result.trading_signals == insights.trading_signals

    def test_the_band_follows_the_price_of_the_asset(self) -> None:
        insights = make_insights(trading_signals=_scaled_signals(100.0))
        snapshot = make_snapshot(price=CurrentPrice(price=34_097.0, currency="USD", as_of=NOW))

        assert assemble_result(Language.EN, snapshot, insights, max_news=3).trading_signals

    def test_band_is_five_times_either_way_inclusive(self) -> None:
        within = make_signals(
            start_level=100.0, safety_stop=20.0, projected_exit_1=400.0, projected_exit_2=500.0
        )
        validate_price_scale(within, 100.0)

        for name, value in (("safety_stop", 19.99), ("projected_exit_2", 500.01)):
            with pytest.raises(TransientFailure) as error:
                validate_price_scale(within.model_copy(update={name: value}), 100.0)
            assert error.value.code is ErrorCode.INVALID_AI_RESPONSE

    @pytest.mark.parametrize(
        "name", ["start_level", "safety_stop", "projected_exit_1", "projected_exit_2"]
    )
    def test_every_level_is_checked(self, name: str) -> None:
        signals = make_signals().model_copy(update={name: 5_000.0})

        with pytest.raises(TransientFailure):
            validate_price_scale(signals, 340.97)
