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
| Schalter | Docker-Container, Compose-Stacks (über das Compose-Manager-Plugin) und VMs — je Gerät ein/aus |
| Updates | `update`-Entität je Container mit Registry-Digest, mit „Installieren" — auch für Compose-Container, die Unraids eigene Prüfung nicht sieht; dazu ein Zähler und ein Knopf „Jetzt prüfen" |

Kein Eintrag hier behauptet einen veröffentlichten Stand — nichts ist
getaggt. Die Tabelle beschreibt, was der Code im Repo tut.

## Installation

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
  ein Update — ein Pin hat definitionsgemäß keines. Auf diesem Server betrifft
  das sechs Container.
- Lokal gebaute Images (ohne Registry-Digest) bekommen gar keine
  Update-Entität.
- „Installieren" führt bei einem Compose-Container `pull` und `up -d` für
  genau diesen Dienst aus. Ist der Container gestoppt, startet er dabei mit.
- Ein Compose-Stack, zu dem `docker compose ls` keine Compose-Datei meldet und
  für den es keinen Ordner im Compose-Manager gibt, bekommt keinen Einschalter
  und kein Installieren — beides bräuchte eine Datei, die es nicht gibt.
  Ausschalten und Anzeigen funktionieren.

## Getestet

Parser, Befehlsbauer, das Snapshot-Modell und der SSH-Transport sind mit
**75 automatisierten Tests** gegen Ausgaben abgedeckt, die von einem echten
Unraid-Server aufgezeichnet wurden (Unraid 7.3.2). Die Home-Assistant-Seite —
Config Flow, Koordinatoren, Entitäten — hat noch nie in einem echten Home
Assistant gelaufen; die Schnittstelle zu Home Assistant ist unbewiesen.
