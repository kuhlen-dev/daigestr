-- Idempotent template repairs for existing PostgreSQL installations.

UPDATE template
SET
    schema = $${
      "$schema": "http://json-schema.org/draft-07/schema#",
      "title": "Bank Statement",
      "description": "Schema für die Extraktion von Kontoauszügen und Sammel-PDFs mit mehreren Auszügen",
      "type": "object",
      "properties": {
        "kontoinhaber": {
          "type": "object",
          "properties": {
            "name": {"type": "string"},
            "adresse": {
              "type": "object",
              "properties": {
                "strasse": {"type": "string"},
                "plz": {"type": "string"},
                "ort": {"type": "string"},
                "land": {"type": "string"}
              },
              "required": ["strasse", "plz", "ort", "land"]
            }
          },
          "required": ["name"]
        },
        "iban": {"type": "string"},
        "bic": {"type": "string"},
        "bank": {"type": "string"},
        "auszugsnummer": {
          "type": "string",
          "description": "Bei Sammel-PDFs kommaseparierte Liste aller enthaltenen Auszugsnummern in Dokumentreihenfolge"
        },
        "datum": {
          "type": "string",
          "format": "date",
          "description": "Bei Sammel-PDFs das Datum des letzten enthaltenen Auszugs"
        },
        "zeitraum": {
          "type": "object",
          "properties": {
            "von": {"type": "string", "format": "date"},
            "bis": {"type": "string", "format": "date"}
          },
          "required": ["von", "bis"]
        },
        "anfangssaldo": {
          "type": "string",
          "description": "Bei Sammel-PDFs der Anfangssaldo des ersten enthaltenen Auszugs"
        },
        "endsaldo": {
          "type": "string",
          "description": "Bei Sammel-PDFs der Endsaldo des letzten enthaltenen Auszugs"
        },
        "währung": {"type": "string"},
        "buchungen": {
          "type": "array",
          "description": "Alle Buchungen des Dokuments in chronologischer Reihenfolge. Bei Sammel-PDFs muss dies die zusammengeführte Liste über alle enthaltenen Auszüge sein.",
          "items": {
            "type": "object",
            "properties": {
              "datum": {"type": "string", "format": "date"},
              "text": {"type": "string"},
              "betrag": {"type": "string"},
              "saldo": {"type": "string"},
              "währung": {"type": "string"}
            },
            "required": ["datum", "text", "betrag", "saldo"]
          }
        },
        "kontoauszuege": {
          "type": "array",
          "description": "Optionaler Detailblock für Sammel-PDFs mit mehreren Kontoauszügen. Bei einem Einzelauszug darf das Array genau einen Eintrag enthalten.",
          "items": {
            "type": "object",
            "properties": {
              "auszugsnummer": {"type": "string"},
              "datum": {"type": "string", "format": "date"},
              "zeitraum": {
                "type": "object",
                "properties": {
                  "von": {"type": "string", "format": "date"},
                  "bis": {"type": "string", "format": "date"}
                },
                "required": ["von", "bis"]
              },
              "anfangssaldo": {"type": "string"},
              "endsaldo": {"type": "string"},
              "währung": {"type": "string"},
              "buchungen": {
                "type": "array",
                "items": {
                  "type": "object",
                  "properties": {
                    "datum": {"type": "string", "format": "date"},
                    "text": {"type": "string"},
                    "betrag": {"type": "string"},
                    "saldo": {"type": "string"},
                    "währung": {"type": "string"}
                  },
                  "required": ["datum", "text", "betrag", "saldo"]
                }
              }
            },
            "required": ["auszugsnummer", "datum", "anfangssaldo", "endsaldo", "buchungen"]
          }
        },
        "_meta": {
          "type": "object",
          "properties": {
            "steuerrelevant": {"type": "boolean"},
            "steuerrelevanz_hinweis": {"type": "string", "nullable": true},
            "steuer_kategorie": {
              "type": "string",
              "enum": ["werbungskosten", "sonderausgaben", "aussergewoehnliche_belastungen", "haushaltsnahe_dienstleistungen", "handwerkerleistungen", "vorsorgeaufwendungen", "kapitalertraege", "vermietung", "spenden", null],
              "nullable": true
            },
            "steuerjahr": {"type": "string", "pattern": "^\\d{4}$", "nullable": true},
            "mwst_ausgewiesen": {"type": "boolean"},
            "mwst_betrag": {"type": "string", "nullable": true},
            "mwst_satz": {"type": "string", "nullable": true},
            "aktenzeichen": {"type": "string", "nullable": true},
            "dokumenten_id": {"type": "string", "nullable": true}
          },
          "required": ["steuerrelevant", "mwst_ausgewiesen"]
        }
      },
      "required": ["kontoinhaber", "iban", "auszugsnummer", "datum", "anfangssaldo", "endsaldo", "buchungen", "_meta"]
    }$$,
    field_descriptions = $${"kontoinhaber": "Name des Kontoinhabers.", "iban": "IBAN ohne Leerzeichen.", "bic": "BIC oder SWIFT-Code.", "bank": "Name der Bank.", "auszugsnummer": "Bei Einzelauszug die laufende Nummer. Bei Sammel-PDFs kommaseparierte Liste aller enthaltenen Auszugsnummern in Reihenfolge.", "datum": "Bei Einzelauszug das Auszugsdatum. Bei Sammel-PDFs das Datum des letzten enthaltenen Auszugs.", "zeitraum": "Gesamtzeitraum des Dokuments mit von und bis.", "anfangssaldo": "Bei Sammel-PDFs der Anfangssaldo des ersten Auszugs.", "endsaldo": "Bei Sammel-PDFs der Endsaldo des letzten Auszugs.", "währung": "Dokumentweite Währung, sofern eindeutig.", "buchungen": "Gesamtliste aller Buchungen des Dokuments in chronologischer Reihenfolge.", "kontoauszuege": "Optionales Array je enthaltenem Auszug. Muss bei Sammel-PDFs alle Einzel-Auszüge mit eigener auszugsnummer, datum, zeitraum, anfangssaldo, endsaldo und buchungen enthalten."}$$,
    notes = 'Unterscheide: laufender Kontoauszug vs. Jahreskontoauszug (annual_bank_statement) vs. Kreditkartenabrechnung (credit_card_statement). Wenn ein PDF mehrere Kontoauszüge enthält, extrahiere sie vollständig in kontoauszuege[]. Die Top-Level-Felder müssen dann den gesamten Sammelbeleg abbilden: auszugsnummer als kommaseparierte Liste, buchungen als chronologisch zusammengeführte Gesamtliste, anfangssaldo vom ersten Auszug, endsaldo vom letzten Auszug, datum als Datum des letzten Auszugs und _meta.zusammenfassung als Zusammenfassung des gesamten Dokuments.',
    version = GREATEST(COALESCE(version, 1), 2),
    updated_at = now()
WHERE id = 'bank_statement';
