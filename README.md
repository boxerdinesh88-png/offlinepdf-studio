# OfflinePDF Studio

Fully offline desktop PDF editor with automatic style matching. The app never
opens a network connection: every socket, DNS and HTTP API is blocked at
start-up (`offlinepdf/netguard.py`).

- **Stack:** Python 3.14, PySide6 (Qt 6) UI, PyMuPDF + pikepdf for PDF
  processing, fontTools/uharfbuzz for text shaping, RapidOCR (ONNX Runtime)
  for scanned pages.
- **Entry point:** `main.py` → `offlinepdf.app:main`.
- **Bundled runtime files:** `assets/fonts` (vendored open-licence fonts),
  `assets/ocr` (OCR models), RapidOCR config files, the Qt plugins, the
  Python runtime, OpenSSL, and the Microsoft VC++ runtime DLLs.

## Running from source (developers)

```bat
.venv\Scripts\python main.py                    :: start the GUI
.venv\Scripts\python main.py file.pdf           :: open a file directly
.venv\Scripts\python main.py --selftest-offline :: headless smoke test
.venv\Scripts\python main.py --version
```

## Installing (end users)

Run `OfflinePDF Studio-Setup-<version>.exe`. Nothing else is needed. Python,
Visual C++ redistributables and other developer tools are **not** required.

- **Requirements (Windows):** 64-bit Windows 10 or 11, about 550 MB of free disk space.
- **Requirements (macOS):** macOS 11 (Big Sur) or later on Apple Silicon or
  Intel, about 600 MB of free disk space. Open the DMG and drag
  *OfflinePDF Studio* to *Applications*.
- **Install mode:** the first dialog asks whether to install for *you only*
  (default, no administrator rights, installs to
  `%LOCALAPPDATA%\Programs\OfflinePDF Studio`) or for *all users* (asks for
  administrator approval, installs to `C:\Program Files\OfflinePDF Studio`).
- **Shortcuts:** a Start Menu entry is always created. A Desktop shortcut is
  an option on the "Additional tasks" page.
- **Uninstall:** *Settings → Apps → Installed apps → OfflinePDF Studio →
  Uninstall*, or the "Uninstall OfflinePDF Studio" Start Menu entry.

### Where files are stored

| What | Location |
| --- | --- |
| Program files (read-only) | the install folder chosen above |
| Settings (`settings.json`), logs, crash-recovery snapshots, temp files | `%LOCALAPPDATA%\OfflinePDF Studio` (Windows) / `~/Library/Application Support/OfflinePDF Studio` (macOS) |
| Your PDFs | wherever you save them. Shortcuts start the app in your *Documents* folder |

Nothing is ever written to the install folder. On uninstall you are asked
whether to delete the settings/logs folder too; your PDFs are never touched.

### If something goes wrong

- If the app can't start, it shows a dialog explaining why (missing
  permissions on its data folder, a damaged or quarantined component, and
  so on) and saves the technical details to
  `%LOCALAPPDATA%\OfflinePDF Studio\logs\offlinepdf-startup-error.log`.
- The normal log is `%LOCALAPPDATA%\OfflinePDF Studio\logs\offlinepdf.log`.
- Setup itself refuses to run on 32-bit Windows or Windows older than 10, and
  says so.

## Building the installer

The packaging uses **PyInstaller** (freezes the Python app and every
dependency into a self-contained folder) and **Inno Setup 6** (wraps that
folder into a single installer `.exe`).

### One-time setup on the build PC

1. Install Python 3.14 (64-bit) and create the virtual environment in the
   project folder:
   ```bat
   py -3.14 -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.lock
   ```
2. Install Inno Setup 6:
   ```bat
   winget install --id JRSoftware.InnoSetup -e -s winget
   ```
   (or download it from https://jrsoftware.org/isdl.php).

`build_installer.bat` installs the pinned PyInstaller (6.22.3) into the venv
itself if it is missing.

### Build

```bat
build_installer.bat            :: pauses at the end so you can read the result
build_installer.bat /nopause   :: for scripts / CI
```

What it does:

1. Reads the version from `pyproject.toml` (keep `offlinepdf/__init__.py`'s
   `__version__` equal; the spec refuses to build if they differ).
2. Freezes the app with `offlinepdf.spec` into
   `%LOCALAPPDATA%\offlinepdf-build\dist\OfflinePDFStudio`. The build
   folder is kept outside the project so OneDrive or antivirus scanning
   of the project folder can't lock files mid-build.
3. Runs the frozen app's `--selftest-offline` with a minimal `PATH`
   (System32 only), so a DLL that exists only on the build PC can't hide a
   missing file. The build stops if any check fails.
4. Compiles `setup.iss` with Inno Setup.

**Output:** `release\OfflinePDF Studio-Setup-<version>.exe`, the single
file to distribute.

### Building on macOS

The same PyInstaller freeze produces a native `.app` bundle; the script then
wraps it in a drag-and-drop DMG. Run on a Mac (Apple Silicon or Intel):

```sh
chmod +x build_macos.sh
./build_macos.sh            # pauses at the end
./build_macos.sh --no-pause # for scripts / CI
```

Requirements on the build Mac:

1. Python 3.14 (python.org installer) and a virtual environment:
   ```sh
   python3.14 -m venv .venv
   .venv/bin/python -m pip install -r requirements.lock
   ```
2. `hdiutil` (ships with macOS) for the DMG.

Optional, for distribution outside the build Mac:

- Set `CODESIGN_IDENTITY` to a "Developer ID Application" identity to
  sign the bundle with the hardened runtime (uses
  `packaging/macos/entitlements.plist`).
- Set `NOTARY_PROFILE` to a `notarytool` keychain profile to notarize
  and staple the DMG.

The frozen app's `--selftest-offline` runs before the DMG is created, so a
broken bundle never ships. Double-clicking a PDF opens it in the app via
macOS file events (`argv_emulation` in the spec).

**Output:** `release/OfflinePDF Studio-<version>.dmg`.

### Releasing a new version

1. Bump `version` in `pyproject.toml` **and** `__version__` in
   `offlinepdf/__init__.py`.
2. Run `build_installer.bat`.
3. Never change `AppId` in `setup.iss`. It's what lets a new installer
   upgrade an existing installation in place.

### Changing the icon

`assets/app.ico` is generated by `scripts/make_icon.py`. Edit the script,
run `.venv\Scripts\python scripts\make_icon.py`, and rebuild.

### Packaging notes

- **OpenSSL:** the spec always bundles the `libssl`/`libcrypto` DLLs that sit
  next to Python's own `_ssl.pyd`. Without this, PyInstaller may pick up an
  unrelated OpenSSL from `PATH` (Git, Anaconda, MySQL, …). `ssl` then fails
  to load inside the frozen app, which previously crashed start-up.
- **Code signing:** the installer is unsigned, so Windows SmartScreen shows
  "Windows protected your PC" on first run (*More info → Run anyway*). To
  avoid that, sign both `OfflinePDF Studio.exe` and the setup file with an
  Authenticode certificate (`signtool sign /fd sha256 /tr <timestamp-url>
  /td sha256 <file>`).

## Testing the installer on another PC

Use a PC (or VM) that has **never** had Python or this project on it. That's
the only way to prove the bundle is self-contained.

1. Copy `release\OfflinePDF Studio-Setup-<version>.exe` to the test PC
   (USB stick, network share, etc.).
2. Double-click it. If SmartScreen appears, choose *More info → Run anyway*.
3. Keep *Install for me only*, accept the defaults, tick *Create a desktop
   shortcut*, and finish with *Launch OfflinePDF Studio* checked.
4. Check:
   - [ ] The app window opens, with no error dialog.
   - [ ] The Start Menu has **OfflinePDF Studio** and **Uninstall OfflinePDF
         Studio**, and the Desktop shortcut exists and shows the PDF icon.
   - [ ] *Settings → Apps → Installed apps* lists **OfflinePDF Studio** with
         the version number and icon.
5. Functional test:
   - [ ] *File → Open*: open any PDF. Pages and thumbnails render.
   - [ ] *Mode → Edit*: click a line of text, change a word, press Enter.
         The new text matches the original font and size.
   - [ ] *Insert → Add Image…*: place a PNG or JPEG on a page.
   - [ ] *File → Save As…*: save to `Documents\test-edited.pdf`.
   - [ ] Close the app, reopen `test-edited.pdf`, and confirm the edits are
         there. Also open it in another viewer (Edge, Acrobat).
   - [ ] On a scanned PDF: *Tools → Recognize Text (OCR)…* finishes and the
         page text becomes selectable or editable.
6. Offline check (optional): from a Command Prompt run
   ```bat
   "%LOCALAPPDATA%\Programs\OfflinePDF Studio\OfflinePDF Studio.exe" --selftest-offline > "%TEMP%\selftest.txt" 2>&1 & type "%TEMP%\selftest.txt"
   ```
   It must end with `SELFTEST OK`.
7. Upgrade: run the same (or a newer) installer again. It must replace the
   installation in place, without a second entry in *Installed apps*.
8. Uninstall from *Settings → Apps*. The install folder and shortcuts must
   disappear. Answer the "delete settings" question either way and check
   `%LOCALAPPDATA%\OfflinePDF Studio`.

Silent install, for scripted tests or IT deployment:

```bat
"OfflinePDF Studio-Setup-<version>.exe" /VERYSILENT /SUPPRESSMSGBOXES /CURRENTUSER /TASKS="desktopicon"
"%LOCALAPPDATA%\Programs\OfflinePDF Studio\unins000.exe" /VERYSILENT
```
