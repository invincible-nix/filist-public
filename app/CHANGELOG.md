# filist – Änderungen

## 1.8.0 (2026-11-01)
- **Scroll-Ende**: Wer am Ende einer Seite weiterscrollt, sieht aus der Kante einen Verlauf aus ░ ▒ ▓ █ aufsteigen, am Mauszeiger am höchsten. Die Liste bewegt sich dabei nicht und der Effekt läuft ohne Überschwingen aus. Er erscheint nur, wenn kein umgebendes Scroll-Fenster mehr weiterscrollen kann. Abschalten: `"scroll_end": false` in ~/.config/rice-apps/settings.json oder Energiesparmodus.

## 1.7.0 (2026-10-28)
- **Suche mit Filtern**: Mit Enter im Filterfeld wird in allen Unterordnern gesucht – jetzt zusätzlich nach Typ, Endung,
  Größe und Alter: `typ:bild` (auch video, audio, dokument, archiv, code, ordner), `ext:pdf,docx`, `größe:>10M`,
  `größe:1k-5M`, `geändert:<7d` (neuer als 7 Tage), `geändert:>1y` (älter als ein Jahr), `geändert:heute`.
  Mit einem Namensteil kombinierbar, z. B. `bericht typ:dokument geändert:<30d`. Der Tooltip des Feldes erklärt es.
- **Verbinden abbrechen**: Beim Warten auf VPN, Netzlaufwerk oder langsame Ordner gibt es auf der Lade-Seite einen
  „abbrechen“-Knopf. Er beendet auch nmcli/eduvpn-cli im Hintergrund.
- **Verständlichere Fehler** bei VPN und Netzlaufwerken (Zeitüberschreitung, Anmeldung nötig, Server nicht gefunden …)
  sagen jetzt, was man tun kann.
- Behoben: Geschlossene Fenster geben ihren Speicher jetzt sauber frei (Wischfläche, Statuszeile).

## 1.6.0 (2026-10-27)
- **Wechsel-Animation**: Der Wechsel zwischen Hell und Dunkel (und zwischen Designs) läuft als Scanbalken über den ganzen Bildschirm. Alle Fenster, die Leiste und das Terminal wechseln darunter im selben Moment, hinter dem Balken steht das neue Design. An/aus und Dauer (Standard 1,5 s) stehen in der Designauswahl der Einstellungen.

## 1.5.1 (2026-10-26)
- **Untermenüs klappen beim Überfahren auf**: „öffnen mit“, „teilen“, „osint“, „vpn vorher verbinden“ & Co. öffnen sich direkt neben dem Eintrag, sobald der Zeiger darauf liegt – kein Umschalten des ganzen Menüs mehr.
- **Teilen auch für Ordner**: „teilen“ im Rechtsklick-Menü erscheint jetzt auch bei Ordnern (sie werden vorher als .zip gepackt) und bei gemischter Auswahl.

## 1.5.0 (2026-10-26)
- **Design wählbar**: In den Einstellungen gibt es die Designauswahl wie in notist (Automatisch + 9 Designs). Die Wahl gilt für alle -ist-Apps zugleich und wirkt sofort. Bei „Automatisch“ (Desktop-Farben) kannst du zwischen **Dunkel** und **Hell** umschalten. Zu finden im Zahnrad-Menü unter „design“.

## 1.4.2 (2026-10-25)
- **Update-Knopf**: Neben der Versionsnummer steht „󰑐“ – Klick sucht sofort nach einem Update. Gibt es eine neuere
  Version, wird daraus „󰚰 update“; ein Klick lädt und installiert sie (Linux und Windows), danach heißt er „󰑓 neu starten“.
- **Rückmeldung bei Umschalt-Knöpfen**: „An“ (z. B. „Ton behalten“) ist jetzt auch unter dem Mauszeiger zu sehen,
  Drücken färbt sofort – man muss die Maus nicht mehr wegbewegen, um zu sehen, ob der Klick angekommen ist.
- **Windows: echte Titelleiste** – das Fenster lässt sich verschieben, in der Größe ändern, minimieren und
  maximieren.

## 1.4.1 (2026-10-25)
- **Speichern-Dialog**: Einfach lostippen – egal wo der Fokus gerade ist, ersetzt die erste Taste den Dateinamen
  (die Endung bleibt) und alles Weitere landet im Namensfeld. Das Namensfeld bekommt außerdem sofort den Fokus,
  sobald das Fenster aktiv wird.

## 1.4.0 (2026-10-23)
- **Umbenennen direkt in der Zeile** wie im Windows-Explorer: `F2` oder Rechtsklick → umbenennen öffnet kein
  Fenster mehr, der Name wird in der Liste bzw. Kachel bearbeitet (Endung bleibt unmarkiert). `Enter` oder ein
  Klick daneben übernimmt, `Esc` bricht ab. **Neuer Ordner / neue Datei** werden sofort angelegt und gleich so benannt.
- **Teilen** im Rechtsklick-Menü: per Quick Share ans Handy (sharist), per Mail (mailist bzw. dein Mailprogramm,
  Dateien hängen schon an), per Bluetooth – und kurze Textdateien als QR-Code.
- **Netzlaufwerke hinter einem VPN gehen von selbst auf**: Für einen Ort in der Seitenleiste lässt sich per
  Rechtsklick → „vpn vorher verbinden“ eine VPN-Verbindung festlegen. Beim Öffnen baut filist sie automatisch auf
  (eduVPN holt dabei bei Bedarf ein neues Zertifikat), hängt das Laufwerk ein und zeigt solange einen Ladekreis.
  Antwortet ein Laufwerk nicht mehr (VPN war weg), verbindet filist einmal neu.
- Netzlaufwerke zeigen beim Verbinden und Laden einen Ladekreis statt einer leeren Fläche.
- **Markierter Text ist wieder lesbar**: helle Schrift auf Akzentfarbe statt dunkel auf dunklem Blau
  (Pfadleiste, Filter, Umbenennen, Vorschau, Dialoge).
- Beim Tippen in Filter, Pfadleiste oder beim Umbenennen gehören `Entf`, `Strg+A/C/X/V/Z` jetzt dem Textfeld –
  vorher griffen dort die Datei-Kürzel (z. B. markierte Strg+A alle Dateien statt den Text).

## 1.3.1 (2026-10-20)
- **Neues Zurück-Wischen im -ist-Design**: Beim Wischen mit zwei Fingern schiebt sich die vorige Seite als Rahmen vom Rand herein – mit einem echten Bild davon und ihrem Namen im Chip. Ist der Rahmen durchgezogen, führt Loslassen zurück, und die Seite klappt über das ganze Fenster auf. Nach vorn geht es genauso von rechts.
- Nach dem Zurück-Wischen springt die Seite nicht mehr ein Stück nach oben (der erste Millimeter der Geste scrollte noch mit und lief nach).

## 1.3.0 (2026-10-20)
- **Häufig geöffnet**: unter „zuletzt benutzt“ in der Seitenleiste stehen jetzt die Dateien, die du am öftesten
  öffnest (aus allen Apps, neuere zählen mehr) – aufklappbar. Klick öffnet, Mittelklick zeigt den Ordner,
  Rechtsklick nimmt eine Datei aus der Liste. Im Dateiauswahl-Dialog springt ein Klick zur Datei.

## 1.2.0 (2026-10-19)
- **Changelog**: oben rechts steht jetzt die Version – Klick darauf zeigt, was sich geändert hat. Nach einem
  Update ist sie hervorgehoben und die Statuszeile sagt einmal Bescheid.
- Super+E öffnet wieder sofort, auch wenn ein Netzlaufwerk (z. B. über VPN) eingebunden ist – der freie
  Speicherplatz wird jetzt im Hintergrund abgefragt.
- Seitenleiste und Vorschau bleiben so, wie du sie eingestellt hast, statt bei schmalen Fenstern zu verschwinden;
  im schmalen Fenster merkt sich filist eigene Breiten. Die Vorschau lässt sich weiter nach rechts schieben.
- Ältere GTK-Versionen (Ubuntu 24.04): Fehlermeldung beim Klicken behoben.
- Startgröße passt auf kleine Bildschirme (z. B. 1366×768).
- **Speicherleck behoben**: Jedes geschlossene Fenster blieb im Speicher (rund 5 MB pro Super+E), ebenso Zeilen,
  die beim Ordnerwechsel verworfen wurden, und die Fortschrittszeilen fertiger Kopieraufträge.

## 1.1.0 (2026-10-18)
- Dateinamen sind wieder gut lesbar (vorher dunkle Schrift auf dunklem Grund).
- **Wischen** mit zwei Fingern: zurück/vor wie im Browser. **Pinch-Zoom** wechselt zwischen Liste und Kacheln.
- Vorschau-Spalte lässt sich in der Breite verschieben, Rechtsklick-Menü mit Symbolen und großen Knöpfen für
  Umbenennen und Papierkorb.
- Tab-✕ und Abwählen per Klick ins Leere funktionieren.
- **Neuer Name: filist** (vorher „filewise“); alte Dienste und Portal-Einträge räumt der Installer weg.

## 1.0.0 (2026-10-18)
- Erste Veröffentlichung: Dateimanager im TUI-Look mit Tabs, geteilter Ansicht, Vorschau, Kacheln/Liste,
  Kopieren/Verschieben mit Rückgängig.
- Kann auch als **Datei-Dialog** für alle Programme dienen (xdg-desktop-portal) – beim Installieren wählbar.
