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
