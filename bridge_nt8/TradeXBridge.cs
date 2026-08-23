#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
#endregion

// ===========================================================================
//  TradeXBridge - NinjaScript-AddOn: schickt geschlossene Bars an TradeX
// ===========================================================================
//
//  EINBAUEN
//    1. Diese Datei nach
//         %USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\AddOns\
//       kopieren. NinjaTrader liest AddOns von dort - Copy-Paste im Editor
//       ist unnoetig.
//    2. NinjaTrader NEU STARTEN. Eine Datei, die waehrend des Betriebs
//       dazukommt, taucht im NinjaScript Explorer nicht auf: der Ordner wird
//       beim Start eingelesen.
//    3. New -> NinjaScript Editor -> AddOns -> TradeXBridge -> F5.
//       Beim ersten Mal fragt NinjaTrader, ob das AddOn autorisiert werden
//       soll ("detected new add on(s)") -> Yes.
//    4. Die Bridge lauscht danach auf 127.0.0.1:39473.
//    5. In TradeX:  python scripts/run_paper.py --symbol MNQ --feed nt8
//
//  Protokoll: siehe README.md in diesem Verzeichnis. Zeilenweises JSON,
//  UTF-8, eine Nachricht je Zeile.
//
//  ---------------------------------------------------------------------
//  DREI ENTSCHEIDUNGEN, DIE HIER NICHT VERHANDELBAR SIND
//  ---------------------------------------------------------------------
//
//  1. NUR GESCHLOSSENE BARS.
//     `BarsRequest` liefert Aktualisierungen auch fuer die laufende Bar. Diese
//     Bridge sendet ausschliesslich Bars, deren Nachfolger bereits begonnen
//     hat. Das ist Architektur-Invariante 1 von TradeX: analysiert wird nur
//     auf geschlossenen Bars. Wuerde hier die laufende Bar hinausgehen, saehe
//     die Engine live einen Zustand, den sie im Backtest nie sieht - und jede
//     Backtest-Aussage waere hinfaellig.
//
//  2. NUR LOOPBACK.
//     Der Listener bindet an IPAddress.Loopback, nie an Any. Ein Marktdaten-
//     socket, der nach aussen offen steht, ist ein Einfallstor - und
//     ausserhalb dieses Rechners hat niemand ein berechtigtes Interesse daran.
//
//  3. KEINE ORDERS ueber diesen Weg.
//     Diese Bridge kann Daten senden und Abonnements empfangen. Sie kennt
//     keine Order-Schnittstelle. Order-Routing laeuft ueber die ATI und ist
//     Phase 8/9 - ein Fehler im Bar-Streaming kann so keine Order ausloesen.
// ===========================================================================

namespace NinjaTrader.NinjaScript.AddOns
{
    public class TradeXBridge : AddOnBase
    {
        // NICHT 36973: das ist NinjaTraders eigener ATI-Port. Er ist belegt,
        // sobald NinjaTrader laeuft - der Listener kaeme gar nicht hoch.
        // Nachgemessen an einer laufenden Installation 8.1.8.2.
        private const int Port = 39473;
        private const int HeartbeatSeconds = 5;

        private TcpListener listener;
        private Thread acceptThread;
        private Thread heartbeatThread;
        private volatile bool running;

        private readonly List<TcpClient> clients = new List<TcpClient>();
        private readonly object clientLock = new object();

        // Je Abonnement eine offene BarsRequest. Der Schluessel ist
        // "SYMBOL|timeframe", damit dasselbe Instrument auf zwei Zeitebenen
        // nicht zweimal angefordert wird.
        private readonly Dictionary<string, BarsRequest> requests =
            new Dictionary<string, BarsRequest>();
        private readonly object requestLock = new object();

        // Zuletzt GESENDETE Bar je Abonnement. Grundlage fuer Punkt 1 oben:
        // gesendet wird eine Bar erst, wenn die naechste begonnen hat.
        private readonly Dictionary<string, int> lastSentIndex = new Dictionary<string, int>();

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "TradeXBridge";
            }
            else if (State == State.Configure)
            {
                StartServer();
            }
            else if (State == State.Terminated)
            {
                StopServer();
            }
        }

        // ------------------------------------------------------------- Server
        private void StartServer()
        {
            if (running) return;
            running = true;

            // Loopback, nicht Any - siehe Entscheidung 2 oben.
            listener = new TcpListener(IPAddress.Loopback, Port);
            listener.Start();

            acceptThread = new Thread(AcceptLoop) { IsBackground = true, Name = "tradex-accept" };
            acceptThread.Start();

            heartbeatThread = new Thread(HeartbeatLoop) { IsBackground = true, Name = "tradex-beat" };
            heartbeatThread.Start();

            NinjaTrader.Code.Output.Process(
                "TradeXBridge lauscht auf 127.0.0.1:" + Port, PrintTo.OutputTab1);
        }

        private void StopServer()
        {
            running = false;
            try { if (listener != null) listener.Stop(); } catch { }

            lock (requestLock)
            {
                foreach (var request in requests.Values)
                {
                    try { request.Dispose(); } catch { }
                }
                requests.Clear();
                lastSentIndex.Clear();
            }

            lock (clientLock)
            {
                foreach (var client in clients)
                {
                    try { client.Close(); } catch { }
                }
                clients.Clear();
            }
        }

        private void AcceptLoop()
        {
            while (running)
            {
                try
                {
                    TcpClient client = listener.AcceptTcpClient();
                    lock (clientLock) { clients.Add(client); }

                    // Bewusst ohne Auskunft ueber den Datenfeed von NinjaTrader:
                    // dieser Socket sagt nur, dass DIE BRIDGE steht. Ob Daten
                    // fliessen, sieht TradeX daran, ob Bars ankommen - eine
                    // zweite, moeglicherweise widerspruechliche Quelle dafuer
                    // waere schlechter als keine.
                    Send(client, "{\"type\":\"status\",\"connected\":true,"
                        + "\"data_feed\":\"unknown\",\"detail\":\"TradeXBridge\"}");

                    var reader = new Thread(() => ReadLoop(client))
                    {
                        IsBackground = true,
                        Name = "tradex-read"
                    };
                    reader.Start();
                }
                catch
                {
                    // listener.Stop() beim Herunterfahren landet hier. Kein
                    // Fehlerfall, der irgendwo gemeldet werden muesste.
                    if (!running) return;
                }
            }
        }

        private void ReadLoop(TcpClient client)
        {
            var buffer = new byte[8192];
            var pending = new StringBuilder();
            try
            {
                NetworkStream stream = client.GetStream();
                while (running && client.Connected)
                {
                    int read = stream.Read(buffer, 0, buffer.Length);
                    if (read <= 0) break;
                    pending.Append(Encoding.UTF8.GetString(buffer, 0, read));

                    // TCP kennt keine Zeilen, nur Bytes: es kann eine halbe
                    // Nachricht ankommen oder drei auf einmal.
                    string all = pending.ToString();
                    int newline;
                    while ((newline = all.IndexOf('\n')) >= 0)
                    {
                        string line = all.Substring(0, newline).Trim();
                        all = all.Substring(newline + 1);
                        if (line.Length > 0) HandleCommand(line);
                    }
                    pending.Clear();
                    pending.Append(all);
                }
            }
            catch { }
            finally
            {
                lock (clientLock) { clients.Remove(client); }
                try { client.Close(); } catch { }
            }
        }

        // ------------------------------------------------------------ Befehle
        private void HandleCommand(string line)
        {
            // Bewusst eine Handvoll String-Suchen statt eines JSON-Parsers:
            // NinjaScript bringt keinen mit, und die drei Befehle haben eine
            // feste Form. Ein unvollstaendiger Eigenbau-Parser waere die
            // schlechtere Wahl als gar keiner.
            string type = ExtractString(line, "type");
            if (type != "subscribe" && type != "unsubscribe" && type != "history") return;

            string symbol = ExtractString(line, "symbol");
            string timeframe = ExtractString(line, "timeframe");
            if (symbol.Length == 0) return;
            if (timeframe.Length == 0) timeframe = "1m";

            if (type == "subscribe") Subscribe(symbol, timeframe);
            else if (type == "unsubscribe") Unsubscribe(symbol, timeframe);
            else History(symbol, timeframe, ExtractLong(line, "from"), ExtractLong(line, "to"));
        }

        // ------------------------------------------------------------ Historie
        private void History(string symbol, string timeframe, long fromNs, long toNs)
        {
            // Warum es diesen Befehl gibt: der laufende Betrieb braucht ihn
            // nicht - TradeX haelt seine Historie selbst. Aber ohne ihn laesst
            // sich die Bar-Uebertragung nur bei GEOEFFNETER Boerse pruefen.
            // Mit ihm ist sie jederzeit gegen echte Daten pruefbar, und genau
            // das ist der Unterschied zwischen "sollte gehen" und "geht".
            Instrument instrument = ResolveInstrument(symbol);
            int minutes = MinutesOf(timeframe);
            if (instrument == null || minutes <= 0)
            {
                Broadcast("{\"type\":\"history_end\",\"symbol\":\"" + Escape(symbol)
                    + "\",\"timeframe\":\"" + Escape(timeframe) + "\",\"count\":0,"
                    + "\"detail\":\"Instrument oder Zeitebene unbekannt\"}");
                return;
            }

            var request = new BarsRequest(instrument, FromEpochNanos(fromNs), FromEpochNanos(toNs))
            {
                BarsPeriod = new BarsPeriod
                {
                    BarsPeriodType = BarsPeriodType.Minute,
                    Value = minutes
                },
                TradingHours = instrument.MasterInstrument.TradingHours
            };

            request.Request((completed, error, message) =>
            {
                int sent = 0;
                if (error == ErrorCode.NoError && completed.Bars != null)
                {
                    // ALLE Bars dieses Bereichs gelten als geschlossen: der
                    // Bereich liegt in der Vergangenheit. Die Regel "nur
                    // geschlossene Bars" ist damit nicht aufgeweicht.
                    for (int index = 0; index < completed.Bars.Count; index++)
                    {
                        SendBar(symbol, timeframe, completed.Bars, index);
                        sent++;
                    }
                }
                Broadcast("{\"type\":\"history_end\",\"symbol\":\"" + Escape(symbol)
                    + "\",\"timeframe\":\"" + Escape(timeframe)
                    + "\",\"count\":" + sent.ToString(CultureInfo.InvariantCulture)
                    + ",\"detail\":\"" + Escape(error == ErrorCode.NoError ? "" : message) + "\"}");
                try { request.Dispose(); } catch { }
            });
        }

        private void Subscribe(string symbol, string timeframe)
        {
            string key = symbol.ToUpperInvariant() + "|" + timeframe;
            lock (requestLock)
            {
                if (requests.ContainsKey(key)) return;
            }

            Instrument instrument = ResolveInstrument(symbol);
            if (instrument == null)
            {
                Broadcast("{\"type\":\"status\",\"connected\":false,\"data_feed\":\"unknown\","
                    + "\"detail\":\"Unbekanntes Instrument " + Escape(symbol) + "\"}");
                return;
            }

            int minutes = MinutesOf(timeframe);
            if (minutes <= 0)
            {
                Broadcast("{\"type\":\"status\",\"connected\":false,\"data_feed\":\"unknown\","
                    + "\"detail\":\"Nicht unterstuetzte Zeitebene " + Escape(timeframe) + "\"}");
                return;
            }

            var request = new BarsRequest(instrument, DateTime.Now.AddDays(-5), DateTime.MaxValue)
            {
                BarsPeriod = new BarsPeriod
                {
                    BarsPeriodType = BarsPeriodType.Minute,
                    Value = minutes
                },
                TradingHours = instrument.MasterInstrument.TradingHours
            };

            // `args.BarsSeries` ist NICHT vom Typ `Bars` - gearbeitet wird
            // deshalb mit `request.Bars`, das die richtige Sicht liefert.
            request.Update += (sender, args) => OnBarsUpdate(key, symbol, timeframe, request);
            request.Request((completed, error, message) =>
            {
                if (error != ErrorCode.NoError)
                {
                    Broadcast("{\"type\":\"status\",\"connected\":false,\"data_feed\":\"error\","
                        + "\"detail\":\"" + Escape(message) + "\"}");
                    return;
                }
                lock (requestLock)
                {
                    requests[key] = request;
                    // Beim Start NICHT die ganze Historie senden: TradeX holt
                    // Historie ueber seinen eigenen Datenbestand. Hier zaehlt
                    // nur, was ab jetzt schliesst.
                    lastSentIndex[key] = completed.Bars == null ? -1 : completed.Bars.Count - 1;
                }
                NinjaTrader.Code.Output.Process(
                    "TradeXBridge abonniert " + key, PrintTo.OutputTab1);
            });
        }

        // ------------------------------------------------- Instrument finden
        private Instrument ResolveInstrument(string symbol)
        {
            // Drei Stufen, und die dritte ist die entscheidende.
            //
            // TradeX kennt nur das Wurzelsymbol "MNQ" und will es ueber
            // Kontraktwechsel hinweg durchgehend handeln. An einer laufenden
            // Installation gemessen:
            //
            //   GetInstrument("MNQ")       -> null
            //   GetInstrumentFuzzy("MNQ")  -> ein GENERISCHER Eintrag "MNQ",
            //                                 den der Datenanbieter ablehnt:
            //                                 "Symbol is inaccessible /
            //                                  UnknownSymbol"
            //
            // Gebraucht wird der zum HEUTIGEN Datum laufende Kontrakt, also
            // "MNQ SEP26". `GetInstrumentByDate` liefert genau den - und rollt
            // damit automatisch mit, ohne dass TradeX etwas davon wissen muss.
            // Welcher Kontrakt es geworden ist, steht in jeder Bar im Feld
            // `contract`; daraus macht die Python-Seite `roll_boundary`.
            // 1. Exakter Name ("MNQ SEP26") - dann ist nichts zu raten.
            Instrument exact = Instrument.GetInstrument(symbol);
            if (exact != null && exact.Expiry > DateTime.Now.Date) return exact;

            // 2. Frontmonat selbst suchen: der naechste noch nicht abgelaufene
            //    Kontrakt desselben Basiswerts. Bewusst ueber `Instrument.All`
            //    statt ueber die Rollover-Logik von NinjaTrader - gemessen an
            //    einer laufenden Installation liefert `GetInstrumentByDate`
            //    fuer ein Wurzelsymbol denselben generischen Eintrag zurueck,
            //    und der Datenanbieter lehnt den ab ("Symbol is inaccessible").
            Instrument front = null;
            foreach (Instrument candidate in Instrument.All)
            {
                if (candidate == null || candidate.MasterInstrument == null) continue;
                if (!string.Equals(candidate.MasterInstrument.Name, symbol,
                        StringComparison.OrdinalIgnoreCase)) continue;
                if (candidate.Expiry <= DateTime.Now.Date) continue;
                if (front == null || candidate.Expiry < front.Expiry) front = candidate;
            }
            if (front != null) return front;

            // 3. Alles andere (Aktien, Devisen): kein Verfall, kein Frontmonat.
            return exact ?? Instrument.GetInstrumentFuzzy(symbol);
        }

        private void Unsubscribe(string symbol, string timeframe)
        {
            string key = symbol.ToUpperInvariant() + "|" + timeframe;
            lock (requestLock)
            {
                BarsRequest request;
                if (!requests.TryGetValue(key, out request)) return;
                try { request.Dispose(); } catch { }
                requests.Remove(key);
                lastSentIndex.Remove(key);
            }
        }

        // ---------------------------------------------------------------- Bars
        private void OnBarsUpdate(string key, string symbol, string timeframe, BarsRequest request)
        {
            Bars bars = request.Bars;
            if (bars == null || bars.Count < 2) return;

            int lastClosed = bars.Count - 2;   // die letzte Bar laeuft noch
            int alreadySent;
            lock (requestLock)
            {
                if (!lastSentIndex.TryGetValue(key, out alreadySent)) return;
                if (lastClosed <= alreadySent) return;
                lastSentIndex[key] = lastClosed;
            }

            // Es koennen mehrere Bars auf einmal fertig werden, etwa nach
            // einer Verbindungsunterbrechung. Alle senden, in Reihenfolge -
            // eine Luecke waere fuer die Engine ein stiller Datenverlust.
            for (int index = alreadySent + 1; index <= lastClosed; index++)
            {
                SendBar(symbol, timeframe, bars, index);
            }
        }

        private void SendBar(string symbol, string timeframe, Bars bars, int index)
        {
            // NinjaTrader stempelt eine Minutenbar auf ihr ENDE, TradeX auf
            // ihren Beginn. Ohne diese Verschiebung laege jede Bar um ihre
            // eigene Dauer in der Zukunft - und jeder Vergleich mit dem
            // historischen Bestand waere um eine Bar verschoben.
            DateTime end = bars.GetTime(index);
            DateTime start = end.AddMinutes(-bars.BarsPeriod.Value);
            long ts = ToEpochNanos(start);

            string json = "{\"type\":\"bar\""
                + ",\"symbol\":\"" + Escape(symbol.ToUpperInvariant()) + "\""
                + ",\"timeframe\":\"" + Escape(timeframe) + "\""
                + ",\"ts\":" + ts.ToString(CultureInfo.InvariantCulture)
                + ",\"open\":" + Num(bars.GetOpen(index))
                + ",\"high\":" + Num(bars.GetHigh(index))
                + ",\"low\":" + Num(bars.GetLow(index))
                + ",\"close\":" + Num(bars.GetClose(index))
                + ",\"volume\":" + Num(bars.GetVolume(index))
                + ",\"contract\":\"" + Escape(bars.Instrument.FullName) + "\"}";
            Broadcast(json);
        }

        // ----------------------------------------------------------- Herzschlag
        private void HeartbeatLoop()
        {
            while (running)
            {
                Thread.Sleep(HeartbeatSeconds * 1000);
                if (!running) break;
                // Ein Feed, der schweigt, ist von einem ruhigen Markt nicht zu
                // unterscheiden. Der Herzschlag ist die einzige Auskunft, die
                // TradeX zwischen zwei Bars ueber die Verbindung bekommt.
                Broadcast("{\"type\":\"heartbeat\",\"ts\":"
                    + ToEpochNanos(DateTime.UtcNow).ToString(CultureInfo.InvariantCulture) + "}");
            }
        }

        // -------------------------------------------------------------- Senden
        private void Broadcast(string json)
        {
            List<TcpClient> snapshot;
            lock (clientLock) { snapshot = new List<TcpClient>(clients); }
            foreach (var client in snapshot) Send(client, json);
        }

        private void Send(TcpClient client, string json)
        {
            try
            {
                byte[] payload = Encoding.UTF8.GetBytes(json + "\n");
                client.GetStream().Write(payload, 0, payload.Length);
            }
            catch
            {
                lock (clientLock) { clients.Remove(client); }
                try { client.Close(); } catch { }
            }
        }

        // ------------------------------------------------------------ Werkzeug
        private static DateTime FromEpochNanos(long nanos)
        {
            if (nanos <= 0) return DateTime.Now.AddDays(-5);
            var epoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);
            return epoch.AddMilliseconds(nanos / 1000000L).ToLocalTime();
        }

        private static long ExtractLong(string json, string field)
        {
            string needle = "\"" + field + "\"";
            int at = json.IndexOf(needle, StringComparison.Ordinal);
            if (at < 0) return 0;
            int colon = json.IndexOf(':', at + needle.Length);
            if (colon < 0) return 0;
            int start = colon + 1;
            while (start < json.Length && (json[start] == ' ' || json[start] == '"')) start++;
            int end = start;
            while (end < json.Length && (char.IsDigit(json[end]) || json[end] == '-')) end++;
            long value;
            return long.TryParse(json.Substring(start, end - start),
                NumberStyles.Integer, CultureInfo.InvariantCulture, out value) ? value : 0;
        }

        private static long ToEpochNanos(DateTime value)
        {
            DateTime utc = value.Kind == DateTimeKind.Utc ? value : value.ToUniversalTime();
            var epoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);
            return (long)(utc - epoch).TotalMilliseconds * 1000000L;
        }

        private static string Num(double value)
        {
            return value.ToString("0.##########", CultureInfo.InvariantCulture);
        }

        private static string Escape(string value)
        {
            if (value == null) return string.Empty;
            return value.Replace("\\", "\\\\").Replace("\"", "\\\"");
        }

        private static int MinutesOf(string timeframe)
        {
            switch (timeframe)
            {
                case "1m": return 1;
                case "5m": return 5;
                case "15m": return 15;
                case "1h": return 60;
                case "4h": return 240;
                default: return -1;
            }
        }

        private static string ExtractString(string json, string field)
        {
            string needle = "\"" + field + "\"";
            int at = json.IndexOf(needle, StringComparison.Ordinal);
            if (at < 0) return string.Empty;
            int colon = json.IndexOf(':', at + needle.Length);
            if (colon < 0) return string.Empty;
            int first = json.IndexOf('"', colon + 1);
            if (first < 0) return string.Empty;
            int last = json.IndexOf('"', first + 1);
            if (last < 0) return string.Empty;
            return json.Substring(first + 1, last - first - 1);
        }
    }
}
