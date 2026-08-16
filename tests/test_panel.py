"""The one layer that draws.

Everything about *what* to show is decided in ``viewmodel.py`` and tested
there without a display. What is left here is where things are placed, and
that turned out not to be free of decisions: a row that was hidden before its
first contents arrived came back at the bottom of the panel instead of where
it was built, because packing a widget again puts it last in its parent's
order. No amount of view-model testing catches that.

Tkinter is not installed on the CI runner, so the widget layer is stubbed
rather than drawn. The stub records the calls instead of painting them, and
models the one Tk behaviour the bug turned on — pack order.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest


class _Widget:
    """Enough of a Tk widget to build the panel and inspect where things went."""

    def __init__(self, parent=None, **kw):
        self.parent = parent
        self.kw = dict(kw)
        self.children: list["_Widget"] = []
        self.packed: list["_Widget"] = []
        self.pack_info_: dict | None = None
        self.grid_info_: dict | None = None
        self.cols: dict[int, dict] = {}
        self.binds: dict[str, object] = {}
        if parent is not None:
            parent.children.append(self)

    # -- geometry: pack order is the part that matters ---------------------

    def pack(self, **kw):
        self.pack_info_ = kw
        if self.parent is not None:
            if self in self.parent.packed:
                self.parent.packed.remove(self)
            # Re-packing appends. This is the Tk behaviour the bug turned on.
            self.parent.packed.append(self)

    def pack_configure(self, **kw):
        if self.pack_info_ is None:
            self.pack(**kw)
            return
        self.pack_info_.update(kw)  # already packed: keeps its place

    def pack_forget(self):
        self.pack_info_ = None
        if self.parent is not None and self in self.parent.packed:
            self.parent.packed.remove(self)

    def grid(self, **kw):
        self.grid_info_ = kw

    def columnconfigure(self, index, **kw):
        self.cols[index] = kw

    # -- everything else is recorded and ignored ---------------------------

    def configure(self, **kw):
        self.kw.update(kw)

    config = configure

    def cget(self, key):
        return self.kw.get(key)

    def bind(self, sequence, fn):
        self.binds[sequence] = fn

    def destroy(self):
        if self.parent is not None and self in self.parent.children:
            self.parent.children.remove(self)

    def winfo_reqheight(self):
        return 700

    def winfo_screenheight(self):
        return 1080

    def winfo_width(self):
        return 300

    def winfo_exists(self):
        return 1

    def focus_displayof(self):
        return None

    def focus_set(self):
        pass

    def update_idletasks(self):
        pass

    def geometry(self, *a):
        pass

    def title(self, *a):
        pass

    def attributes(self, *a):
        pass

    def wm_attributes(self, *a):
        pass

    def overrideredirect(self, *a):
        pass

    def protocol(self, *a):
        pass

    def resizable(self, *a):
        pass

    def after(self, *a):
        return "after#1"

    def after_cancel(self, *a):
        pass

    def create_rectangle(self, *a, **k):
        return 1

    def delete(self, *a):
        pass

    def insert(self, *a):
        pass

    def get(self):
        return ""

    def selection_range(self, *a):
        pass


@pytest.fixture
def panel_module(monkeypatch):
    """The panel module, built against a stub Tk."""
    tk = types.ModuleType("tkinter")
    for name in (
        "Tk", "Toplevel", "Frame", "Label", "Button", "Canvas", "Entry", "Text",
    ):
        setattr(tk, name, type(name, (_Widget,), {}))
    tk.TclError = type("TclError", (Exception,), {})
    tk.LEFT = "left"

    tkfont = types.ModuleType("tkinter.font")
    tkfont.Font = lambda **kw: kw
    tkfont.families = lambda *a, **k: ("Segoe UI", "Consolas")
    tkfont.nametofont = lambda *a, **k: {}
    tk.font = tkfont

    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "tkinter.font", tkfont)
    module = importlib.reload(importlib.import_module("poa.overlay.panel"))
    yield module
    sys.modules.pop("poa.overlay.panel", None)


def _panel(panel_module, **kw):
    from poa.overlay.viewmodel import OverlayViewModel

    return panel_module.OverlayPanel(OverlayViewModel(), **kw)


def _row(asset="EUR/USD OTC", score=60.0, actionable=False, active=False):
    return {
        "asset": asset,
        "label": asset.replace(" OTC", ""),
        "score": score,
        "direction": "CALL" if actionable else "WAIT",
        "actionable": actionable,
        "color": "#22c55e" if actionable else "#64748b",
        "active": active,
    }


class TestTheWatchlistStaysWhereItWasBuilt:
    """It belongs under the tiles, and it has to still be there on arrival.

    The row is empty until the first sweep finishes, which is always — no
    session starts with it populated. Hiding it with ``pack_forget`` and
    packing it again when the verdicts arrived moved it to the end of the
    panel, below the risk block and the disclaimer.
    """

    def test_it_keeps_its_place_after_being_hidden_and_shown(self, panel_module):
        panel = _panel(panel_module)
        body = panel._body
        watch = panel._widgets["watchlist"]
        built = body.packed.index(watch)

        panel._render_watchlist([])  # no sweep has run yet
        panel._render_watchlist([_row()])  # the first verdicts arrive

        assert body.packed.index(watch) == built

    def test_it_is_above_the_verdict_box(self, panel_module):
        """Under the tiles, where the pair it belongs to is named."""
        panel = _panel(panel_module)
        body = panel._body
        watch = body.packed.index(panel._widgets["watchlist"])
        verdict = body.packed.index(panel._widgets["verdict_box"])
        assert watch < verdict

    def test_an_empty_row_takes_no_space(self, panel_module):
        panel = _panel(panel_module)
        panel._render_watchlist([_row()])
        panel._render_watchlist([])
        frame = panel._widgets["watchlist"]
        assert frame.children == []
        assert frame.pack_info_["pady"] == 0


class TestTheWatchlistTabs:
    def test_they_lay_out_two_to_a_row(self, panel_module):
        """Eight side by side would run off a 320px panel."""
        panel = _panel(panel_module)
        panel._render_watchlist([_row(f"PAIR{i} OTC") for i in range(8)])
        places = [
            (tab.grid_info_["row"], tab.grid_info_["column"])
            for tab in panel._watch_tabs
        ]
        assert places == [(0, 0), (0, 1), (1, 0), (1, 1),
                          (2, 0), (2, 1), (3, 0), (3, 1)]
        assert panel._widgets["watchlist"].cols[0]["weight"] == 1
        assert panel._widgets["watchlist"].cols[1]["weight"] == 1

    def test_a_click_sends_the_full_pair_name(self, panel_module):
        """Not the shortened label the tab shows."""
        picked = []
        panel = _panel(panel_module, on_asset=picked.append)
        panel._render_watchlist([_row("GBP/USD OTC"), _row("USD/JPY OTC")])
        panel._watch_tabs[1].binds["<Button-1>"](None)
        assert picked == ["USD/JPY OTC"]

    def test_only_a_chart_with_a_setup_is_coloured(self, panel_module):
        panel = _panel(panel_module)
        panel._render_watchlist(
            [_row("EUR/USD OTC", 82.0, actionable=True), _row("USD/JPY OTC", 44.0)]
        )
        assert panel._watch_tabs[0].cget("fg") == "#22c55e"
        assert panel._watch_tabs[1].cget("fg") == "#64748b"

    def test_the_open_chart_is_marked(self, panel_module):
        panel = _panel(panel_module)
        panel._render_watchlist(
            [_row("EUR/USD OTC"), _row("GBP/USD OTC", active=True)]
        )
        backgrounds = [tab.cget("bg") for tab in panel._watch_tabs]
        assert backgrounds[0] != backgrounds[1]

    def test_tabs_are_not_rebuilt_when_only_the_scores_move(self, panel_module):
        """Re-creating them on every repaint makes the row flicker and eats
        the click that was already on its way."""
        panel = _panel(panel_module)
        panel._render_watchlist([_row("EUR/USD OTC", 40.0)])
        before = panel._watch_tabs[0]
        panel._render_watchlist([_row("EUR/USD OTC", 90.0, actionable=True)])
        assert panel._watch_tabs[0] is before
        assert panel._watch_tabs[0].cget("text") == "EUR/USD 90"


def _trend(label, arrow, color, views, detail=""):
    return {
        "label": label, "arrow": arrow, "color": color, "views": views,
        "strength": 70.0, "agreement": 100.0, "detail": detail, "blanked": False,
    }


def _view(name, arrow, color):
    return {"name": name, "arrow": arrow, "color": color, "bias": "X"}


class TestTheMarketDirectionIsVisible:
    """WAIT is most of what the panel says, and on its own it says nothing
    about which way the market is going — which is the thing you can see
    plainly on the chart and had to read a paragraph of risk prose to find."""

    def test_it_sits_inside_the_signal_box(self, panel_module):
        panel = _panel(panel_module)
        assert panel._widgets["trend_strip"].parent is panel._widgets["verdict_box"]

    def test_falling_reads_falling_and_is_red(self, panel_module):
        panel = _panel(panel_module)
        panel._render_trend(
            _trend("FALLING", "▼", "#ef4444",
                   [_view("HIGH", "▼", "#ef4444"), _view("NOW", "▼", "#ef4444")],
                   "100% of the timeframes agree")
        )
        w = panel._widgets
        assert w["trend_label"].cget("text") == "FALLING"
        assert w["trend_arrow"].cget("text") == "▼"
        assert w["trend_label"].cget("fg") == "#ef4444"
        assert [v.cget("text") for v in panel._trend_views] == ["HIGH ▼", "NOW ▼"]

    def test_it_never_says_buy_or_sell(self, panel_module):
        """Those words belong to the verdict box.

        A trend word that reads like an instruction is how "the market is
        rising" becomes "buy" — the one thing this tool is built not to say.
        """
        from poa.models import Bias
        from poa.overlay.viewmodel import _bias_label

        words = {_bias_label(b) for b in Bias}
        assert words == {"RISING", "FALLING", "SIDEWAYS"}

    def test_the_arrows_change_without_rebuilding_the_labels(self, panel_module):
        panel = _panel(panel_module)
        two = [_view("HIGH", "▼", "#ef4444"), _view("NOW", "▼", "#ef4444")]
        panel._render_trend(_trend("FALLING", "▼", "#ef4444", two))
        first = panel._trend_views[0]
        flipped = [_view("HIGH", "▲", "#22c55e"), _view("NOW", "▼", "#ef4444")]
        panel._render_trend(_trend("SIDEWAYS", "▬", "#eab308", flipped))
        assert panel._trend_views[0] is first
        assert panel._trend_views[0].cget("text") == "HIGH ▲"

    def test_a_blank_trend_renders(self, panel_module):
        """Before the first read there is nothing to say, and saying nothing
        must not crash the repaint."""
        panel = _panel(panel_module)
        panel._render_trend(
            {"label": "--", "arrow": "", "color": "#64748b", "views": [],
             "strength": None, "agreement": None, "detail": "", "blanked": True}
        )
        assert panel._widgets["trend_label"].cget("text") == "--"
        assert panel._trend_views == []


class TestThePairIsNotCutOff:
    """The one field naming what is being analysed was the one being cut.

    Three equal tiles across a 320px panel left the pair nine characters
    wide. "GBP/USD OTC" is eleven, so it rendered as "GBP/USD O" — on the
    field whose entire job is saying which market this is.
    """

    def test_the_field_holds_the_longest_ordinary_pair_name(self, panel_module):
        panel = _panel(panel_module)
        longest = "GBP/USD OTC"
        assert panel._widgets["tile_pair"].kw["width"] >= len(longest)

    def test_the_pair_has_a_row_to_itself(self, panel_module):
        """Sharing one with the payout and the expiry is what squeezed it."""
        panel = _panel(panel_module)
        pair_row = panel._widgets["tile_pair"].parent
        siblings = {type(child).__name__ for child in pair_row.children}
        # A caption, the field and the price — no other tiles competing.
        assert len(pair_row.children) == 3
        assert "Entry" in siblings

    def test_the_expiry_and_the_chart_are_named_apart(self, panel_module):
        """Confusing the two is the most consequential mistake available.

        A 5-minute chart read as 1-minute would suggest expirations five times
        too short, so they are never given the same word.
        """
        panel = _panel(panel_module)
        panel.refresh()
        captions = [
            child.cget("text")
            for tile in (panel._widgets["tile_time"].parent,
                         panel._widgets["tile_chart"].parent)
            for child in tile.children
            if child.cget("text") in ("EXPIRY", "CHART")
        ]
        assert sorted(captions) == ["CHART", "EXPIRY"]


class TestTheVerdictStaysAtTheTopOfItsBox:
    """It is swapped for the scanning dots and swapped back, and packing a
    widget that was hidden puts it at the *end* of its parent — so the word
    the whole panel exists to print ended up underneath the market strip as
    soon as the first scan finished."""

    def test_it_survives_a_scan(self, panel_module):
        panel = _panel(panel_module)
        box = panel._widgets["verdict_box"]
        before = box.packed.index(panel._widgets["verdict_slot"])

        panel.refresh()
        panel.vm.scan.begin()
        panel.refresh()  # scanning: the dots take its place
        panel.vm.scan.reset()
        panel.refresh()  # and it comes back

        assert box.packed.index(panel._widgets["verdict_slot"]) == before
        assert before < box.packed.index(panel._widgets["trend_strip"])

    def test_the_dots_appear_where_the_verdict_was(self, panel_module):
        """Not at the bottom of the box, which is where packing sends them."""
        panel = _panel(panel_module)
        panel.vm.scan.begin()
        panel.refresh()
        assert panel._widgets["dots_frame"].parent is panel._widgets["verdict_slot"]
        assert panel._widgets["verdict"].pack_info_ is None


def _entry(text="TAKE IT NOW", clock="0:23", progress=0.6, urgent=False, ready=True):
    return {
        "text": text, "detail": "3 MIN expiry — 23s left on this candle",
        "clock": clock, "seconds": 23, "progress": progress,
        "urgent": urgent, "ready": ready,
    }


class TestTheEntryCountdownIsInTheSignalBox:
    """The verdict and its timing are one decision.

    A BUY with no indication of how much of the candle is left is an
    instruction with the timing filed somewhere else, and the panel had no
    somewhere else — it simply never said when.
    """

    def test_it_sits_inside_the_signal_box(self, panel_module):
        panel = _panel(panel_module)
        assert panel._widgets["entry_wrap"].parent is panel._widgets["verdict_box"]

    def test_it_is_above_the_market_direction_strip(self, panel_module):
        """What to do, then when, then which way the market is going."""
        panel = _panel(panel_module)
        box = panel._widgets["verdict_box"]
        assert box.packed.index(panel._widgets["entry_wrap"]) < box.packed.index(
            panel._widgets["trend_strip"]
        )

    def test_the_clock_and_the_call_both_show(self, panel_module):
        panel = _panel(panel_module)
        panel._render_entry(_entry(), "#22c55e")
        assert panel._widgets["entry_text"].cget("text") == "TAKE IT NOW"
        assert panel._widgets["entry_clock"].cget("text") == "0:23"
        assert panel._widgets["entry_text"].cget("fg") == "#22c55e"

    def test_nothing_to_act_on_is_greyed_rather_than_coloured(self, panel_module):
        """Colour is how the panel says "this one" — it cannot also mean "not
        this one" or it stops meaning anything."""
        panel = _panel(panel_module)
        panel._render_entry(_entry("WAIT FOR NEXT CANDLE", ready=False), "#22c55e")
        from poa.overlay.viewmodel import COLORS

        assert panel._widgets["entry_text"].cget("fg") == COLORS["faint"]

    def test_the_last_seconds_of_a_bar_pulse(self, panel_module):
        """The one thing on this panel worth animating: a number counting
        down in a corner is easy to miss, a breathing one is not."""
        panel = _panel(panel_module)
        tones = set()
        for _ in range(panel.PULSE_FRAMES):
            panel._pulse += 1
            panel._render_entry(_entry(urgent=True), "#22c55e")
            tones.add(panel._widgets["entry_text"].cget("fg"))
        assert len(tones) > 4

    def test_a_setup_that_is_not_urgent_holds_still(self, panel_module):
        panel = _panel(panel_module)
        tones = set()
        for _ in range(panel.PULSE_FRAMES):
            panel._pulse += 1
            panel._render_entry(_entry(), "#22c55e")
            tones.add(panel._widgets["entry_text"].cget("fg"))
        assert tones == {"#22c55e"}

    def test_the_bar_drains_rather_than_fills(self, panel_module):
        """It shows what is left, not what is gone — the countdown is the
        point, and a bar that grows as time runs out reads backwards."""
        panel = _panel(panel_module)
        drawn = []
        panel._widgets["entry_bar"].create_rectangle = (
            lambda *a, **k: drawn.append(a[2])
        )
        panel._draw_entry_bar(0.25, "#22c55e")
        panel._draw_entry_bar(0.75, "#22c55e")
        assert drawn[0] > drawn[1]


class TestTheEvidenceBlockFolds:
    """Every line under the decision earned its place one at a time, and
    together they turned a panel meant to be glanced at into a page."""

    def test_the_detail_lines_live_in_the_folding_body(self, panel_module):
        panel = _panel(panel_module)
        body = panel._widgets["details_body"]
        for key in (
            "calibration", "duration_score", "recommended", "session_edge",
            "session_calls", "session_taught", "proof", "lesson", "tuning",
        ):
            widget = panel._widgets[key]
            assert body in (widget.parent, widget.parent.parent), key

    def test_the_decision_itself_never_folds(self, panel_module):
        """The verdict, the countdown, the score and the brake stay out."""
        panel = _panel(panel_module)
        body = panel._widgets["details_body"]
        for key in (
            "verdict", "entry_text", "score", "badge", "take_now",
            "session_win", "session_loss", "paused",
        ):
            node, parents = panel._widgets[key], set()
            while node is not None:
                parents.add(id(node))
                node = node.parent
            assert id(body) not in parents, key

    def test_it_is_folded_after_a_repaint(self, panel_module):
        panel = _panel(panel_module)
        panel.refresh()
        assert panel._widgets["details_body"].pack_info_ is None
        assert panel._widgets["details_caret"].cget("text") == "▸"

    def test_unfolding_brings_it_back_under_its_own_header(self, panel_module):
        """Safe to hide by unpacking only because the body is the last child
        of its wrapper — re-packing appends, and appending is where it goes.
        The assertion is the guard on that staying true."""
        panel = _panel(panel_module)
        wrap = panel._widgets["details_body"].parent
        built = wrap.packed.index(panel._widgets["details_body"])
        panel.refresh()  # folded
        panel.vm.details_collapsed = False
        panel.refresh()  # and back
        assert wrap.packed.index(panel._widgets["details_body"]) == built
        assert panel._widgets["details_caret"].cget("text") == "▾"

    def test_the_header_click_asks_the_app_to_toggle(self, panel_module):
        clicks = []
        panel = _panel(panel_module, on_toggle_details=lambda: clicks.append(1))
        panel._widgets["details_caret"].binds["<Button-1>"](None)
        assert clicks == [1]

    def test_folding_keeps_the_headline_number_on_screen(self, panel_module):
        """Folding must not hide whether the measured record is good news."""
        panel = _panel(panel_module)
        panel.refresh()
        assert panel._widgets["details_summary"].cget("text") == "not measured yet"


class TestTheScoreBarEases:
    def test_it_moves_toward_a_new_score_rather_than_jumping(self, panel_module):
        panel = _panel(panel_module)
        panel._draw_score_bar(20.0, "#22c55e")
        panel._draw_score_bar(90.0, "#22c55e")
        assert 20.0 < panel._score_shown < 90.0

    def test_it_arrives(self, panel_module):
        """Easing that never lands would leave the bar disagreeing with the
        number printed beside it."""
        panel = _panel(panel_module)
        panel._draw_score_bar(20.0, "#22c55e")
        for _ in range(40):
            panel._draw_score_bar(90.0, "#22c55e")
        assert panel._score_shown == 90.0

    def test_nothing_to_show_resets_it(self, panel_module):
        panel = _panel(panel_module)
        panel._draw_score_bar(90.0, "#22c55e")
        panel._draw_score_bar(None, "#64748b")
        assert panel._score_shown is None


class TestItIsReadableAcrossADesk:
    def test_no_type_is_smaller_than_eight_point(self, panel_module):
        """7pt captions are below comfortable reading on a 1080p screen."""
        panel = _panel(panel_module)
        sizes = [
            font["size"]
            for name, font in vars(panel).items()
            if name.startswith("f_") and isinstance(font, dict)
        ]
        assert sizes and min(sizes) >= 8

    def test_the_text_tones_are_distinguishable(self, panel_module):
        """"Important", "supporting" and "aside" have to differ at a glance."""
        from poa.overlay.viewmodel import COLORS

        def luminance(hex_colour):
            r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
            return 0.2126 * r + 0.7152 * g + 0.0722 * b

        text, dim, faint = (luminance(COLORS[k]) for k in ("text", "dim", "faint"))
        assert text > dim > faint
        assert text - dim > 20 and dim - faint > 20
