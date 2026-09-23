@echo off
REM ── TraxJourney development launcher ──────────────────────────────────────
REM Starts the FastAPI server (port 8000) and the Flutter web client (port 5500)
REM in separate terminal windows.

REM ── Activate Python venv ──────────────────────────────────────────────────
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
) else if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
) else (
    echo WARNING: no venv found, using system Python
)

REM ── Find Flutter SDK ──────────────────────────────────────────────────────
set FLUTTER_CMD=flutter
for %%D in (
    "%LOCALAPPDATA%\flutter\bin\flutter.bat"
    "C:\flutter\bin\flutter.bat"
    "C:\src\flutter\bin\flutter.bat"
) do (
    if exist %%D set FLUTTER_CMD=%%D
)

REM ── Start Flutter web client ──────────────────────────────────────────────
echo Starting Flutter web client on port 5500...
start "TraxJourney Flutter" cmd /k "cd flutter_client && %FLUTTER_CMD% run -d chrome --web-port 5500 --dart-define=API_BASE_URL=http://localhost:8000"

echo.
echo  Flutter: http://localhost:5500
echo.
