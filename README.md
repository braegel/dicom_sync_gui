# DICOM Sync GUI

Plattformübergreifendes DICOM-Transfer-Tool mit Echtzeit-Dashboard.

## Features

- **Mehrere PACS-Quellen**: Konfigurierbare Quell- und Ziel-PACS-Systeme
- **Download-Modi**: Zeitraum (Stunden), Tag, Patienten-ID
- **Voruntersuchungen**: Automatischer Download der letzten X Voruntersuchungen (konfigurierbar nach Modalität)
- **Echtzeit-Dashboard**: 
  - Aktuelle Serie mit Patient, Studie, Serienname
  - Bilder geladen / ausstehend
  - Geschwindigkeit in Bildern/Minute (1 Min, 10 Min, 15 Min, Gesamt)
  - Farbcodierung: Grün (>1σ über Mittelwert), Rot (<1σ unter Mittelwert)
  - Nur aktive Transferzeit wird gemessen (keine Pausen)
- **Eingebautes Storage SCP**: Automatischer Fallback wenn kein lokaler DICOM-Server läuft
- **Dark Theme**: Modernes, dunkles UI

## Installation

```bash
pip install -r requirements.txt
```

## Start

```bash
# Direkt
python main.py

# Als Modul
python -m dicom_sync_gui
```

## Ersteinrichtung

Beim ersten Start öffnet sich automatisch der Einstellungsdialog:

1. **Lokales PACS**: AE Title, IP, Port des lokalen Empfängers
2. **Quell-PACS**: Ein oder mehrere Remote-PACS-Systeme hinzufügen
3. **Allgemein**: Speicherordner, Zeitraum, Max. Bilder, Voruntersuchungen

## Projektstruktur

```
dicom_sync_gui/
├── main.py              # Einstiegspunkt
├── requirements.txt     # Abhängigkeiten
├── core/
│   ├── config.py        # Konfigurationsverwaltung
│   ├── dicom_ops.py     # DICOM-Netzwerkoperationen
│   ├── storage_scp.py   # Eingebauter Storage SCP
│   └── transfer_engine.py  # Transfer-Engine mit Qt-Signals
└── gui/
    ├── main_window.py   # Hauptfenster
    ├── settings_dialog.py  # Einstellungsdialog
    └── dashboard.py     # Transfer-Dashboard
```

## Konfiguration

Die Konfiguration wird plattformabhängig gespeichert:
- **macOS**: `~/Library/Application Support/DicomSyncGUI/`
- **Linux**: `~/.config/DicomSyncGUI/`
- **Windows**: `%APPDATA%/DicomSyncGUI/`
