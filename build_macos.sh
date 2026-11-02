#!/usr/bin/env bash
# Builds release/OfflinePDF Studio-<version>.dmg on macOS.
# Usage: ./build_macos.sh [--no-pause]
set -euo pipefail

cd "$(dirname "$0")"

NOPAUSE=0
if [[ "${1:-}" == "--no-pause" || "${1:-}" == "/nopause" ]]; then NOPAUSE=1; fi

PROJECT_DIR="$(pwd)"
VENV_PY="$PROJECT_DIR/.venv/bin/python"
BUILD_ROOT="${TMPDIR:-/tmp}/offlinepdf-build"
DIST_DIR="$BUILD_ROOT/dist"
WORK_DIR="$BUILD_ROOT/build"
OUT_DIR="$PROJECT_DIR/release"
APP_NAME="OfflinePDF Studio"
PYINSTALLER_VERSION="6.22.3"

echo "============================================================"
echo "  OfflinePDF Studio - macOS build"
echo "============================================================"
echo

if [[ ! -x "$VENV_PY" ]]; then
    echo "[ERROR] Python virtual environment not found at: $VENV_PY"
    echo "        Create it first: python3.14 -m venv .venv && .venv/bin/pip install -r requirements.lock"
    exit 1
fi

APPVER="$("$VENV_PY" - <<'PY'
import tomllib
with open("pyproject.toml", "rb") as fh:
    print(tomllib.load(fh)["project"]["version"])
PY
)"
echo "Building $APP_NAME version $APPVER"
echo

echo "[1/4] Checking PyInstaller $PYINSTALLER_VERSION in the venv..."
if ! "$VENV_PY" -c "import PyInstaller, sys; sys.exit(PyInstaller.__version__ != '$PYINSTALLER_VERSION')" >/dev/null 2>&1; then
    "$VENV_PY" -m pip install --disable-pip-version-check "PyInstaller==$PYINSTALLER_VERSION"
fi
echo

echo "[2/4] Freezing the application with PyInstaller..."
rm -rf "$BUILD_ROOT"
mkdir -p "$BUILD_ROOT"
"$VENV_PY" -m PyInstaller --noconfirm --clean --log-level WARN \
    --distpath "$DIST_DIR" --workpath "$WORK_DIR" \
    "$PROJECT_DIR/offlinepdf.spec"
APP_BUNDLE="$DIST_DIR/$APP_NAME.app"
if [[ ! -d "$APP_BUNDLE" ]]; then
    echo "[ERROR] PyInstaller did not produce $APP_BUNDLE"
    exit 1
fi
echo

echo "[3/4] Verifying the frozen application (offline self-test)..."
APP_BIN="$APP_BUNDLE/Contents/MacOS/$APP_NAME"
"$APP_BIN" --selftest-offline > "$BUILD_ROOT/selftest.log" 2>&1 || SELFTEST_RC=$?
SELFTEST_RC="${SELFTEST_RC:-0}"
grep -E '\[PASS\]|\[FAIL\]|selftest:|Error|Traceback' "$BUILD_ROOT/selftest.log" || true
if [[ "$SELFTEST_RC" != "0" ]]; then
    echo "[ERROR] The frozen application failed its self-test (exit code $SELFTEST_RC)."
    echo "        Full output: $BUILD_ROOT/selftest.log"
    exit 1
fi
echo

echo "[4/4] Building the DMG..."
mkdir -p "$OUT_DIR"
DMG_PATH="$OUT_DIR/$APP_NAME-Setup-$APPVER.dmg"
STAGING="$BUILD_ROOT/dmg"
rm -rf "$STAGING"; mkdir -p "$STAGING"
cp -R "$APP_BUNDLE" "$STAGING/"
ln -s /Applications "$STAGING/Applications"

# Optional code signing.
if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
    echo "Signing with identity: $CODESIGN_IDENTITY"
    codesign --deep --force --verify --verbose \
        --sign "$CODESIGN_IDENTITY" --options runtime \
        --entitlements "$PROJECT_DIR/packaging/macos/entitlements.plist" \
        "$APP_BUNDLE"
fi

rm -f "$DMG_PATH"
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGING" \
    -ov -format UDZO "$DMG_PATH"

# Optional notarization.
if [[ -n "${NOTARY_PROFILE:-}" ]]; then
    echo "Submitting for notarization (profile: $NOTARY_PROFILE)..."
    xcrun notarytool submit "$DMG_PATH" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$DMG_PATH"
fi

echo
echo "============================================================"
echo "  SUCCESS"
echo "  DMG: $DMG_PATH"
echo "============================================================"
if [[ "$NOPAUSE" == "0" ]]; then read -r -p "Press Enter to continue..."; fi