# Unraid SSH

Eine Home-Assistant-Integration, die einen **Unraid**-Server über **SSH**
liest und schaltet — ohne Unraid Connect, ohne GraphQL-API, ohne Dienst auf
dem Server. Sie liest dieselben Dateien wie das Unraid-Webinterface
(`/var/local/emhttp/*.ini`) und ruft dieselben Skripte wie dessen Knöpfe.

**Warum nicht die API?** Der `unraid-api`-Dienst hängt, kennt keine
Compose-Stacks und verliert nach Updates die VMs. SSH ist immer da.

## Was sie liefert

| Bereich | Inhalt |
| --- | --- |
| Server | CPU, RAM, Load, Uptime, Array-Status, Parity, Mover, je GPU, je Platte, je Share |
| Temperaturen | Je Kanal aus `/sys/class/hwmon` ein Sensor — CPU, Mainboard, NVMe —, dazu die beiden festen Sensoren **CPU-Temperatur** und **Mainboard-Temperatur**, deren ID sich auch bei einem Hardwaretausch nicht ändert |
| Lüfter | Je Lüfter **Drehzahl** in RPM und **Leistung** in Prozent (aus `pwm`, 0–255); beim GPU-Lüfter kommen die Prozent direkt von `nvidia-smi`. Nur Anzeige — die Integration schreibt nie nach `pwm` |
| Schalter | Docker-Container, Compose-Stacks (über das Compose-Manager-Plugin) und VMs — je Container, Stack oder VM ein/aus |
| Neustart | Native Docker-/Compose-Restartbuttons; keine Stop/Start-Kette |
| Updates | `update`-Entität je Container mit Registry-Digest, mit „Installieren" — auch für Compose-Container, die Unraids eigene Prüfung nicht sieht; dazu ein Zähler und ein Knopf „Jetzt prüfen" |

Diese Fassung bereitet **0.3.0** vor. Der bisher veröffentlichte Stand ist
**0.1.0**; HACS installiert Releases, nicht automatisch den Entwicklungszweig.

### Mainboard-Temperaturen brauchen einen Treiber

CPU-Kern und NVMe melden sich von selbst. Der **Super-I/O-Chip des
Mainboards** — und damit SYSTIN, CPUTIN und alle Gehäuselüfter — erscheint
erst, wenn sein Treiber geladen ist; Unraid lädt ihn nicht von allein. Entweder
das Plugin **Dynamix System Temperature** installieren und den Chip dort
auswählen, oder den Treiber selbst laden und den Aufruf in `/boot/config/go`
verankern, damit er den Neustart überlebt:

```sh
modprobe nct6775        # ASUS/Gigabyte/MSI mit Nuvoton; ältere Boards: it87, w83627ehf
```

Ohne diesen Schritt fehlen die betreffenden Entitäten schlicht — die
Integration lädt selbst keine Treiber und meldet keinen Fehler.

Angelegt wird jeder Kanal, den der Kernel zeigt; **eingeschaltet** sind nur die
aussagekräftigen (`Tctl`/`Tdie`/`Package id 0`, `SYSTIN`, `CPUTIN`, NVMe
`Composite`, drehende Lüfter). Die übrigen stehen in Einstellungen → Geräte &
Dienste → Entitäten und lassen sich einzeln aktivieren. Kanäle, deren erster
Messwert außerhalb von 1–150 °C liegt, sind unbelegte Eingänge (dieses Board
meldet −59 °C und viermal 0 °C) und bekommen gar keine Entität.

## Installation

**Voraussetzung: Home Assistant Core 2026.7.0 oder neuer.**

1. HACS → Integrationen → drei Punkte → **Benutzerdefinierte Repositories** →
   `https://github.com/luukkii123/ha-unraid-ssh-integrations`, Kategorie
   **Integration** → hinzufügen, installieren, Home Assistant neu starten.
2. Einstellungen → Geräte & Dienste → **Integration hinzufügen** → „Unraid SSH".
3. Host und Port eintragen. Der nächste Schritt zeigt zwei Zeilen; beide einmal
   im Unraid-Terminal als root ausführen (`/boot/config/ssh/root.pubkeys`
   überlebt den Neustart, `/root/.ssh/authorized_keys` wirkt sofort).
4. Absenden — die Integration prüft die Verbindung mit ihrem Schlüssel und
   legt erst dann den Eintrag an.

Kein Passwort wird gespeichert. Der private Schlüssel liegt in den Daten des
Eintrags (`.storage`), wie API-Token anderer Integrationen.

## Neben der Unraid-Connect-Integration

Beide können parallel laufen. Diese hier hat eine eigene Domain
(`unraid_ssh`), und der Eintragstitel wird zum Präfix der Entity-IDs — bei
„Unraid SSH" also `sensor.unraid_ssh_…`.

## Entfernen

Eintrag löschen (Einstellungen → Geräte & Dienste → Unraid SSH → drei Punkte →
Löschen), dann auf Unraid die Zeile mit `unraid_ssh@homeassistant` aus
`/boot/config/ssh/root.pubkeys` und `/root/.ssh/authorized_keys` entfernen.

## Wie es funktioniert

Je Abfrage eine SSH-Verbindung mit **einem** Befehlsstrang: `var.ini`,
`disks.ini`, `shares.ini`, `/proc/stat`, `/proc/meminfo`, `nvidia-smi`,
`/sys/class/hwmon`, `docker ps`, `docker compose ls`, die Projektordner des
Compose-Managers, `virsh list`. Die Auswertung passiert in Home Assistant. Auf
Unraid liegt nichts; ein Neustart löscht nichts.

## Containeridentitäten und Dashboardvertrag (lokaler Entwicklungsstand)

Containersteuerung, Updates und native Restartbuttons liefern strukturierte
Attribute. Karten ordnen sie ausschließlich innerhalb derselben Config Entry zu:

| Attribut | Vertrag |
| --- | --- |
| `kind` | `container` oder `stack` |
| `role` | `control`, `update` oder `restart` |
| `config_entry_id` | Instanznamensraum |
| `container_key` | Opake Containerkennung; gleiche Kennung auf Switch, Update und Restart |
| `container_name`, `container_state`, `image` | Aktueller Dockername, genauer Zustand und Image |
| `stack_key`, `stack_name` | Stabile Stackkennung und aktueller Composeprojektname; bei Standalone `null` |
| `compose_service`, `compose_replica` | Echte Compose-Labels, fehlend als `null` |
| `identity_status` | `stable`, `legacy` oder `ambiguous` |
| `running_containers`, `total_containers` | Containerzahlen auf Stacksteuerung und Stackrestart |

Die Composekennung besteht aus vorhandener stabiler Stackkennung, Service und
positivem Replikaindex (`com.docker.compose.container-number`). Der Dockername
bleibt das aktuelle Aktionsziel. Standalone-Container behalten ihre namensbasierte
Kennung. Bei fehlenden Labels bleibt der bestehende Name als ausdrücklich
gekennzeichnete Übergangsidentität erhalten; bei doppelten Service-/Replikakennungen
werden keine neuen Containerentities angelegt. Bereits belegte Composezuordnungen
bleiben in eigenen Entity-Registry-Optionen erhalten. Verlieren solche Container
vorübergehend Projekt-, Service- oder Replikalabels, bleiben die kanonischen
Entities unverfügbar; es werden auch nach einem Reload keine Namensaliasentities
angelegt. Vollständige Labels stellen die bisherigen Entities wieder her. Ein
bewusster Wechsel eines bekannten Composecontainers zu Standalone erfordert
daher einen expliziten Zuordnungsabgleich. Temporäre Hashnamen werden nicht
als kanonische Composeidentität übernommen.

Ein exakt passender vorhandener Registryeintrag wird unter Erhalt seiner
`entity_id`, Benutzereinstellungen und Verbraucherreferenzen auf die stabile
Kennung umgestellt. Ist bereits eine kanonische Entity vorhanden, gewinnt sie;
die alte Aliasentity wird weder gelöscht noch blind zusammengeführt. Historische
Namen ohne belegte Zuordnung werden nicht geraten. Compose-Container bleiben am
Stackgerät; Standalone-Container erhalten jeweils ein Gerät `Docker container`.

Restart führt **`docker restart` beziehungsweise `docker compose … restart`** aus.
Es gibt keine Stop/Start-Kette. Stackrestart ist nur mit den gemeldeten
Compose-Dateien verfügbar und verwendet den tatsächlich laufenden Projektnamen.
Fehler und Timeouts lösen ebenfalls eine Zustandsaktualisierung aus. Updates
behalten ihre vorhandenen INSTALL-Fähigkeitsprüfungen.

## Aufräumen und Bestandsschutz

Container-Schalter, Container-Updates und Container-Restartbuttons werden nicht
automatisch wegen Abwesenheit gelöscht. Dies schützt alte Entity-IDs während der
Identitätsumstellung. Eine spätere Bereinigung benötigt einen ausdrücklichen
Registry- und Verbraucherabgleich. Auch leere alte Containergeräte bleiben erhalten.
Für andere bekannte Entitätstypen gilt weiterhin die fünfminütige Karenz mit
Prüfung erfolgreicher Quellabschnitte. Serverentities bleiben geschützt.

**Installationsgrenze:** Der Liveabgleich vom 21.09.2026 zeigt bereits einzelne
Containergeräte, während der lokale Ausgangsstand `d60b324` freie Container noch
zusammenfasst. Die Gleichheit des installierten Pythoncodes wurde nicht belegt.
Dieser Entwicklungsstand wurde weder veröffentlicht noch live installiert.
Vor einer Installation müssen installierter Code und Registrybestand privat
abgeglichen werden; die bestehende historische Datenschutzsperre für Releasetags
bleibt unverändert wirksam.

## Was sie bewusst nicht kann

- Ein Image, das auf einen Digest gepinnt ist (`repo@sha256:…`), meldet nie
  ein Update — ein Pin hat definitionsgemäß keines.
- Lokal gebaute Images (ohne Registry-Digest) bekommen gar keine
  Update-Entität.
- „Installieren" führt bei einem Compose-Container `pull` und `up -d` für
  genau diesen Dienst aus. Ist der Container gestoppt, startet er dabei mit.
- Ein Compose-Stack, zu dem `docker compose ls` keine Compose-Datei meldet und
  für den es keinen Ordner im Compose-Manager gibt, bekommt **gar keinen
  Stack-Schalter** — weder ein noch aus —, und „Installieren" gibt es für
  seine Container ebenfalls nicht: beides bräuchte eine Compose-Datei, die es
  nicht gibt. Ein reiner Ausschalter wäre außerdem eine Falltür: danach hätte
  der Stack keine Container mehr, verschwände aus der Abfrage und käme nie
  zurück. Was bleibt, ist das Gerät des Stacks samt seinen Anzeigen — und der
  eigene Schalter jedes einzelnen Containers.
- **Lüfter steuern kann sie nicht.** Sie liest `pwm`, sie schreibt nie dorthin.
  Die Regelung bleibt beim Mainboard bzw. bei dem Plugin, das sie übernommen
  hat; das Attribut `pwm_mode` sagt nur, ob gerade eine automatische Kurve
  (`auto`) oder ein fester Wert (`manual`) gilt.
- **Temperaturen kommen ungefiltert aus `hwmon`.** Welcher Kanal wo im Gehäuse
  sitzt, weiß nur das Mainboard-Handbuch; die Integration erfindet keine
  Zuordnung und nennt jeden Kanal so, wie der Chip ihn nennt.

## Geräte und Namen ab 0.2.0

Der Eintragstitel bleibt das Präfix. Der Server behält sein Gerät; GPU-Sensoren
liegen dort mit GPU-Index und Modell im Namen. Compose-Stacks behalten ihre
Geräte und Kennungen. Alle freien Container eines Servers teilen ein Gerät
`<Präfix> Freie Container` (`Standalone containers` auf Englisch); jeder
Schalter und jede Update-Entität nennt weiterhin den Container.

VMs heißen `<Präfix> VM <Name>`, Platten `<Präfix> Festplatte <Name>`
(`Disk` auf Englisch). Jeder Share erhält `<Präfix> Share <Name>` mit den
Sensoren `Belegt` und `Frei` (`Used` und `Free`); der Share-Name steht nur einmal
im vollständigen Entitätsnamen.

Bestehende `entity_id` und `unique_id` bleiben erhalten. Persönliche
Entitätsnamen und benutzerdefinierte Namen weiter bestehender Geräte bleiben
erhalten. Namen entfernter einzelner Container-/GPU-Geräte werden nicht in
Entitätsnamen oder den Namen des Sammelgeräts übernommen. Bei der Zusammenführung
werden Bereiche und Labels soweit eindeutig auf Entitäten übertragen; bestehende
explizite Einstellungen bleiben maßgeblich. Bei widersprüchlichen Bereichen
wird keine Zuordnung geraten.

Vor dem Update eine HA-Sicherung erstellen und **Geräteverweise** in
Automationen, Skripten und Dashboards prüfen: alte einzelne Container-/GPU-
Geräte können entfallen. Entitätsverweise bleiben erhalten. Ein altes
Container-Geräteziel gezielt auf dessen erhaltene Entität umstellen; nicht
pauschal auf das ganze Sammelgerät, sonst könnten weitere Container geschaltet
werden. Ansichten, die automatisch alle Entitäten des Servers anzeigen,
zeigen Share-Sensoren künftig an den eigenen Share-Geräten. Die Integration
schreibt keine Nutzerautomationen um.

## Containerbilder

Schalter und Update-Entität eines Containers verwenden dieselbe Quelle:

1. Ein vorhandenes gültiges Unraid-Cachebild aus den Docker-Metadaten hat Vorrang.
   Es wird begrenzt über SSH gelesen, validiert und unter HAs `/local/unraid_ssh/`
   bereitgestellt. Der Browser benötigt für dieses Bild nur HA-Zugriff.
2. Ohne verwendbares Cachebild dient das gesetzte Docker-Label
   `net.unraid.docker.icon` als HTTP(S)-URL. Diese Adresse lädt der Browser direkt;
   sie muss von dort erreichbar sein. Die Integration lädt keine beliebigen
   externen Bildadressen auf den HA-Server herunter.
3. Ohne verwendbare Bildquelle erscheinen die normalen HA-Icons. Es gibt keine
   automatische Logo-Suche oder erfundene Ersatzlogos.

HAs Standard-Entitätenkarte zeigt die Bilder in den Zeilen. Bei der Tile-Karte
im visuellen Editor **Bild der Entität anzeigen** einschalten
(`Show entity picture`, YAML: `show_entity_picture: true`).
Die vorhandene Busch-Gerätekarte 0.11.0 zeigt Bilder in ihren normalen
Entitätenzeilen; ihre Kopfzeile verwendet ein Icon und ihre eingebettete Tile
reicht die Bildoption derzeit nicht durch. Diese Integration ändert die Karte
nicht. Lange vollständige Namen werden von HA je nach Platz gekürzt; die
Busch-Entitätenzeilen zeigen den Containernamen ohne das Gerätepräfix.
Bei 320 px kann das lange Standardpräfix den Containerteil in HA-Karten
abschneiden. Für schmale Karten einen kurzen **Karten-Anzeigenamen** setzen
(Tile-Editor: Inhalt → Name → Benutzerdefiniert, etwa `alpha_one`; bei der
Entitätenkarte den Namen der jeweiligen Zeile setzen). Entitätsnamen und IDs
müssen dafür nicht geändert werden. Auch HAs Gerätedetail zeigt kurze
Entitätsnamen.

## Geprüft am 15.09.2026

Die nachbaubare Versionsmatrix und Resolverstrategie stehen in
[`tests_ha/README.md`](tests_ha/README.md). Getestet werden **HA Core 2026.7.0
und 2026.9.2**, jeweils mit Python 3.14 und `asyncssh==2.24.0`. Die höhere
Mindestversion korrigiert eine bereits bestehende Abhängigkeitsinkompatibilität:
`asyncssh` benötigt `cryptography>=48.0.1`, ältere HA-Versionen pinnen ältere
Versionen. Die Bibliothek wird dafür nicht heruntergestuft. Python-Basisimage,
APT-Pakete und nicht separat gepinnte transitive Abhängigkeiten sind beweglich;
die Befehle garantieren daher keine bitgleichen Testumgebungen.

Die Abnahme umfasst reine Parser-/Befehls-/Transporttests, echte HA-Plattformen,
Registry-Migration, Reload, Cache-Lebenszyklus und HTTP-Bilder; für 0.3.0 dazu
die hwmon-Kanäle gegen eine Aufzeichnung dieses Servers, die Sensor- und
Lüfterentitäten in einem echten Home Assistant und das Aufräumen verwaister
Entitäten samt Karenzfenster (176 reine und 103 HA-Tests je Matrixversion). Der isolierte
Browserlauf verwendet das echte Frontend von **2026.9.2** mit synthetischen
Daten: Standard-Entitäten-/Tile-Karten, vorhandene Busch-Gerätekarte und
Geräteübersicht in Deutsch/Englisch, 320/480/960 px und hell/dunkel.
Cachebilder laden bei gesperrtem externem Bildserver weiter; URL-Fallbacks
sind dann erwartungsgemäß nicht verfügbar.

Am **15.09.2026** ist `0.3.0` (Commit `1f45b2f`, GitHub-Lauf `34940711248`
grün) über HACS als Version `main` installiert und nach einem HA-Neustart
am echten System geprüft: 15 Temperatur-, 13 Lüfter- und drei feste Kanäle
sind angelegt, die eingeschalteten Werte decken sich mit `/sys/class/hwmon`
und `nvidia-smi` auf dem Server (Tctl 73,2 °C, SYSTIN 43,0 °C, CPUTIN
57,5 °C, NVMe 52,9 °C, Lüfter 4 bei 889 RPM / 92 %, GPU-Lüfter 31 %;
Lüfter 6 schwankt zwischen 3400 und 5900 RPM, beide Seiten lesen denselben
Kanal). Das Aufräumen hat nach der Karenz 193 nicht verfügbare Entitäten
entfernt, darunter Schalter längst gelöschter Test-Container; alle 44 zu
dem Zeitpunkt eingeschalteten Schalter behielten ihre Entity-ID, das
Systemprotokoll blieb ohne Eintrag von `unraid_ssh`. Ein Stack-Schalter
oder Update-Install ist mit 0.3.0 weiterhin nicht produktiv ausgelöst worden.
