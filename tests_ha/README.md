# Reale Home-Assistant-Abnahme

Prüfstand vom **14.09.2026**. `tests_ha` importiert die Integration normal
(einschließlich `__init__.py`) und verwendet echte HA-Registries, Config Entries,
Entity-Plattformen, HTTP und Reload. Nur SSH und die Koordinator-Rückgaben sind
synthetisch. Ein automatisches Fixture verbietet echte `asyncssh.connect`-Aufrufe.
Die Migrationstests erhalten Entitäts-IDs, Namen, Bereiche, Labels und Disable-
Flags und decken Teilfehler, Fremdeigentümer und verschwundene Container ab.

| HA Core | Python | pytest-homeassistant-custom-component | asyncssh |
| --- | --- | --- | --- |
| 2026.7.0 | 3.14 | 0.13.344 | 2.24.0 |
| 2026.9.2 | 3.14 | 0.13.365 | 2.24.0 |

Der Resolver installiert Core und Testplugin gemeinsam; `pip check` muss grün
sein. Das Plugin pinnt seine HA-Testabhängigkeiten. Die CI verwendet dieselben
Pins. `2026.7.0` ist die Mindestversion: `asyncssh==2.24.0` benötigt
`cryptography>=48.0.1`, womit ältere HA-Pins nicht kompatibel sind.

Vom Repository aus, je Matrixzeile mit deren Versionswerten:

```sh
docker build --target tests --build-arg HA_VERSION=2026.9.2 --build-arg TEST_PLUGIN=0.13.365 -f tests_ha/Dockerfile -t unraid-ssh-acceptance:2026.9.2 .
docker run --rm -v "$PWD:/repo:ro" -w /repo unraid-ssh-acceptance:2026.9.2 python -m pytest -p no:homeassistant tests -q
docker run --rm -v "$PWD:/repo:ro" -w /repo unraid-ssh-acceptance:2026.9.2 python -m pytest -c tests_ha/pytest.ini tests_ha -q
```

Die beiden pytest-Aufrufe bleiben getrennt: `tests/conftest.py` hat absichtlich
einen Package-Shim für reine Python-Tests. PHP ist im Image enthalten, damit
die tatsächlichen PHP-Befehle ausgeführt werden und diese Tests nicht ausfallen.

## Browser gegen das echte Frontend

`render/bootstrap.py` startet ein eigenes HA auf Loopback-Port 18123 und legt
einen synthetischen Eintrag an. `FakeSSH` akzeptiert ausschließlich den exakten
Bildlesebefehl; echte SSH-Verbindungen und echte SSH-Transportmethoden sind
zusätzlich gesperrt. Es gibt keine produktiven Zugangsdaten. Die vorhandene
Busch-Gerätekarte wird unverändert und schreibgeschützt zugeliefert.

```sh
docker build --target frontend -f tests_ha/Dockerfile -t unraid-ssh-acceptance-ui:2026.9.2 .
# Für jeden Sprachlauf ein neues leeres Verzeichnis außerhalb des Repos verwenden.
UI_RUN=$(mktemp -d /tmp/unraid-ssh-ui.XXXXXX)
# CARD_DIST auf den vorhandenen lokalen busch-cards/dist-Ordner setzen.
# RENDER_HELPERS auf den vorhandenen HACS-docs/render-Ordner setzen.
docker run --rm --name unraid-ssh-synthetic-ui --network host -e UI_LANGUAGE=de -v "$UI_RUN:/ui" -v "$PWD:/repo:ro" -v "$CARD_DIST:/cards:ro" -w /repo unraid-ssh-acceptance-ui:2026.9.2 python tests_ha/render/bootstrap.py
# Zweites Terminal; dieselben UI_RUN- und RENDER_HELPERS-Werte verwenden:
docker run --rm --network host -v "$UI_RUN:/ui" -v "$PWD:/repo:ro" -v "$RENDER_HELPERS:/helpers:ro" -w /repo mcr.microsoft.com/playwright/python:v1.62.0-noble bash -c 'pip install -q playwright==1.62.0 && python tests_ha/render/browser.py'
docker stop unraid-ssh-synthetic-ui
```

Mit `UI_LANGUAGE=en` und neuem Laufverzeichnis wiederholen. Der Browser prüft
320/480/960 px und HA-eigene helle/dunkle Themes, reale Bildladeergebnisse und
den Ausfall der externen URL bei weiter erreichbarem HA-Cache. Fremde Browser-
Origins sind gesperrt; allein die synthetische Fallbackadresse wird zunächst
vom Test geliefert und anschließend gesperrt. Die Textmessung verwendet
`messe_text`/`bewerte` ohne Mock-Theme-Injektion.

Auth, HA-Konfiguration, Screenshots und JSON-Berichte liegen ausschließlich im
externen Laufverzeichnis und gehören nicht in Git. Screenshots müssen zusätzlich
angesehen werden. Ein Fixture beweist keine produktive Migration oder Schaltung.

## Ergebnis und Darstellungsgrenzen

Abschlusssuiten: **116 reine Tests** (PHP tatsächlich ausgeführt) und **62 HA-
Tests je Matrixversion**, ohne übersprungene Tests. Die Browsermatrix umfasst
je Sprache sechs Dashboard- und sechs Geräteübersichtsansichten. Zusätzliche
Belege zeigen den echten Tile-Editor, kurze Karten-Anzeigenamen auf 320 px,
Geräte-/Share-Details und den externen Bildausfall. Die Textmessung ist kein
Urteil über bewusst nicht messbare Inline-/Nullmaß-Elemente; diese bleiben im
JSON-Bericht als `kein_urteil` ausgewiesen.

Im Defaultlayout kürzen Standardkarten lange vollständige Namen auf 320 px.
Der Harness dokumentiert dies zuerst und zeigt danach kurze **Karten**namen,
ohne die Registry zu ändern. Der echte Tile-Editor wird dazu über HAs
`getConfigElement()` geladen: Bildoption einschalten und Name → Benutzerdefiniert
setzen. Der Harness übernimmt die vom Editor erzeugte Konfiguration. Bei der
Entitätenkarte setzt er dieselbe vorhandene Zeilenoption `name` in der
Kartenkonfiguration; für diese Zeilen wird kein Editor-Klick behauptet.
Busch 0.11.0 zeigt Bilder in Standardzeilen, aber weder im Kopficon noch in
seiner eingebetteten Tile. Native Gerätedetails zeigen kurze Containernamen;
bei sehr schmalen Update-Zeilen kann der Suffix gekürzt werden.
