"""Demo data: three users that show history, a failed upload and a spent quota.

| x-user-id          | What it demonstrates                                              |
|--------------------|-------------------------------------------------------------------|
| demo-user          | History: AAPL (en), BTC (es, no analyst review), TSLA (de) and    |
|                    | one failed upload that was not a chart                            |
| demo-user-no-quota | 495,000 tokens spent today: a new upload is rejected with 429     |
| demo-user-new      | No rows at all: empty history and the full quota                  |

The quota resets at 00:00 UTC, so ``demo-user-no-quota`` is over the limit only on the
day the migration ran. The images of the seeded analyses are copied from ``samples/``
into the upload directory.

Revision ID: 0002
Revises: 0001
"""

import json
import os
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"

AAPL_ID = "0d5e1c1a-0001-4a6b-9c1d-000000000001"
BTC_ID = "0d5e1c1a-0002-4a6b-9c1d-000000000002"
TSLA_ID = "0d5e1c1a-0003-4a6b-9c1d-000000000003"
FAILED_ID = "0d5e1c1a-0004-4a6b-9c1d-000000000004"
QUOTA_ID = "0d5e1c1a-0005-4a6b-9c1d-000000000005"


def _share(count: int, total: int) -> dict[str, Any]:
    return {"count": count, "percent": round(count * 100 / total, 1)}


def _review(
    consensus: str, strong_buy: int, buy: int, hold: int, sell: int, strong_sell: int
) -> dict[str, Any]:
    total = strong_buy + buy + hold + sell + strong_sell
    return {
        "period": "2026-09-01",
        "total_analysts": total,
        "consensus": consensus,
        "strong_buy": _share(strong_buy, total),
        "buy": _share(buy, total),
        "hold": _share(hold, total),
        "sell": _share(sell, total),
        "strong_sell": _share(strong_sell, total),
    }


def _insight(value: str, explanation: str) -> dict[str, str]:
    return {"value": value, "explanation": explanation}


def _demo(
    *,
    analysis_id: str,
    sample: str,
    language: str,
    age: timedelta,
    tokens: int,
    asset: dict[str, Any],
    price: dict[str, Any],
    review: dict[str, Any] | None,
    news: list[dict[str, Any]],
    summary: str,
    insights: dict[str, Any],
    signals: dict[str, Any],
    printed_symbol: str,
) -> dict[str, Any]:
    """One completed demo analysis with the same checkpoints a real run leaves behind."""
    articles = [
        {
            "id": item["id"],
            "headline": item["original_headline"],
            "summary": "",
            "source": item["source"],
            "url": item["url"],
            "published_at": item["published_at"],
        }
        for item in news
    ]
    latest_news = [{key: value for key, value in item.items() if key != "id"} for item in news]
    return {
        "id": analysis_id,
        "user_id": "demo-user",
        "sample": sample,
        "language": language,
        "status": "completed",
        "age": age,
        "tokens_used": tokens,
        "error_code": None,
        "recognition": {
            "is_chart": True,
            "symbol": printed_symbol,
            "asset_name": asset["name"],
            "asset_type": asset["type"],
        },
        "market_snapshot": {
            "asset": asset,
            "price": price,
            "analyst_review": review,
            "news": articles,
        },
        "result": {
            "language": language,
            "asset": asset,
            "summary": summary,
            "current_price": price,
            "analyst_review": review,
            "key_insights": insights,
            "trading_signals": signals,
            "latest_news": latest_news,
        },
    }


DEMO_ANALYSES: list[dict[str, Any]] = [
    _demo(
        analysis_id=AAPL_ID,
        sample="aapl.png",
        language="en",
        age=timedelta(hours=2),
        tokens=7466,
        printed_symbol="AAPL",
        asset={
            "symbol": "AAPL",
            "name": "Apple Inc",
            "type": "stock",
            "logo_url": "https://static2.finnhub.io/file/publicdatany/finnhubimage/stock_logo/AAPL.png",
        },
        price={
            "price": 340.97,
            "currency": "USD",
            "change": 4.33,
            "change_percent": 1.2862,
            "as_of": "2026-10-12T20:00:00Z",
        },
        review=_review("buy", 12, 22, 15, 3, 1),
        news=[
            {
                "id": "142825301",
                "headline": "Apple Stock Nears Record High As iPhone Share Hits 22%",
                "original_headline": "Apple Stock Nears Record High As iPhone Share Hits 22%",
                "sentiment": "positive",
                "source": "Yahoo",
                "url": "https://finance.yahoo.com/quote/AAPL/news/",
                "published_at": "2026-10-12T19:32:55Z",
            },
            {
                "id": "142825302",
                "headline": "Apple Is Near a $5 Trillion Market Cap (Again)",
                "original_headline": "Apple Is Near a $5 Trillion Market Cap (Again)",
                "sentiment": "positive",
                "source": "Yahoo",
                "url": "https://finance.yahoo.com/quote/AAPL/news/",
                "published_at": "2026-10-12T19:17:29Z",
            },
        ],
        summary=(
            "The chart shows a steady climb followed by a sideways pause just below the recent "
            "highs. Buyers are still in control, but the price needs to break above resistance "
            "near 380 to confirm the next leg up. A drop below the 329 area would weaken the "
            "picture."
        ),
        insights={
            "trend": _insight(
                "bullish", "A series of higher highs and higher lows across the visible period."
            ),
            "volatility": _insight(
                "medium", "Wide candles during the rally, narrower ones in the recent range."
            ),
            "market_sentiment": _insight(
                "bullish", "The uptrend, a Buy consensus among analysts and upbeat news agree."
            ),
        },
        signals={
            "position": "long",
            "start_level": 340.97,
            "safety_stop": 325.0,
            "projected_exit_1": 380.0,
            "projected_exit_2": 400.0,
            "support_level": 328.62,
            "resistance_level": 380.09,
            "rationale": (
                "The trend favours buyers; the stop sits below visible support and the targets "
                "are the resistance and a possible breakout above it."
            ),
        },
    ),
    _demo(
        analysis_id=BTC_ID,
        sample="btcusdt.png",
        language="es",
        age=timedelta(days=1, hours=3),
        tokens=6912,
        printed_symbol="BTCUSDT",
        asset={"symbol": "BTC", "name": "Bitcoin", "type": "crypto", "logo_url": None},
        price={
            "price": 83774.71,
            "currency": "USD",
            "change": -455.29,
            "change_percent": -0.5405,
            "as_of": "2026-10-12T08:40:00Z",
        },
        review=None,  # nobody publishes analyst ratings for cryptocurrencies
        news=[
            {
                "id": "8613311",
                "headline": "Bitcoin cae por debajo de 84.000 dólares; baja el apetito de riesgo",
                "original_headline": "Bitcoin slips below $84,000 as risk appetite cools",
                "sentiment": "negative",
                "source": "Cointelegraph",
                "url": "https://cointelegraph.com/tags/bitcoin",
                "published_at": "2026-10-12T07:55:10Z",
            }
        ],
        summary=(
            "El gráfico muestra una tendencia bajista: cada rebote termina en un máximo más "
            "bajo que el anterior. El precio se acerca a la zona de soporte de 82.000; si la "
            "pierde, la caída podría continuar. Solo una recuperación por encima de 88.000 "
            "cambiaría el panorama."
        ),
        insights={
            "trend": _insight(
                "bearish", "Máximos y mínimos cada vez más bajos en todo el periodo."
            ),
            "volatility": _insight(
                "high", "Las velas son amplias en relación con el precio y hay giros bruscos."
            ),
            "market_sentiment": _insight(
                "bearish", "La presión vendedora domina y las noticias recientes son negativas."
            ),
        },
        signals={
            "position": "short",
            "start_level": 83700.0,
            "safety_stop": 88000.0,
            "projected_exit_1": 80500.0,
            "projected_exit_2": 77000.0,
            "support_level": 82000.0,
            "resistance_level": 88000.0,
            "rationale": (
                "La tendencia favorece a los vendedores; el stop queda por encima de la "
                "resistencia y los objetivos están bajo el soporte actual."
            ),
        },
    ),
    _demo(
        analysis_id=TSLA_ID,
        sample="tsla.png",
        language="de",
        age=timedelta(days=3, hours=6),
        tokens=7120,
        printed_symbol="TSLA",
        asset={
            "symbol": "TSLA",
            "name": "Tesla Inc",
            "type": "stock",
            "logo_url": "https://static2.finnhub.io/file/publicdatany/finnhubimage/stock_logo/TSLA.png",
        },
        price={
            "price": 412.35,
            "currency": "USD",
            "change": -2.1,
            "change_percent": -0.5067,
            "as_of": "2026-10-09T20:00:00Z",
        },
        review=_review("hold", 8, 14, 21, 9, 4),
        news=[
            {
                "id": "142790417",
                "headline": "Tesla liefert im dritten Quartal mehr Fahrzeuge aus als erwartet",
                "original_headline": "Tesla Q3 deliveries top expectations",
                "sentiment": "positive",
                "source": "MarketWatch",
                "url": "https://www.marketwatch.com/investing/stock/tsla",
                "published_at": "2026-10-09T13:05:00Z",
            },
            {
                "id": "142790455",
                "headline": "Analysten uneins über die Bewertung von Tesla",
                "original_headline": "Analysts split on Tesla valuation",
                "sentiment": "neutral",
                "source": "Yahoo",
                "url": "https://finance.yahoo.com/quote/TSLA/news/",
                "published_at": "2026-10-08T16:40:00Z",
            },
        ],
        summary=(
            "Der Chart zeigt eine Seitwärtsbewegung mit starken Ausschlägen in beide "
            "Richtungen. Eine klare Richtung ist nicht erkennbar; der Kurs pendelt zwischen "
            "Unterstützung bei 395 und Widerstand bei 440. Erst ein Ausbruch aus dieser Spanne "
            "würde ein neues Signal liefern."
        ),
        insights={
            "trend": _insight("sideways", "Der Kurs bewegt sich in einer Spanne ohne neue Hochs."),
            "volatility": _insight("high", "Große Kerzen und häufige Richtungswechsel."),
            "market_sentiment": _insight(
                "neutral", "Analysten raten mehrheitlich zum Halten, die Nachrichten sind gemischt."
            ),
        },
        signals={
            "position": "long",
            "start_level": 412.0,
            "safety_stop": 392.0,
            "projected_exit_1": 438.0,
            "projected_exit_2": 455.0,
            "support_level": 395.0,
            "resistance_level": 440.0,
            "rationale": (
                "Der Kurs liegt näher an der Unterstützung als am Widerstand; das Signal ist "
                "schwach, solange die Spanne hält."
            ),
        },
    ),
    {
        "id": FAILED_ID,
        "user_id": "demo-user",
        "sample": "not_a_chart.png",
        "language": "en",
        "status": "failed",
        "age": timedelta(hours=5),
        "tokens_used": 1629,
        "error_code": "unclear_chart",
        "recognition": {
            "is_chart": False,
            "symbol": None,
            "asset_name": None,
            "asset_type": None,
        },
        "market_snapshot": None,
        "result": None,
    },
    {
        **_demo(
            analysis_id=QUOTA_ID,
            sample="aapl.png",
            language="en",
            age=timedelta(0),
            tokens=495_000,
            printed_symbol="AAPL",
            asset={"symbol": "AAPL", "name": "Apple Inc", "type": "stock", "logo_url": None},
            price={
                "price": 340.97,
                "currency": "USD",
                "change": 4.33,
                "change_percent": 1.2862,
                "as_of": "2026-10-12T20:00:00Z",
            },
            review=_review("buy", 12, 22, 15, 3, 1),
            news=[],
            summary=(
                "Demo analysis that carries most of today's token budget, so the next upload "
                "of this user is rejected by the quota."
            ),
            insights={
                "trend": _insight("bullish", "Higher highs and higher lows."),
                "volatility": _insight("medium", "Moderate candle ranges."),
                "market_sentiment": _insight("bullish", "The trend and the analysts agree."),
            },
            signals={
                "position": "long",
                "start_level": 340.97,
                "safety_stop": 325.0,
                "projected_exit_1": 380.0,
                "projected_exit_2": 400.0,
                "support_level": 328.62,
                "resistance_level": 380.09,
                "rationale": "Demo values.",
            },
        ),
        "user_id": "demo-user-no-quota",
    },
]


def _upload_dir() -> Path:
    return Path(os.environ.get("UPLOAD_DIR", "uploads"))


def _jsonb(value: dict[str, Any] | None) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def upgrade() -> None:
    analyses = sa.table(
        "analyses",
        sa.column("id", postgresql.UUID),
        sa.column("user_id", sa.String),
        sa.column("language", sa.String),
        sa.column("status", sa.String),
        sa.column("stage", sa.String),
        sa.column("image_key", sa.Text),
        sa.column("image_content_type", sa.String),
        sa.column("recognition", postgresql.JSONB),
        sa.column("market_snapshot", postgresql.JSONB),
        sa.column("result", postgresql.JSONB),
        sa.column("error_code", sa.String),
        sa.column("tokens_used", sa.Integer),
        sa.column("attempts", sa.Integer),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("completed_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(UTC)
    upload_dir = _upload_dir()
    upload_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for demo in DEMO_ANALYSES:
        image_key = f"{demo['id']}.png"
        source = SAMPLES_DIR / demo["sample"]
        if source.exists():
            shutil.copyfile(source, upload_dir / image_key)
        completed_at = now - demo["age"]
        created_at = completed_at - timedelta(seconds=20)
        rows.append(
            {
                "id": demo["id"],
                "user_id": demo["user_id"],
                "language": demo["language"],
                "status": demo["status"],
                "stage": "done",
                "image_key": image_key,
                "image_content_type": "image/png",
                "recognition": demo["recognition"],
                "market_snapshot": demo["market_snapshot"],
                "result": demo["result"],
                "error_code": demo["error_code"],
                "tokens_used": demo["tokens_used"],
                "attempts": 1,
                "created_at": created_at,
                "updated_at": completed_at,
                "completed_at": completed_at,
            }
        )
    op.bulk_insert(analyses, rows)


def downgrade() -> None:
    ids = [demo["id"] for demo in DEMO_ANALYSES]
    op.execute(
        sa.text("DELETE FROM analyses WHERE id = ANY(CAST(:ids AS uuid[]))").bindparams(ids=ids)
    )
    for analysis_id in ids:
        (_upload_dir() / f"{analysis_id}.png").unlink(missing_ok=True)
