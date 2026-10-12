# bard-board - Soundboard für Mumble und Discord mit Webinterface

Ein Bot sitzt in deinem Mumble-Kanal und spielt Sounds ab, ein zweiter auf Wunsch genauso in Discord.
Außerdem spielt er Musik von YouTube (Links, Playlists, Suche) aus einer Warteschlange ab, wie ein Radio.
Dazu kommen Sprachausgabe, ein Pull-Timer für Raids, persönliche Join-Sounds, eigene Kürzel und durchsuchbare Sammlungen mit Tausenden Sprachzeilen.
Steuern lässt er sich über ein Webinterface im Browser und über Chat-Befehle direkt in Mumble bzw. Discord.
Beide Bots teilen sich Sounds und Sammlungen, alles andere (Warteschlange, Lautstärke, Join-Sounds, Favoriten) hat jeder für sich.

## Schnellstart (Windows)

1. `start.bat` doppelklicken.
2. Falls Docker Desktop fehlt, bietet das Skript die Installation per `winget` an.
   Danach einmal neu starten, Docker Desktop öffnen und `start.bat` erneut ausführen.
3. Beim ersten Start öffnet sich der Editor mit der Datei `%USERPROFILE%\.claudemumble\zugang.env`.
   Dort Server-Adresse, Benutzername und ggf. Passwort eintragen, speichern, Editor schließen.
4. Das Webinterface öffnet sich unter <http://localhost:8080>.

`start.bat` kann jederzeit erneut ausgeführt werden.
Es startet den Bot oder baut ihn nach Änderungen am Code neu.

| Datei | Zweck |
| --- | --- |
| `start.bat` | Einrichten, starten, aktualisieren |
| `stop.bat` | Bots beenden |
| `logs.bat` | Live-Log anzeigen |
| `zugangsdaten.bat` | Zugangsdaten (Mumble oder Discord) bearbeiten und Bots neu starten |
| `export.bat` | Sounds, Sammlungen und Daten in eine Datei für den Kubernetes-Cluster packen |

Discord ist optional, die Einrichtung steht unter [Discord einrichten](#discord-einrichten).

Die Container starten nach einem Neustart von Windows automatisch mit Docker Desktop (`restart: unless-stopped`).

## Zugangsdaten

Die Zugangsdaten liegen absichtlich **außerhalb** des Projektordners in `%USERPROFILE%\.claudemumble\zugang.env`.
Nur Docker liest die Datei beim Start des Containers ein (`env_file` in `docker-compose.yml`).
So landen sie weder in Git noch in einem Ordner, den du mit anderen Tools oder Claude teilst.

```ini
MUMBLE_HOST=mumble.example.org
MUMBLE_PORT=64738
MUMBLE_USER=Soundboard
MUMBLE_PASSWORD='geh$im#123'
MUMBLE_TOKENS=
```

Werte mit `$`, `#` oder Leerzeichen gehören in einfache Anführungszeichen.
`docker compose config` und `docker inspect` zeigen die Werte im Klartext an.
Diese Befehle also nicht in geteilten Bildschirmen oder Logs verwenden.

Der Bot meldet sich mit einem eigenen Zertifikat an (`data/cert.pem`, `data/key.pem`).
Fehlt es, wird beim ersten Start automatisch eines erzeugt.
Wenn du den Bot auf dem Server registrierst, bleibt er über dieses Zertifikat wiedererkennbar.
Den Ordner `data` deshalb nicht löschen.

## Discord einrichten

Der Discord-Bot läuft als zweiter Container aus demselben Image neben dem Mumble-Bot.
Er startet automatisch, sobald ein Token eingetragen ist.

1. Auf <https://discord.com/developers/applications> mit „New Application“ eine Anwendung anlegen, z. B. „Soundboard“.
2. Links auf „Bot“:
   - „Reset Token“ klicken und den Token kopieren.
   - Unter „Privileged Gateway Intents“ den **Message Content Intent** einschalten und speichern.
     Ohne ihn funktionieren nur Slash-Befehle, Direktnachrichten und Befehle mit @Erwähnung.
   - „Public Bot“ ausschalten, dann kann nur der Besitzer der Anwendung den Bot einladen.
3. `zugangsdaten.bat` ausführen, `2` wählen, den Token hinter `DISCORD_TOKEN=` einfügen, speichern und den Editor schließen.
   Das Skript startet danach beide Bots und öffnet das Discord-Webinterface unter <http://localhost:8081>.
4. Solange der Bot auf keinem Server ist, zeigt das Discord-Webinterface oben den Knopf „Bot einladen“.
   Der Link fragt genau die nötigen Rechte an: Kanäle sehen, Nachrichten senden und lesen, Verbinden, Sprechen, Sprachaktivierung.
   Den Link schreibt der Bot auch ins Log (`logs.bat`).
5. In Discord einen Sprachkanal betreten und `!come` schreiben, im Webinterface einen Kanal wählen oder `DISCORD_CHANNEL` bzw. `DISCORD_FOLLOW_USER` setzen.

Der Token liegt wie die Mumble-Zugangsdaten außerhalb des Projektordners in `%USERPROFILE%\.claudemumble\discord.env`.
Der Discord-Container bekommt nur diese Datei, der Mumble-Container nur `zugang.env`.
Bleibt `DISCORD_TOKEN` leer, läuft nur der Mumble-Bot.

Oben links im Webinterface schaltet „Mumble | Discord“ zwischen den beiden Bots um.
Ein ausgegrauter Eintrag heißt, dass der andere Bot gerade nicht läuft.

Discord verschlüsselt Sprachkanäle Ende-zu-Ende (DAVE).
Das erledigt das Paket `davey`, das im Image mitinstalliert wird.
Fehlt es, zeigt das Webinterface einen Hinweis und der Bot kann zwar Befehle annehmen, aber nichts abspielen.

## Einstellungen

Alle übrigen Einstellungen stehen in `.env` im Projektordner (Vorlage: `.env.example`).
Nach einer Änderung `start.bat` ausführen.

| Variable | Standard | Bedeutung |
| --- | --- | --- |
| `HOST_PORT` | `8080` | Port des Webinterfaces auf deinem PC |
| `MUMBLE_CHANNEL` | leer | Kanal nach dem Verbinden, Unterkanäle mit `/` (z. B. `Gaming/Dota`) |
| `MUMBLE_FOLLOW_USER` | `Zasa` | Der Bot folgt diesem Benutzer von Kanal zu Kanal |
| `COMMAND_PREFIXES` | `/,!` | Prefixe für Chat-Befehle |
| `ADMINS` | leer | Wer `/vol`, `/come`, `/follow`, `/unfollow` nutzen darf (leer = alle) |
| `BLOCKED_USERS` | leer | Benutzer, deren Befehle ignoriert werden |
| `COMMAND_COOLDOWN` | `1` | Sekunden zwischen zwei Sounds pro Benutzer |
| `MAX_VOICES` | `4` | Wie viele Sounds gleichzeitig laufen können |
| `OVERLAP` | `true` | `false` = ein neuer Sound beendet den laufenden |
| `DEFAULT_VOLUME` | `70` | Start-Lautstärke in Prozent |
| `BITRATE` | `64000` | Opus-Bitrate, wird automatisch an das Server-Limit angepasst |
| `NORMALIZE` | `true` | Alle Sounds auf ähnliche Lautheit bringen |
| `TRIM_SILENCE` | `true` | Stille am Anfang und Ende entfernen |
| `MAX_SOUND_SECONDS` | `300` | Längere Dateien werden abgeschnitten |
| `IGNORE_DIRS` | `aussortiert` | Unterordner, die ignoriert werden |
| `MAX_UPLOAD_MB` | `50` | Maximale Dateigröße beim Hochladen |
| `MUSIC_DEFAULT_VOLUME` | `50` | Start-Lautstärke der Musik in Prozent |
| `MUSIC_DUCK_VOLUME` | `25` | Musik-Lautstärke in Prozent, während ein Sound läuft (`100` = kein Absenken) |
| `MUSIC_NORMALIZE` | `true` | Musik auf gleichmäßige Lautheit bringen |
| `MUSIC_ANNOUNCE` | `true` | Neuen Titel im Chat ansagen |
| `MAX_TRACK_MINUTES` | `20` | Längere Titel werden abgelehnt bzw. abgeschnitten (gilt nicht für Livestreams) |
| `LIVE_IDLE_MINUTES` | `5` | Livestream pausiert, wenn so lange niemand außer dem Bot im Kanal war (`0` = nie) |
| `MAX_PLAYLIST_ITEMS` | `100` | Höchstens so viele Titel aus einer Playlist |
| `MAX_CLIP_SECONDS` | `60` | Maximale Länge eines Clips |
| `CLIP_CATEGORY` | `YouTube Clips` | Unterordner für neue Clips |
| `YTDLP_AUTO_UPDATE` | `true` | yt-dlp beim Start aktualisieren und täglich nach neuen Versionen sehen |
| `JOIN_SOUNDS` | `true` | Join-Sounds an oder aus |
| `JOIN_SOUND_COOLDOWN` | `600` | Mindestabstand in Sekunden zwischen zwei Join-Sounds derselben Person |
| `TTS_MAX_CHARS` | `300` | Maximale Textlänge für `/tts` |
| `PULL_SOUND` | leer | Eigener Sound am Ende des Pull-Timers statt des Kriegshorns |
| `PULL_MAX_SECONDS` | `120` | Längster erlaubter Pull-Timer |
| `DISCORD_HOST_PORT` | `8081` | Port des Discord-Webinterfaces auf deinem PC |
| `DISCORD_GUILD` | leer | Server (Name oder ID), falls der Bot auf mehreren ist |
| `DISCORD_CHANNEL` | leer | Sprachkanal nach dem Start (leer = erst auf `!come` oder über das Webinterface) |
| `DISCORD_FOLLOW_USER` | leer | Der Discord-Bot folgt diesem Benutzer (Anzeigename) von Kanal zu Kanal |
| `DISCORD_TEXT_CHANNELS` | leer | Textkanäle für `!`-Befehle, kommagetrennt (leer = alle) |
| `DISCORD_ADMIN_ROLE` | leer | Rolle, die Admin-Befehle nutzen darf (Server-Admins dürfen immer) |

Beide Bots lesen dieselbe `.env`.
`MUMBLE_*` gilt nur für Mumble, `DISCORD_*` nur für Discord, alles andere für beide.
Neue Einstellungen aus `.env.example` ergänzt `start.bat` automatisch am Ende deiner `.env`, vorhandene Werte bleiben unverändert.

## Sounds

Sounds liegen im Ordner `sounds`.
Unterordner werden im Webinterface zu Kategorien.
Ordner, die mit `_` oder `.` beginnen, und die Ordner aus `IGNORE_DIRS` werden ignoriert.
Unterstützt wird alles, was ffmpeg lesen kann: MP3, WAV, OGG, Opus, FLAC, M4A, AAC, WebM.

Neue Dateien kannst du einfach in den Ordner kopieren, der Bot findet sie nach wenigen Sekunden.
Alternativ Dateien ins Browserfenster ziehen oder „Hochladen“ verwenden.
Gelöschte Sounds landen in `data/papierkorb` und können von dort zurückkopiert werden.

## Webinterface

- Klick auf eine Kachel spielt den Sound im Kanal ab.
- Die Ordner stehen als Chips über den Kacheln.
  Hat ein Ordner Unterordner, klappt ein Klick darauf eine zweite Zeile mit diesen Unterordnern auf.
  Während der Suche zeigen die Chips nur Ordner mit Treffern und die Zahl der Treffer darin.
- `/` oder `Strg+K` springt in die Suche, `Enter` spielt den ersten Treffer, `Esc` stoppt alles.
- Rechtsklick oder `⋯` auf einer Kachel: lokal vorhören, eigene Lautstärke pro Sound, Favorit, umbenennen, Kategorie ändern, löschen.
- In der Seitenleiste: laufende Sounds einzeln stoppen, Kanal wechseln, Folgen ein- und ausschalten, Benutzer im Kanal, Aktivitätsverlauf.

Das Webinterface ist nur auf deinem PC erreichbar (Port an `127.0.0.1` gebunden).
Läuft es auf einem Server, schützt die Einstellung `WEB_PASSWORD` es mit einem Passwort (beliebiger Benutzername), siehe [DEPLOYMENT.md](DEPLOYMENT.md).

## Betrieb auf einem Server (Kubernetes)

Neben dem lokalen Setup gibt es ein Helm-Chart (`charts/bard-board`) und eine CI mit GitHub Actions, die Image und Chart baut, testet und optional ausrollt.
Alles dazu, inklusive Umzug von Sounds, Sammlungen und Join-Sounds, steht in [DEPLOYMENT.md](DEPLOYMENT.md).

## Musik (YouTube)

Unten im Webinterface sitzt die Player-Leiste.
Ins Eingabefeld passt ein YouTube-Link, ein Playlist-Link oder einfach ein Suchbegriff, `Enter` hängt ihn an die Warteschlange an.
Der Knopf daneben spielt den Titel als Nächstes, die Schere speichert einen Ausschnitt als neuen Sound.
Die Warteschlange steht in der Seitenleiste, dort lassen sich Titel per Ziehen umsortieren, entfernen, mischen oder alle löschen.

- Während ein Soundboard-Sound läuft, wird die Musik automatisch leiser (Ducking).
- Musik und Sounds haben getrennte Lautstärken, die Gesamtlautstärke oben gilt für beides.
- `Stopp` bzw. `Esc` beendet alle Sounds und pausiert die Musik, die Warteschlange bleibt erhalten.
- Bei einem Link mit `watch?v=...&list=...` wird nur das Video gespielt, für die ganze Playlist den Link der Playlist-Seite nehmen.
- Titel über `MAX_TRACK_MINUTES` werden übersprungen.
- Livestreams laufen, bis jemand `/skip` macht, was dahinter in der Warteschlange steht, wartet so lange.
  Im Player steht dann „LIVE“ statt einer Zeitleiste.
  Bricht die Verbindung ab oder läuft die Stream-Adresse ab, steigt der Bot wieder live ein.
  Ist außer dem Bot `LIVE_IDLE_MINUTES` lang niemand im Kanal, pausiert der Stream und lädt nichts mehr, beim Fortsetzen geht es live weiter.
  Streams und Premieren, die erst später beginnen, lehnt der Bot mit der Startzeit ab.
- **Sender:** `/radio add lofi <link>` speichert einen Livestream, `/radio lofi` startet ihn, `/radio` listet alle, `/radio del lofi` löscht.
  Läuft schon ein Sender, wird sofort umgeschaltet, ein normaler Titel spielt dagegen erst zu Ende.
  Im Webinterface stehen die Sender als Knöpfe über der Warteschlange, verwalten lassen sie sich im Regler-Dialog oben rechts.
- Die Warteschlange übersteht einen Neustart, die Musik startet danach aber erst auf Knopfdruck oder mit `/resume`.
- Bricht ein Stream ab, setzt der Bot an derselben Stelle fort.
- Lehnt YouTube einen Stream ab (Fehler 403), holt der Bot erst eine frische Stream-Adresse und lädt notfalls über den eigenen Downloader von yt-dlp, der YouTubes Eigenheiten kennt.
- yt-dlp aktualisiert sich bei jedem Start selbst, weil YouTube regelmäßig Dinge ändert.
  Außerdem sieht der Bot einmal am Tag nach, ob es eine neue Version gibt.
  Dann startet er sich kurz neu, sobald gerade nichts läuft, und ist nach wenigen Sekunden wieder im Kanal.
  Spielt YouTube einmal gar nichts mehr, hilft meist ein Neustart über `start.bat`.

## Durchsagen, Pull-Timer, Join-Sounds und Kürzel

- **Durchsage:** `/tts Text` oder das Feld „Durchsage“ in der Seitenleiste lässt den Bot den Text sprechen.
  Die Stimme ist Piper „Thorsten“, sie läuft komplett offline im Container.
- **Pull-Timer:** `/pull` (10 s) oder `/pull 15` sagt „Pull in 15 Sekunden“, zählt ab 5 laut herunter und spielt bei „Pull!“ ein Kriegshorn.
  `/pull stop` bricht ab, in der Seitenleiste gibt es Knöpfe für 10, 15 und 30 Sekunden.
- **Join-Sounds:** `/joinsound leeroy` legt deinen Sound fest, der beim Betreten des Bot-Kanals spielt (höchstens alle 10 Minuten).
  `/joinsound` zeigt ihn an, `/joinsound aus` entfernt ihn, `/joinsound @Name sound` setzt ihn für andere.
- **Kürzel:** `/alias lr leeroy` macht `/lr` zum Kürzel für den Sound, `/alias party /play <link>` für einen ganzen Befehl.
  `/alias` listet alle, `/unalias lr` löscht eines.
- Join-Sounds und Kürzel lassen sich auch im Webinterface verwalten (Regler-Symbol oben rechts), und im Menü jeder Kachel gibt es „Als Join-Sound ...“.
- Fremde Join-Sounds und Kürzel anderer ändern dürfen nur Admins, sofern `ADMINS` gesetzt ist.

## Sammlungen (z. B. alle Anno-1800-Sprachzeilen)

Große Sound-Sammlungen mit Tausenden Dateien gehören nicht aufs Board, sondern in den Ordner `collections`.
Jeder Unterordner dort ist eine Sammlung, seine Unterordner sind Gruppen, z. B. Sprecher.
Der Ordner wird nur lesend eingebunden, der Bot verändert dort nichts.

- Im Webinterface erscheint über der Suche eine Umschaltung, z. B. „Soundboard | Anno 1800“.
- Links stehen die Sprecher mit Anzahl, rechts die Zeilen, 100 auf einmal, beim Scrollen kommen weitere.
- Die Suche findet Wörter im gesprochenen Text, im englischen Text, im Sprechernamen und in der Situation.
- Klick auf eine Zeile spielt sie im Kanal, der Kopfhörer spielt sie lokal im Browser.
- Der Knopf rechts legt eine Zeile als normale Kachel aufs Board, alle in die Kategorie „Anno 1800“.
  Der Sprecher steht klein auf der Kachel, der volle Text im Tooltip, und beides ist in der Suche auffindbar.
  Die Kachel lässt sich wie jeder Sound umbenennen, als Join-Sound nutzen oder als Kürzel ansprechen.
- `/anno schiff` spielt eine zufällige Zeile, in der „schiff“ vorkommt, und schreibt Sprecher und Text in den Chat.
  `/anno @bente` nimmt nur Zeilen dieses Sprechers, `/anno` allein eine ganz zufällige.
  Der Befehl entsteht aus dem Ordnernamen: aus „Anno 1800“ werden `/anno` und `/anno1800`, sofern es noch keinen gleichnamigen Befehl gibt.

Liegt im Sammlungsordner eine `_index.csv` mit den Spalten `Charakter`, `Datei` und `Text (Deutsch)`, nutzt der Bot den Text daraus.
Optional sind `Text (Englisch)`, `Situation` und `Stimmung`, damit lässt sich zusätzlich nach positiv oder negativ filtern.
Ohne Index dient der Dateiname als Text.
Ordner, die mit `_` beginnen, erscheinen ohne Unterstrich und am Ende der Liste, z. B. „Ohne Zuordnung“.
Neue Dateien tauchen nach spätestens 10 Minuten auf, sofort mit dem Knopf „Neu einlesen“ (Pfeil im Kreis) oder nach einem Neustart.
Dabei liest der Bot nur Ordner neu, die sich geändert haben, das bleibt auch bei Zehntausenden Dateien schnell; einmal pro Stunde liest er alles komplett.
Dateien, die gerade noch geschrieben werden (`.part`, `.tmp`), werden übersprungen.
Weitere Aufnahmen derselben Zeile („Zeile (2).mp3“) erscheinen mit dem Text „Zeile“.

### Sammlung aus einem anderen Ordner einbinden

Liegt eine Sammlung woanders und wird dort noch ergänzt (z. B. ein laufender Export), muss sie nicht kopiert werden.
Lege dazu im Projektordner eine Datei `docker-compose.override.yml` an, Docker liest sie beim Start automatisch mit:

```yaml
x-sammlungen: &sammlungen
  - type: bind
    source: ${USERPROFILE}/sounds/anno1800
    target: /collections-extern/Anno 1800
    read_only: true

services:
  soundboard:
    volumes: *sammlungen
  discord:
    volumes: *sammlungen
```

`&sammlungen` und `*sammlungen` sorgen dafür, dass beide Bots dieselben Sammlungen sehen.
Für jede weitere Sammlung kommt ein weiterer Eintrag dazu, z. B. `source: ${USERPROFILE}/sounds/dota` mit `target: /collections-extern/Dota 2`.
Die Chat-Wheel-Sounds (`sounds/dota chatwheel` als `Dota Chatwheel`) antworten auf `/chatwheel`, weil `/dota` schon vergeben ist, z. B. `/chatwheel @tsunami` oder `/chatwheel @liquid`.
Wer `/cw` möchte, legt ein Kürzel an: `/alias cw /chatwheel`.
Die WoW-Sammlung (`sounds/wow` als `WoW`) antwortet auf `/wow`, z. B. `/wow moo`, `/wow @orc female mana` oder `/wow @tauren`.
Passt ein `@`-Name auf mehrere Ordner (bei `@tauren` Spieler und NPCs, männlich und weiblich), spielt der Bot eine Zeile aus allen.
Steht im Index neben dem Ordner eine Spalte „Sprecher/Team“, lässt sich auch danach mit `@` filtern.
Bei Zeilen in anderen Sprachen („Text (Original)“) zeigen Webinterface und Chat die englische Übersetzung mit an.
Der Name hinter `/collections-extern/` wird zum Namen der Sammlung, aus „Dota 2“ werden die Befehle `/dota` und `/dota2`.
Gibt es eine Sammlung gleichen Namens auch in `collections`, gewinnt der eingebundene Ordner.
Die Datei gehört nur zu deinem PC und ist deshalb nicht Teil des Projekts.
Die Zeilen werden beim Abspielen dekodiert und nur im Arbeitsspeicher zwischengespeichert, damit kein riesiger Cache auf der Platte entsteht.

## Chat-Befehle in Mumble

| Befehl | Wirkung |
| --- | --- |
| `/sb name` | Sound abspielen |
| `/name` | Kurzform, z. B. `/leeroy` |
| `/sb kategorie/name` | Sound aus einer bestimmten Kategorie |
| `/random [filter]` | Zufälliger Sound, optional gefiltert |
| `/again` | Letzten Sound wiederholen |
| `/stop` | Alle Sounds stoppen und Musik pausieren |
| `/list [filter]` | Sounds auflisten |
| `/search text` | Sounds suchen |
| `/top` | Meistgespielte Sounds |
| `/np` | Was läuft gerade? |
| `/vol [0-100]` | Gesamtlautstärke anzeigen oder setzen, auch relativ: `/vol +10` |
| `/come` | Bot in den eigenen Kanal holen |
| `/follow [name]` | Bot folgt dir oder `name` |
| `/unfollow` | Bot folgt niemandem mehr |
| `/help` | Alle Befehle |

**Musik**

| Befehl | Wirkung |
| --- | --- |
| `/play link-oder-suche` | YouTube-Link, Playlist oder Suchbegriff in die Warteschlange (auch `/yt`, `/m`) |
| `/playnext link-oder-suche` | Als Nächstes spielen (auch `/pn`) |
| `/skip` | Aktuellen Song überspringen (auch `/next`, `/n`) |
| `/pause`, `/resume` | Musik pausieren und fortsetzen (auch `/weiter`) |
| `/queue` | Warteschlange anzeigen (auch `/q`) |
| `/remove nr` | Titel aus der Warteschlange entfernen, Nummer wie bei `/queue` |
| `/clear` | Warteschlange leeren |
| `/shuffle` | Warteschlange mischen |
| `/mvol [0-100]` | Musik-Lautstärke anzeigen oder setzen |
| `/clip link 1:23 1:27 [name]` | Ausschnitt eines Videos als neuen Sound speichern |
| `/radio [name]` | Gespeicherten Livestream starten, ohne Namen alle Sender anzeigen (auch `/sender`) |
| `/radio add name link` / `/radio del name` | Sender speichern bzw. löschen |

**Extras**

| Befehl | Wirkung |
| --- | --- |
| `/tts text` | Text vorlesen (auch `/say`, `/sag`) |
| `/pull [sekunden oder stop]` | Pull-Timer mit Countdown, Standard 10 Sekunden |
| `/joinsound [sound oder aus]` | Eigenen Join-Sound anzeigen, setzen oder entfernen (auch `/js`) |
| `/joinsound @name sound` | Join-Sound für jemand anderen setzen |
| `/alias [name] [ziel]` | Kürzel auflisten, anzeigen oder anlegen |
| `/unalias name` | Kürzel löschen |
| `/anno [suche]` | Zufällige Zeile aus der Sammlung „Anno 1800“, passend zur Suche |
| `/anno @name [suche]` | Zufällige Zeile eines bestimmten Sprechers |

Namen werden tolerant gesucht: Groß- und Kleinschreibung, Umlaute (`kase` findet `käse`), Anfänge und kleine Tippfehler.
Bei mehreren Treffern schlägt der Bot passende Namen vor.
Statt `/` funktioniert auch `!`.

Der Bot reagiert auf Nachrichten in seinem Kanal und auf private Nachrichten.
Bist du in einem anderen Kanal, schick ihm den Befehl per Rechtsklick auf den Bot und „Nachricht senden“, z. B. `/come`.

## Chat-Befehle in Discord

In Discord gibt es dieselben Befehle, im Chat mit `!` statt `/`, z. B. `!sb leeroy`, `!leeroy` oder `!play link`.
Der Bot reagiert in allen Textkanälen (einschränken mit `DISCORD_TEXT_CHANNELS`), im Chat der Sprachkanäle und auf Direktnachrichten.
In Direktnachrichten und mit @Erwähnung geht es auch ohne `!`, z. B. `@Soundboard leeroy`.

Zusätzlich gibt es jeden Befehl als Slash-Befehl, z. B. `/sb`, `/play`, `/radio` oder `/joinsound`.
Discord zeigt dabei eine Liste der Befehle, bei `/sb`, `/joinsound`, `/radio` und `/unalias` auch passende Namen zum Antippen.
Antworten auf Slash-Befehle sieht nur, wer den Befehl geschickt hat, damit der Kanal nicht zugespammt wird.
Ansagen wie „Jetzt: …“ erscheinen im Chat des Sprachkanals, in dem der Bot ist.

Admin-Befehle (`!vol`, `!come`, `!follow`, `!unfollow`, `!mvol`) dürfen Server-Admins, die Rolle aus `DISCORD_ADMIN_ROLE` und die Namen aus `ADMINS`.
Ist weder `ADMINS` noch `DISCORD_ADMIN_ROLE` gesetzt, dürfen alle.

## Technik

- Python 3.13, eigener asyncio-Mumble-Client (TLS, Protobuf, Opus über den TCP-Tunnel), funktioniert mit Murmur/mumble-server 1.2 bis 1.5.
- Discord über discord.py 2.7 (mit `davey` für die DAVE-Verschlüsselung der Sprachkanäle).
  Der Kern (Mixer, Musik, Befehle, Webinterface) kennt keine Plattform, kleine Adapter in `app/platforms` übersetzen.
  Ein Container bedient genau eine Plattform (`PLATFORM=mumble` oder `discord`).
- Mixer mit 20-ms-Frames: Sounds können sich überlagern, ein weicher Limiter verhindert Übersteuern.
- Ausgehende Chat-Nachrichten und Kanalwechsel werden auf das Server-Limit (`messagelimit`/`messageburst`) gedrosselt, verlorene Kanalwechsel werden wiederholt.
- Sounds werden einmal dekodiert, normalisiert und in `data/cache` zwischengespeichert, dadurch startet die Wiedergabe sofort.
- Der Sounds-Ordner wird regelmäßig abgefragt, weil Dateisystem-Events unter Docker Desktop für Windows nicht ankommen.
- Webinterface: Starlette, WebSocket für Live-Updates, reines HTML/CSS/JS ohne Build-Schritt.
- Verbindungsabbrüche werden automatisch mit wachsendem Abstand neu versucht (1 s bis 60 s).
- Sprachausgabe: Piper (ONNX, offline) mit der Stimme „Thorsten“, Pull-Timer-Ansagen werden vorab erzeugt, damit der Countdown exakt läuft.
- Musik: yt-dlp (mit Deno als JavaScript-Laufzeit für YouTube) liefert die Stream-Adresse, ffmpeg dekodiert und normalisiert live, der Mixer mischt Musik und Sounds zusammen.

### Tests

```bash
# Unit-Tests im Container
docker compose run --rm soundboard python -m unittest discover -s tests -t .

# End-to-End-Test gegen einen echten Mumble-Server (SuperUser-Passwort nötig, legt Testkanäle an)
python tools/make_test_sounds.py /tmp/testsounds
python tools/e2e_test.py --host 127.0.0.1 --superuser-password ... --sounds /tmp/testsounds
```

Die CI startet dafür einen eigenen Mumble-Server (`tools/ci/murmur.sh`).

Der End-to-End-Test startet den Bot, verbindet einen zweiten Client und prüft per Frequenzanalyse, dass die Sounds wirklich hörbar ankommen.
Außerdem testet er Chat-Befehle, Folgen, Kanalwechsel, Überlagerung, Web-API und Reconnect.
Für die Musik-Tests liefert er selbst Audio-Streams über einen lokalen HTTP-Server aus und prüft Warteschlange, Playlists, Skip, Pause, Ducking, Fehlerfälle und Clips (`--music-only` für nur diesen Teil).
Der Discord-Adapter wird ohne Discord-Zugang mit nachgebauten Discord-Objekten getestet (`tests/test_discord.py`), inklusive echtem discord.py-Abspielthread.

### Ohne Docker

Voraussetzungen: Python 3.11 oder neuer, ffmpeg, libopus und für YouTube Deno.

```bash
pip install -r requirements.txt
export MUMBLE_HOST=... SOUNDS_DIR=./sounds DATA_DIR=./data
python -m app.main
```
