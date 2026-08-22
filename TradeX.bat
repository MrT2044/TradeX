@echo off
rem ===========================================================================
rem  TradeX starten (Doppelklick oder Aufruf aus der Eingabeaufforderung).
rem
rem  Ohne Argument oeffnet sich das Desktop-Fenster. Argumente werden an
rem  `python -m tradex.shell` durchgereicht:
rem
rem      TradeX.bat              Fenster oeffnen
rem      TradeX.bat --server     nur die Engine, Port 8765
rem
rem  Das Konsolenfenster bleibt bewusst sichtbar: dort laeuft das Protokoll
rem  der Engine. Ein stiller Start ueber pythonw.exe wuerde genau die
rem  Meldungen verschlucken, die man bei einem Problem braucht.
rem ===========================================================================

setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"

rem --- 1. Virtuelle Umgebung ------------------------------------------------
if not exist "%PY%" (
    echo.
    echo   Die virtuelle Umgebung fehlt: %CD%\.venv
    echo.
    echo   Einmalig anlegen:
    echo.
    echo       python -m venv .venv
    echo       .venv\Scripts\activate
    echo       pip install -e ".[dev]"
    echo.
    goto :fehler
)

rem --- 2. Paket installiert? ------------------------------------------------
rem Ohne diese Pruefung endet ein unvollstaendiges Setup in einem rohen
rem ModuleNotFoundError statt in einem brauchbaren Hinweis.
"%PY%" -c "import tradex" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Das Paket ist in der virtuellen Umgebung nicht installiert.
    echo.
    echo   Nachholen:
    echo.
    echo       .venv\Scripts\pip install -e ".[dev]"
    echo.
    goto :fehler
)

rem --- 3. Oberflaeche gebaut? -----------------------------------------------
rem Die Shell liefert fertige Dateien aus ui\dist aus - zur Laufzeit wird kein
rem Node gebraucht, zum Bauen schon. Beim ersten Start passiert das hier.
if exist "ui\dist\index.html" goto :starten

echo.
echo   Die Oberflaeche ist noch nicht gebaut - das passiert jetzt einmalig.
echo   Beim ersten Mal dauert es einige Minuten.
echo.

where npm >nul 2>&1
if errorlevel 1 (
    echo   Dafuer wird Node.js gebraucht: https://nodejs.org
    echo   Danach im Ordner ui\ ausfuehren:  npm install ^&^& npm run build
    echo.
    goto :fehler
)

pushd ui
if not exist "node_modules" call npm install
call npm run build
popd

if not exist "ui\dist\index.html" (
    echo.
    echo   Der Build hat keine ui\dist\index.html erzeugt - Ausgabe oben pruefen.
    echo.
    goto :fehler
)

rem --- 4. Start -------------------------------------------------------------
:starten
"%PY%" -m tradex.shell %*
if errorlevel 1 goto :fehler

endlocal
exit /b 0

:fehler
echo.
echo   Start abgebrochen.
echo.
pause
endlocal
exit /b 1
