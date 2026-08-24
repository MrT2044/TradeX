@echo off
rem ===========================================================================
rem  TradeX starten - leitet auf start_tradex.bat weiter.
rem
rem  Frueher stand hier ein zweiter, eigener Startweg. Der war eine Teilmenge
rem  von start_tradex.bat, aber OHNE die Doppelstartsperre: wer versehentlich
rem  diese Datei nahm, waehrend TradeX schon lief, bekam eine zweite Engine
rem  mit einem zweiten Risikobuch - und damit zusammen das doppelte erlaubte
rem  Risiko. Genau dagegen ist die Sperre da.
rem
rem  Zwei Startwege bedeuten immer, dass einer davon die Pruefungen nicht hat.
rem  Es gibt deshalb nur noch einen. Diese Datei bleibt bestehen, damit
rem  vorhandene Verknuepfungen weiter funktionieren.
rem
rem      TradeX.bat              Fenster oeffnen
rem      TradeX.bat --server     nur die Engine
rem      TradeX.bat --check      nur pruefen, nichts starten
rem ===========================================================================

cd /d "%~dp0"
call "%~dp0start_tradex.bat" %*
exit /b %ERRORLEVEL%
