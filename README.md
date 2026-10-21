# CAN Sniffer

Kleines Werkzeug zum Aufzeichnen, Markieren und Reverse-Engineering eines unbekannten
CAN-Bus-Protokolls – entstanden für den NGT-Schankbus einer AUG-Elektronik-Schankanlage,
aber für jeden CAN-Bus brauchbar.

Kernidee: Frames und **Marker** („Kopf 3, Taste 5 gedrückt“) landen mit Zeitstempel in
derselben SQLite-Datei. Die Analyse korreliert beides automatisch.

## Funktionen

- **Live**: Quelle öffnen (CANable/slcan, socketcan, gs_usb, PEAK oder Simulator), Frames live
  sehen, ID-Übersicht mit Häufigkeit und minimalem Abstand, Filter, Aufnahme starten/stoppen
- **Marker**: Tastenraster T1–T12 pro Kopf, Schlüssel rein/raus, Seite blättern, Boot, freie Marker
- **Analyse**:
  - ID-Statistik mit Byte-Klassifikation (konstant / Zähler / variabel)
  - Marker-Fenster: welche IDs tauchen ±500 ms um einen Marker auf, die sonst selten sind
  - Byte-Diff zwischen zwei Markern für eine ID (Taste 5 vs. Taste 6 → welches Byte ist der Tastencode)
  - Prüfsummen-Test (XOR, Summe, gängige CRC-8-Varianten)
- **Konfig-Diff**: zwei Aufnahmen Frame für Frame vergleichen, ASCII-Ansicht – für Boot-Bursts
- **Dekoder**: Byte-Layout pro ID als JSON hinterlegen, Live-Ansicht zeigt Klartext
- CSV-Export pro Aufnahme

## Installation

```bash
git clone https://github.com/mschneiderhesel/Cansniffer.git
cd Cansniffer
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

Dann <http://localhost:8000> öffnen – auch vom Handy im selben WLAN über die IP des Rechners.

## Quellen

| Interface   | Kanal (Beispiel)            | Wann                                         |
|-------------|-----------------------------|----------------------------------------------|
| `slcan`     | `/dev/tty.usbmodem14101`    | CANable mit slcan-Firmware am Mac/Windows    |
| `socketcan` | `can0`                      | Linux/Raspberry Pi (candleLight oder CAN-HAT)|
| `gs_usb`    | `0`                         | CANable mit candleLight-Firmware, libusb     |
| `pcan`      | `PCAN_USBBUS1`              | PEAK PCAN-USB                                |
| `sim`       | –                           | Simulator zum Testen der Oberfläche          |

Am Mac den Port mit `ls /dev/tty.usbmodem*` ermitteln. Auf dem Pi vorher:

```bash
sudo ip link set can0 up type can bitrate 250000 listen-only on
```

**Listen-only** lassen, solange nur mitgehört wird – dann kann der Adapter den Bus nicht stören.

## Vorgehen beim Reverse Engineering

1. Quelle öffnen, Bitrate durchprobieren (125k / 250k / 500k), bis Frames ohne Fehler kommen
2. Aufnahme starten, ein paar Minuten Grundrauschen aufzeichnen
3. Im Tab *Marker*: am Gerät eine Taste drücken, sofort den passenden Button drücken.
   Jede Taste, jeden Kopf, Schlüssel, Blättern – einzeln, mit Pausen dazwischen
4. Tab *Analyse* → *ID-Statistik*: Zähler und Konstanten erkennen, Prüfsumme testen
5. *Marker-Fenster*: die ID, die beim Tastendruck aufblitzt, ist der Tastendruck-Frame
6. *Byte-Diff* Taste 5 vs. Taste 6 → Tastencode-Byte; Kopf 3 vs. Kopf 7 → Kopf-Byte
7. Zwei Boot-Aufnahmen mit genau einer geänderten Einstellung → *Konfig-Diff*
8. Erkanntes Layout im Tab *Dekoder* eintragen → Live-Ansicht zeigt Klartext

## Simulator

`sim` erzeugt einen erfundenen Bus mit 10 Köpfen (Heartbeat, Tastendruck, Zapfstatus,
Konfig-Burst, XOR-Prüfsumme). Marker mit Kopf und Taste lösen dort einen simulierten
Tastendruck aus, der Button „Sim: Boot-Burst“ einen Konfig-Burst. Damit lässt sich der
komplette Workflow ohne Hardware durchspielen.

## Tests

```bash
pip install pytest
python -m pytest tests
```

## Struktur

```
cansniffer/
  capture.py    Capture-Thread (python-can) und Simulator
  db.py         SQLite-Store: Sessions, Frames, Marker, Dekoder
  analysis.py   ID-Statistik, Marker-Fenster, Diff, Prüfsummen, Dekoder
  main.py       FastAPI-API und WebSocket
  static/       Web-UI (eine Datei, kein Build-Schritt)
run.py          Startskript
```
