"""Generate the sample chart images in ``samples/``.

They are synthetic candlestick charts with a clearly visible ticker, used as demo uploads
and by the seed migration. The output is deterministic, so re-running changes nothing.

    uv run python scripts/make_sample_charts.py
"""

import random
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "samples"
WIDTH, HEIGHT = 1200, 720
PLOT = (70, 90, 1090, 640)  # left, top, right, bottom
BG, GRID, TEXT, MUTED = "#131722", "#232838", "#d1d4dc", "#787b86"
UP, DOWN = "#26a69a", "#ef5350"


@dataclass(frozen=True)
class Chart:
    filename: str
    title: str
    subtitle: str
    start: float
    drift: float  # average move per candle, as a fraction of price
    volatility: float
    seed: int


CHARTS = [
    Chart("aapl.png", "AAPL", "Apple Inc · 1D · NASDAQ", 300.0, 0.0022, 0.012, 11),
    Chart(
        "btcusdt.png", "BTCUSDT", "Bitcoin / TetherUS · 4h · BINANCE", 91000.0, -0.0012, 0.018, 23
    ),
    Chart("tsla.png", "TSLA", "Tesla Inc · 1D · NASDAQ", 420.0, 0.0002, 0.024, 37),
]


def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in (
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "arialbd.ttf" if bold else "arial.ttf",
    ):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def candles(chart: Chart, count: int = 70) -> list[tuple[float, float, float, float]]:
    rng = random.Random(chart.seed)
    price, series = chart.start, []
    for _ in range(count):
        open_ = price
        close = open_ * (1 + rng.gauss(chart.drift, chart.volatility))
        high = max(open_, close) * (1 + abs(rng.gauss(0, chart.volatility / 2)))
        low = min(open_, close) * (1 - abs(rng.gauss(0, chart.volatility / 2)))
        series.append((open_, high, low, close))
        price = close
    return series


def draw_chart(chart: Chart) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = PLOT
    series = candles(chart)
    lowest = min(low for _, _, low, _ in series)
    highest = max(high for _, high, _, _ in series)
    padding = (highest - lowest) * 0.08
    lowest, highest = lowest - padding, highest + padding

    def y(price: float) -> float:
        return bottom - (price - lowest) / (highest - lowest) * (bottom - top)

    draw.text((left, 22), chart.title, font=font(34, bold=True), fill=TEXT)
    title_width = draw.textlength(chart.title, font=font(34, bold=True))
    draw.text((left + title_width + 16, 36), chart.subtitle, font=font(18), fill=MUTED)

    for step in range(6):
        price = lowest + (highest - lowest) * step / 5
        draw.line([(left, y(price)), (right, y(price))], fill=GRID, width=1)
        label = f"{price:,.0f}" if price >= 1000 else f"{price:,.2f}"
        draw.text((right + 12, y(price) - 9), label, font=font(15), fill=MUTED)

    slot = (right - left) / len(series)
    body = slot * 0.62
    for index, (open_, high, low, close) in enumerate(series):
        x = left + slot * (index + 0.5)
        color = UP if close >= open_ else DOWN
        draw.line([(x, y(high)), (x, y(low))], fill=color, width=2)
        body_top, body_bottom = sorted((y(open_), y(close)))
        draw.rectangle(
            [x - body / 2, body_top, x + body / 2, max(body_bottom, body_top + 1)], fill=color
        )

    last = series[-1][3]
    draw.line([(left, y(last)), (right, y(last))], fill=MUTED, width=1)
    label = f"{last:,.0f}" if last >= 1000 else f"{last:,.2f}"
    draw.rectangle([right + 6, y(last) - 12, right + 104, y(last) + 12], fill=MUTED)
    draw.text((right + 12, y(last) - 9), label, font=font(15, bold=True), fill=BG)
    return image


def draw_not_a_chart() -> Image.Image:
    """A photo-like picture with no chart on it: the 'please upload a clear chart' case."""
    image = Image.new("RGB", (WIDTH, HEIGHT), "#87b5d9")
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 470, WIDTH, HEIGHT], fill="#5d8c4a")
    draw.ellipse([930, 70, 1070, 210], fill="#ffe17a")
    draw.polygon([(120, 470), (420, 180), (720, 470)], fill="#6f7a86")
    draw.polygon([(520, 470), (800, 250), (1080, 470)], fill="#7f8a96")
    draw.polygon(
        [(360, 240), (420, 180), (480, 240), (440, 260), (410, 235), (390, 262)], fill="#f2f5f7"
    )
    return image


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for chart in CHARTS:
        draw_chart(chart).save(OUT / chart.filename, optimize=True)
    draw_not_a_chart().save(OUT / "not_a_chart.png", optimize=True)
    for path in sorted(OUT.glob("*.png")):
        print(f"{path.name}: {path.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
