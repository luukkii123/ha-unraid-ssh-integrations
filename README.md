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
