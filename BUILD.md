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

`.github/workflows/build.yml` builds the Windows executable automatically on
every push to `main`. To download one:

1. Open the repository's **Actions** tab.
2. Click the most recent **Build GateKeeper** run.
3. Download the **GateKeeper-windows** artifact at the bottom.

The workflow runs the test suite first, so an executable is only produced from
a build where every test passed.
