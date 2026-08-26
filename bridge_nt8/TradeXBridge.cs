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
//  3. ORDERS NUR AUF SIMULATIONSKONTEN.
//     Hier stand bis Phase 9 "KEINE ORDERS ueber diesen Weg" - die Bridge war
//     reine Datenquelle, Order-Routing war fuer die ATI vorgesehen. Das ist
//     aufgegeben: die ATI kennt keinen Order-Lifecycle (kein Accepted/Working/
//     PartFilled, keine Fuellpreise, keine Positions- oder Kontoereignisse),
//     und genau der wird gebraucht.
//
//     Der Preis dafuer ist die Pfadtrennung: Marktdaten und Orders teilen sich
//     jetzt einen Socket. Ersetzt wird sie durch (a) eine Befehls-Whitelist
//     ohne Default-Zweig, (b) `order_key` als Pflichtfeld, (c) getrennte
//     Warteschlangen - und vor allem durch (d):
//
//     Gehandelt wird ausschliesslich auf Konten mit
//     `Account.Provider == Provider.Simulator`. Nicht die VERBINDUNG wird
//     geprueft, sondern das KONTO - am 26.08.2026 an dieser Installation
//     gemessen melden `Sim101` und das externe Demokonto `DEMO8847061` ueber
//     die Verbindung denselben Provider und waeren darueber nicht zu
//     unterscheiden.
//
//     Es gibt dafuer keinen Schalter, keinen Parameter und keinen
//     Konfigurationseintrag. Ein solcher waere genau der Punkt, an dem aus
//     einem Papertrading-System versehentlich ein Echtgeldsystem wird.
//
//  ---------------------------------------------------------------------
//  TICKS: NUR ZUR ANZEIGE, UND NUR DER JEWEILS NEUESTE
//  ---------------------------------------------------------------------
//  Zusaetzlich zu den geschlossenen Bars wird der zuletzt gehandelte Preis
//  gesendet (`type":"tick"`). Er geht auf der Python-Seite in KEINEN Detektor
//  und in KEINE Entscheidung ein - Punkt 1 oben bleibt unberuehrt. Ohne ihn
//  steht der Chart zwischen zwei Minutenschluessen still, obwohl sich der
//  Markt bewegt, und die laufende Kerze fehlt ganz.
//
//  Gesendet wird NICHT aus dem NinjaTrader-Faden heraus. Ein `Write` auf einen
//  TCP-Socket kann blockieren; passiert das im Marktdaten-Callback, haengt bei
//  hoher Tickrate der Datenfaden von NinjaTrader an einem langsamen Leser.
//  Stattdessen: eine Sendewarteschlange mit eigenem Faden.
//
//  Und dort gilt fuer Ticks eine Sonderregel: ein noch nicht gesendeter Tick
//  wird durch den naechsten ERSETZT statt angehaengt. Fuer eine Kursanzeige
//  zaehlt nur der neueste Preis; eine Warteschlange, die jeden einzelnen Tick
//  aufhebt, waechst bei einem langsamen Client unbegrenzt und zeigt am Ende
//  Kurse von vor einer Minute. Bars werden NIE zusammengefasst - dort waere
//  jeder verworfene Datensatz ein stiller Datenverlust.
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

        // Mindestabstand zwischen zwei Tick-Sendungen desselben Symbols. 50 ms
        // sind 20 Aktualisierungen je Sekunde - fuer das Auge fluessig, und weit
        // unter dem, was ein aktiver MNQ-Feed an Ticks liefert. Ohne Deckel
        // saettigt eine Eroeffnungsminute den Socket mit Daten, von denen die
        // Anzeige nur den letzten braucht.
        private const int TickIntervalMs = 50;

        // Obergrenze der Warteschlange fuer NICHT zusammenfassbare Nachrichten
        // (Bars, Status, history_end). Praktisch unerreichbar; sie steht hier,
        // damit ein Client, der gar nicht mehr liest, den NinjaTrader-Prozess
        // nicht ueber den Speicher mitnimmt.
        private const int MaxQueuedMessages = 50000;

        private TcpListener listener;
        private Thread acceptThread;
        private Thread heartbeatThread;
        private Thread sendThread;
        private volatile bool running;

        // Sendeweg. Alles, was hinausgeht, laeuft hierueber - kein Aufrufer
        // schreibt selbst auf einen Socket, ausser der Begruessung beim
        // Verbindungsaufbau (die geht an genau einen Client).
        private readonly Queue<string> outbox = new Queue<string>();
        private readonly Dictionary<string, string> pendingTicks = new Dictionary<string, string>();
        private readonly object sendLock = new object();
        private readonly AutoResetEvent sendSignal = new AutoResetEvent(false);
        private int droppedMessages;

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

        // Marktdaten-Abonnements. Schluessel ist das INSTRUMENT, nicht das
        // Abonnement: "MNQ|1m" und "MNQ|5m" sind zwei Bar-Anforderungen, aber
        // derselbe Tickstrom. Ohne Zaehler haenge man denselben Handler zweimal
        // an - und jeder Tick ginge doppelt hinaus.
        private class TickSubscription
        {
            public Instrument Instrument;
            public EventHandler<MarketDataEventArgs> Handler;
            public int RefCount;
        }

        private readonly Dictionary<string, TickSubscription> tickSubs =
            new Dictionary<string, TickSubscription>();
        //: Abonnementschluessel ("MNQ|1m") -> Instrumentschluessel. Ohne diese
        //  Zuordnung liesse sich beim `unsubscribe` nicht sagen, welches
        //  Instrument freizugeben ist, ohne es erneut aufzuloesen - und ein
        //  Roll dazwischen wuerde dann das falsche abmelden.
        private readonly Dictionary<string, string> tickKeyOf = new Dictionary<string, string>();
        private readonly object tickLock = new object();

        // --------------------------------------------------------- Orderteil
        //
        // Das Konto, auf dem gehandelt wird. Wird beim ersten Orderbefehl
        // aufgeloest und danach gehalten - samt der Ereignisanmeldungen, die
        // den Rueckkanal bilden.
        private Account tradingAccount;

        // Jeder je gesehene order_key. Der Duplikatschutz muss auch dann noch
        // greifen, wenn die Order laengst geschlossen ist: eine zweite Order
        // unter derselben Kennung waere im Protokoll nicht mehr von der ersten
        // zu unterscheiden. Deshalb ein Set, das nur waechst, und keine
        // Aufraeumung nach Abschluss.
        private readonly HashSet<string> seenOrderKeys = new HashSet<string>();

        // order_key -> die Orders, die dazu gehoeren (Entry plus Klammer).
        // Gebraucht fuer `order_cancel`: der Client kennt nur seinen eigenen
        // Schluessel, nie NinjaTraders Order-IDs.
        private readonly Dictionary<string, List<Order>> ordersByKey =
            new Dictionary<string, List<Order>>();
        private readonly object orderLock = new object();

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

            // Loopback, nicht Any - siehe Entscheidung 2 oben.
            listener = new TcpListener(IPAddress.Loopback, Port);
            try
            {
                listener.Start();
            }
            catch (SocketException error)
            {
                // Der haeufigste Fall ist F5 im NinjaScript-Editor: das alte
                // AddOn wird entladen und das neue geladen, waehrend der
                // vorige Listener den Port noch haelt. Bisher flog die
                // Ausnahme lautlos nach oben und das AddOn war tot, bis
                // jemand NinjaTrader neu startete - von aussen nicht von
                // einem ruhigen Markt zu unterscheiden. Jetzt steht es im
                // Output-Tab, mitsamt dem, was zu tun ist.
                NinjaTrader.Code.Output.Process(
                    "TradeXBridge: Port " + Port + " ist belegt (" + error.Message
                    + "). Meist haelt ihn die vorige Fassung noch - ein paar Sekunden "
                    + "warten und erneut F5 druecken.", PrintTo.OutputTab1);
                listener = null;
                return;
            }

            running = true;

            acceptThread = new Thread(AcceptLoop) { IsBackground = true, Name = "tradex-accept" };
            acceptThread.Start();

            heartbeatThread = new Thread(HeartbeatLoop) { IsBackground = true, Name = "tradex-beat" };
            heartbeatThread.Start();

            sendThread = new Thread(SendLoop) { IsBackground = true, Name = "tradex-send" };
            sendThread.Start();

            NinjaTrader.Code.Output.Process(
                "TradeXBridge lauscht auf 127.0.0.1:" + Port, PrintTo.OutputTab1);
        }

        private void StopServer()
        {
            running = false;
            try { if (listener != null) listener.Stop(); } catch { }
            sendSignal.Set();

            // Marktdaten ZUERST abhaengen: ein Handler, der noch feuert,
            // waehrend die Warteschlange schon abgeraeumt wird, laeuft in eine
            // halb abgebaute Struktur. NinjaTrader haelt die Referenz auf den
            // Delegaten so lange, bis er ausdruecklich abgemeldet wird - ein
            // vergessenes `-=` ueberlebt das AddOn und tickt weiter.
            lock (tickLock)
            {
                foreach (var sub in tickSubs.Values) DetachTicks(sub);
                tickSubs.Clear();
                // Auch die Zuordnung: bliebe sie stehen, hielte ein
                // Wiederanlauf den Schluessel fuer bereits abonniert und
                // haenge nie wieder einen Handler an.
                tickKeyOf.Clear();
            }

            // Konto-Ereignisse aus demselben Grund abmelden wie die
            // Marktdaten - hier wiegt es sogar schwerer: ein weiterlaufender
            // OrderUpdate-Handler wuerde nach dem Wiederanlauf ein zweites Mal
            // angemeldet, und jede Fuellung ginge doppelt hinaus. Auf der
            // Python-Seite saehe das aus wie zwei Ausfuehrungen.
            if (tradingAccount != null)
            {
                try
                {
                    tradingAccount.OrderUpdate -= OnOrderUpdate;
                    tradingAccount.ExecutionUpdate -= OnExecutionUpdate;
                    tradingAccount.PositionUpdate -= OnPositionUpdate;
                }
                catch { }
                tradingAccount = null;
            }

            // `seenOrderKeys` wird ABSICHTLICH nicht geleert: der
            // Duplikatschutz soll einen Neustart des AddOns ueberdauern,
            // solange NinjaTrader laeuft. Wer ihn hier zuruecksetzt, macht aus
            // einem Wiederanlauf ein Schlupfloch.

            lock (sendLock)
            {
                outbox.Clear();
                pendingTicks.Clear();
            }

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

            // Orderbefehle zuerst, mit eigener Whitelist. Sie stehen VOR der
            // Datenweiche, damit ein Orderbefehl nie versehentlich als
            // Abonnement gelesen wird - und sie haben keinen `else`-Zweig:
            // was nicht namentlich hier steht, wird verworfen.
            //
            // Warum das strenger sein muss als bei den Datenbefehlen: seit
            // Phase 9 teilen sich Marktdaten und Orders einen Socket. Die
            // frueher zugesagte Pfadtrennung ist damit weg, und eine
            // verstuemmelte Zeile darf unter keinen Umstaenden als Order
            // durchgehen. Sie passt auf keinen dieser Namen - das ist der
            // Ersatz fuer die Trennung.
            if (type == "order_submit") { SubmitOrder(line); return; }
            if (type == "order_cancel") { CancelOrder(ExtractString(line, "order_key")); return; }
            if (type == "flatten")
            {
                Flatten(ExtractString(line, "account"), ExtractString(line, "symbol"));
                return;
            }
            // Der Kontoname MUSS durchgereicht werden. Ohne ihn fragte die
            // Abfrage nach "irgendeinem Simulationskonto" - und weil an dieser
            // Installation `Sim101` UND `Backtest` beide Provider.Simulator
            // sind, lehnte die Aufloesung wegen Mehrdeutigkeit ab. Die
            // Verbindung scheiterte damit an einer Sicherheitsstufe, die
            // voellig richtig arbeitete; nur die Frage war falsch gestellt.
            if (type == "account_query") { SendAccount(ExtractString(line, "account")); return; }

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
                AttachTicks(key, symbol, instrument);
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
            ReleaseTicks(key);
        }

        // --------------------------------------------------------------- Ticks
        //
        // Der laufende Preis. Er beantwortet zwei Fragen, die geschlossene Bars
        // nicht beantworten koennen: wo steht der Markt GERADE, und lebt der
        // Feed ueberhaupt, wenn eine Minute lang keine Bar schliesst.
        //
        // Was hier NICHT passiert: aus Ticks werden keine Bars gebaut und keine
        // laufenden Bars gesendet. Was TradeX daraus zur Anzeige zusammensetzt,
        // ist dort ausdruecklich von der Analyse ausgenommen.
        private void AttachTicks(string subscriptionKey, string symbol, Instrument instrument)
        {
            if (instrument == null) return;
            string instrumentKey = instrument.FullName;

            lock (tickLock)
            {
                if (tickKeyOf.ContainsKey(subscriptionKey)) return;
                tickKeyOf[subscriptionKey] = instrumentKey;

                TickSubscription existing;
                if (tickSubs.TryGetValue(instrumentKey, out existing))
                {
                    // Dasselbe Instrument auf einer zweiten Zeitebene. Ein
                    // zweiter Handler wuerde jeden Tick doppelt hinausschicken.
                    existing.RefCount++;
                    return;
                }

                string wire = symbol.ToUpperInvariant();
                var sub = new TickSubscription { Instrument = instrument, RefCount = 1 };
                sub.Handler = (sender, args) => OnMarketData(wire, args);
                try
                {
                    instrument.MarketData.Update += sub.Handler;
                }
                catch (Exception error)
                {
                    // Ohne Level-1-Abo gibt es keine Ticks. Das ist ein
                    // Betriebszustand, kein Absturzgrund - Bars laufen weiter.
                    // Gemeldet werden muss es trotzdem: eine stille Kursanzeige,
                    // die nie erklaert warum, ist schlimmer als gar keine.
                    tickKeyOf.Remove(subscriptionKey);
                    NinjaTrader.Code.Output.Process(
                        "TradeXBridge: keine Marktdaten fuer " + instrumentKey
                        + " (" + error.Message + ")", PrintTo.OutputTab1);
                    return;
                }
                tickSubs[instrumentKey] = sub;
            }
        }

        private void ReleaseTicks(string subscriptionKey)
        {
            lock (tickLock)
            {
                string instrumentKey;
                if (!tickKeyOf.TryGetValue(subscriptionKey, out instrumentKey)) return;
                tickKeyOf.Remove(subscriptionKey);

                TickSubscription sub;
                if (!tickSubs.TryGetValue(instrumentKey, out sub)) return;
                sub.RefCount--;
                if (sub.RefCount > 0) return;
                DetachTicks(sub);
                tickSubs.Remove(instrumentKey);
            }
        }

        private static void DetachTicks(TickSubscription sub)
        {
            try { sub.Instrument.MarketData.Update -= sub.Handler; } catch { }
        }

        private void OnMarketData(string symbol, MarketDataEventArgs args)
        {
            // NUR Abschluesse. Bid und Ask feuern um ein Vielfaches haeufiger
            // und beantworten eine andere Frage; was der Chart zeichnet, ist der
            // zuletzt GEHANDELTE Preis.
            if (args == null || args.MarketDataType != MarketDataType.Last) return;

            string json = "{\"type\":\"tick\""
                + ",\"symbol\":\"" + Escape(symbol) + "\""
                + ",\"ts\":" + ToEpochNanos(args.Time).ToString(CultureInfo.InvariantCulture)
                + ",\"price\":" + Num(args.Price)
                + ",\"size\":" + Num(args.Volume) + "}";
            QueueTick(symbol, json);
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
                // `dropped` gehoert dazu: eine ueberlaufene Warteschlange, die
                // niemand meldet, sieht von aussen aus wie ein ruhiger Markt.
                Broadcast("{\"type\":\"heartbeat\",\"ts\":"
                    + ToEpochNanos(DateTime.UtcNow).ToString(CultureInfo.InvariantCulture)
                    + ",\"dropped\":" + droppedMessages.ToString(CultureInfo.InvariantCulture) + "}");
            }
        }

        // -------------------------------------------------------------- Senden
        //
        // Kein Aufrufer schreibt selbst auf den Socket. `Broadcast` und
        // `QueueTick` legen ab, ein eigener Faden traegt aus - siehe die
        // Vorbemerkung oben: ein blockierendes Write im Marktdaten-Callback
        // haengt den Datenfaden von NinjaTrader an einen langsamen Leser.
        private void Broadcast(string json)
        {
            lock (sendLock)
            {
                if (outbox.Count >= MaxQueuedMessages)
                {
                    // Hier wird verworfen statt zu wachsen. Das ist die
                    // schlechtere von zwei schlechten Moeglichkeiten - die
                    // andere waere, NinjaTrader den Speicher zu nehmen.
                    outbox.Dequeue();
                    droppedMessages++;
                }
                outbox.Enqueue(json);
            }
            sendSignal.Set();
        }

        private void QueueTick(string symbol, string json)
        {
            lock (sendLock)
            {
                // ERSETZEN, nicht anhaengen: fuer eine Kursanzeige zaehlt nur
                // der neueste Preis. Ein noch nicht gesendeter Tick ist bereits
                // ueberholt, sobald der naechste da ist.
                pendingTicks[symbol] = json;
            }
            sendSignal.Set();
        }

        private void SendLoop()
        {
            while (running)
            {
                // Warten statt pollen; der Takt deckelt zugleich die Tickrate.
                sendSignal.WaitOne(TickIntervalMs);
                if (!running) break;

                List<string> batch = null;
                lock (sendLock)
                {
                    if (outbox.Count > 0 || pendingTicks.Count > 0)
                    {
                        batch = new List<string>(outbox.Count + pendingTicks.Count);
                        // Bars und Zustaende zuerst und in Reihenfolge - sie
                        // sind der Datenpfad. Ticks sind Beiwerk.
                        while (outbox.Count > 0) batch.Add(outbox.Dequeue());
                        batch.AddRange(pendingTicks.Values);
                        pendingTicks.Clear();
                    }
                }
                if (batch == null) continue;

                List<TcpClient> snapshot;
                lock (clientLock) { snapshot = new List<TcpClient>(clients); }
                foreach (var client in snapshot)
                {
                    foreach (var json in batch) Send(client, json);
                }

                // Der Takt gilt auch dann, wenn staendig Signale hereinkommen:
                // sonst liefe die Schleife bei einem aktiven Feed durch und der
                // Deckel auf der Tickrate waere wirkungslos.
                if (running) Thread.Sleep(TickIntervalMs);
            }
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

        // -------------------------------------------------------------- Orders
        //
        // ACHTUNG: Dieser Abschnitt ist gegen die NinjaTrader-Assemblies noch
        // NICHT uebersetzt worden. Beim Bar-Teil hat genau dieser Schritt
        // Fehler gefunden, die eine Spezifikation nicht findet
        // (`AddOnBase` statt `NinjaScript.AddOnBase`, `BarsUpdateEventArgs.BarsSeries`
        // mit falschem Typ). Es waere unredlich, hier etwas anderes zu
        // erwarten - siehe README, Abschnitt "Abnahmekriterien".

        /// <summary>
        /// Das Handelskonto aufloesen - ausschliesslich Simulationskonten.
        /// </summary>
        /// <remarks>
        /// Dies ist die Sperre, auf der die ganze Ausbaustufe ruht.
        /// `Provider.Simulator` ist eine EIGENSCHAFT des Kontos, keine
        /// Namenskonvention - anders als der frueher bei IBKR moegliche
        /// Nachweis (Port + Kontopraefix + Allowlist), der strukturell
        /// indirekt bleiben musste.
        ///
        /// Es gibt hier bewusst keinen Parameter, keinen Schalter und keine
        /// Konfigurationsdatei, die das aufweicht. Ein solcher Schalter waere
        /// genau der Punkt, an dem aus einem Papertrading-System versehentlich
        /// ein Echtgeldsystem wird.
        /// </remarks>
        private Account ResolveSimAccount(string wanted)
        {
            if (tradingAccount != null) return tradingAccount;

            lock (Account.All)
            {
                foreach (Account candidate in Account.All)
                {
                    if (wanted.Length > 0 && candidate.Name != wanted) continue;

                    // `candidate.Provider` - die Eigenschaft des KONTOS.
                    //
                    // Hier stand zuerst `candidate.Connection.Options.Provider`,
                    // und das war ein Denkfehler mit gefaehrlicher Schlagseite:
                    // das ist der Provider der VERBINDUNG. An einer laufenden
                    // Installation gemessen (26.08.2026) meldeten `Sim101` und
                    // das externe Demokonto `DEMO8847061` darueber denselben
                    // Wert - sie haengen an derselben Verbindung. Die Pruefung
                    // lehnte damit einerseits das echte Simulationskonto ab,
                    // haette andererseits aber ein Fremdkonto durchgelassen,
                    // sobald die Verbindung selbst als Simulator gilt.
                    //
                    // Eine Sicherheitsstufe, die zwei Konten nicht
                    // unterscheiden kann, prueft nichts.
                    if (candidate.Provider != Provider.Simulator) continue;

                    // Ohne ausdruecklichen Namen wird NICHT gewaehlt, sondern
                    // abgelehnt, sobald es mehr als einen Kandidaten gibt.
                    // Gemessen an dieser Installation sind `Sim101` UND
                    // `Backtest` beide Provider.Simulator - "das erste
                    // passende" haette hier stillschweigend das
                    // Backtest-Konto gehandelt (net_liquidation 0).
                    // "Welches Konto hat der Bot eigentlich gehandelt?" ist
                    // keine Frage, die man aus Bequemlichkeit offenlaesst.
                    if (wanted.Length == 0 && tradingAccount != null)
                    {
                        tradingAccount = null;
                        break;
                    }
                    tradingAccount = candidate;
                    if (wanted.Length > 0) break;
                }
            }

            if (tradingAccount != null)
            {
                // Der Rueckkanal. Ohne diese drei Anmeldungen gibt es keinen
                // Lifecycle - "Filled" waere dann eine Vermutung des Senders
                // statt einer Meldung des Kontos.
                tradingAccount.OrderUpdate += OnOrderUpdate;
                tradingAccount.ExecutionUpdate += OnExecutionUpdate;
                tradingAccount.PositionUpdate += OnPositionUpdate;
            }
            return tradingAccount;
        }

        private void SubmitOrder(string line)
        {
            string key = ExtractString(line, "order_key");
            if (key.Length == 0) { Reject("", "order_key_missing", "kein order_key"); return; }

            string wantedAccount = ExtractString(line, "account");
            Account account = ResolveSimAccount(wantedAccount);
            if (account == null)
            {
                Reject(key, "account_not_simulated",
                    "kein Konto mit Provider=Simulator gefunden (gesucht: "
                    + (wantedAccount.Length > 0 ? wantedAccount : "<beliebig>") + ")");
                return;
            }

            lock (orderLock)
            {
                // Auch dann ablehnen, wenn die erste Order geschlossen ist -
                // siehe Kommentar bei `seenOrderKeys`.
                if (seenOrderKeys.Contains(key))
                {
                    Reject(key, "duplicate_order_key", "order_key bereits verwendet");
                    return;
                }
            }

            string symbol = ExtractString(line, "symbol");
            Instrument instrument = ResolveInstrument(symbol);
            if (instrument == null)
            {
                Reject(key, "instrument_unknown", "Instrument " + symbol + " nicht aufloesbar");
                return;
            }

            int quantity = (int)ExtractLong(line, "quantity");
            if (quantity <= 0) { Reject(key, "quantity_invalid", "quantity <= 0"); return; }

            string side = ExtractString(line, "side");
            OrderAction entryAction = side == "SELL" ? OrderAction.SellShort : OrderAction.Buy;
            OrderAction exitAction = side == "SELL" ? OrderAction.BuyToCover : OrderAction.Sell;

            double stopLoss = ExtractDouble(line, "stop_loss");
            double takeProfit = ExtractDouble(line, "take_profit");
            double limitPrice = ExtractDouble(line, "limit_price");
            bool isLimit = ExtractString(line, "kind") == "LIMIT";
            if (isLimit && limitPrice <= 0)
            {
                Reject(key, "bracket_invalid", "LIMIT ohne limit_price");
                return;
            }

            var created = new List<Order>();
            try
            {
                Order entry = account.CreateOrder(
                    instrument,
                    entryAction,
                    isLimit ? OrderType.Limit : OrderType.Market,
                    OrderEntry.Automated,
                    TimeInForce.Day,
                    quantity,
                    isLimit ? limitPrice : 0,
                    0,
                    string.Empty,
                    OrderRef(key, "entry"),
                    Core.Globals.MaxDate,
                    null);
                created.Add(entry);

                // Stop und Ziel gehen als ECHTE Orders hinaus, nicht als
                // interne Merkposten: sie muessen auch dann wirken, wenn TradeX
                // nicht laeuft. Beide teilen sich eine OCO-Gruppe, damit die
                // eine die andere zurueckzieht - sonst bliebe nach dem Ziel ein
                // Stop stehen und oeffnete eine Gegenposition.
                string oco = created.Count > 0 ? key + "-oco" : string.Empty;
                if (stopLoss > 0)
                {
                    created.Add(account.CreateOrder(
                        instrument, exitAction, OrderType.StopMarket, OrderEntry.Automated,
                        TimeInForce.Gtc, quantity, 0, stopLoss, oco,
                        OrderRef(key, "stop"), Core.Globals.MaxDate, null));
                }
                if (takeProfit > 0)
                {
                    created.Add(account.CreateOrder(
                        instrument, exitAction, OrderType.Limit, OrderEntry.Automated,
                        TimeInForce.Gtc, quantity, takeProfit, 0, oco,
                        OrderRef(key, "target"), Core.Globals.MaxDate, null));
                }

                lock (orderLock)
                {
                    seenOrderKeys.Add(key);
                    ordersByKey[key] = created;
                }
                account.Submit(created);
            }
            catch (Exception error)
            {
                Reject(key, "submit_failed", error.Message);
            }
        }

        private void CancelOrder(string key)
        {
            if (key.Length == 0) return;
            List<Order> orders;
            lock (orderLock)
            {
                if (!ordersByKey.TryGetValue(key, out orders)) return;
            }
            // Entry UND Klammer. Eine stornierte Entry-Order, deren Stop stehen
            // bleibt, waere eine Order ohne Position.
            try { if (tradingAccount != null) tradingAccount.Cancel(orders); } catch { }
        }

        /// <summary>Der NOTAUS-Weg: erst stornieren, dann glattstellen.</summary>
        /// <remarks>
        /// Die Reihenfolge ist Korrektheit, nicht Stil. Wird zuerst
        /// glattgestellt, loest eine noch stehende Klammerorder auf der
        /// geschlossenen Position eine GEGENposition aus - aus einem NOTAUS
        /// wuerde ein neuer Trade.
        /// </remarks>
        private void Flatten(string wantedAccount, string symbol)
        {
            Account account = ResolveSimAccount(wantedAccount);
            if (account == null) return;

            try { account.CancelAllOrders(null); } catch { }

            try
            {
                if (symbol.Length == 0)
                {
                    var alle = new List<Instrument>();
                    foreach (Position position in account.Positions) alle.Add(position.Instrument);
                    if (alle.Count > 0) account.Flatten(alle);
                }
                else
                {
                    Instrument instrument = ResolveInstrument(symbol);
                    if (instrument != null) account.Flatten(new[] { instrument });
                }
            }
            catch { }
        }

        // ------------------------------------------------- Rueckkanal (Konto)
        //
        // Alle drei Handler legen nur ab und kehren zurueck. Kein `Send`, kein
        // Netzzugriff - dieselbe Regel und derselbe Grund wie beim
        // Marktdaten-Callback: ein blockierender Aufruf haenge sonst den
        // NinjaTrader-Faden auf, und zwar ausgerechnet den, der gerade eine
        // Position haelt.
        //
        // `Broadcast` statt `QueueTick`: Order-Ereignisse werden NIE
        // zusammengefasst. Ein verworfener Tick kostet einen Kursstand, eine
        // verworfene Fuellung erzeugt eine Position, die TradeX nicht kennt.

        private void OnOrderUpdate(object sender, OrderEventArgs args)
        {
            Order order = args.Order;
            if (order == null) return;
            Broadcast("{\"type\":\"order_update\""
                + ",\"order_key\":\"" + Escape(KeyOfRef(order.Name)) + "\""
                + ",\"order_id\":\"" + Escape(order.OrderId) + "\""
                + ",\"role\":\"" + Escape(RoleOfRef(order.Name)) + "\""
                + ",\"ts\":" + ToEpochNanos(DateTime.UtcNow).ToString(CultureInfo.InvariantCulture)
                + ",\"state\":\"" + MapOrderState(order.OrderState) + "\""
                + ",\"filled_quantity\":" + order.Filled.ToString(CultureInfo.InvariantCulture)
                + ",\"avg_fill_price\":"
                + order.AverageFillPrice.ToString(CultureInfo.InvariantCulture)
                // `Comment`, nicht `NativeError`: OrderEventArgs spiegelt die
                // Signatur von `OnOrderUpdate` in NinjaScript-Strategien, und
                // die endet auf (ErrorCode error, string comment).
                + ",\"error\":\"" + Escape(args.Error == ErrorCode.NoError ? "" : args.Comment)
                + "\"}");
        }

        private void OnExecutionUpdate(object sender, ExecutionEventArgs args)
        {
            Execution execution = args.Execution;
            if (execution == null || execution.Order == null) return;
            Broadcast("{\"type\":\"execution\""
                + ",\"order_key\":\"" + Escape(KeyOfRef(execution.Order.Name)) + "\""
                + ",\"exec_id\":\"" + Escape(execution.ExecutionId) + "\""
                + ",\"ts\":" + ToEpochNanos(execution.Time).ToString(CultureInfo.InvariantCulture)
                + ",\"quantity\":" + execution.Quantity.ToString(CultureInfo.InvariantCulture)
                + ",\"price\":" + execution.Price.ToString(CultureInfo.InvariantCulture)
                + ",\"commission\":" + execution.Commission.ToString(CultureInfo.InvariantCulture)
                + "}");
        }

        private void OnPositionUpdate(object sender, PositionEventArgs args)
        {
            Position position = args.Position;
            if (position == null) return;
            // Vorzeichenbehaftet: negativ = short. TradeX haelt diese Zahl
            // absichtlich getrennt vom eigenen Risikobuch - die Differenz
            // zwischen beiden ist genau die Information, die man nach einem
            // Verbindungsabriss braucht.
            int signed = position.MarketPosition == MarketPosition.Short
                ? -position.Quantity
                : position.Quantity;
            Broadcast("{\"type\":\"position\""
                + ",\"account\":\"" + Escape(position.Account.Name) + "\""
                + ",\"symbol\":\"" + Escape(RootOf(position.Instrument)) + "\""
                + ",\"quantity\":" + signed.ToString(CultureInfo.InvariantCulture)
                + ",\"avg_price\":"
                + position.AveragePrice.ToString(CultureInfo.InvariantCulture)
                + "}");
        }

        private void SendAccount(string wanted)
        {
            Account account = ResolveSimAccount(wanted);
            if (account == null)
            {
                // Warum nichts gefunden wurde, statt nur DASS nichts gefunden
                // wurde. Ein leeres Ergebnis ohne Begruendung zwingt sonst zum
                // Raten - und geraten hatte ich bei den Enum-Namen schon
                // einmal falsch. `candidates` nennt jedes Konto mit dem, was
                // die Pruefung tatsaechlich gesehen hat.
                var teile = new List<string>();
                lock (Account.All)
                {
                    foreach (Account candidate in Account.All)
                    {
                        // Beide Provider nebeneinander: der des Kontos (die
                        // Pruefgroesse) und der der Verbindung (die frueher
                        // faelschlich geprueft wurde). Sie stehen zusammen da,
                        // damit der Unterschied im Zweifel ablesbar bleibt
                        // statt erneut verwechselt zu werden.
                        string verbindung = "<keine Verbindung>";
                        if (candidate.Connection != null && candidate.Connection.Options != null)
                            verbindung = candidate.Connection.Options.Provider.ToString();
                        teile.Add("{\"name\":\"" + Escape(candidate.Name)
                            + "\",\"account_provider\":\"" + Escape(candidate.Provider.ToString())
                            + "\",\"connection_provider\":\"" + Escape(verbindung) + "\"}");
                    }
                }
                // WONACH gesucht wurde gehoert in die Meldung. "kein Konto mit
                // Provider=Simulator" allein war irrefuehrend: es gab zwei
                // davon, und abgelehnt wurde wegen Mehrdeutigkeit.
                Broadcast("{\"type\":\"account\",\"name\":\"\",\"provider\":\"\""
                    + ",\"is_simulation\":false"
                    + ",\"detail\":\"kein eindeutiges Konto mit Provider=Simulator (gesucht: "
                    + Escape(wanted.Length > 0 ? wanted : "<beliebig>") + ")\""
                    + ",\"candidates\":[" + string.Join(",", teile.ToArray()) + "]}");
                return;
            }
            Broadcast("{\"type\":\"account\""
                + ",\"name\":\"" + Escape(account.Name) + "\""
                + ",\"provider\":\"" + Escape(account.Provider.ToString()) + "\""
                + ",\"is_simulation\":true"
                + ",\"net_liquidation\":" + AccountValue(account, AccountItem.NetLiquidation)
                + ",\"buying_power\":" + AccountValue(account, AccountItem.BuyingPower)
                + ",\"realized_pnl\":" + AccountValue(account, AccountItem.RealizedProfitLoss)
                + "}");
        }

        private static string AccountValue(Account account, AccountItem item)
        {
            try
            {
                return account.Get(item, Currency.UsDollar)
                    .ToString(CultureInfo.InvariantCulture);
            }
            catch { return "0"; }
        }

        private void Reject(string key, string code, string detail)
        {
            Broadcast("{\"type\":\"order_rejected\""
                + ",\"order_key\":\"" + Escape(key) + "\""
                + ",\"code\":\"" + Escape(code) + "\""
                + ",\"detail\":\"" + Escape(detail) + "\"}");
        }

        // Reason-Codes, keine Saetze - dieselbe Konvention wie im uebrigen
        // System. `de.ts` uebersetzt sie fuer die Anzeige.
        private static string MapOrderState(OrderState state)
        {
            switch (state)
            {
                // Die Namen stammen aus NinjaTrader.Cbi.OrderState und wurden
                // gegen den Compiler geprueft. Geraten hatte ich zunaechst
                // `PendingSubmit`/`PendingCancel` - beide gibt es dort nicht.
                case OrderState.Initialized:
                case OrderState.Submitted:
                    return "submitted";

                // Vier Zustaende auf `accepted`: fuer TradeX ist die Frage
                // "liegt sie beim Broker und kann sich noch aendern?", und die
                // beantworten alle vier gleich. Eigene Zustaende dafuer waeren
                // NinjaTrader-Begriffe in einem Enum, das keine kennen soll -
                // `is_live` in types.py deckt sie ab.
                case OrderState.Accepted:
                case OrderState.Working:
                case OrderState.ChangePending:
                case OrderState.ChangeSubmitted:
                case OrderState.TriggerPending:
                    return "accepted";

                case OrderState.PartFilled:
                    return "partially_filled";
                case OrderState.Filled:
                    return "filled";

                // Storno ANGEFRAGT ist noch nicht storniert. Beide gelten hier
                // trotzdem als `cancelled`: TradeX nimmt auf eine Order, deren
                // Storno laeuft, ohnehin keine Position mehr auf, und der
                // Unterschied waere ein Zustand, den niemand auswertet.
                case OrderState.CancelPending:
                case OrderState.CancelSubmitted:
                case OrderState.Cancelled:
                    return "cancelled";

                case OrderState.Rejected:
                    return "rejected";
                default:
                    return "inactive";
            }
        }

        // `order_ref` aus tradex/broker/types.py, spiegelbildlich. Der
        // Schluessel ist der einzige Faden, an dem sich nach einem
        // Verbindungsabriss wiederfinden laesst, welche fremde Order zu
        // welchem eigenen Signal gehoert - Order-IDs vergibt NinjaTrader, und
        // nach einem Neustart kennt TradeX sie nicht mehr.
        private static string OrderRef(string key, string role)
        {
            return role == "entry" ? key : key + "#" + role;
        }

        /// <summary>Wurzelsymbol: aus "MNQ SEP26" wird "MNQ".</summary>
        /// <remarks>
        /// Die Gegenrichtung von `nt8_symbol` aus `config/instruments.yaml`.
        /// Bei Bars traegt der Aufrufer das Wurzelsymbol durch das Abonnement
        /// mit; eine Positionsmeldung entsteht dagegen ohne vorherigen Befehl
        /// und muss es selbst bestimmen. `MasterInstrument.Name` ist genau das
        /// und bleibt ueber den Roll hinweg stabil - was hier gerade der Punkt
        /// ist: TradeX soll `MNQ` sehen, nicht den Kontrakt des Monats.
        /// </remarks>
        private static string RootOf(Instrument instrument)
        {
            if (instrument == null || instrument.MasterInstrument == null) return string.Empty;
            return instrument.MasterInstrument.Name.ToUpperInvariant();
        }

        private static string KeyOfRef(string reference)
        {
            if (string.IsNullOrEmpty(reference)) return string.Empty;
            int marker = reference.IndexOf('#');
            return marker < 0 ? reference : reference.Substring(0, marker);
        }

        private static string RoleOfRef(string reference)
        {
            if (string.IsNullOrEmpty(reference)) return "entry";
            int marker = reference.IndexOf('#');
            return marker < 0 ? "entry" : reference.Substring(marker + 1);
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

        /// <summary>Wie `ExtractLong`, aber mit Nachkommastellen.</summary>
        /// <remarks>
        /// Gebraucht fuer Kurse (`stop_loss`, `take_profit`, `limit_price`).
        /// `InvariantCulture` ist hier keine Formalie: auf einer deutschen
        /// Windows-Installation wuerde `double.Parse` ohne sie das Komma als
        /// Dezimaltrenner erwarten und `29245.75` als 2924575 lesen - ein
        /// Stop, der um Faktor 100.000 danebenliegt.
        /// </remarks>
        private static double ExtractDouble(string json, string field)
        {
            string needle = "\"" + field + "\"";
            int at = json.IndexOf(needle, StringComparison.Ordinal);
            if (at < 0) return 0;
            int colon = json.IndexOf(':', at + needle.Length);
            if (colon < 0) return 0;
            int start = colon + 1;
            while (start < json.Length && (json[start] == ' ' || json[start] == '"')) start++;
            int end = start;
            while (end < json.Length
                && (char.IsDigit(json[end]) || json[end] == '-' || json[end] == '.'))
            {
                end++;
            }
            double value;
            return double.TryParse(json.Substring(start, end - start),
                NumberStyles.Float, CultureInfo.InvariantCulture, out value) ? value : 0;
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
