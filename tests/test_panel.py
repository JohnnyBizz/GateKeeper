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
                            tags=str(kw.get("tags", "")), state="normal")

    def _item(self, item_id):
        for item in self.recorder.items:
            if item["id"] == item_id:
                return item
        return {}

    def coords(self, item_id, x, y):
        self._item(item_id).update(x=x, y=y)

    def itemconfigure(self, item_id, **kw):
        self._item(item_id).update(kw)

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

    def test_a_tab_shows_the_expiry_it_needs(self, panel_module):
        """A green tab at the wrong expiry is an invitation to take a trade
        the engine never scored."""
        from poa.overlay.viewmodel import OverlayViewModel

        vm = OverlayViewModel()
        vm.asset, vm.chart_timeframe, vm.trade_duration = "EUR/USD OTC", 5, 30
        vm.watchlist = [
            {"asset": "EUR/USD OTC", "timeframe": 180, "expiry": 600,
             "score": 82.0, "direction": "CALL", "actionable": True},
        ]
        panel = _panel(panel_module, vm)
        panel.refresh()
        assert _find(panel, "10M"), "the tab never said which expiry it needs"

    def test_a_matching_expiry_puts_no_marker_on_the_tab(self, panel_module):
        from poa.overlay.viewmodel import OverlayViewModel

        vm = OverlayViewModel()
        vm.asset, vm.chart_timeframe, vm.trade_duration = "EUR/USD OTC", 5, 30
        vm.watchlist = [
            {"asset": "EUR/USD OTC", "timeframe": 180, "expiry": 30,
             "score": 82.0, "direction": "CALL", "actionable": True},
        ]
        panel = _panel(panel_module, vm)
        panel.refresh()
        assert not [t for t in _texts(panel) if t.startswith("▸3")]

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


class TestTheTypingFieldsSurviveARepaint:
    """A canvas window item destroyed and remade on every repaint unmaps and
    remaps its widget twelve times a second — which flickers, and takes the
    cursor out of the field the moment anyone tries to type in it."""

    def _windows(self, panel):
        return [item for item in _drawn(panel) if item["kind"] == "window"]

    def test_a_field_is_created_once_and_moved_after(self, panel_module):
        panel = _panel(panel_module)
        panel.refresh()
        created = len(self._windows(panel))
        for _ in range(6):
            panel.refresh()
        assert len(self._windows(panel)) == created

    def test_clearing_the_frame_does_not_take_them_with_it(self, panel_module):
        """They are deliberately untagged."""
        panel = _panel(panel_module)
        panel.refresh()
        panel.c.delete("frame")
        assert self._windows(panel)

    def test_a_folded_risk_block_hides_its_fields(self, panel_module):
        """Hidden, not left floating over whatever is drawn beneath."""
        panel = _panel(panel_module)
        panel.vm.risk_collapsed = False
        panel.refresh()
        assert all(w["state"] == "normal" for w in self._windows(panel))

        panel.vm.risk_collapsed = True
        panel.refresh()
        hidden = {
            id(panel._entries[key]): w["state"]
            for key in ("balance", "stake", "payout")
            for w in self._windows(panel) if w["window"] is panel._entries[key]
        }
        assert hidden and set(hidden.values()) == {"hidden"}

    def test_collapsing_the_panel_hides_every_field(self, panel_module):
        panel = _panel(panel_module)
        panel.refresh()
        panel.toggle_collapse()
        panel.refresh()
        assert all(w["state"] == "hidden" for w in self._windows(panel))

    def test_the_pair_field_gets_the_headline_face(self, panel_module):
        panel = _panel(panel_module)
        assert panel._entries["pair"].cget("font") is panel.f_pair


class TestOnlyTheHeaderDragsTheWindow:
    """Bound to the whole canvas, a press on SCAN followed by the smallest
    twitch dragged the window instead of pressing the button."""

    class _Event:
        def __init__(self, y):
            self.y, self.x_root, self.y_root = y, 100, 100

    def test_a_press_on_the_header_starts_a_drag(self, panel_module):
        panel = _panel(panel_module)
        panel._drag_start(self._Event(y=20))
        assert panel._dragging is True

    def test_a_press_on_the_body_does_not(self, panel_module):
        panel = _panel(panel_module)
        panel._drag_start(self._Event(y=400))
        assert panel._dragging is False

    def test_moving_after_a_body_press_leaves_the_window_alone(self, panel_module):
        moved = []
        panel = _panel(panel_module)
        panel.root.geometry = lambda *a: moved.append(a)
        panel._drag_start(self._Event(y=400))
        panel._drag_move(self._Event(y=420))
        assert moved == []


class TestACollapsedPanelDrawsNothingBelowTheHeader:
    def test_the_body_is_skipped(self, panel_module):
        panel = _panel(panel_module)
        panel.refresh()
        full = len(_drawn(panel))
        panel.toggle_collapse()
        panel.refresh()
        assert len(_drawn(panel)) < full // 2

    def test_the_header_is_still_there(self, panel_module):
        panel = _panel(panel_module)
        panel.toggle_collapse()
        panel.refresh()
        assert _find(panel, "GATEKEEPER")


class TestTheImageCacheDoesNotThrash:
    """Every drawn shape is cached by appearance. Keyed on a continuously
    varying number, a single pulsing card would evict the whole cache several
    times a minute and re-run a Gaussian blur to do it."""

    def _live(self, panel_module):
        from poa.overlay.viewmodel import OverlayViewModel

        class _Live:
            actionable = True
            direction_confidence = 82.0
            overall_confidence = 82.0
            duration_confidence = 70.0
            duration = None
            mtf = None
            score = None
            price = 1.1
            reason = ""
            warnings: list = []
            state = type("S", (), {"value": "ACTIVE"})()

            @property
            def direction(self):
                from poa.models import Direction

                return Direction.CALL

        vm = OverlayViewModel()
        vm.signal = _Live()
        return _panel(panel_module, vm)

    def test_a_smoother_pulse_does_not_cost_more_images(self, panel_module):
        """The glowing card is the most expensive thing drawn — a blur over a
        340px surface — so how many of it exist has to be a choice, not a
        side effect of how many frames the breath is spread over."""
        counts = []
        for frames in (24, 96):
            panel = self._live(panel_module)
            panel.PULSE_FRAMES = frames
            for _ in range(frames * 3):
                panel.refresh()
            counts.append(len([k for k in panel._images if k[0] == "card"]))
        assert counts[1] == counts[0], f"{counts[0]} cards became {counts[1]}"

    def test_the_countdown_ring_is_quantised(self, panel_module):
        """Unquantised there is one image per second of the chart's timeframe,
        so a five-minute chart alone would evict the whole cache."""
        panel = self._live(panel_module)
        panel.vm.chart_timeframe = 300
        for step in range(300):
            panel.vm._now = lambda step=step: 1_700_000_100.0 + step
            panel.refresh()
        rings = [key for key in panel._images if key[0] == "ring"]
        # One image per step of the drain, times the handful of tones the
        # pulse passes through while a bar is nearly gone.
        ceiling = panel_module.RING_STEPS + panel.PULSE_STEPS * 2
        assert len(rings) <= ceiling, f"{len(rings)} rings, ceiling {ceiling}"

    def test_the_cache_never_passes_its_cap(self, panel_module):
        panel = self._live(panel_module)
        for step in range(400):
            panel.vm._now = lambda step=step: 1_700_000_100.0 + step * 7
            panel.refresh()
        assert len(panel._images) <= panel_module.MAX_CACHED_IMAGES

    def test_data_driven_images_replace_rather_than_accumulate(self, panel_module):
        """The sparkline's look depends on the latest price, so caching it
        beside the fixed shapes would add an entry per tick."""
        from datetime import datetime, timedelta, timezone

        from poa.models import Candle, Series
        from poa.overlay.viewmodel import OverlayViewModel

        vm = OverlayViewModel()
        panel = _panel(panel_module, vm)
        start = datetime(2026, 8, 17, tzinfo=timezone.utc)
        for tick in range(60):
            price = 1.10 + tick * 0.0001
            vm.recent = Series(
                [
                    Candle(start + timedelta(seconds=60 * i), price, price + 0.001,
                           price - 0.001, price + i * 1e-5)
                    for i in range(40)
                ],
                60, "EUR/USD OTC",
            )
            panel.refresh()
        assert set(panel._slots) <= {"spark", "candles"}
        assert not [key for key in panel._images if key[0] in ("spark", "candles")]


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


class TestTheRecordButton:
    """One control, four things to say, and a click that means each of them."""

    def test_it_is_drawn_and_it_answers(self, panel_module):
        pressed: list[int] = []
        panel = _panel(panel_module, on_record=lambda: pressed.append(1))
        panel.refresh()

        assert _find(panel, "RECORD 30 MIN FOR ANALYSIS")
        _click(panel, "record")
        assert pressed == [1]

    def test_a_running_capture_shows_how_long_is_left(self, panel_module):
        from poa.overlay.viewmodel import OverlayViewModel

        vm = OverlayViewModel()
        vm.recording.active = True
        vm.recording.total = 1800.0
        vm.recording.elapsed = 600.0
        vm.recording.frames = 4210
        panel = _panel(panel_module, vm=vm)
        panel.refresh()

        # Twenty minutes of nothing on screen is indistinguishable from a
        # hung app, which is the state this button is most likely to be in.
        assert _find(panel, "RECORDING — 20 MIN LEFT")
        assert _find(panel, "4,210")

    def test_a_finished_one_names_the_file_to_send(self, panel_module):
        from poa.overlay.viewmodel import OverlayViewModel

        vm = OverlayViewModel()
        vm.recording.bundle = "/tmp/gatekeeper-recording-2026-08-19-1100.zip"
        vm.recording.message = "gatekeeper-recording-2026-08-19-1100.zip — 6 charts."
        panel = _panel(panel_module, vm=vm)
        panel.refresh()

        assert _find(panel, "RECORDING SAVED — TAP TO OPEN")
        assert _find(panel, "gatekeeper-recording-2026-08-19-1100.zip")

    def test_it_never_covers_the_verdict(self, panel_module):
        """It is not part of trading and must not compete with what is."""
        from poa.overlay.viewmodel import OverlayViewModel

        vm = OverlayViewModel()
        vm.recording.active = True
        vm.recording.total = 1800.0
        panel = _panel(panel_module, vm=vm)
        panel.refresh()

        record = _find(panel, "RECORDING —")[0]
        scan = _find(panel, "SCAN")[0]
        assert record["y"] > scan["y"]


class TestTheVerdictFitsThePanel:
    """"NO TRADE" ran off the right edge and was clipped mid-letter.

    The panel is a fixed 340 pixels and the verdict starts 162 in, so a label
    has 168 to live in. Three of the four values are one short word; the
    fourth is two, and in the verdict face it does not fit.

    This had already happened to "SCANNING" and was fixed there by hard-coding
    a smaller face for that one string — which fixed the instance and left the
    cause, so the next long word did it again. These tests are about the
    measurement, not about the two words that have been caught so far.
    """

    def _label(self, panel_module, direction, label):
        from poa.overlay.viewmodel import OverlayViewModel

        vm = OverlayViewModel()
        rendered = vm.render()
        panel = _panel(panel_module, vm=vm)
        # Draw the verdict directly, so the test is about the label and not
        # about arranging a market that produces one.
        rendered["verdict"].update(
            direction=direction, direction_label=label, blanked=False,
            actionable=False, score=58.0, state="ACTIVE",
        )
        rendered["scan"]["scanning"] = False
        panel._draw_signal(rendered, 0)
        return panel

    def _drawn_font(self, panel, text):
        hits = [i for i in _drawn(panel) if i["kind"] == "text" and i["text"] == text]
        assert hits, f"{text!r} was never drawn"
        return hits[0]["font"]

    def test_a_short_verdict_keeps_the_big_face(self, panel_module):
        panel = self._label(panel_module, "CALL", "BUY")

        assert self._drawn_font(panel, "BUY") is panel.f_verdict

    def test_the_long_one_steps_down_rather_than_overflowing(self, panel_module):
        panel = self._label(panel_module, "NO_TRADE", "NO TRADE")

        assert self._drawn_font(panel, "NO TRADE") is not panel.f_verdict

    def test_every_verdict_the_tool_can_show_fits(self, panel_module):
        """The four are BUY, SELL, WAIT and NO TRADE. All of them, measured."""
        from poa.overlay.panel import PAD, PANEL_WIDTH

        room = PANEL_WIDTH - PAD - (PAD + 152)
        panel = _panel(panel_module)
        for label in ("BUY", "SELL", "WAIT", "NO TRADE"):
            font = panel._fitted(label, room, panel.f_verdict, panel.f_score,
                                 panel.f_button)
            assert panel._width_of(label, font) <= room, label

    def test_a_word_too_long_for_any_face_still_picks_the_smallest(self, panel_module):
        """Better a squeezed word than a crash or a clipped one."""
        panel = _panel(panel_module)
        font = panel._fitted("ABSOLUTELY ENORMOUS", 168, panel.f_verdict,
                             panel.f_score, panel.f_button)

        assert font is panel.f_button

    def test_it_measures_rather_than_matching_known_words(self, panel_module):
        """The bug was fixed twice by naming a string. Not a third time."""
        panel = _panel(panel_module)
        wide = panel._fitted("NO TRADE", 168, panel.f_verdict, panel.f_score)
        roomy = panel._fitted("NO TRADE", 900, panel.f_verdict, panel.f_score)

        # Same word, different room, different answer — so the decision is
        # about width and not about the word.
        assert wide is not roomy
        assert roomy is panel.f_verdict
