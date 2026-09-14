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
| Schalter | Docker-Container, Compose-Stacks (über das Compose-Manager-Plugin) und VMs — je Container, Stack oder VM ein/aus |
| Updates | `update`-Entität je Container mit Registry-Digest, mit „Installieren" — auch für Compose-Container, die Unraids eigene Prüfung nicht sieht; dazu ein Zähler und ein Knopf „Jetzt prüfen" |

Diese Fassung bereitet **0.2.0** vor. Der bisher veröffentlichte Stand ist
**0.1.0**; HACS installiert Releases, nicht automatisch den Entwicklungszweig.

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
`docker ps`, `docker compose ls`, die Projektordner des Compose-Managers,
`virsh list`. Die Auswertung passiert in Home Assistant. Auf Unraid liegt
nichts; ein Neustart löscht nichts.

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

## Geprüft am 14.09.2026

Die reproduzierbaren Prüfungen und festgelegten Abhängigkeiten stehen in
[`tests_ha/README.md`](tests_ha/README.md). Getestet werden **HA Core 2026.7.0
und 2026.9.2**, jeweils mit Python 3.14 und `asyncssh==2.24.0`. Die höhere
Mindestversion korrigiert eine bereits bestehende Abhängigkeitsinkompatibilität:
`asyncssh` benötigt `cryptography>=48.0.1`, ältere HA-Versionen pinnen ältere
Versionen. Die Bibliothek wird dafür nicht heruntergestuft.

Die Abnahme umfasst reine Parser-/Befehls-/Transporttests, echte HA-Plattformen,
Registry-Migration, Reload, Cache-Lebenszyklus und HTTP-Bilder. Der isolierte
Browserlauf verwendet das echte Frontend von **2026.9.2** mit synthetischen
Daten: Standard-Entitäten-/Tile-Karten, vorhandene Busch-Gerätekarte und
Geräteübersicht in Deutsch/Englisch, 320/480/960 px und hell/dunkel.
Cachebilder laden bei gesperrtem externem Bildserver weiter; URL-Fallbacks
sind dann erwartungsgemäß nicht verfügbar.

Dies ist kein Nachweis einer produktiven Migration oder eines produktiven
Schaltvorgangs mit 0.2.0. Veröffentlichung und HACS-Installation einschließlich
Prüfung der tatsächlich installierten Version folgen getrennt.
