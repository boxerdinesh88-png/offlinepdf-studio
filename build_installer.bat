@echo off
setlocal EnableExtensions
pushd "%~dp0"

set "PROJECT_DIR=%~dp0"
set "VENV_PY=%PROJECT_DIR%.venv\Scripts\python.exe"
set "BUILD_ROOT=%LOCALAPPDATA%\offlinepdf-build"
set "DIST_DIR=%BUILD_ROOT%\dist"
set "WORK_DIR=%BUILD_ROOT%\build"
set "OUT_DIR=%PROJECT_DIR%installer_output"
set "APP_NAME=OfflinePDF Studio"

echo ============================================================
echo   OfflinePDF Studio - Windows installer build
echo ============================================================
echo.

if not exist "%VENV_PY%" (
    echo [ERROR] Python virtual environment not found at:
    echo         %VENV_PY%
    goto :fail
)

rem --- version comes from pyproject.toml (single source of truth) ---
set "APPVER="
for /f "tokens=1,2 delims== " %%a in ('findstr /b /c:"version =" pyproject.toml') do set "APPVER=%%b"
set "APPVER=%APPVER:"=%"
if not defined APPVER (
    echo [ERROR] Could not read the version from pyproject.toml
    goto :fail
)
echo Building %APP_NAME% version %APPVER%
echo.

echo [1/3] Updating PyInstaller in the venv...
"%VENV_PY%" -m pip install --upgrade --disable-pip-version-check PyInstaller
if errorlevel 1 goto :fail
echo.

echo [2/3] Freezing the application with PyInstaller...
if exist "%BUILD_ROOT%" rmdir /s /q "%BUILD_ROOT%"
mkdir "%BUILD_ROOT%" >nul 2>&1
"%VENV_PY%" -m PyInstaller --noconfirm --clean --distpath "%DIST_DIR%" --workpath "%WORK_DIR%" "%PROJECT_DIR%offlinepdf.spec"
if errorlevel 1 goto :fail
if not exist "%DIST_DIR%\OfflinePDFStudio\%APP_NAME%.exe" (
    echo [ERROR] PyInstaller did not produce "%DIST_DIR%\OfflinePDFStudio\%APP_NAME%.exe"
    goto :fail
)
echo.

echo [3/3] Building the installer with Inno Setup...
set "ISCC="
for %%p in (
    "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
    "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
    "%ProgramFiles%\Inno Setup 6\ISCC.exe"
    "%LOCALAPPDATA%\Programs\Inno Setup 7\ISCC.exe"
    "%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe"
    "%ProgramFiles%\Inno Setup 7\ISCC.exe"
) do if exist %%p set "ISCC=%%~p"
if not defined ISCC (
    for /f "delims=" %%i in ('where ISCC.exe 2^>nul') do set "ISCC=%%i"
)

if not defined ISCC (
    echo.
    echo [WARNING] Inno Setup ^(ISCC.exe^) was not found on this PC.
    echo           The application itself was built successfully at:
    echo             %DIST_DIR%\OfflinePDFStudio
    echo.
    echo           To finish the installer, install Inno Setup 6 or 7:
    echo             winget install --id JRSoftware.InnoSetup -e -s winget
    echo           then run this script again.
    goto :fail
)

if not exist "%OUT_DIR%" mkdir "%OUT_DIR%" >nul 2>&1

rem --- hand paths to setup.iss through the environment (spaces are safe) ---
set "OFFLINEPDF_BUILD=%BUILD_ROOT%"
set "OFFLINEPDF_VERSION=%APPVER%"
set "OFFLINEPDF_OUTPUT=%OUT_DIR%"
set "ICONOPT="
if exist "%PROJECT_DIR%assets\app.ico" set "ICONOPT=/DIconFile=%PROJECT_DIR%assets\app.ico"

"%ISCC%" %ICONOPT% "%PROJECT_DIR%setup.iss"
if errorlevel 1 goto :fail

set "SETUP_EXE=%OUT_DIR%\%APP_NAME%-Setup-%APPVER%.exe"
if not exist "%SETUP_EXE%" (
    echo [ERROR] The compiler finished but "%SETUP_EXE%" is missing.
    goto :fail
)

echo.
echo ============================================================
echo   SUCCESS
echo   Installer: %SETUP_EXE%
echo ============================================================
popd
pause
exit /b 0

:fail
echo.
echo ============================================================
echo   BUILD FAILED - see the messages above.
echo ============================================================
popd
pause
exit /b 1
