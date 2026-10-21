# Entwicklung

Technische Hintergründe, Verträge und Testaufbau. Nutzerdoku: [README](../README.md).

## Funktionsweise

Je Abfrage eine SSH-Verbindung mit einem Befehlsstrang: `var.ini`, `disks.ini`, `shares.ini`, `/proc/stat`, `/proc/meminfo`, `nvidia-smi`, `/sys/class/hwmon`, `docker ps`, `docker compose ls`, die Projektordner des Compose-Managers, `virsh list`. Die Auswertung passiert in Home Assistant; auf Unraid liegt nichts.

## Containeridentitäten und Dashboardvertrag

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
Stackgerät; Standalone-Container teilen sich das Sammelgerät „Freie Container“.

Restart führt **`docker restart` beziehungsweise `docker compose … restart`** aus.
Es gibt keine Stop/Start-Kette. Stackrestart ist nur mit den gemeldeten
Compose-Dateien verfügbar und verwendet den tatsächlich laufenden Projektnamen.
Fehler und Timeouts lösen ebenfalls eine Zustandsaktualisierung aus. Updates
behalten ihre vorhandenen INSTALL-Fähigkeitsprüfungen.
Die Home-Assistant-Update-Entität meldet während der Installation
`in_progress: true` und danach wieder `false`. Die SSH-Installation liefert
derzeit keinen verlässlichen numerischen Fortschritt; deshalb wird keine
Prozentzahl geschätzt.

## Update-Warteschlange

„Alle Updates installieren“ am Server und „Alle Container aktualisieren“ am
Stack nehmen alle aktuell verfügbaren, installierbaren Ziele atomar auf.
Einzelupdates verwenden dieselbe serielle Warteschlange. Doppelte Aufträge für
ein Ziel desselben noch aktiven Batches werden zusammengeführt (erneuter
Versuch eines gescheiterten Ziels erst nach Batchende); Stack und
Container erzeugen keine konkurrierenden Jobs.

Die bisherigen `update.*`-Entitäten behalten ihre IDs und melden
`in_progress: true` für wartende **und** laufende Aufträge. Zusätzliche Attribute:
`update_state`, `progress_percent`, `queue_position`, `queued_at`, `started_at`,
`finished_at`, `last_error`, `update_queue` und `stack_update_queue`.
Phasen sind `idle`, `queued`, `preparing`, `pulling`, `updating`, `recreating`,
`verifying`, `completed`, `failed`. Die Queue-Position zählt die noch wartenden
Ziele ab 1. Der Server-Sensor „Update-Warteschlange“ sowie beide Update-All-
Buttons liefern globale bzw. Stack-Zähler, aktuelles Ziel und Blockadestatus.
Gesamtprozent bedeutet **abgearbeitete Ziele** (`completed + failed`) / `total`;
Erfolg und Fehler werden getrennt gezählt. Nach Abschluss bleibt der Bericht
bis zum nächsten neuen Auftrag sichtbar. Ohne aktiven Auftrag startet ein
neuer Batch; innerhalb eines aktiven Batches werden neue Ziele angehängt.

Compose veröffentlicht echte Pull-/Recreate-Phasen. Unraids Template-Updater
liefert nur den laufenden Skriptzustand. Beide liefern keine belastbare
Byteaggregation: `progress_percent` je Ziel bleibt ausdrücklich `null`.
Abgeschlossen bedeutet: Backendbefehl beendet **und** erneuter Inventar-/
Registryvergleich bestätigt das installierte Image. Bekannt fehlgeschlagene
Updates blockieren die übrigen Ziele nicht; Fehlertext und Zähler bleiben.

Aufträge werden vor Ausführung in HAs lokaler `.storage` gespeichert. Der
idempotente Backendjob überlebt Integration-Reload und HA-Neustart; HA beobachtet
anschließend dieselbe zufällige Job-ID, statt erneut zu installieren. Laufende
Jobs liegen im flüchtigen `/run` auf Unraid. Verbindungsabbrüche, fehlender
Backendstatus (etwa nach Unraid-Neustart) und überschrittene Laufzeit lassen
den Auftrag **aktiv/blockiert**, einschließlich wartender Ziele. Keine
ungeklärte Installation wird blind wiederholt oder als fertig/idle dargestellt.
Unraid-Verbindung bzw. Backendjob prüfen; die Queue beobachtet automatisch
weiter. Ein Abbruch laufender Installationen wird nicht angeboten, weil das
Template-Skript/Compose keinen sicheren transaktionalen Abbruch garantiert.
Statusdateien enthalten nur Phasen/Exitcode; weder Skriptausgabe noch private
Verbindungsdaten werden in HA-Fehlerattribute übernommen.

## Ressourcenvertrag

Container liefern CPU in Prozent (100 % je ausgelastetem Kern) und RAM in
Bytes. `ram_limit` stammt ausschließlich aus dem gezielt gelesenen
`HostConfig.Memory`: kein konfiguriertes Limit oder eine fehlgeschlagene
Limitabfrage ergibt keinen Wert. Der Docker-Stats-Nenner kann Host-RAM sein
und dient daher nicht als Containerlimit. Docker zeigt gerundete RAM-Werte;
die Einheiten werden explizit in Bytes umgerechnet.

VM-CPU verwendet das Delta von `cpu.time` über monotone Pollzeit und kann
über 100 % liegen. Erster Poll, geänderter libvirt-Domain-ID, Counterreset,
fehlende Probe und ausgeschaltete VM ergeben keinen CPU-Wert.
`ram_allocated` ist `balloon.current`, `ram_max` ist `balloon.maximum`,
`ram_host_rss` ist Hostprozess-RSS einschließlich QEMU-Overhead.
`ram_used` ist ausschließlich `balloon.available - balloon.unused` bei
konsistenten Gastwerten und höchstens 120 Sekunden altem `balloon.last-update`.
Fehlende Gaststatistik bleibt nicht verfügbar. Die Integration aktiviert
keine Gaststatistik und installiert keinen Agent. Keine VM-Uptime/IP oder
Gast-Dateisystembelegung wird aus diesen Werten geschätzt.

Ressourcensensoren bleiben auf den bisherigen Geräten, mit stabilen
`kind`/`role`/`config_entry_id` und `container_key`/`stack_key` bzw. `vm_key`.
Ein Statsausfall entfernt kein Inventar, Gerät oder bestehende Entität.
Der VM-Neustartbutton verwendet genau `virsh reboot` für laufende VMs,
mit gequotetem Namen, ohne harten Fallback. Befehlsannahme bestätigt keinen
abgeschlossenen Gastneustart.

## Tests

- Reine Python-Tests (`tests/`, Fixtures unter `tests/fixtures/` sind Aufzeichnungen bzw. synthetische Daten) und HA-Bibliothekstests (`tests_ha/`, echte HA-Registries und -Plattformen, SSH ersetzt). Versionsmatrix, Resolverstrategie, Dockerbefehle und Browserlauf: [tests_ha/README.md](../tests_ha/README.md).
- Das Manifest erlaubt `asyncssh>=2.24.0` (hassfest lehnt exakte Pins eines von HA geführten Pakets ab); die Tests pinnen `asyncssh==2.24.0`.
- CI: hassfest, HACS-Validierung und die HA-Matrix (`.github/workflows/validate.yml`); Config-Flow-Zeilen- und Zweigabdeckung 100 %.
- Bronze-Checkliste: `custom_components/unraid_ssh/quality_scale.yaml`; das Manifest beansprucht keine Stufe.

## Namens- und Geräteschema

Der Eintragstitel ist das Präfix. VMs heißen `<Präfix> VM <Name>`, Platten `<Präfix> Festplatte <Name>` (`Disk`), Shares `<Präfix> Share <Name>`, Stacks `<Präfix> Stack <Name>`; freie Container teilen sich `<Präfix> Freie Container`. Bestehende `entity_id`/`unique_id` bleiben bei Migrationen erhalten; Geräteverweise in Automationen nach Updates prüfen, Entitätsverweise bleiben bestehen. Die Integration schreibt keine Nutzerautomationen um.
