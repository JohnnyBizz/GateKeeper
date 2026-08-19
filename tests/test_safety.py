"""The two promises the whole tool rests on, checked mechanically.

    "DO NOT automatically place trades."
    "The system must NEVER claim that a trade is guaranteed to win."

Both have been true by care until now — read the diff, notice the word, take
it out. Care does not survive a codebase this size, and neither promise is the
kind that can be broken a little. So they are checked here, over every module
in the package rather than over the handful a feature test happens to touch:
one sweep of every string the tool can print, and one of every message it is
capable of sending to the platform.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "poa"

# Never say these. Taken from the module that owns the tool's wording, so
# there is one list rather than two that can disagree, plus the phrasings the
# spec names which that list does not spell out verbatim.
from poa.signals.narrative import BANNED_PHRASES  # noqa: E402

FORBIDDEN = tuple(BANNED_PHRASES) + (
    "guaranteed to win",
    "100% accuracy",
    "certain profit",
    "never loses",
)

# The module holding the list is the one place these words are allowed to
# appear, because holding the list is how they are kept out of everywhere else.
DEFINES_THE_LIST = "narrative.py"

# What the tool is allowed to say to the browser. Every one of these asks the
# browser about itself; none of them reaches the platform. "Network.enable"
# and "Page.enable" subscribe to events, and "Page.reload" reloads the tab the
# user already has open, which is how a dropped feed is recovered.
#
# The list is deliberately short and deliberately here: a trade is placed by
# sending something, and this is the only place in the package that sends.
ALLOWED_CDP_METHODS = {"Network.enable", "Page.enable", "Page.reload"}


def _modules() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def _strings(path: Path) -> list[str]:
    """Every string literal in a module, docstrings included."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


class TestNothingIsEverPromised:
    def test_the_spec_s_four_named_phrases_are_all_on_the_list(self):
        """The list is the fence. It must not be quietly shortened."""
        for phrase in ("guarantee", "100% accurate", "cannot lose", "risk-free"):
            assert any(phrase in banned for banned in BANNED_PHRASES), phrase

    def test_no_module_can_print_a_promise(self):
        offences: list[str] = []
        for path in _modules():
            if path.name == DEFINES_THE_LIST:
                continue
            for text in _strings(path):
                lowered = text.lower()
                for phrase in FORBIDDEN:
                    if phrase in lowered:
                        offences.append(f"{path.name}: {phrase!r} in {text[:70]!r}")
        assert offences == []

    def test_the_sweep_would_notice(self):
        """A test that cannot fail is not a test."""
        sample = "This is a GUARANTEED WIN, you cannot lose.".lower()
        assert [p for p in FORBIDDEN if p in sample]


class TestItPlacesNoTrades:
    """Nothing here is capable of pressing the platform's buy or sell button."""

    def _sent_methods(self, path: Path) -> list[str]:
        """CDP method names in dictionaries built anywhere in a module."""
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "method"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and "." in value.value
                ):
                    found.append(value.value)
        return found

    def test_every_message_it_can_send_is_one_that_only_listens(self):
        sent: set[str] = set()
        for path in _modules():
            sent.update(self._sent_methods(path))

        # A method outside this set is not necessarily a trade — but it is a
        # new thing being said to the browser, and that is worth a second
        # pair of eyes rather than a green build.
        assert sent <= ALLOWED_CDP_METHODS, sorted(sent - ALLOWED_CDP_METHODS)

    def test_the_recorder_only_subscribes(self):
        from poa.feed import recorder

        methods = self._sent_methods(Path(recorder.__file__))
        assert methods == ["Network.enable"]

    @pytest.mark.parametrize(
        "verb", ["Input.dispatchMouseEvent", "Input.dispatchKeyEvent",
                 "Runtime.evaluate", "Runtime.callFunctionOn"]
    )
    def test_it_cannot_drive_the_page(self, verb):
        """Clicking BUY takes one of these. None of them is present."""
        for path in _modules():
            source = path.read_text(encoding="utf-8")
            assert verb not in source, f"{path.name} mentions {verb}"
