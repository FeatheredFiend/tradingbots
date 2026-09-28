@echo off
rem Starts one trading bot in its own console window:
rem
rem     start_bot.bat oanda-momentum-scanner
rem
rem Each bot runs on its broker's Python environment. The defaults are the
rem ones in your user folder; override them with TRADINGBOTS_ALPACA_ENV,
rem TRADINGBOTS_OANDA_ENV, TRADINGBOTS_PEPPERSTONE_ENV or TRADINGBOTS_IG_ENV
rem (the TradingBots launcher sets these from its Paths tab).
rem
rem The window closes when the bot stops cleanly (Ctrl+C) and stays open
rem on an error, so the message can be read.
setlocal

set "REPO=%~dp0.."
if not defined TRADINGBOTS_ALPACA_ENV set "TRADINGBOTS_ALPACA_ENV=%USERPROFILE%\alpaca-bot-env"
if not defined TRADINGBOTS_OANDA_ENV set "TRADINGBOTS_OANDA_ENV=%USERPROFILE%\ig-bot-env"
if not defined TRADINGBOTS_PEPPERSTONE_ENV set "TRADINGBOTS_PEPPERSTONE_ENV=%USERPROFILE%\pepperstone-bot-env"
if not defined TRADINGBOTS_IG_ENV set "TRADINGBOTS_IG_ENV=%USERPROFILE%\ig-bot-env"

set "BOT=%~1"
set "SCRIPT="
if /i "%BOT%"=="alpaca-momentum-scanner" (set "SCRIPT=alpaca-momentum-scanner-bot\alpaca_momentum_scanner_bot.py" & set "VENV=%TRADINGBOTS_ALPACA_ENV%")
if /i "%BOT%"=="alpaca-ema-bot" (set "SCRIPT=alpaca-ema-bot\alpaca_ema_bot.py" & set "VENV=%TRADINGBOTS_ALPACA_ENV%")
if /i "%BOT%"=="oanda-momentum-scanner" (set "SCRIPT=oanda-momentum-scanner-bot\oanda_momentum_scanner_bot.py" & set "VENV=%TRADINGBOTS_OANDA_ENV%")
if /i "%BOT%"=="oanda-ema-bot" (set "SCRIPT=oanda-ema-bot\oanda_ema_bot.py" & set "VENV=%TRADINGBOTS_OANDA_ENV%")
if /i "%BOT%"=="pepperstone-momentum-scanner" (set "SCRIPT=pepperstone-momentum-scanner-bot\pepperstone_momentum_scanner_bot.py" & set "VENV=%TRADINGBOTS_PEPPERSTONE_ENV%")
if /i "%BOT%"=="pepperstone-ema-bot" (set "SCRIPT=pepperstone-ema-bot\pepperstone_ema_bot.py" & set "VENV=%TRADINGBOTS_PEPPERSTONE_ENV%")
if /i "%BOT%"=="ig-momentum-scanner" (set "SCRIPT=ig-momentum-scanner-bot\ig_momentum_scanner_bot.py" & set "VENV=%TRADINGBOTS_IG_ENV%")
if /i "%BOT%"=="ig-ema-bot" (set "SCRIPT=ig-cfd-ema-bot\ig_cfd_ema_bot.py" & set "VENV=%TRADINGBOTS_IG_ENV%")

if not defined SCRIPT (
    echo Unknown bot "%BOT%". Choose one of:
    echo   alpaca-momentum-scanner  alpaca-ema-bot
    echo   oanda-momentum-scanner   oanda-ema-bot
    echo   pepperstone-momentum-scanner  pepperstone-ema-bot
    echo   ig-momentum-scanner      ig-ema-bot
    exit /b 2
)
if not exist "%VENV%\Scripts\python.exe" (
    echo No Python environment at "%VENV%" for %BOT%.
    exit /b 3
)

start "Trading bot: %BOT%" cmd /c ""%VENV%\Scripts\python.exe" "%REPO%\%SCRIPT%" || pause"
