# Unraid SSH

Eine Home-Assistant-Integration, die einen **Unraid**-Server über **SSH**
liest und schaltet — ohne Unraid Connect, ohne GraphQL-API, ohne Dienst auf
dem Server. Sie liest dieselben Dateien wie das Unraid-Webinterface
(`/var/local/emhttp/*.ini`) und ruft dieselben Skripte wie dessen Knöpfe.

**Warum nicht die API?** Der `unraid-api`-Dienst hängt, kennt keine
Compose-Stacks und verliert nach Updates die VMs. SSH ist immer da.

## Was sie liefert

| Stufe | Inhalt |
| --- | --- |
| 1 (`v0.1.0`) | CPU, RAM, Load, Uptime, Array, Parity, Mover, je GPU, je Platte, je Share |
| 2 (`v0.2.0`) | Schalter für Docker-Container, Compose-Stacks (Compose-Manager-Plugin) und VMs |
| 3 (`v0.3.0`) | `update`-Entität je Container — auch für Compose-Container, die Unraids eigene Prüfung nicht sieht |

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
