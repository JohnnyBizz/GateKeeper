"""The drawn shapes.

Nothing here needs a display or a toolkit: the module returns plain images, so
what it draws can be looked at directly. Assertions are on pixels — that a
gauge fills further for a higher score, that a rising candle is green and a
falling one red, that a rounded corner is actually transparent — because a
shape that renders without raising is not the same as a shape that is right.
"""

from __future__ import annotations

import pytest

from poa.overlay import graphics as g


def _alpha(image, x, y):
    return image.getpixel((x, y))[3]


def _pixels(image):
    """Every pixel, read through ``load`` — ``getdata`` is on its way out."""
    grid = image.load()
    width, height = image.size
    return [grid[x, y] for y in range(height) for x in range(width)]


def _lit(image) -> int:
    """How many pixels carry any colour at all."""
    return sum(1 for pixel in _pixels(image) if pixel[3] > 24)


class TestColourHelpers:
    def test_a_blend_ends_where_it_should(self):
        assert g.mix("#000000", "#ffffff", 0.0) == "#000000"
        assert g.mix("#000000", "#ffffff", 1.0) == "#ffffff"

    def test_a_half_blend_is_halfway(self):
        assert g.mix("#000000", "#ffffff", 0.5) == "#808080"

    def test_a_blend_cannot_overshoot(self):
        assert g.mix("#000000", "#ffffff", 4.0) == "#ffffff"
        assert g.mix("#000000", "#ffffff", -2.0) == "#000000"

    def test_alpha_is_read_when_it_is_given(self):
        assert g._hex("#11223344") == (0x11, 0x22, 0x33, 0x44)
        assert g._hex("#112233") == (0x11, 0x22, 0x33, 255)


class TestCards:
    def test_a_card_is_the_size_asked_for(self):
        assert g.card(120, 40, glow=None).size == (120, 40)

    def test_a_glowing_card_reserves_room_for_its_halo(self):
        """The glow bleeds past the edge, so the image has to be larger than
        the card or the light would be clipped into a square."""
        plain = g.card(120, 40)
        lit = g.card(120, 40, glow="#22c55e", glow_strength=1.0)
        assert lit.size[0] > plain.size[0] and lit.size[1] > plain.size[1]

    def test_the_corners_are_actually_round(self):
        card = g.card(80, 40, radius=14)
        assert _alpha(card, 0, 0) < 40          # cut away
        assert _alpha(card, 40, 20) > 200       # solid in the middle

    def test_a_gradient_is_not_a_flat_fill(self):
        card = g.card(60, 60, radius=0, fill="#000000", fill_to="#ffffff")
        top = card.getpixel((30, 2))[0]
        bottom = card.getpixel((30, 57))[0]
        assert bottom - top > 100

    def test_a_stronger_glow_carries_further(self):
        faint = g.card(80, 40, glow="#22c55e", glow_strength=0.15)
        bright = g.card(80, 40, glow="#22c55e", glow_strength=1.0)
        assert _lit(bright) > _lit(faint)


class TestTheScoreDial:
    def test_a_higher_score_fills_more_of_the_arc(self):
        low = _lit(g.arc_gauge(60, 10, track="#00000000"))
        high = _lit(g.arc_gauge(60, 95, track="#00000000"))
        assert high > low * 2

    def test_no_score_draws_only_the_track(self):
        blank = g.arc_gauge(60, None, color="#22c55e", track="#00000000")
        assert _lit(blank) == 0

    def test_the_scale_is_clamped_rather_than_wrapped(self):
        """A score above a hundred must not wrap the dial back to the start."""
        full = _lit(g.arc_gauge(60, 100, track="#00000000"))
        over = _lit(g.arc_gauge(60, 400, track="#00000000"))
        assert over == pytest.approx(full, rel=0.02)

    def test_it_is_square(self):
        assert g.arc_gauge(48, 50).size == (48, 48)


class TestTheCountdownRing:
    def test_a_full_ring_is_a_bar_that_has_just_opened(self):
        assert _lit(g.countdown_ring(40, 1.0, track="#00000000")) > _lit(
            g.countdown_ring(40, 0.1, track="#00000000")
        )

    def test_an_empty_ring_draws_no_arc(self):
        assert _lit(g.countdown_ring(40, 0.0, track="#00000000")) == 0


class TestTheSparkline:
    def test_it_needs_two_points_to_draw_a_line(self):
        assert _lit(g.sparkline(80, 30, [1.0])) == 0
        assert _lit(g.sparkline(80, 30, [1.0, 2.0])) > 0

    def test_a_flat_series_does_not_divide_by_zero(self):
        """A quiet market is a real case, and the line still has to sit
        somewhere in the box rather than raise."""
        image = g.sparkline(80, 30, [1.1] * 20)
        assert _lit(image) > 0

    def test_the_fill_adds_ink_below_the_line(self):
        closes = [1.0 + i * 0.01 for i in range(20)]
        assert _lit(g.sparkline(80, 30, closes, fill=True)) > _lit(
            g.sparkline(80, 30, closes, fill=False)
        )


class TestTheCandles:
    def _bars(self, rising: bool):
        bars = []
        price = 1.10
        for _ in range(10):
            step = 0.001 if rising else -0.001
            bars.append(g.Bar(price, max(price, price + step) + 0.0002,
                              min(price, price + step) - 0.0002, price + step))
            price += step
        return bars

    def _dominant(self, image):
        counts: dict[tuple[int, int, int], int] = {}
        for pixel in _pixels(image):
            if pixel[3] < 100:
                continue
            counts[pixel[:3]] = counts.get(pixel[:3], 0) + 1
        return max(counts, key=counts.get) if counts else None

    def test_a_rising_series_is_drawn_in_the_rising_colour(self):
        image = g.candles(120, 60, self._bars(True), up="#00ff00", down="#ff0000")
        red, green, _ = self._dominant(image)
        assert green > red

    def test_a_falling_series_is_drawn_in_the_falling_colour(self):
        image = g.candles(120, 60, self._bars(False), up="#00ff00", down="#ff0000")
        red, green, _ = self._dominant(image)
        assert red > green

    def test_no_candles_is_an_empty_image_rather_than_an_error(self):
        assert _lit(g.candles(120, 60, [])) == 0

    def test_only_the_most_recent_window_is_drawn(self):
        """A hundred candles across three hundred pixels would be narrower
        than the wicks."""
        many = self._bars(True) * 20
        image = g.candles(120, 60, many, max_bars=8)
        assert _lit(image) < _lit(g.candles(120, 60, many, max_bars=40))

    def test_a_doji_still_has_a_visible_body(self):
        """Open equal to close is a real shape and one the engine names, so it
        cannot round away to nothing."""
        flat = [g.Bar(1.1, 1.1005, 1.0995, 1.1) for _ in range(6)]
        assert _lit(g.candles(120, 60, flat)) > 0


class TestSmallOrnaments:
    def test_a_meter_fills_in_proportion(self):
        assert _lit(g.bar_meter(100, 8, 0.9, track="#00000000")) > _lit(
            g.bar_meter(100, 8, 0.2, track="#00000000")
        )

    def test_a_meter_is_clamped(self):
        assert g.bar_meter(100, 8, 5.0).size == (100, 8)
        assert g.bar_meter(100, 8, -3.0).size == (100, 8)

    def test_the_direction_glyphs_differ_from_each_other(self):
        shapes = {
            name: tuple(_pixels(g.direction_glyph(24, name, "#ffffff")))
            for name in ("CALL", "PUT", "WAIT", "NO_TRADE")
        }
        assert len(set(shapes.values())) == 4

    def test_the_shimmer_travels(self):
        early = g.shimmer(80, 6, 0.1)
        late = g.shimmer(80, 6, 0.8)
        assert _pixels(early) != _pixels(late)

    def test_the_shimmer_stays_inside_its_box(self):
        assert g.shimmer(80, 6, 0.5).size == (80, 6)
