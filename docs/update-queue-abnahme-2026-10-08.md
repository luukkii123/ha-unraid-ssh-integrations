# Update-Queue — lokaler Implementierungsstand 08.10.2026

Todo #153, Version 0.7.0. Dauerhafte serielle Queue, atomare Batchaufnahme,
idempotenter detached Backendstart und Reattach nach Reload. Bestehende
Update-IDs bleiben. `PROGRESS` zeigt wartende/laufende Jobs dauerhaft aktiv.
Globale und Stack-Attribute/Button plus Queue-Sensor liefern Erfolg, Fehler,
Wartepositionen und abgearbeitete Ziele. Zielprozent bleibt null: CLI-/Template-
Ausgabe bietet keine zuverlässig aggregierbare Bytequelle. Statusmarker im
rootgeschützten `/run` werden atomar geschrieben, Namen/Dateien shellquotiert.
Bekannte Fehler lassen die Queue weiterlaufen; SSH-/Statusunsicherheit und
Timeout behalten einen sichtbaren blockierten aktiven Auftrag.

Tatsächlich ausgeführt: reine Suite im vorhandenen `unraid-ssh-tests` und
`unraid-ssh-tests-2607` mit `-p no:homeassistant`; echte HA-Suite in beiden
Images mit `tests_ha/pytest.ini` und ConfigFlow-Zweigcoverage 100%. HA 2026.9.2
und 2026.7.0: jeweils 152 HA-Tests bestanden; reine Suite final
206 bestanden, einschließlich Duplicate-after-failure-Fall. Queue prüft 1/2/5
Ziele, Mittel-Fehler, konkurrierende Batch-/Einzelaufnahme, Storefehler,
Reload/Neustart mit laufendem Job, Verbindungsabbruch, Timeout, echte lokale
Shell-Mkdir-Idempotenz und Pfadvalidierung. Ein zusätzlicher echter HA-Test
prüft Wrapper-Rückkehr, atomaren Batch und Integration-Reload mit erhaltenen
Job-IDs/in_progress. Statischer UI-Prüfer: 0 Verstöße.

Browserbeleg der Darstellung in Busch Cards: 216 synthetische und 216 im
bestehenden tatsächlichen HA-Host ausgeführte Fälle, 320/360/390/480/768/960 px,
Light/Dark, Queued/Phasen/Prozent/Completed/Failed/Blocked/Unavailable;
keine Layout-/Touchfehler, keine Serviceaufrufe. Die Backend-SSH-Seite ist
synthetisch; kein produktiver Update-/Container-/VM-Schaltversuch.

## Alle 26 Regeln im Änderungsumfang

| ID | Urteil und tatsächlicher Beleg / Grenze |
|---|---|
| R01 | HA-Update-IDs/Features bleiben; Einheitliche Queue bei Container/Stack/All, HA-/Puretests. |
| R02 | Kein neuer Auswahl-UI-Pfad; Batch fachlich zentral über native Buttons. HA-Mehrfachauswahl gehört Host. |
| R03 | Native Entity-States getestet; Tastatur der Config-/Options-/More-info-Hostdialoge hier ungeprüft. |
| R04 | Keine eigenen Backenddialoge. Config-/Options-/Bestätigungs-Hostdialoge ungeprüft; Busch R04 separat. |
| R05 | Store-Recovery/echter Reload bestanden; Host-Back/Forward der Integrationsformulare ungeprüft. |
| R06 | Queueanzeige tatsächlicher Host 320/360/390/480/768/960 bestanden; Config-/Optionsformulare ungeprüft. |
| R07 | Keine eigenständige PWA; HA/Companion verantwortlich, keine Offline-Installation zugesagt. |
| R08 | Eine Queue/Validierung für Einzel-, Stack-, Serverauftrag, dedup-/batchtests. |
| R09 | Standard HA Update/Button/Sensor, keine eigenen Themes/Controls. Karten verwenden HA-Tokens. |
| R10 | Keine Configform-Änderung; Store-Ausfall nimmt keinen Auftrag an, Nachweis Puretest. Hostform-Dirty ungeprüft. |
| R11 | Queued/running/terminal samt Zeiten, boolean Progress und Queuezähler, Pure/HA/Browser bestanden. |
| R12 | Deutsche handlungsfähige Fehler; bekannte Fehler laufen weiter, Unklarheit blockiert sichtbar; Tests. |
| R13 | Kein neuer Lösch-/Stop-/Abbruchpfad. Sicheren Installationsabbruch nicht behauptet. Produktive Updates ungeprüft. |
| R14 | FIFO mehrere Ziele geprüft; keine neue große Verwaltungsansicht oder Virtualisierung. |
| R15 | Kein Drag&Drop im Änderungsumfang. |
| R16 | Native UI/Tokens; Karten-Touchziele/Overflow gemessen. Screenreader/Zoom/Kontrastfeldmessung ungeprüft. |
| R17 | Idle/queued/phases/failed/completed/blocked, Wrapper/Reload dauerhaft aktiv, HA/Puretests. |
| R18 | Registry-/Inventarprüfung, unklare SSH-/Backend-/Timeoutantwort, kein Falsch-Idle, Pure/HA/Browser. |
| R19 | Keine neue Browserberechtigung. |
| R20 | Serielle Jobs, 3s Statuspoll nur aktive Queue, globale Registryprüfung erst Verifikation; Feld-WebVitals ungeprüft. |
| R21 | Keine neue UI-Präferenz. Queuezustand persistent in HA-Store; Jobs/Zeiten erhalten, Reloadtest. |
| R22 | Sichtbare benannte native Batchbuttons und Sensor; tatsächliche Kartendarstellung geprüft. |
| R23 | Einheitliches Backendmodell, Karte responsive unveränderte Logik; 216 Hostfälle. |
| R24 | Prozent nur abgearbeitete Gesamtziele, Zielprozent null; /run-Verlust blockiert statt Neuanlauf; README-Grenzen. |
| R25 | Zwei native Batchbuttons pro fachlicher Ebene, keine lange neue Settingsliste. |
| R26 | Neue Labels und klare Fehler Deutsch/Englisch, Terminologie Unraid/SSH/Compose/Image erhalten; Übersetzungstests. |

Private Browserartefakte liegen außerhalb Git unter dem lokalen Codex-
Verzeichnis `hacs-unraid-queue` (native/synthetic). Unabhängiger Review,
Veröffentlichung und Liveinstallation werden durch den koordinierenden Agenten
abgeschlossen; Todo noch nicht erledigt. Keine Vollabnahme von #139 behauptet.
