# Building a standalone GateKeeper app

Two ways to get an app you can double-click. Neither needs the terminal once
it is done.

## Easiest: no build at all

Double-click **`GateKeeper.bat`** (Windows) or **`GateKeeper.command`**
(macOS/Linux). The first run installs what it needs and then starts the
overlay; every run after that just starts it.

This still requires Python to be installed on the machine. If you would rather
not have that, build the executable below.

**Make a Start-menu / desktop shortcut:** right-click `GateKeeper.bat` →
*Send to* → *Desktop (create shortcut)*. Rename it to GateKeeper. You can give
it an icon from the shortcut's Properties.

## A true standalone .exe

Produces a single file that runs on a machine with no Python at all.

```bash
pip install pyinstaller
pyinstaller gatekeeper.spec
```

The result is `dist/GateKeeper.exe` (or `dist/GateKeeper` on macOS/Linux).
Copy it anywhere and double-click it.

Notes:

* Build on the platform you are targeting — PyInstaller does not cross-compile,
  so a Windows .exe has to be built on Windows.
* The first launch is slower than later ones, because the bundle unpacks to a
  temporary directory.
* Windows SmartScreen may warn about an unsigned executable you built yourself.
  Choose *More info* → *Run anyway*. Signing it requires a code-signing
  certificate, which is only worth it if you plan to distribute the app.

## Let GitHub build it for you

`.github/workflows/build.yml` builds the Windows executables automatically on
every push to `main`, and publishes them to a release with a fixed address:

    https://github.com/JohnnyBizz/GateKeeper/releases/tag/latest

Four files are published each time: `GateKeeper.exe`, `RecordFeed.exe`, and a
`.zip` of each. The workflow runs the test suite first, so an executable is
only produced from a build where every test passed.

### When the download itself is refused

Edge refused `RecordFeed.exe` outright — *"Couldn't download — virus
detected"*. That is not the usual SmartScreen prompt: there is no file to
allow, because none arrived.

The honest answer is that the cause cannot be read off the log — a
reputation-based refusal does not say what tipped it. What can be said is
which properties of the file make a refusal likely, and those have been
removed:

* **No code signature.** This is the one that matters and the one that costs
  money. A certificate is the only thing that gives a brand-new executable any
  reputation on the day it is built; without it, every build is a binary
  nothing has ever seen. Not fixed.
* **UPX compression.** Both specs now set `upx=False`. Being straight about
  this: `windows-latest` does not ship UPX, so `upx=True` was probably doing
  nothing on the runner and probably was not the cause. It is off anyway,
  because it saves a few megabytes on a file downloaded once and costs the
  shape of a binary that unpacks itself before it runs — and the day the
  toolchain does have UPX, nobody would connect the two.
* **No version resource.** A binary with no publisher, product or version is
  anonymous, and anonymous is most of what the check has to go on. Both
  executables now carry one, from `packaging/*_version.txt`.

If a download is still refused, take the `.zip` beside it: the check reads
executables coming down the wire, and a zip is not one. Windows then judges
the file on extraction, where *keep anyway* is at least offered.

**You may not need `RecordFeed.exe` at all.** GateKeeper records the feed
itself — the **RECORD 30 MIN FOR ANALYSIS** button on the panel — using the
browser it is already attached to, and writes the same
`gatekeeper-recording-*.zip`. The separate executable is for running a capture
without the app.
