"""The one layer that draws.

Everything about *what* to show is decided in ``viewmodel.py`` and tested there
without a display. What is left here is where things are placed and what
answers a click — which turned out not to be free of decisions.

The panel draws on a single canvas, so the stub records canvas calls instead of
building a widget tree: which images were placed where, which text was written,
and which tags were bound to which handler. That is enough to assert the things
that have actually broken — a section drawn in the wrong order, a click sending
the wrong chart, a fold that hides something it should not.

Tkinter is not installed on the CI runner, so it is stubbed rather than drawn.
The shapes themselves are real: ``graphics`` renders genuine images, and those
are tested in ``test_graphics.py`` where no toolkit is needed at all.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest


class _Recorder:
    """Records what was drawn, in the order it was drawn."""

    def __init__(self):
        self.items: list[dict] = []
        self.binds: dict[str, object] = {}
        self._next = 0


class _Widget:
    """Enough of a Tk widget for the panel to build and paint itself."""

    def __init__(self, parent=None, **kw):
        self.parent = parent
        self.kw = dict(kw)
        self.recorder = getattr(parent, "recorder", None) or _Recorder()
        self.children: list["_Widget"] = []
        if parent is not None:
            parent.children.append(self)

    # -- canvas ------------------------------------------------------------

    def _record(self, kind, **fields):
        self.recorder._next += 1
        item = {"id": self.recorder._next, "kind": kind, **fields}
        self.recorder.items.append(item)
        return item["id"]

    def create_image(self, x, y, **kw):
        return self._record("image", x=x, y=y, tags=str(kw.get("tags", "")),
                            image=kw.get("image"))

    def create_text(self, x, y, **kw):
        return self._record("text", x=x, y=y, text=str(kw.get("text", "")),
                            fill=kw.get("fill"), font=kw.get("font"),
                            anchor=kw.get("anchor"), tags=str(kw.get("tags", "")))

    def create_window(self, x, y, **kw):
        return self._record("window", x=x, y=y, window=kw.get("window"),
                            tags=str(kw.get("tags", "")))

    def delete(self, which):
        if which in ("all", None):
            self.recorder.items = []
            return
        self.recorder.items = [
            item for item in self.recorder.items
            if which not in str(item.get("tags", "")).split()
        ]

    def bbox(self, _item):
        return (0, 0, 100, 12)

    def tag_bind(self, tag, sequence, fn):
        self.recorder.binds[f"{tag}{sequence}"] = fn

    # -- everything else ---------------------------------------------------

    def configure(self, **kw):
        self.kw.update(kw)

    config = configure

    def cget(self, key):
        return self.kw.get(key)

    def bind(self, sequence, fn):
        self.recorder.binds[f"self{sequence}"] = fn

    def pack(self, **kw):
        pass

    def destroy(self):
        pass

    def winfo_screenheight(self):
        return 1080

    def winfo_x(self):
        return 0

    def winfo_y(self):
        return 0

    def focus_displayof(self):
        return None

    def focus_set(self):
        pass

    def geometry(self, *a):
        pass

    def title(self, *a):
        pass

    def attributes(self, *a):
        pass

    def overrideredirect(self, *a):
        pass

    def delete_(self, *a):
        pass

    def insert(self, *a):
        pass

    def get(self):
        return ""

    def mainloop(self):
        pass


@pytest.fixture
def panel_module(monkeypatch):
    """The panel module, built against a stub Tk."""
    tk = types.ModuleType("tkinter")
    for name in ("Tk", "Toplevel", "Frame", "Label", "Canvas", "Entry", "Text"):
        setattr(tk, name, type(name, (_Widget,), {}))
    # Entry needs a delete that clears text rather than canvas items.
    tk.Entry.delete = lambda self, *a: None
    tk.TclError = type("TclError", (Exception,), {})

    tkfont = types.ModuleType("tkinter.font")
    tkfont.Font = lambda **kw: kw
    tkfont.families = lambda *a, **k: ("Segoe UI", "Consolas")
    tk.font = tkfont

    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "tkinter.font", tkfont)

    # PhotoImage needs a display; the images themselves are real and tested
    # elsewhere, so here only the handle is stood in for.
    imagetk = types.ModuleType("PIL.ImageTk")
    imagetk.PhotoImage = lambda image: ("photo", image.size)
    monkeypatch.setitem(sys.modules, "PIL.ImageTk", imagetk)

    module = importlib.reload(importlib.import_module("poa.overlay.panel"))
    yield module
    sys.modules.pop("poa.overlay.panel", None)


def _panel(panel_module, vm=None, **kw):
    from poa.overlay.viewmodel import OverlayViewModel

    return panel_module.OverlayPanel(vm or OverlayViewModel(), **kw)


def _drawn(panel):
    return panel.c.recorder.items


def _texts(panel):
    return [item["text"] for item in _drawn(panel) if item["kind"] == "text"]


def _find(panel, needle):
    return [
        item for item in _drawn(panel)
        if item["kind"] == "text" and needle in item["text"]
    ]


def _click(panel, tag):
    handler = panel.c.recorder.binds.get(f"{tag}<Button-1>")
    assert handler is not None, f"nothing is bound to {tag}"
    handler(None)


class TestItPaints:
    def test_a_first_paint_with_no_data_does_not_crash(self, panel_module):
        panel = _panel(panel_module)
        panel.refresh()
        assert _drawn(panel)

    def test_repainting_replaces_rather_than_accumulates(self, panel_module):
        """Every frame is redrawn, so leftovers would pile up invisibly and
        the canvas would grow without bound over a session."""
        panel = _panel(panel_module)
        panel.refresh()
        first = len(_drawn(panel))
        for _ in range(5):
            panel.refresh()
        assert len(_drawn(panel)) == first

    def test_the_disclaimer_is_always_drawn(self, panel_module):
        panel = _panel(panel_module)
        panel.refresh()
        assert _find(panel, "not a trading recommendation")


class TestTheOrderThingsAppearIn:
    """What to do, then when, then what the market is doing behind it."""

    def _order(self, panel, *needles):
        found = []
        for needle in needles:
            hits = _find(panel, needle)
            assert hits, f"{needle!r} was never drawn"
            found.append(hits[0]["id"])
        return found

    def test_the_signal_comes_before_the_market_strip(self, panel_module):
        panel = _panel(panel_module)
        panel.refresh()
        signal, market = self._order(panel, "SIGNAL", "GATEKEEPER")
        assert market < signal  # the header is first

    def test_the_watchlist_sits_above_the_signal(self, panel_module):
        from poa.overlay.viewmodel import OverlayViewModel

        vm = OverlayViewModel()
        vm.asset, vm.chart_timeframe = "EUR/USD OTC", 60
        vm.watchlist = [
            {"asset": "EUR/USD OTC", "timeframe": 60, "score": 70.0,
             "direction": "CALL", "actionable": False, "expiry": 180},
            {"asset": "GBP/USD OTC", "timeframe": 15, "score": 81.0,
             "direction": "CALL", "actionable": True, "expiry": 60},
        ]
        panel = _panel(panel_module, vm)
        panel.refresh()
        tabs = _find(panel, "GBP/USD")
        signal = _find(panel, "SIGNAL")
        assert tabs and signal and tabs[0]["id"] < signal[0]["id"]


class TestTheWatchlistTabs:
    def _vm(self):
        from poa.overlay.viewmodel import OverlayViewModel

        vm = OverlayViewModel()
        vm.asset, vm.chart_timeframe = "EUR/USD OTC", 60
        vm.watchlist = [
            {"asset": "EUR/USD OTC", "timeframe": 60, "score": 70.0,
             "direction": "CALL", "actionable": False, "expiry": 180},
            {"asset": "GBP/USD OTC", "timeframe": 15, "score": 81.0,
             "direction": "CALL", "actionable": True, "expiry": 60},
        ]
        return vm

    def test_a_click_sends_the_pair_and_the_timeframe(self, panel_module):
        """A chart is a pair *and* a length. Sending only the pair opened
        whichever length that pair happened to be followed at."""
        picked = []
        panel = _panel(panel_module, self._vm(),
                       on_asset=lambda a, tf=None: picked.append((a, tf)))
        panel.refresh()
        _click(panel, "watch1")
        assert picked == [("GBP/USD OTC", 15)]

    def test_every_tab_is_clickable(self, panel_module):
        picked = []
        panel = _panel(panel_module, self._vm(),
                       on_asset=lambda a, tf=None: picked.append((a, tf)))
        panel.refresh()
        _click(panel, "watch0")
        _click(panel, "watch1")
        assert len(picked) == 2

    def test_an_empty_watchlist_draws_no_tabs(self, panel_module):
        panel = _panel(panel_module)
        panel.refresh()
        assert "watch0<Button-1>" not in panel.c.recorder.binds


class TestTheControlsAnswer:
    def test_scan_reset_settings_and_close_are_all_bound(self, panel_module):
        pressed = []
        panel = _panel(
            panel_module,
            on_scan=lambda: pressed.append("scan"),
            on_reset=lambda: pressed.append("reset"),
            on_settings=lambda: pressed.append("settings"),
            on_close=lambda: pressed.append("close"),
        )
        panel.refresh()
        for tag in ("scan", "reset", "gear", "shut"):
            _click(panel, tag)
        assert pressed == ["scan", "reset", "settings", "close"]

    def test_scan_is_ignored_while_already_scanning(self, panel_module):
        pressed = []
        panel = _panel(panel_module, on_scan=lambda: pressed.append(1))
        panel.vm.scan.begin()
        panel.refresh()
        _click(panel, "scan")
        assert pressed == []

    def test_the_win_and_loss_buttons_adjust_in_both_directions(self, panel_module):
        moves = []
        panel = _panel(panel_module, on_adjust=lambda w, l: moves.append((w, l)))
        panel.refresh()
        for tag in ("winup", "windn", "lossup", "lossdn"):
            _click(panel, tag)
        assert moves == [(1, 0), (-1, 0), (0, 1), (0, -1)]

    def test_the_fold_headers_ask_the_app_to_toggle(self, panel_module):
        toggled = []
        panel = _panel(panel_module,
                       on_toggle_details=lambda: toggled.append("details"),
                       on_toggle_risk=lambda: toggled.append("risk"))
        panel.refresh()
        _click(panel, "evidence")
        _click(panel, "riskhead")
        assert toggled == ["details", "risk"]


class TestTheEvidenceBlockFolds:
    """Every line under the decision earned its place one at a time, and
    together they turned a panel meant to be glanced at into a page."""

    def test_it_is_folded_by_default(self, panel_module):
        panel = _panel(panel_module)
        panel.refresh()
        assert _find(panel, "▸ EVIDENCE")
        assert not _find(panel, "Duration fit")

    def test_unfolding_shows_the_detail(self, panel_module):
        panel = _panel(panel_module)
        panel.vm.details_collapsed = False
        panel.refresh()
        assert _find(panel, "▾ EVIDENCE")
        assert _find(panel, "Duration fit")

    def test_the_headline_number_stays_out_when_it_is_folded(self, panel_module):
        """Folding must not hide whether the measured record is good news."""
        panel = _panel(panel_module)
        panel.refresh()
        assert _find(panel, "not measured yet")

    def test_the_decision_itself_never_folds(self, panel_module):
        panel = _panel(panel_module)
        panel.refresh()
        for needed in ("SIGNAL", "WINS", "LOSSES", "SCAN"):
            assert _find(panel, needed), needed


class TestTheEntryCountdown:
    """A BUY with no indication of how much of the candle is left is an
    instruction with the timing filed somewhere else."""

    def test_the_clock_is_drawn_inside_the_signal_card(self, panel_module):
        from poa.overlay.viewmodel import OverlayViewModel

        vm = OverlayViewModel(chart_timeframe=60, _now=lambda: 1_700_000_137.0)
        panel = _panel(panel_module, vm)
        panel.refresh()
        signal = _find(panel, "SIGNAL")[0]["id"]
        market = _find(panel, "MARKET")
        after = market[0]["id"] if market else 10**9
        clocks = [
            item for item in _drawn(panel)
            if item["kind"] == "text" and signal < item["id"] < after
        ]
        assert clocks

    def test_it_pulses_only_when_the_bar_is_nearly_gone(self, panel_module):
        panel = _panel(panel_module)
        tones = set()
        for _ in range(panel.PULSE_FRAMES):
            panel._pulse += 1
            tones.add(panel._pulse_toward("#22c55e"))
        assert len(tones) > 4


class TestTheScoreDialEases:
    def test_it_moves_toward_a_new_score_rather_than_jumping(self, panel_module):
        panel = _panel(panel_module)
        panel._ease_score(20.0)
        panel._ease_score(90.0)
        assert 20.0 < panel._score_shown < 90.0

    def test_it_arrives(self, panel_module):
        """Easing that never lands would leave the dial disagreeing with the
        number printed inside it."""
        panel = _panel(panel_module)
        panel._ease_score(20.0)
        for _ in range(40):
            panel._ease_score(90.0)
        assert panel._score_shown == 90.0

    def test_nothing_to_show_resets_it(self, panel_module):
        panel = _panel(panel_module)
        panel._ease_score(90.0)
        assert panel._ease_score(None) is None
        assert panel._score_shown is None


class TestDrawnImagesAreKeptAlive:
    """Tk holds only a weak claim on a PhotoImage: an unreferenced one is
    collected and the canvas draws a blank where the picture was."""

    def test_images_are_retained(self, panel_module):
        panel = _panel(panel_module)
        panel.refresh()
        assert panel._images

    def test_the_cache_is_bounded(self, panel_module):
        panel = _panel(panel_module)
        for step in range(400):
            panel._photo(("junk", step), lambda: _FakeImage())
        assert len(panel._images) <= panel_module.MAX_CACHED_IMAGES


class _FakeImage:
    size = (4, 4)


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
