@echo off
rem ===========================================================================
rem  TradeX - Ein-Klick-Start.
rem
rem      start_tradex.bat              Fenster oeffnen, alles pruefen
rem      start_tradex.bat --server     nur die Engine (Port aus default.yaml)
rem      start_tradex.bat --check      nur pruefen, nichts starten
rem
rem  Unterschied zu TradeX.bat: diese Datei prueft die gesamte Betriebskette
rem  (Umgebung, Daten, Marktdatenbridge, Broker) und verhindert einen zweiten
rem  gleichzeitigen Start. TradeX.bat startet nur die Oberflaeche.
rem
rem  Das Konsolenfenster bleibt sichtbar: dort laeuft das Protokoll. Ein
rem  stiller Start ueber pythonw.exe wuerde genau die Meldungen verschlucken,
rem  die man bei einem Problem braucht.
rem ===========================================================================

setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
set "LOCK=%TEMP%\tradex-%COMPUTERNAME%.lock"

rem --- 1. Doppelstart verhindern --------------------------------------------
rem  Zwei Sitzungen haetten getrennte Risikobuecher und zusammen das doppelte
rem  erlaubte Risiko. Die Sperre haengt an einer Datei, die offen GEHALTEN
rem  wird: ein abgestuerzter Vorgaenger gibt sie beim Prozessende frei, ein
rem  blosser Existenztest wuerde dagegen nach jedem Absturz blockieren.
rem  Im gesperrten Block wird NICHT gewartet - kein `pause`, keine Eingabe.
rem  Sonst haelt ein Fenster, das auf einen Tastendruck wartet, die Sperre
rem  unbegrenzt: nach einem Absturz der Engine bliebe TradeX gesperrt, bis
rem  jemand das unsichtbare Fenster findet. Genau das ist beim Testen
rem  passiert. Gewartet wird erst, nachdem die Sperre wieder frei ist.
set "ERGEBNIS=0"
2>nul (
    9>"%LOCK%" (
        call :hauptlauf %*
        set "ERGEBNIS=!ERRORLEVEL!"
    )
) || (
    echo.
    echo   TradeX laeuft bereits.
    echo.
    echo   Zwei gleichzeitige Sitzungen haetten getrennte Risikobuecher und
    echo   zusammen das doppelte erlaubte Risiko. Erst die laufende beenden.
    echo.
    endlocal
    exit /b 1
)

if not "%ERGEBNIS%"=="0" (
    echo.
    echo   Start abgebrochen ^(Rueckgabewert %ERGEBNIS%^).
    echo.
    if not defined TRADEX_NO_PAUSE pause
)
endlocal & exit /b %ERGEBNIS%


rem ===========================================================================
:hauptlauf
rem --- 2. Virtuelle Umgebung ------------------------------------------------
if not exist "%PY%" (
    echo.
    echo   Die virtuelle Umgebung fehlt: %CD%\.venv
    echo.
    echo   Einmalig anlegen:
    echo       python -m venv .venv
    echo       .venv\Scripts\pip install -e ".[dev]"
    echo.
    goto :fehler
)

"%PY%" -c "import tradex" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Das Paket ist in der virtuellen Umgebung nicht installiert.
    echo       .venv\Scripts\pip install -e ".[dev]"
    echo.
    goto :fehler
)

rem --- 3. Oberflaeche bauen, falls noetig ------------------------------------
rem  Zur Laufzeit wird kein Node gebraucht, zum Bauen schon.
if exist "ui\dist\index.html" goto :pruefen

echo.
echo   Die Oberflaeche ist noch nicht gebaut - das passiert jetzt einmalig.
echo   Beim ersten Mal dauert es einige Minuten.
echo.
where npm >nul 2>&1
if errorlevel 1 (
    echo   Dafuer wird Node.js gebraucht: https://nodejs.org
    echo   Danach im Ordner ui\ ausfuehren:  npm install ^&^& npm run build
    goto :fehler
)
pushd ui
if not exist "node_modules" call npm install
call npm run build
popd
if not exist "ui\dist\index.html" (
    echo   Der Build hat keine ui\dist\index.html erzeugt - Ausgabe oben pruefen.
    goto :fehler
)

rem --- 4. Vorpruefung: Daten, Marktdaten, Broker ------------------------------
:pruefen
echo.
echo   ============================================================
echo     TradeX - Startpruefung
echo   ============================================================
"%PY%" scripts\preflight.py --stage pre
if errorlevel 1 goto :fehler

rem  --check hoert hier auf: nachsehen, ohne etwas zu starten.
if /i "%~1"=="--check" (
    echo   Nur Pruefung verlangt - es wurde nichts gestartet.
    exit /b 0
)

rem --- 5. Engine und Oberflaeche starten -------------------------------------
rem  Beides in EINEM Vorgang: `tradex.shell` fuehrt uvicorn in einem
rem  Hintergrundfaden und oeffnet das Fenster davor. Zwei getrennte Vorgaenge
rem  waeren zwei Dinge, die einzeln haengen bleiben koennen.
echo   ============================================================
echo     TradeX startet
echo   ============================================================
echo.
"%PY%" -m tradex.shell %*
set "ERGEBNIS=%ERRORLEVEL%"

rem --- 6. Nachpruefung -------------------------------------------------------
rem  Nach einem regulaeren Ende ist die Engine weg - das ist kein Fehler.
rem  Nach einem Absturz steht hier, was noch lief.
if not "%ERGEBNIS%"=="0" (
    echo.
    echo   TradeX endete mit Rueckgabewert %ERGEBNIS%. Zustand danach:
    "%PY%" scripts\preflight.py --stage post
    goto :fehler
)
exit /b 0

:fehler
rem  Kein `pause` hier: der Aufrufer wartet, sobald die Sperre frei ist.
exit /b 1
