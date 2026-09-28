@echo off
rem Builds TradingBots.exe from tradingbots_launcher.py and installs it to
rem %LOCALAPPDATA%\Programs\TradingBots, with a desktop shortcut.
rem Run it again after pulling launcher changes (close the app first).
setlocal

set "HERE=%~dp0"
set "BASE=%LOCALAPPDATA%\TradingBots"
set "BUILD_ENV=%BASE%\build-env"
set "DEST=%LOCALAPPDATA%\Programs\TradingBots"

if not exist "%BUILD_ENV%\Scripts\python.exe" (
    echo Creating the build environment in %BUILD_ENV% ...
    py -3 -m venv "%BUILD_ENV%" 2>nul || python -m venv "%BUILD_ENV%" || goto :failed
)
"%BUILD_ENV%\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r "%HERE%requirements.txt" || goto :failed

"%BUILD_ENV%\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name TradingBots --icon "%HERE%icon.ico" --add-data "%HERE%icon.ico;." ^
    --distpath "%DEST%" --workpath "%BASE%\build" --specpath "%BASE%\build" ^
    "%HERE%tradingbots_launcher.py" || goto :failed

powershell -NoProfile -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\Trading Bots.lnk'); $s.TargetPath = '%DEST%\TradingBots.exe'; $s.IconLocation = '%DEST%\TradingBots.exe'; $s.Description = 'Start, stop and configure the trading bots'; $s.Save()"

echo.
echo Built %DEST%\TradingBots.exe - there's a "Trading Bots" shortcut on the desktop.
exit /b 0

:failed
echo.
echo The build failed - see the messages above.
exit /b 1