"""How the executables are built, and why it decides whether anyone gets one.

Edge refused to download `RecordFeed.exe` — *"Couldn't download — virus
detected"*. That is not the usual SmartScreen prompt with a *Run anyway*
behind it: the file never arrives, so there is nothing to allow, and a build
nobody can download is the same as no build.

Two things in the spec files caused it and both are cheap to keep fixed:
compressing the executable so it has to unpack itself before it runs, and
shipping it with no name, publisher or version on it. Neither is visible in a
green build, which is exactly why they are asserted here.

The version resources are parsed with PyInstaller's own class signatures, so a
typo in them fails on this machine rather than on the Windows runner.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPECS = {
    "gatekeeper.spec": "packaging/gatekeeper_version.txt",
    "recordfeed.spec": "packaging/recordfeed_version.txt",
}


def _spec(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


class TestTheExecutablesAreNotShapedLikeMalware:
    @pytest.mark.parametrize("name", sorted(SPECS))
    def test_nothing_is_upx_packed(self, name):
        """A binary that unpacks itself in memory is what the check refuses."""
        assert re.search(r"^\s*upx=False,", _spec(name), re.M)
        assert not re.search(r"^\s*upx=True,", _spec(name), re.M)

    @pytest.mark.parametrize("name", sorted(SPECS))
    def test_each_carries_a_version_resource(self, name):
        expected = SPECS[name]
        assert f'version="{expected}"' in _spec(name)
        assert (ROOT / expected).exists()


class TestTheVersionResourcesParse:
    """PyInstaller ``eval()``s these. A typo is a failed Windows build."""

    #: The constructor signatures PyInstaller's ``versioninfo`` module defines.
    #: Copied rather than imported, because importing that module needs
    #: ``win32api`` and therefore Windows — and the mistake worth catching is
    #: a misspelt keyword, which these catch on any machine.
    SIGNATURES = {
        "VSVersionInfo": ("ffi", "kids"),
        "FixedFileInfo": (
            "filevers", "prodvers", "mask", "flags", "OS", "fileType",
            "subtype", "date",
        ),
        "StringFileInfo": ("kids",),
        "StringTable": ("name", "kids"),
        "StringStruct": ("name", "val"),
        "VarFileInfo": ("kids",),
        "VarStruct": ("name", "kids"),
    }

    def _namespace(self, seen: dict):
        namespace = {}
        for class_name, parameters in self.SIGNATURES.items():
            def make(class_name=class_name, parameters=parameters):
                def build(*args, **kwargs):
                    if len(args) > len(parameters):
                        raise TypeError(f"{class_name} takes {len(parameters)} args")
                    bound = dict(zip(parameters, args))
                    for key in kwargs:
                        if key not in parameters:
                            raise TypeError(f"{class_name} has no argument {key!r}")
                    bound.update(kwargs)
                    seen.setdefault(class_name, []).append(bound)
                    return bound
                return build
            namespace[class_name] = make()
        return namespace

    @pytest.mark.parametrize("path", sorted(set(SPECS.values())))
    def test_it_deserialises(self, path):
        seen: dict = {}
        text = (ROOT / path).read_text(encoding="utf-8")
        eval(compile(text, path, "eval"), self._namespace(seen))
        assert "VSVersionInfo" in seen

    @pytest.mark.parametrize("path", sorted(set(SPECS.values())))
    def test_it_names_a_publisher_a_product_and_a_version(self, path):
        seen: dict = {}
        eval(
            compile((ROOT / path).read_text(encoding="utf-8"), path, "eval"),
            self._namespace(seen),
        )
        fields = {entry["name"]: entry["val"] for entry in seen["StringStruct"]}

        # The three an anonymous binary is missing, which is most of what a
        # download check has to judge it on.
        for key in ("CompanyName", "ProductName", "FileVersion", "ProductVersion"):
            assert fields.get(key), f"{path} does not set {key}"

    def test_each_executable_is_named_as_itself(self):
        for spec, path in SPECS.items():
            seen: dict = {}
            eval(
                compile((ROOT / path).read_text(encoding="utf-8"), path, "eval"),
                self._namespace(seen),
            )
            fields = {entry["name"]: entry["val"] for entry in seen["StringStruct"]}
            exe = re.search(r'name="([^"]+)"', _spec(spec)).group(1)
            assert fields["OriginalFilename"] == f"{exe}.exe"


class TestTheBuildPublishesSomethingDownloadable:
    """The release is the download. An artifact quota must never be the reason
    a finished executable reaches nobody, and neither must a refused .exe."""

    WORKFLOW = ROOT / ".github/workflows/build.yml"

    def test_both_executables_and_both_zips_are_published(self):
        text = self.WORKFLOW.read_text(encoding="utf-8")
        release = text[text.index("gh release create"):]
        for asset in (
            "dist/GateKeeper.exe",
            "dist/RecordFeed.exe",
            "dist/GateKeeper-windows.zip",
            "dist/RecordFeed-windows.zip",
        ):
            assert asset in release, f"{asset} is not published"

    def test_the_zips_are_actually_built_first(self):
        text = self.WORKFLOW.read_text(encoding="utf-8")
        assert text.index("Compress-Archive") < text.index("gh release create")

    def test_the_artifact_upload_can_never_fail_the_build(self):
        """It has, three times, on a full quota, with a working exe in hand."""
        text = self.WORKFLOW.read_text(encoding="utf-8")
        uploads = text.count("uses: actions/upload-artifact@v4")
        assert uploads == text.count("continue-on-error: true") - 1  # + the tidy step
