# Unraid SSH

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)

Home-Assistant-Integration, die einen **Unraid**-Server per **SSH** liest und
schaltet — ohne Unraid Connect, ohne GraphQL-API und ohne Dienst auf dem
Server. Sie liest dieselben Statusdateien wie das Unraid-Webinterface
(`/var/local/emhttp/*.ini`) und ruft dieselben Skripte wie dessen Schaltflächen.
Auf dem Server wird nichts installiert.

Sie kann parallel zur Unraid-Connect-Integration laufen (eigene Domain
`unraid_ssh`).

## Voraussetzungen

- Home Assistant Core **2026.7.0** oder neuer (Python-Paket `asyncssh` wird von
  Home Assistant installiert).
- Der Unraid-Server ist von Home Assistant per SSH erreichbar (Standardport 22;
  siehe Unraid → Einstellungen → Verwaltungszugriff).
- Zugang als Benutzer **root** per **SSH-Schlüssel**. Die Integration erzeugt
  ihr Schlüsselpaar selbst; es wird kein Passwort gespeichert. Du trägst den
  öffentlichen Schlüssel einmal im Unraid-Terminal ein (siehe Einrichtung).
- Root-Rechte sind nötig, weil Docker, `virsh`, Compose-Manager-Skripte und
  Sensoren gelesen bzw. aufgerufen werden.

## Installation über HACS

1. HACS → Integrationen → ⋮ → **Benutzerdefinierte Repositories**
2. Repository `https://github.com/luukkii123/ha-unraid-ssh-integrations`,
   Kategorie **Integration** → hinzufügen
3. **Unraid SSH** herunterladen und Home Assistant neu starten

## Einrichtung

Einstellungen → Geräte & Dienste → **Integration hinzufügen** → *Unraid SSH*.

**Schritt 1 — Server**

| Feld | Standard | Bedeutung |
| --- | --- | --- |
| Host | — | IP-Adresse oder Hostname des Unraid-Servers, ohne Protokoll |
| SSH-Port | 22 | Port des SSH-Dienstes |
| Name | Unraid SSH | Titel des Eintrags; wird zum Präfix aller Entitäts-IDs (z. B. `sensor.unraid_ssh_…`) |

Pro Host und Port ist nur ein Eintrag möglich. Die Integration holt dabei den
SSH-Hostschlüssel des Servers und merkt ihn sich (Trust on first use).

**Schritt 2 — Schlüssel freigeben**

Der Dialog zeigt den Fingerabdruck und zwei Zeilen. Beide einmal im
Unraid-Terminal als root ausführen: die erste schreibt in
`/boot/config/ssh/root.pubkeys` (übersteht den Neustart), die zweite in
`/root/.ssh/authorized_keys` (wirkt sofort). Danach absenden — die Integration
testet die Verbindung mit dem Schlüssel und legt erst dann den Eintrag an.

Fehlermeldungen: Server nicht erreichbar, Schlüssel abgelehnt, Hostschlüssel
geändert, „sieht nicht nach Unraid aus" (keine `var.ini`), Schlüssel nicht
lesbar.

Der private Schlüssel liegt in den Daten des Config-Entrys (`.storage` von Home
Assistant), wie Zugangsdaten anderer Integrationen. Sicherungen von Home
Assistant enthalten ihn daher.

### Optionen

Am Eintrag **Konfigurieren**:

| Option | Standard | Bereich | Bedeutung |
| --- | --- | --- | --- |
| Abfrageintervall | 30 s | 10–300 | Abstand der SSH-Abfragen für System, Platten, Container, VMs |
| Intervall der Updateprüfung | 6 h | 1–24 | Abstand der Prüfung der Container-Images gegen ihre Registries |

### Erneute Anmeldung und Adresse ändern

- **Reauth:** Wird der Schlüssel abgelehnt oder ändert sich der Hostschlüssel,
  zeigt Home Assistant „Erneut anmelden". Der Dialog nennt den Hostschlüssel-
  Fingerabdruck und die beiden Zeilen zur Freigabe. Das Häkchen **Neuen
  Host-Key annehmen** nur setzen, wenn bekannt ist, warum er sich
  geändert hat (z. B. Unraid neu installiert).
- **Neu konfigurieren:** ⋮ am Eintrag → *Neu konfigurieren* ändert Host und Port;
  der Schlüssel bleibt, die Verbindung wird gegen die neue Adresse getestet.

## Entitäten

### Benutzer-, USB- und Coral-Inventar

Ab Version 0.10 liefern drei zusätzliche Dashboardsensoren die Anzahl und die
aktuellen Listen für Benutzer, USB-Geräte und Coral TPUs. Die passenden
Unraid Cards ab 0.3 zeigen diese Daten als eigene Kacheln. Erfolgreich leere
Quellen ergeben 0; fehlgeschlagene Quellen bleiben nicht verfügbar.

- **Benutzer:** Schreiben/Lesen zählt SMB-Shares mit der jeweiligen Berechtigung,
  keine Übertragungsmenge und keine aktiven Sitzungen. root, ausgeschaltetes
  SMB oder unvollständige Rechtequellen haben unbekannte Zähler. Kennwortstatus
  ist ausschließlich ein Ja/Nein-Hinweis; Passwortwerte werden nicht gelesen.
- **USB:** Host-Sysfs liefert das sichtbare Inventar. „Am Host erkannt“ bedeutet
  keine freie Verfügbarkeit. Aktive VM-Zuordnungen benötigen einen eindeutigen
  libvirt-Beleg. USB Manager ist optional; unvollständiges Mapping wird
  gekennzeichnet. Ein nur gebundenes USB/IP-Gerät gilt nicht als verbunden.
  Ein Remoteziel ohne sicheren Gerätebezug bleibt unbekannt. Geräte hinter
  vollständig an VMs durchgereichten USB-Controllern sind am Host unsichtbar.
- **Coral:** PCI-Status und Temperatur stammen aus vorhandenen Kernelattributen;
  einzelne PCI-Temperaturen bleiben normale HA-Messsensoren. USB-Coral wird
  ausschließlich als erkannt angezeigt, ohne angenommene Betriebsbereitschaft
  oder Temperatur. Die Integration öffnet keine TPU-Geräteknoten und führt
  keine Inferenz oder Treiberinstallation aus.

Diese Inventaransichten sind lesend. Benutzernamen, Beschreibungen und belegte
VM-Zuordnungen sind im HA-State sichtbar. Listen und wechselnde Inventarmetadaten
werden nicht im Recorder gespeichert; PCI-Coral-Temperaturmesswerte behalten
ihre Historie. Benutzerrechte werden alle fünf Minuten, VM-USB-Zuordnungen
höchstens einmal pro Minute aktualisiert; USB- und Coralstatus folgen dem
Abfrageintervall.


Die Entitäten verteilen sich auf Geräte: Server, je Compose-Stack, je Platte,
je Share, je VM, je GPU sowie ein Sammelgerät „Freie Container" für Container
ohne Compose-Projekt. Namen folgen dem Muster `<Präfix> <Gerät> …`.

| Gruppe | Inhalt |
| --- | --- |
| Server | CPU-Auslastung, Speicherauslastung, Systemlast (1 min), letzter Start, Array-Status, Paritätsprüfung (einschließlich Pause), Mover; zusätzliche Dashboarddaten und Serverknöpfe |
| Temperaturen | je Kanal aus `/sys/class/hwmon` ein Sensor, dazu die festen Sensoren **CPU-Temperatur** und **Mainboard-Temperatur** |
| Lüfter | je Lüfter Drehzahl (RPM) und PWM-Ansteuerung (%); GPU-Lüfter in Prozent. Nur Anzeige — die Integration schreibt nie nach `pwm` |
| GPU (NVIDIA, über `nvidia-smi`) | Auslastung, VRAM, Temperatur, Leistungsaufnahme, Lüfter |
| Platten und Pools | Temperatur, Belegung in Prozent, gesamter/belegter/freier Speicher, Status; „Standby" für schlafende Platten |
| Shares | Belegter und freier Speicher |
| Docker-Container | Schalter *Status* (an/aus), Neustart-Knopf, CPU-, RAM- und RAM-Limit-Sensoren, Update-Entität |
| Compose-Stacks | Schalter *Status*, Neustart-Knopf, Update-Knopf „Alle Container aktualisieren" |
| VMs | Schalter *Status*, Betriebszustand, Neustart-Knopf, Knopf *Sofort ausschalten*, CPU und RAM (zugewiesen, maximal, Host-RSS, Gast belegt) |
| Updates | Sensor „Container mit Updates", Sensor „Update-Warteschlange", Knöpfe „Jetzt prüfen" und „Alle Updates installieren" |

Hinweise:

- **Speicherplatz** kommt direkt aus den Dateisystemwerten von Unraid, mit
  KiB als nativer Einheit. Bei Pools liefert das Gerät mit Dateisystem die
  Poolkapazität; Mitglieder ohne eigenes Dateisystem bekommen keine
  Kapazitätssensoren. Fehlende oder ungültige Werte bleiben unbekannt.
- **Mainboard-Sensoren** (SYSTIN, CPUTIN, Gehäuselüfter) erscheinen erst, wenn
  der Treiber des Super-I/O-Chips geladen ist; Unraid lädt ihn nicht von allein.
  Entweder das Plugin *Dynamix System Temperature* nutzen oder den Treiber
  selbst laden (z. B. `modprobe nct6775`, bei älteren Boards `it87`,
  `w83627ehf`) und den Aufruf in `/boot/config/go` eintragen. Die Integration
  lädt selbst keine Treiber.
- Angelegt wird jeder Kanal, den der Kernel meldet; **aktiviert** sind nur die
  aussagekräftigen (CPU-Paket, SYSTIN, CPUTIN, NVMe-Composite, drehende
  Lüfter). Die übrigen lassen sich unter Einstellungen → Geräte & Dienste →
  Entitäten einzeln aktivieren. Kanäle mit erstem Messwert außerhalb von
  1–150 °C gelten als unbelegt und bekommen keine Entität.
- **Container-Updates:** Die Update-Entität vergleicht Registry-Digests und
  installiert auf Wunsch (bei Compose-Containern `pull` + `up -d` für genau
  diesen Dienst, ein gestoppter Container startet dabei mit). Auf Digest
  gepinnte Images (`repo@sha256:…`) und lokal gebaute Images ohne Registry-Digest
  melden nie ein Update. Updates laufen in einer seriellen Warteschlange, die
  einen HA-Neustart übersteht.
- **VMs:** Ausschalten über den Schalter sendet `virsh shutdown` (geordnetes
  Herunterfahren). Reagiert der Gast nicht, trennt *Sofort ausschalten*
  (`virsh destroy`) die VM hart — ungespeicherte Gastdaten gehen verloren.
  Der Knopf ist nur bei laufender VM verfügbar und wird nie automatisch als
  Fallback benutzt.
- **Neustart** führt `docker restart` bzw. `docker compose … restart` bzw.
  `virsh reboot` aus, keine Stop/Start-Kette. Für Stacks nur verfügbar, wenn
  Compose-Dateien gemeldet werden.
- Ein Compose-Stack ohne gemeldete Compose-Datei und ohne Ordner im
  Compose-Manager bekommt keinen Stack-Schalter und keine Update-Installation;
  die Schalter der einzelnen Container bleiben.
- **Containerbilder:** Vorrang hat das Unraid-Cachebild (wird über SSH gelesen
  und unter `/local/unraid_ssh/` ausgeliefert), danach die URL aus dem Label
  `net.unraid.docker.icon`, sonst normale HA-Icons. Eine `https`-Adresse lädt
  der Browser direkt; eine reine `http`-Adresse (etwa ein Dienst im eigenen
  Netz) holt der Unraid-Server selbst ab, damit sie auch auf einem
  `https`-Dashboard erscheint. Bei der Tile-Karte *Bild der Entität anzeigen*
  einschalten.
- **Stackbilder und -namen:** Der Stack-Schalter trägt das Bild des ersten
  Dienstes mit Bild in der Reihenfolge der Compose-Datei; Icons aus dem
  Compose Manager (Override-Datei) zählen auch, solange der Stack gestoppt ist.
  Das Attribut `stack_display_name` nennt den Namen, den der Compose Manager
  zeigt (Datei `name` des Projektordners, sonst der Ordnername); `stack_name`
  bleibt der Compose-Projektname. Die Compose-Dateien werden nur gelesen.
- **Aufräumen:** Entitäten, die fünf Minuten lang in keiner erfolgreichen
  Abfrage mehr vorkommen, werden entfernt (nur wenn der zuständige Abschnitt
  gelesen werden konnte); leere Geräte verschwinden mit. Ein ausgeschalteter
  Stack behält seine Entitäten. Einmal-Container (`docker run` ohne Compose und
  ohne Unraid-Verwaltung) bekommen keine Entität.

## Zusätzliche Dashboarddaten

Ab Version 0.9 stehen weitere Daten für Automationen und Unraid Cards bereit:
CPU-Kerne und belegbare CPU-Package-Leistung, RAM-Aufteilung, Mainboard/BIOS,
Servermodell und Lizenztyp, Boot-/Log-/Docker-Image-Belegung, Netzwerk-Interfaces,
SMART-Cache und Poolzustand, Share-Sicherheit und offene Pfade sowie
VM-Host-CPU und Netzwerk-/Datenträgerdurchsatz. NVIDIA-Modell und Treiber
werden ergänzt, soweit die vorhandene Abfrage sie liefert.

Einzelwerte wie Leistung, Durchsatz und Auslastung sind normale Sensoren.
Die sieben Dashboard-Sammelsensoren enthalten strukturierte Listen in ihren
Attributen. Diese Listen und wechselnden Metadaten bleiben aus dem Recorder
ausgenommen; aktuelle Werte sind weiterhin über Home Assistant lesbar.
Numerische Messwerte behalten ihre Historie. Die bisherigen IDs bleiben erhalten.

Die Integration erkennt optionale Datenquellen einzeln. Fehlende Treiber,
Dateien oder Programme ergeben unbekannte bzw. teilweise verfügbare Daten;
sie führen nicht zum Laden von Treibern oder Ausführen von Plugin-Hooks.
SMART stammt ausschließlich aus vorhandenem Cache und weckt keine Platten.
CPU-Leistung ohne belastbare Quelle oder eindeutiges Zählerdelta wird nicht
geschätzt. Netzwerkraten verschiedener Interfaces werden nicht summiert.

Docker kann eine Image-Datei oder ein Verzeichnis verwenden. Eine eigene
vdisk-Belegung erscheint nur bei belegtem Image-Mount; die Kapazität eines
Pools wird nicht als exklusiver Docker-Verbrauch ausgegeben. Share-Streams
zählen offene eindeutige Pfade wie im Unraid-Dashboard, keine SMB-Sitzungen.
Der bestehende VM-CPU-Sensor zählt weiterhin 100 % je belegtem Hostkern;
der zusätzliche Hostanteil hat einen eigenen eindeutigen Namen.

## Aktionen

Die Integration registriert keine eigenen Aktionen (Services). Gesteuert wird
über Schalter, Knöpfe und Update-Entitäten (`switch.turn_on/off`,
`button.press`, `update.install`).

Zusätzliche Serverknöpfe: Neustart, Ausschalten, Array starten/stoppen,
Parität prüfen, pausieren, fortsetzen oder abbrechen, alle Platten schlafen
legen und Mover starten. **Paritätsstart prüft ausschließlich ohne Korrektur.**
Unbekannte oder unzulässige Array-/Sync-Zustände werden abgewiesen. Arraystart
verlangt eine vollständige, frisch belegte Poolinventur; bei unklaren Pools
muss der Start in Unraids WebGui geprüft werden. Eine Laufzeitsperre verhindert
gleichzeitige Serverbefehle und konkurrierende Container-Updatejobs.
Ein Timeout oder Verbindungsabbruch wird nicht automatisch wiederholt und ist
kein Beleg für einen erfolgreichen Neustart oder Shutdown.

Die Karten verlangen vor Serveraktionen eine Bestätigung. Direkte HA-Knöpfe
und Automationen besitzen diese Kartenbestätigung nicht; deren Ausführung
unterliegt weiterhin den Backendprüfungen.

## Sicherheitshinweise

- Die Integration verbindet sich als **root**. Wer Zugriff auf Home Assistant
  oder dessen Sicherungen hat, erhält damit Zugriff auf den Unraid-Server.
  Den Zugang entsprechend schützen.
- Der Hostschlüssel wird beim ersten Verbinden gemerkt und danach erzwungen; bei
  einer Abweichung wird die Verbindung verweigert, bis du ihn bewusst
  akzeptierst.
- Schreibende Aktionen sind auf die beschriebenen Container-/Stack-/VM- und
  Serverknöpfe sowie Container-Updates begrenzt; es gibt keine frei
  konfigurierbaren Shellbefehle.

## Entfernen

1. Eintrag löschen: Einstellungen → Geräte & Dienste → *Unraid SSH* → ⋮ → Löschen.
2. Auf dem Unraid-Server die Zeile mit `unraid_ssh@homeassistant` aus
   `/boot/config/ssh/root.pubkeys` und `/root/.ssh/authorized_keys` entfernen.
3. Optional in HACS entfernen und Home Assistant neu starten.

## Lizenz

MIT — siehe [LICENSE](LICENSE). Technische Hintergründe und Tests:
[docs/entwicklung.md](docs/entwicklung.md).
