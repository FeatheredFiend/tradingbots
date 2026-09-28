@echo off
rem Starts one trading bot as a new tab in the "TradingBots" Windows Terminal
rem window, which opens with the first bot (or in a console window of its
rem own, if Windows Terminal isn't installed):
rem
rem     start_bot.bat oanda-momentum-scanner
rem
rem The tab gets this script's environment, so the bot sees the settings the
rem TradingBots launcher passes it. Closing a tab stops that bot; closing the
rem whole window stops them all.
rem
rem Each bot runs on its broker's Python environment. The defaults are the
rem ones in your user folder; override them with TRADINGBOTS_ALPACA_ENV,
rem TRADINGBOTS_OANDA_ENV, TRADINGBOTS_PEPPERSTONE_ENV, TRADINGBOTS_CAPITAL_ENV
rem or TRADINGBOTS_IG_ENV (the TradingBots launcher sets these from its Paths tab).
rem
rem The tab closes when the bot stops cleanly (Ctrl+C) and stays open on an
rem error, so the message can be read.
setlocal

set "REPO=%~dp0.."
if not defined TRADINGBOTS_ALPACA_ENV set "TRADINGBOTS_ALPACA_ENV=%USERPROFILE%\alpaca-bot-env"
if not defined TRADINGBOTS_OANDA_ENV set "TRADINGBOTS_OANDA_ENV=%USERPROFILE%\ig-bot-env"
if not defined TRADINGBOTS_PEPPERSTONE_ENV set "TRADINGBOTS_PEPPERSTONE_ENV=%USERPROFILE%\pepperstone-bot-env"
if not defined TRADINGBOTS_CAPITAL_ENV set "TRADINGBOTS_CAPITAL_ENV=%USERPROFILE%\ig-bot-env"
if not defined TRADINGBOTS_IG_ENV set "TRADINGBOTS_IG_ENV=%USERPROFILE%\ig-bot-env"

set "BOT=%~1"
set "SCRIPT="
if /i "%BOT%"=="alpaca-momentum-scanner" (set "SCRIPT=alpaca-momentum-scanner-bot\alpaca_momentum_scanner_bot.py" & set "VENV=%TRADINGBOTS_ALPACA_ENV%")
if /i "%BOT%"=="alpaca-ema-bot" (set "SCRIPT=alpaca-ema-bot\alpaca_ema_bot.py" & set "VENV=%TRADINGBOTS_ALPACA_ENV%")
if /i "%BOT%"=="oanda-momentum-scanner" (set "SCRIPT=oanda-momentum-scanner-bot\oanda_momentum_scanner_bot.py" & set "VENV=%TRADINGBOTS_OANDA_ENV%")
if /i "%BOT%"=="oanda-ema-bot" (set "SCRIPT=oanda-ema-bot\oanda_ema_bot.py" & set "VENV=%TRADINGBOTS_OANDA_ENV%")
if /i "%BOT%"=="pepperstone-momentum-scanner" (set "SCRIPT=pepperstone-momentum-scanner-bot\pepperstone_momentum_scanner_bot.py" & set "VENV=%TRADINGBOTS_PEPPERSTONE_ENV%")
if /i "%BOT%"=="pepperstone-ema-bot" (set "SCRIPT=pepperstone-ema-bot\pepperstone_ema_bot.py" & set "VENV=%TRADINGBOTS_PEPPERSTONE_ENV%")
if /i "%BOT%"=="capital-momentum-scanner" (set "SCRIPT=capital-momentum-scanner-bot\capital_momentum_scanner_bot.py" & set "VENV=%TRADINGBOTS_CAPITAL_ENV%")
if /i "%BOT%"=="capital-ema-bot" (set "SCRIPT=capital-ema-bot\capital_ema_bot.py" & set "VENV=%TRADINGBOTS_CAPITAL_ENV%")
if /i "%BOT%"=="ig-momentum-scanner" (set "SCRIPT=ig-momentum-scanner-bot\ig_momentum_scanner_bot.py" & set "VENV=%TRADINGBOTS_IG_ENV%")
if /i "%BOT%"=="ig-ema-bot" (set "SCRIPT=ig-cfd-ema-bot\ig_cfd_ema_bot.py" & set "VENV=%TRADINGBOTS_IG_ENV%")

if not defined SCRIPT (
    echo Unknown bot "%BOT%". Choose one of:
    echo   alpaca-momentum-scanner  alpaca-ema-bot
    echo   oanda-momentum-scanner   oanda-ema-bot
    echo   pepperstone-momentum-scanner  pepperstone-ema-bot
    echo   capital-momentum-scanner  capital-ema-bot
    echo   ig-momentum-scanner      ig-ema-bot
    exit /b 2
)
if not exist "%VENV%\Scripts\python.exe" (
    echo No Python environment at "%VENV%" for %BOT%.
    exit /b 3
)

where wt.exe >nul 2>nul || goto :own_window
wt.exe -w TradingBots new-tab --title "%BOT%" --suppressApplicationTitle cmd /c ""%VENV%\Scripts\python.exe" "%REPO%\%SCRIPT%" || pause"
exit /b 0

:own_window
start "Trading bot: %BOT%" cmd /c ""%VENV%\Scripts\python.exe" "%REPO%\%SCRIPT%" || pause"
