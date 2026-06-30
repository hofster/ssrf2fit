# ssrf2fit

![CI](https://github.com/hofster/ssrf2fit/actions/workflows/ci.yml/badge.svg)

Convert [Subsurface](https://subsurface-divelog.org/) dive logs (`.ssrf`) into
Garmin `.fit` activity files that can be imported into **Garmin Connect** as
scuba dives — with depth profile, GPS position, correct local time, water type
and dive computer model.

*[Deutsche Version weiter unten](#deutsch)*

---

## English

### Why?

Subsurface cannot export to Garmin Connect, and Garmin Connect only accepts its
proprietary `.fit` format for dives. This tool bridges that gap: it reads a
Subsurface native export (`.ssrf`) and writes one `.fit` file per dive that
Garmin Connect recognises as a single-gas scuba dive.

The well-known workaround via *Subsurface -> UDDF -> Shearwater Cloud -> Garmin
XML* is fragile (broken character encoding, date-format issues, frequent upload
errors). This tool goes straight from `.ssrf` to `.fit`.

### Features

- Recognised by Garmin Connect as a **single-gas scuba dive**
- Full **depth profile** (depth + temperature samples)
- **GPS position** from the linked dive site (shows the map)
- **Correct local time** - the timezone is derived from the GPS coordinates and
  applied with historic DST (e.g. Egypt summer time), so the displayed dive time
  matches the original regardless of the country
- **Average depth** (written into the `session` message fields that Garmin
  actually reads - these are missing from the `fit_tool` profile and are added
  manually)
- Water type (fresh/salt) from `watersalinity`
- Water temperature, max depth, dive computer model
- A **companion CSV** listing every field Garmin does *not* import from the file
  (see limitations), so you can fill those in manually

### Requirements

- Python 3.9+ (uses the standard-library `zoneinfo`)
- See [`requirements.txt`](requirements.txt):
  - [`fit_tool`](https://pypi.org/project/fit-tool/)
  - [`timezonefinder`](https://pypi.org/project/timezonefinder/)

### Installation

```bash
git clone https://github.com/hofster/ssrf2fit.git
cd ssrf2fit
pip install -r requirements.txt
```

### Usage

In Subsurface, export your log via **File -> Export -> Subsurface XML** (`.ssrf`).

```bash
# Single dive (by index, 0-based) -> ./dive_000.fit
python3 ssrf2fit.py export.ssrf 0

# All dives -> ./out/  plus  ./out/_begleitliste.csv
python3 ssrf2fit.py export.ssrf --all --outdir out
```

Then import the `.fit` files in **Garmin Connect Web -> Activities -> Import**.

> **Import in batches of max. 4 files.** Garmin Connect can fail or desync when
> more than four dive files are imported at once.

### What gets transferred - and what does not

This was determined empirically by re-exporting an imported dive from Garmin and
comparing it field by field. The key insight: *a field existing in the FIT
standard is not the same as Garmin Connect reading it on import.*

| Subsurface field | Status in Garmin |
|---|---|
| Date + time (local, timezone-correct) | imported |
| GPS / dive site coordinates | imported (map) |
| Depth profile (samples) | imported |
| Temperature profile | imported |
| Max depth | imported |
| Average depth | imported |
| Water salinity -> water type | imported |
| Water temperature | imported |
| Dive computer model | imported |
| **Dive duration / bottom time** | in the file, but Garmin shows it only after manual entry |
| **Dive number** | in the file, but Garmin shows it only after manual entry |
| Notes | no FIT import path - manual only |
| Buddy / divemaster | no FIT import path - manual only |
| Suit | no FIT import path - manual only |
| Weight (lead) | no FIT field exists - manual only |
| Rating / tags | not transferable |
| Cylinder / tank pressure | unreliable (air-integration) |

The **dive time** and **dive number** are managed by Garmin in the *Garmin Dive*
mobile app, or can be edited manually per dive on the web. The companion CSV
contains all of these values for quick manual entry.

### Notes on data quality

- Manually logged dives (e.g. after a computer failure) may contain only a few
  sample points; the resulting profile is correct but coarse.
- The *altitude* shown by Garmin is computed by Garmin from the GPS coordinates,
  not taken from the file.

### Example data

[`examples/example_dives.ssrf`](examples/example_dives.ssrf) contains three
anonymised dives (Germany / Spain / Egypt, including one manually logged dive)
for testing.

### Running the tests

```bash
pip install pytest
pytest -v
```

The test suite covers the parsing helpers, the timezone logic, SSRF parsing,
and end-to-end FIT generation verified with the official Garmin FIT SDK.

### License

GPLv3 - see [LICENSE](LICENSE).

---

## Deutsch

### Wozu?

Subsurface kann nicht nach Garmin Connect exportieren, und Garmin Connect
akzeptiert fuer Tauchgaenge ausschliesslich das proprietaere `.fit`-Format.
Dieses Werkzeug schliesst die Luecke: Es liest einen Subsurface-Nativexport
(`.ssrf`) und schreibt pro Tauchgang eine `.fit`-Datei, die Garmin Connect als
Singlegas-Tauchgang erkennt.

Der bekannte Umweg ueber *Subsurface -> UDDF -> Shearwater Cloud -> Garmin-XML*
ist fehleranfaellig (kaputte Zeichenkodierung, Datumsformat-Probleme, haeufige
Upload-Fehler). Dieses Werkzeug geht direkt von `.ssrf` nach `.fit`.

### Funktionen

- Wird von Garmin Connect als **Singlegas-Tauchgang** erkannt
- Vollstaendiges **Tiefenprofil** (Tiefe + Temperatur)
- **GPS-Position** aus dem verknuepften Tauchplatz (zeigt die Karte)
- **Korrekte Ortszeit** - die Zeitzone wird aus den GPS-Koordinaten abgeleitet
  und mit historischer Sommerzeit angewandt (z. B. Aegypten-Sommerzeit), sodass
  die angezeigte Uhrzeit unabhaengig vom Land mit dem Original uebereinstimmt
- **Durchschnittstiefe** (in die `session`-Felder geschrieben, die Garmin
  tatsaechlich ausliest - diese fehlen im `fit_tool`-Profil und werden manuell
  ergaenzt)
- Gewaesserart (Suess-/Salzwasser) aus `watersalinity`
- Wassertemperatur, Maximaltiefe, Tauchcomputermodell
- Eine **CSV-Begleitliste** mit allen Feldern, die Garmin *nicht* aus der Datei
  importiert (siehe Einschraenkungen), zum manuellen Nachtragen

### Voraussetzungen

- Python 3.9+ (nutzt das Standardbibliotheks-Modul `zoneinfo`)
- Siehe [`requirements.txt`](requirements.txt):
  - [`fit_tool`](https://pypi.org/project/fit-tool/)
  - [`timezonefinder`](https://pypi.org/project/timezonefinder/)

### Installation

```bash
git clone https://github.com/hofster/ssrf2fit.git
cd ssrf2fit
pip install -r requirements.txt
```

### Verwendung

In Subsurface ueber **Ablage -> Exportieren -> Subsurface-XML** (`.ssrf`)
exportieren.

```bash
# Einzelner Tauchgang (per Index, 0-basiert) -> ./dive_000.fit
python3 ssrf2fit.py export.ssrf 0

# Alle Tauchgaenge -> ./out/  plus  ./out/_begleitliste.csv
python3 ssrf2fit.py export.ssrf --all --outdir out
```

Anschliessend die `.fit`-Dateien in **Garmin Connect Web -> Aktivitaeten ->
Importieren** hochladen.

> **In Haeppchen von maximal 4 Dateien importieren.** Garmin Connect kann
> fehlschlagen oder desynchronisieren, wenn mehr als vier Tauchgang-Dateien auf
> einmal importiert werden.

### Was uebertragen wird - und was nicht

Empirisch ermittelt, indem ein importierter Tauchgang aus Garmin re-exportiert
und Feld fuer Feld verglichen wurde. Die zentrale Erkenntnis: *Dass ein Feld im
FIT-Standard existiert, heisst nicht, dass Garmin Connect es beim Import
ausliest.*

| Subsurface-Feld | Status in Garmin |
|---|---|
| Datum + Zeit (lokal, zeitzonenkorrekt) | importiert |
| GPS / Tauchplatz-Koordinaten | importiert (Karte) |
| Tiefenprofil (Samples) | importiert |
| Temperaturprofil | importiert |
| Maximaltiefe | importiert |
| Durchschnittstiefe | importiert |
| Salinitaet -> Gewaesserart | importiert |
| Wassertemperatur | importiert |
| Tauchcomputermodell | importiert |
| **Tauchzeit / Bottom Time** | in der Datei, aber Garmin zeigt es erst nach manueller Eingabe |
| **Tauchgangnummer** | in der Datei, aber Garmin zeigt es erst nach manueller Eingabe |
| Notizen | kein FIT-Importpfad - nur manuell |
| Buddy / Divemaster | kein FIT-Importpfad - nur manuell |
| Anzug | kein FIT-Importpfad - nur manuell |
| Gewicht (Blei) | kein FIT-Feld vorhanden - nur manuell |
| Bewertung / Tags | nicht uebertragbar |
| Tank / Flaschendruck | unzuverlaessig (Air-Integration) |

**Tauchzeit** und **Tauchgangnummer** verwaltet Garmin in der *Garmin-Dive*-App
am Handy, oder man traegt sie pro Tauchgang im Web manuell nach. Die
CSV-Begleitliste enthaelt all diese Werte zum schnellen Nachtragen.

### Hinweise zur Datenqualitaet

- Manuell angelegte Tauchgaenge (z. B. nach Tauchcomputer-Ausfall) koennen nur
  wenige Profilpunkte enthalten; das Profil ist dann korrekt, aber grob.
- Die von Garmin angezeigte *Hoehe* berechnet Garmin aus den GPS-Koordinaten,
  sie stammt nicht aus der Datei.

### Beispieldaten

[`examples/example_dives.ssrf`](examples/example_dives.ssrf) enthaelt drei
anonymisierte Tauchgaenge (Deutschland / Spanien / Aegypten, inkl. eines manuell
angelegten Tauchgangs) zum Testen.

### Tests ausfuehren

```bash
pip install pytest
pytest -v
```

Die Testsuite deckt die Parsing-Helfer, die Zeitzonen-Logik, das SSRF-Parsing
und die End-to-End-FIT-Erzeugung ab, verifiziert mit dem offiziellen Garmin FIT
SDK.

### Lizenz

GPLv3 - siehe [LICENSE](LICENSE).

---

## Acknowledgements / Danksagung

Built with [`fit_tool`](https://pypi.org/project/fit-tool/) and
[`timezonefinder`](https://pypi.org/project/timezonefinder/). FIT is a trademark
of Garmin Ltd. This project is not affiliated with or endorsed by Garmin or
Subsurface.
