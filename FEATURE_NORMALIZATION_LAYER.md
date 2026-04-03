# Feature: Normalization Layer — Zentrale Harmonisierung aller extrahierter Daten

**Status:** Konzept (zur Review und Umsetzung)
**Autor:** Claude Opus (Buddy-Session) + Hans Kuhlen
**Datum:** 2026-04-03
**Projekt:** daigestr

---

## 1. Warum dieses Feature existiert

### Das Problem

daigestr extrahiert strukturierte Daten aus Dokumenten ueber 143+ Templates. Jedes Template definiert seine eigenen Feldnamen — weil jeder Dokumenttyp seine eigene Fachsprache hat. Eine Telekomrechnung hat eine "Grundgebuehr" und einen "Gesamtbetrag", eine normale Rechnung hat "Brutto" und "Netto", eine Beihilfe-Mitteilung hat einen "Leistungsbetrag" und einen "Eigenanteil".

Das fuehrt dazu, dass Consumer wie buddy, Twin oder Metabase nicht einheitlich abfragen koennen. Die Frage "Zeig mir alle Rechnungsbetraege" scheitert, weil der Betrag je nach Template `brutto`, `gesamtbetrag`, `leistungsbetrag`, `erstattungsbetrag` oder `betrag` heisst. Ohne Normalisierung muss jeder Consumer fuer jedes Template ein eigenes Mapping pflegen — das ist nicht wartbar und fuehrt zu Luecken und Inkonsistenzen.

Dieses Problem wurde am 01.04.2026 entdeckt, als die buddy Attachment-Pipeline 340 PDFs durch daigestr mode=full extrahierte und die Ergebnisse in buddy-db landeten. Bei der Qualitaetspruefung zeigte sich, dass Telekomrechnungen keine Rechnungsnummer und keinen Betrag hatten — nicht weil die Daten fehlten, sondern weil sie unter anderen Feldnamen gespeichert waren. Das darf nicht passieren.

### Die Loesung

daigestr liefert bei jeder Extraktion zusaetzlich zum bestehenden `extracted` Objekt (Template-spezifische Felder, bleibt unveraendert) ein neues `normalized` Objekt. Dieses Objekt enthaelt harmonisierte Feldnamen aus einer zentralen, DB-gesteuerten Wahrheit. Consumer lesen ausschliesslich `normalized` und koennen sich darauf verlassen, dass `amount` immer der Gesamtbetrag ist — egal ob das Dokument eine Rechnung, eine Telekomabrechnung oder ein Beihilfebescheid ist.

### Kern-Prinzipien

Diese Prinzipien sind nicht verhandelbar und muessen bei jeder Designentscheidung beachtet werden:

**Zentralisierung und Standardisierung auf ALLEN Ebenen.** Es gibt fuer alles eine fuehrende Tabelle: fuer Felder, fuer Kategorien, fuer erlaubte Werte, fuer Einheiten, fuer Waehrungen. Nichts darf ausserhalb dieser Tabellen existieren. Wenn ein Wert nicht in der zentralen Tabelle steht, ist er nicht erlaubt. Das verhindert den Wildwuchs der in der Vergangenheit zu 627 verschiedenen Extra-Keys gefuehrt hat.

**Plausibilisierung und Validierung — aber NIEMALS Extraktion brechen oder verhindern.** Validierung ist ein Qualitaetsindikator, kein Gatekeeper. Wenn ein Betrag negativ ist oder eine IBAN ungueltig, wird das als Warning gemeldet, aber die Daten werden trotzdem ausgeliefert. Es muessen immer so viel Daten wie moeglich extrahiert und ausgeliefert werden. Ein unvollstaendiges Ergebnis ist besser als gar kein Ergebnis.

**Vernuenftige Defaults nur soweit keine Informationen verfaelscht werden.** Ein Default fuer `currency: "EUR"` bei einem deutschen Dokument ist akzeptabel (99% korrekt). Ein Default fuer `amount: 0` wenn kein Betrag gefunden wurde ist Verfaelschung und verboten. Die Grenze: Defaults nur fuer ableitbare Kontextinformationen (Land aus IBAN, Sprache aus Text), niemals fuer faktische Daten (Betraege, Nummern, Daten).

**daigestr bleibt autonom.** Keine Abhaengigkeiten zu buddy, Metabase, Twin oder anderen Consumern. daigestr funktioniert vollstaendig alleinstehend. Die Feedback-Schnittstelle (Corrections) ist optional — daigestr funktioniert auch wenn nie eine Correction eingeht.

**Akribisches, vollstaendiges Seeding.** Kein Template ohne vollstaendiges Mapping. Kein Feld ohne Validierung. Kein kanonischer Wert ohne Aliases. Kein Dokumenttyp ohne Pflichtfelder-Definition. 100% Coverage vor Go-Live. Das Seeding ist der kritischste Teil dieses Features und darf nicht abgekuerzt werden.

**Keine kuenstlichen Limits.** Keine maximale Anzahl von Feldern, Werten oder Mappings. Das Schema ist jederzeit erweiterbar. Neue Felder, neue Templates, neue Werte koennen jederzeit hinzugefuegt werden.

**Voraussetzung:** Epic E-DAI-POSTGRES (SQLite → PostgreSQL Migration) muss abgeschlossen sein. Alle Tabellen in diesem Dokument nutzen PostgreSQL-Syntax (SERIAL, TIMESTAMPTZ, TEXT[], JSONB, REFERENCES). Die Tabellen werden in der bestehenden `daigestr` PostgreSQL-Datenbank erstellt (`daigestr-postgres` Container).

**Konfigurierbarkeit:** Alle Limits, Timeouts, Gewichtungen und Cache-TTLs sind ueber .env oder die `scoring_weight` DB-Tabelle konfigurierbar. Keine hardcodierten Werte.

---

## 2. Neue DB-Tabellen

### 2.1 `normalized_categories` — Fuehrende Tabelle aller Kategorien

Jedes Feld, jeder Wert, jedes Mapping gehoert zu einer Kategorie. Die Kategorien sind hierarchisch organisiert (Dot-Notation mit parent-Referenz), damit man Felder gruppiert abfragen kann ("zeig mir alle financial-Felder") ohne Freitext-Suche. Kein Feld und kein Wert darf eine Kategorie verwenden die hier nicht definiert ist.

Die Entscheidung fuer eine hierarchische Struktur kam aus dem Bedarf, Felder sowohl granular (financial.tax vs financial.payment) als auch aggregiert (alles unter financial) abfragen zu koennen. Eine flache Kategorie-Liste wuerde das nicht ermoeglichen.

```sql
CREATE TABLE normalized_categories (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    parent_name TEXT REFERENCES normalized_categories(name),
    label_de TEXT NOT NULL,
    label_en TEXT NOT NULL,
    description TEXT NOT NULL,
    sort_order INT DEFAULT 100,
    active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
```

**Initiale Kategorien:**

Die Hierarchie bildet die natuerlichen Gruppierungen von Dokumenteninformationen ab. Sie wurde aus der Analyse von 143 Templates abgeleitet — jedes extrahierte Feld laesst sich eindeutig einer dieser Kategorien zuordnen.

```
financial                    — Alles was mit Geld zu tun hat
  financial.amount           — Betraege (Brutto, Netto, MwSt, Eigenanteil)
  financial.payment          — Zahlungsinformationen (IBAN, BIC, Methode, Mandat)
  financial.tax              — Steuerrelevante Daten (Absetzbarkeit, Kategorie, Steuernummer)
reference                    — Nummern und Kennzeichen
  reference.document         — Dokumentnummern (Rechnungsnr, Aktenzeichen, Bestellnummer)
  reference.customer         — Kundennummern, Vertragsnummern, Versicherungsnummern
party                        — Beteiligte Parteien
  party.vendor               — Absender/Anbieter (Name, Adresse, Kontakt, USt-ID)
  party.recipient            — Empfaenger (Name, Adresse)
temporal                     — Zeitliche Informationen
  temporal.date              — Einzelne Daten (Ausstellung, Faelligkeit, Leistung)
  temporal.period            — Zeitraeume (Abrechnungszeitraum von-bis)
detail                       — Inhaltliche Details
  detail.line_items          — Einzelpositionen und deren Struktur
  detail.content             — Inhaltliche Informationen (Zusammenfassung, Diagnose, Kuendigung)
quality                      — Meta-Informationen ueber die Extraktion selbst
  quality.meta               — Qualitaets-Scores, Sprache, Seitenzahl
```

### 2.2 `normalized_fields` — Zentrale Wahrheit aller erlaubten Zielfelder

Dies ist die wichtigste Tabelle des gesamten Features. Sie definiert ALLE Felder die in einem `normalized` Output vorkommen duerfen. Kein Output darf ein Feld enthalten das hier nicht definiert ist. Das ist der Vertrag zwischen daigestr und allen Consumern.

Jedes Feld hat einen technischen englischen Namen (API-Vertrag, snake_case), deutsche und englische Labels (fuer UIs und Dashboards), einen Datentyp, eine Kategorie-Zuordnung, eine Beschreibung, optionale Validierungsregeln, und optionale kontextabhaengige Defaults.

Die Entscheidung fuer englische technische Namen wurde getroffen weil die API international lesbar sein soll und Code-Konventionen ueblicherweise englisch sind. Die deutschen Labels existieren explizit damit buddy und Metabase deutsche Dashboards bauen koennen ohne eigenes Mapping.

```sql
CREATE TABLE normalized_fields (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    label_de TEXT NOT NULL,
    label_en TEXT NOT NULL,
    type TEXT NOT NULL,                  -- decimal, text, date, boolean, jsonb
    category TEXT NOT NULL REFERENCES normalized_categories(name),
    description TEXT NOT NULL,
    validation_rules JSONB,
    default_value TEXT,
    default_context JSONB,
    is_array BOOLEAN DEFAULT false,
    sort_order INT DEFAULT 100,
    active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
```

Zu `is_array`: Manche Felder koennen mehrfach vorkommen. Ein Dokument kann zwei IBANs haben (Absender und Empfaenger). Deshalb gibt es separate Felder (`iban_vendor`, `iban_recipient`) statt eines Arrays — weil die Semantik unterschiedlich ist. `is_array` ist fuer echte Listen reserviert (z.B. mehrere Telefonnummern eines Anbieters).

Zu `validation_rules`: Die Regeln sind als JSONB gespeichert damit sie flexibel erweiterbar sind. Moegliche Regel-Typen:

```json
{"type": "range", "min": 0}                          // Numerischer Bereich
{"type": "pattern", "regex": "^[A-Z]{2}\\d{2}"}      // Regex-Pattern
{"type": "mod97"}                                      // IBAN MOD-97 Pruefziffernverfahren
{"type": "iso4217"}                                    // Gueltige ISO-Waehrung
{"type": "iso8601"}                                    // Gueltiges ISO-Datum
{"type": "enum", "source": "normalized_values"}        // Wert muss in normalized_values existieren
```

Zu `default_value` und `default_context`: Defaults werden NUR angewendet wenn das Feld im Dokument nicht vorhanden ist UND ein passender Kontext erkannt wurde. Beispiel: `currency` hat Default `"EUR"` mit Context `{"country": "DE"}`. Nur wenn das Dokument als deutsch erkannt wird UND keine Waehrung extrahiert wurde, wird EUR eingesetzt. Der Default wird in der Traceability als solcher gekennzeichnet.

**Initiale Felder:**

Die folgende Liste wurde aus der Analyse aller 143 Templates abgeleitet. Sie deckt die Felder ab die in mindestens einem Template vorkommen und fuer Consumer relevant sind. Die Liste ist erweiterbar — neue Felder koennen jederzeit hinzugefuegt werden.

| name | type | category | label_de | label_en | description |
|------|------|----------|----------|----------|-------------|
| amount | decimal | financial.amount | Gesamtbetrag | Total Amount | Gesamtbetrag inkl. MwSt |
| amount_net | decimal | financial.amount | Nettobetrag | Net Amount | Nettobetrag ohne MwSt |
| amount_tax | decimal | financial.amount | MwSt-Betrag | Tax Amount | Mehrwertsteuerbetrag |
| tax_rate | decimal | financial.tax | MwSt-Satz | Tax Rate | MwSt-Satz in Prozent |
| currency | text | financial.amount | Waehrung | Currency | Waehrung nach ISO 4217 |
| iban_vendor | text | financial.payment | IBAN Anbieter | Vendor IBAN | IBAN des Zahlungsempfaengers |
| iban_recipient | text | financial.payment | IBAN Empfaenger | Recipient IBAN | IBAN des Empfaengers/Kunden |
| bic | text | financial.payment | BIC | BIC | BIC/SWIFT des Anbieters |
| payment_method | text | financial.payment | Zahlungsweg | Payment Method | Normalisierte Zahlungsmethode |
| mandate_reference | text | financial.payment | Mandatsreferenz | Mandate Reference | SEPA-Mandatsreferenz |
| tax_relevant | boolean | financial.tax | Steuerrelevant | Tax Relevant | Steuerlich absetzbar |
| tax_category | text | financial.tax | Steuer-Kategorie | Tax Category | Normalisierte Steuerkategorie |
| tax_deductible_amount | decimal | financial.tax | Absetzbarer Betrag | Deductible Amount | Steuerlich absetzbarer Betrag |
| invoice_number | text | reference.document | Rechnungsnummer | Invoice Number | Rechnungs- oder Belegnummer |
| reference_number | text | reference.document | Aktenzeichen | Reference Number | Aktenzeichen oder Vorgangsnummer |
| order_number | text | reference.document | Bestellnummer | Order Number | Bestellnummer/Auftragsnummer |
| customer_number | text | reference.customer | Kundennummer | Customer Number | Kundennummer beim Anbieter |
| contract_number | text | reference.customer | Vertragsnummer | Contract Number | Vertragsnummer |
| insurance_number | text | reference.customer | Versicherungsnummer | Insurance Number | Versicherungsschein-Nr. |
| vendor_name | text | party.vendor | Anbieter | Vendor Name | Name des Absenders/Anbieters |
| vendor_address | jsonb | party.vendor | Anbieter-Adresse | Vendor Address | Strukturierte Adresse: {strasse, plz, ort, land} |
| vendor_contact | jsonb | party.vendor | Anbieter-Kontakt | Vendor Contact | Strukturierter Kontakt: {telefon, email, website} |
| vendor_tax_id | text | party.vendor | USt-ID Anbieter | Vendor Tax ID | Umsatzsteuer-Identifikationsnummer des Anbieters |
| vendor_slug | text | party.vendor | Anbieter-Slug | Vendor Slug | Normalisierter Kurz-Slug (mistral, telekom, hetzner) |
| recipient_name | text | party.recipient | Empfaenger | Recipient Name | Name des Empfaengers |
| recipient_address | jsonb | party.recipient | Empfaenger-Adresse | Recipient Address | Strukturierte Adresse: {strasse, plz, ort, land} |
| date_issued | date | temporal.date | Ausstellungsdatum | Date Issued | Datum der Ausstellung des Dokuments |
| date_due | date | temporal.date | Faelligkeitsdatum | Due Date | Faelligkeitsdatum fuer Zahlung |
| date_paid | date | temporal.date | Bezahldatum | Date Paid | Datum der tatsaechlichen Zahlung |
| date_period_from | date | temporal.period | Zeitraum von | Period From | Beginn des Abrechnungszeitraums |
| date_period_to | date | temporal.period | Zeitraum bis | Period To | Ende des Abrechnungszeitraums |
| date_service | date | temporal.date | Leistungsdatum | Service Date | Datum der Leistungserbringung |
| line_items | jsonb | detail.line_items | Positionen | Line Items | Normalisierte Einzelpositionen (siehe 4.4) |
| line_items_count | integer | detail.line_items | Anzahl Positionen | Line Items Count | **Computed Field** — automatisch als len(line_items) nach Step 6 berechnet, NICHT aus Mapping. |
| summary | text | detail.content | Zusammenfassung | Summary | Kurzzusammenfassung des Dokumentinhalts |
| notes | text | detail.content | Notizen | Notes | Zusaetzliche Hinweise oder Freitext |
| language | text | quality.meta | Sprache | Language | Normalisierte Dokumentsprache |
| page_count | integer | quality.meta | Seitenzahl | Page Count | Anzahl Seiten des Dokuments |
| quality_score | decimal | quality.meta | Qualitaets-Score | Quality Score | Normalisierungs-Vollstaendigkeit (0.0 bis 1.0) |
| completeness_score | decimal | quality.meta | Vollstaendigkeit | Completeness Score | Anteil gefuellter Pflichtfelder (0.0 bis 1.0) |
| cancellation_period | text | detail.content | Kuendigungsfrist | Cancellation Period | Kuendigungsfrist als Text (z.B. "3 Monate zum Quartalsende") |
| contract_end | date | temporal.date | Vertragsende | Contract End | Vertragsende-Datum |
| auto_renewal | boolean | detail.content | Automatische Verlaengerung | Auto Renewal | Vertrag verlaengert sich automatisch |
| phone_number | text | detail.content | Rufnummer | Phone Number | Telefon-/Mobilfunknummer (relevant bei Telekom-Dokumenten) |
| insurance_type | text | detail.content | Versicherungsart | Insurance Type | Normalisierte Versicherungsart |
| reimbursement_rate | decimal | financial.amount | Erstattungssatz | Reimbursement Rate | Erstattungssatz in Prozent (Beihilfe, Versicherung) |
| own_share | decimal | financial.amount | Eigenanteil | Own Share | Eigenanteil-Betrag |
| diagnosis | text | detail.content | Diagnose | Diagnosis | Medizinische Diagnose (Gesundheitsdokumente) |
| treatment_date | date | temporal.date | Behandlungsdatum | Treatment Date | Datum der aerztlichen Behandlung |
| vendor_country | text | party.vendor | Anbieter-Land | Vendor Country | ISO 3166-1 Alpha-2 Laendercode des Anbieters |
| recipient_country | text | party.recipient | Empfaenger-Land | Recipient Country | ISO 3166-1 Alpha-2 Laendercode des Empfaengers |
| tax_country | text | financial.tax | Steuer-Land | Tax Country | Resultierendes Land fuer Steuer-Zuordnung (recipient > iban_prefix > vendor > Fallback DE) |

### 2.3 `normalized_values` — Kanonische Werte und Aliases

Diese Tabelle verhindert Wildwuchs bei Werten. Wenn ein Feld wie `payment_method` oder `currency` nur bestimmte Werte haben darf, muessen diese hier definiert sein. Jeder kanonische Wert hat eine Liste von Aliases die automatisch auf ihn gemappt werden.

Die Entscheidung fuer drei Tiers (system, managed, user) wurde getroffen weil verschiedene Werte unterschiedliche Aenderungshaeufigkeiten und Verantwortlichkeiten haben. ISO-Waehrungen aendern sich nie und duerfen nicht versehentlich geloescht werden. Steuer-Kategorien aendern sich mit dem Steuerrecht und muessen vom Team gepflegt werden. User-Aliases sind individuell und koennen jederzeit geaendert werden.

```sql
CREATE TABLE normalized_values (
    id SERIAL PRIMARY KEY,
    field_name TEXT NOT NULL REFERENCES normalized_fields(name),
    canonical_value TEXT NOT NULL,
    aliases TEXT[] DEFAULT '{}',
    context JSONB,
    is_default BOOLEAN DEFAULT false,
    source TEXT NOT NULL DEFAULT 'system'
        CHECK (source IN ('system', 'managed', 'user')),
    description TEXT,
    sort_order INT DEFAULT 100,
    active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(field_name, canonical_value)
);

-- GIN-Index fuer performante Alias-Suche (WHERE 'SEPA' = ANY(aliases))
CREATE INDEX idx_normalized_values_aliases ON normalized_values USING GIN (aliases);
```

Zu den Tiers:
- **system** — ISO-Standards und internationale Konventionen. Unveraenderlich. Koennen NICHT geloescht werden. Beispiele: Waehrungen (ISO 4217), Laendercodes (ISO 3166), Sprachen (ISO 639).
- **managed** — Vom daigestr-Team gepflegt. Aenderungen mit Audit-Log. Beispiele: Steuerkategorien (aendern sich mit deutschem Steuerrecht), Zahlungsmethoden, Versicherungsarten.
- **user** — Vom Endnutzer hinzufuegbare Aliases. Koennen jederzeit geloescht werden. Beispiel: "Mein Anbieter schreibt EUR als 'Eur.'" — der User fuegt den Alias hinzu.

Zu `context`: Manche Defaults und Werte gelten nur in bestimmten Kontexten. `currency: "EUR"` ist Default fuer `{"country": "DE"}`, aber nicht fuer `{"country": "CH"}` (dort waere CHF Default). Der Normalizer matcht den erkannten Dokument-Kontext gegen die Context-Bedingungen.

**Initiale Werte (Auswahl — vollstaendiges Seeding ist umfangreicher):**

| field_name | canonical_value | aliases | context | is_default | source |
|-----------|----------------|---------|---------|------------|--------|
| currency | EUR | ["euro", "Euro", "eur", "Eur."] | {"country": "DE"} | true | system |
| currency | EUR | [] | {"country": "AT"} | true | system |
| currency | USD | ["$", "usd", "Dollar", "US$"] | {"country": "US"} | true | system |
| currency | CHF | ["CHF", "Fr.", "Franken", "SFr."] | {"country": "CH"} | true | system |
| currency | GBP | ["£", "gbp", "Pfund", "pound"] | {"country": "GB"} | true | system |
| payment_method | lastschrift | ["SEPA", "Einzug", "Abbuchung", "Bankeinzug", "SEPA-Lastschrift", "SEPA-Basislastschrift"] | null | false | managed |
| payment_method | ueberweisung | ["Ueberweisung", "Bankueberweisung", "Vorkasse", "Vorauszahlung"] | null | false | managed |
| payment_method | kreditkarte | ["Visa", "Mastercard", "CC", "Kreditkarte", "credit card", "AmEx"] | null | false | managed |
| payment_method | paypal | ["PayPal", "PP", "PayPal-Zahlung"] | null | false | managed |
| payment_method | rechnung | ["auf Rechnung", "Rechnungskauf", "Kauf auf Rechnung"] | null | false | managed |
| tax_category | werbungskosten | ["Arbeitsmittel", "Fachliteratur", "Fortbildung", "Bueroausstattung", "Arbeitszimmer", "Fahrtkosten"] | null | false | managed |
| tax_category | sonderausgaben | ["Versicherung", "Spende", "Kirchensteuer", "Vorsorge", "Altersvorsorge"] | null | false | managed |
| tax_category | aussergewoehnliche_belastung | ["Krankheitskosten", "Pflegekosten", "Behinderung", "Kur", "Zahnersatz"] | null | false | managed |
| tax_category | haushaltsnahe_dienstleistung | ["Handwerker", "Reinigung", "Gartenpflege", "Haushaltshilfe", "Schornsteinfeger"] | null | false | managed |
| tax_category | betriebsausgaben | ["Bueromaterial", "Software", "Hardware", "Cloud-Dienste", "Hosting"] | null | false | managed |
| language | de | ["deutsch", "German", "Deutsch", "german", "deu"] | null | false | system |
| language | en | ["englisch", "English", "english", "eng"] | null | false | system |
| language | fr | ["franzoesisch", "French", "Francais"] | null | false | system |
| insurance_type | kranken | ["Krankenversicherung", "KV", "PKV", "GKV", "health insurance"] | null | false | managed |
| insurance_type | haftpflicht | ["Privathaftpflicht", "PHV", "liability"] | null | false | managed |
| insurance_type | kfz | ["KFZ-Versicherung", "Autoversicherung", "Kasko", "Teilkasko", "Vollkasko"] | null | false | managed |
| insurance_type | berufsunfaehigkeit | ["BU", "Berufsunfaehigkeitsversicherung"] | null | false | managed |
| insurance_type | leben | ["Lebensversicherung", "Risikolebensversicherung", "Kapitallebensversicherung"] | null | false | managed |

### 2.4 `normalize_mapping` auf der bestehenden `templates` Tabelle

Statt eine neue Tabelle zu erstellen, erhaelt die bestehende `templates` Tabelle zwei neue Felder. Die Entscheidung gegen eine separate Tabelle wurde getroffen weil das Mapping 1:1 zum Template gehoert und beim Laden des Templates direkt verfuegbar sein muss — ein JOIN wuerde die Performance bei jedem Extraktions-Vorgang belasten.

```sql
ALTER TABLE template ADD COLUMN normalize_mapping JSONB;
ALTER TABLE template ADD COLUMN required_normalized_fields TEXT[] DEFAULT '{}';
```

Das `normalize_mapping` mappt jedes normalisierte Zielfeld auf ein Quellfeld im `extracted` Output. Die Quellfelder nutzen Dot-Notation fuer verschachtelte Pfade (z.B. `_meta.absender.firma`).

**Regeln:**
- Jedes Zielfeld (Key im Mapping) MUSS in `normalized_fields` existieren. Unbekannte Zielfelder werden beim Speichern des Mappings abgelehnt.
- Quellfelder (Value) nutzen Dot-Notation fuer verschachtelte Pfade.
- `null` als Value bedeutet: Dieses normalisierte Feld ist fuer dieses Template nicht relevant (z.B. `diagnosis: null` bei einem invoice-Template).
- Jedes Template MUSS ein vollstaendiges Mapping haben — alle `normalized_fields` muessen als Key vorhanden sein, auch wenn der Value `null` ist. Das stellt sicher, dass nicht versehentlich Felder vergessen werden.

Das `required_normalized_fields` Array definiert welche normalisierten Felder fuer diesen Dokumenttyp als Pflicht gelten. Fehlende Pflichtfelder erzeugen eine Warning und senken den Completeness-Score, verhindern aber NIEMALS die Auslieferung.

**Beispiel fuer Template "invoice":**

```json
{
    "amount": "brutto",
    "amount_net": "netto",
    "amount_tax": "mwst",
    "tax_rate": "_meta.mwst_satz",
    "currency": "waehrung",
    "iban_vendor": "iban",
    "iban_recipient": null,
    "bic": "bic",
    "payment_method": "zahlungsart",
    "mandate_reference": "mandatsreferenz",
    "tax_relevant": "_meta.steuerrelevant",
    "tax_category": "_meta.steuer_kategorie",
    "tax_deductible_amount": null,
    "invoice_number": "rechnungsnummer",
    "reference_number": null,
    "order_number": "bestellnummer",
    "customer_number": "kundennummer",
    "contract_number": null,
    "insurance_number": null,
    "vendor_name": "_meta.absender.firma",
    "vendor_address": "_meta.absender.adresse",
    "vendor_contact": null,
    "vendor_tax_id": "ust_id",
    "vendor_slug": "_meta.absender.slug",
    "recipient_name": "_meta.empfaenger.name",
    "recipient_address": "_meta.empfaenger.adresse",
    "date_issued": "datum",
    "date_due": "zahlungsfrist",
    "date_paid": null,
    "date_period_from": null,
    "date_period_to": null,
    "date_service": "leistungsdatum",
    "line_items": {"source_field": "positionen", "item_mapping": {"description": "beschreibung", "quantity": "menge", "unit": "einheit", "unit_price": "einzelpreis", "total": "gesamt", "tax_rate": "mwst_satz", "tax_amount": "mwst_betrag"}},
    "line_items_count": null,
    "summary": "_meta.zusammenfassung",
    "notes": null,
    "language": null,
    "page_count": null,
    "quality_score": null,
    "completeness_score": null,
    "cancellation_period": null,
    "contract_end": null,
    "auto_renewal": null,
    "phone_number": null,
    "insurance_type": null,
    "reimbursement_rate": null,
    "own_share": null,
    "diagnosis": null,
    "treatment_date": null
}
```

### 2.5 `normalized_test_fixtures` — Automatisierte Tests pro Template

Jedes Template braucht mindestens ein Test-Fixture das sicherstellt, dass das Mapping korrekt funktioniert. Das Fixture besteht aus einem simulierten `extracted` Input und dem erwarteten `normalized` Output. Bei jedem Mapping-Update werden alle Fixtures automatisch geprueft.

Die Entscheidung, Fixtures in der DB statt als Dateien zu speichern, wurde getroffen weil sie direkt neben den Mappings leben sollen und per API verwaltbar sein muessen.

```sql
CREATE TABLE normalized_test_fixtures (
    id SERIAL PRIMARY KEY,
    template_name TEXT NOT NULL,
    input_extracted JSONB NOT NULL,
    expectednormalized JSONB NOT NULL,
    description TEXT,
    active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
```

Bewusst KEIN Foreign Key auf `template(id)` fuer `template_name` — Fixtures sollen auch existieren wenn ein Template temporaer deaktiviert, umbenannt oder geloescht wird. Fixtures sind Test-Artefakte die unabhaengig vom Template-Lifecycle bestehen.

### 2.6 `extraction_corrections` — Feedback-Schnittstelle

Consumer koennen Korrekturen an daigestr melden. Diese Korrekturen werden gespeichert und dienen als Signal fuer Mapping-Verbesserungen. Wichtig: Korrekturen aendern das Mapping NICHT automatisch. Sie erzeugen Vorschlaege die manuell reviewed werden muessen. daigestr funktioniert vollstaendig ohne dass je eine Correction eingeht.

```sql
CREATE TABLE extraction_corrections (
    id SERIAL PRIMARY KEY,
    document_id TEXT,
    template_name TEXT,
    field_name TEXT NOT NULL REFERENCES normalized_fields(name),
    old_value TEXT,
    new_value TEXT NOT NULL,
    source TEXT DEFAULT 'user',
    applied BOOLEAN DEFAULT false,
    reviewed_at TIMESTAMPTZ,
    reviewed_by TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
```

---

## 3. Normalizer-Logik

### 3.1 Ablauf

Der Normalizer ist ein zusaetzlicher Schritt der laeuft wenn `extracted` nicht None ist UND ein `normalize_mapping` fuer das Template existiert. Er veraendert den bestehenden Flow nicht — wenn die Normalisierung fehlschlaegt, wird `extracted` trotzdem ausgeliefert und `normalized` ist `null` mit einer Fehlerbeschreibung.

Die 13 Schritte in Reihenfolge:

0. **Vorpruefung** — Ist `extracted` vorhanden? Existiert ein `normalize_mapping` fuer das Template? Wenn nicht: `normalized = null`, Warning generieren, ABBRUCH (kein Fehler, nur kein Normalisierungs-Ergebnis). `extracted` wird trotzdem ausgeliefert.
1. **Template identifizieren** — Das Template ist bereits durch `auto_extract` oder expliziten `template` Parameter bestimmt. `normalize_mapping` wird aus dem Cache geladen (oder DB bei Cache-Miss).
2. **Kontext bestimmen** — Land, Sprache und Waehrung werden aus dem Dokument abgeleitet (siehe 3.2).
3. **Feld-Mapping anwenden** — Fuer jedes Zielfeld im Mapping: Quellfeld via Dot-Notation im `extracted` Output suchen und Wert uebernehmen.
4. **Wert-Normalisierung** — Extrahierte Werte werden gegen `normalized_values` geprueft (mit GIN-Index auf `aliases` Array). Aliases werden auf kanonische Werte gemappt ("SEPA" wird zu "lastschrift").
5. **Datentyp-Konvertierung** — Locale-aware Konvertierung: "29,95" (deutsch) wird zu 29.95, "01.03.2025" wird zu "2025-03-01" (ISO 8601).
6. **line_items Sub-Normalisierung** — Jede Position in der Positionsliste wird einzeln normalisiert mit dem `item_mapping` aus dem Template.
7. **Computed Fields berechnen** — Felder die nicht aus dem Mapping kommen sondern abgeleitet werden: `line_items_count = len(line_items)`, `tax_country` aus Kontext-Logik. Computed Fields haben in der `normalized_fields` Tabelle ein Flag oder werden im Normalizer-Code hart definiert.
8. **Defaults anwenden** — Nur fuer leere Felder die einen kontextbasierten Default haben. Niemals fuer faktische Daten.
9. **Validierung** — Jedes Feld wird gegen seine `validation_rules` geprueft. Ungueltige Werte werden als Warning gemeldet, NICHT entfernt.
10. **Cross-Field-Plausibilisierung** — Zusammenhaenge zwischen Feldern pruefen (amount = amount_net + amount_tax, etc.).
11. **Quality Score berechnen** — Basierend auf Pflichtfeld-Abdeckung, Validierung und Plausibilitaet.
12. **Traceability generieren** — Fuer jedes Feld dokumentieren woher der Wert kam.
13. **Output zusammenstellen** — `normalized`, `normalized_version`, `normalized_warnings`, `normalized_trace`, `normalized_context` ans Result haengen. Wenn `compact: true`: null-Felder entfernen.

### 3.2 Kontext-Erkennung

Der Normalizer muss den Kontext des Dokuments kennen um kontextabhaengige Defaults und Werte anwenden zu koennen. Der Kontext wird in dieser Prioritaetsreihenfolge bestimmt:

1. **Explizit im Dokument:** `_meta.absender.adresse.land` — wenn das Dokument ein Land enthaelt, hat das hoechste Prioritaet.
2. **IBAN-Prefix:** `DE` → Deutschland, `AT` → Oesterreich, `CH` → Schweiz. IBANs beginnen immer mit dem Laendercode.
3. **Dokumentsprache:** Wenn das Dokument als deutsch erkannt wurde, ist der Kontext wahrscheinlich `DE`.
4. **Fallback:** Konfigurierbar via `NORMALIZE_FALLBACK_COUNTRY` (Default: `DE`). Dieser Fallback wird explizit als solcher in der Traceability gekennzeichnet.

Der erkannte Kontext wird transparent im Output mitgeliefert. Es gibt drei Country-Felder mit unterschiedlicher Semantik:

- `vendor_country` — Land des Absenders/Anbieters (aus Adresse oder IBAN)
- `recipient_country` — Land des Empfaengers (aus Adresse)
- `tax_country` — **Resultierendes Feld**: Wo die Steuer anfaellt. Das ist das Feld das Consumer fuer Steuer-Zuordnung nutzen. Regel: `recipient_country` wenn vorhanden, sonst IBAN-Prefix des Empfaengers, sonst `vendor_country`, sonst Fallback `DE`.

```json
"normalized_context": {
    "vendor_country": "FR",
    "vendor_country_source": "vendor_address.ort=Paris",
    "recipient_country": "DE",
    "recipient_country_source": "recipient_address.plz=41063",
    "tax_country": "DE",
    "tax_country_source": "recipient_country",
    "language": "de",
    "language_source": "extracted"
}
```

### 3.3 Datentyp-Konvertierung

Die Konvertierung ist locale-aware. Dasselbe Dokument kann deutsche Dezimalkommas ("29,95") und ISO-Datumsformate ("2025-03-01") enthalten. Der Normalizer erkennt das Format automatisch:

- **Dezimalzahlen:** "29,95" / "29.95" / "EUR 29,95" / "29,95 €" → `29.95` (Float, Punkt als Dezimaltrenner)
- **Daten:** "01.03.2025" / "2025-03-01" / "1. Maerz 2025" / "March 1, 2025" → `"2025-03-01"` (ISO 8601)
- **Booleans:** "ja" / "yes" / "true" / "1" / "Ja" → `true`
- **Text:** Whitespace-Normalisierung, Trim, aber keine inhaltliche Aenderung

### 3.4 line_items Sub-Normalisierung

Einzelpositionen auf Rechnungen haben Template-spezifische Feldnamen. Das Template "invoice" nennt eine Position `beschreibung`, das Template "telecom_bill" nennt es `dienstleistung`. Die Sub-Normalisierung mappt jede Position auf die einheitliche Zielstruktur:

```json
{
    "description": "API tokens mistral-embed",
    "quantity": 1,
    "unit": "stueck",
    "unit_price": 12.50,
    "total": 12.50,
    "tax_rate": 19.0,
    "tax_amount": 2.38
}
```

Die Struktur ist flach — keine Verschachtelung innerhalb einer Position. Die Entscheidung fuer eine flache Struktur wurde getroffen weil sie in SQL einfacher abfragbar ist (`line_items->0->>'tax_rate'` statt `line_items->0->'tax'->>'rate'`) und es bei so wenigen Feldern pro Position keinen Vorteil der Verschachtelung gibt.

Werte innerhalb der Positionen durchlaufen ebenfalls die Wert-Normalisierung — `unit: "Stk"` wird zu `unit: "stueck"`.

### 3.5 Defaults — Erlaubt und Verboten

Dies ist eine kritische Grenze die nicht verschwimmen darf.

**Erlaubte Defaults** (ableitbare Kontextinformation, hohe Sicherheit):
- `currency: "EUR"` wenn country=DE und kein Waehrungsfeld extrahiert
- `language: "de"` wenn Dokument deutsch geschrieben ist
- `country: "DE"` als Fallback wenn kein Land erkennbar

**Verbotene Defaults** (faktische Daten, die erfunden wuerden):
- `amount: 0` wenn kein Betrag gefunden → MUSS `null` bleiben
- `invoice_number: "unknown"` → MUSS `null` bleiben
- `date_issued: "2026-04-03"` (heute) → MUSS `null` bleiben
- `vendor_name: "Unbekannt"` → MUSS `null` bleiben
- Jeder Default der einen Wert erfindet der nicht im Dokument steht

Die Regel: Wenn der Default falsch sein KOENNTE und die Falschheit eine Konsequenz haette (falscher Betrag in der Steuererklraerung, falsche Rechnungsnummer beim Matching), ist der Default verboten.

### 3.6 Validierung

Validierungsregeln werden aus `normalized_fields.validation_rules` geladen und pro Feld angewendet. Validierung warnt, bricht aber NIEMALS die Extraktion.

Bei einem ungueltigen Wert:
1. Der Wert wird trotzdem in `normalized` ausgeliefert
2. Eine Warning wird in `_normalized_warnings` hinzugefuegt
3. Der Quality-Score wird reduziert
4. Die Traceability zeigt `"validation_failed": true`

### 3.7 Cross-Field-Plausibilisierung

Pruefungen zwischen Feldern die logisch zusammenhaengen:

- **Betragskonsistenz:** `amount` sollte ungefaehr `amount_net + amount_tax` entsprechen (Toleranz konfigurierbar via `NORMALIZE_PLAUSIBILITY_TOLERANCE`, Default 0.01 = 1%)
- **Positionssumme:** Die Summe aller `line_items.total` sollte ungefaehr `amount_net` entsprechen
- **MwSt-Konsistenz:** `amount_tax` sollte ungefaehr `amount_net * tax_rate / 100` entsprechen
- **Datumslogik:** `date_due` sollte nach `date_issued` liegen
- **Zeitraumlogik:** `date_period_to` sollte nach `date_period_from` liegen

Bei Abweichung: Warning + Quality-Score-Abzug. Extraktion wird NICHT verhindert.

### 3.8 Quality Score

Der Quality Score gibt an wie vollstaendig und konsistent die Normalisierung ist. Er besteht aus drei Komponenten:

```
quality_score = (
    pflichtfelder_gefuellt / pflichtfelder_total * W_COMPLETENESS +
    validierung_bestanden / validierung_total * W_VALIDATION +
    plausibilitaet_bestanden / plausibilitaet_total * W_PLAUSIBILITY
)
```

Die Gewichtungen sind zentral konfigurierbar — in der bestehenden `scoring_weight` Tabelle (DB hat Vorrang ueber .env):
- `normalize_weight_completeness` (Default: 0.5)
- `normalize_weight_validation` (Default: 0.3)
- `normalize_weight_plausibility` (Default: 0.2)

Der Score liegt zwischen 0.0 und 1.0. Consumer koennen damit filtern: "Zeig mir nur Dokumente mit Quality > 0.8".

Der `completeness_score` ist ein Subset: Nur der Anteil der gefuellten Pflichtfelder, ohne Validierung und Plausibilitaet.

### 3.9 Traceability

Fuer jedes normalisierte Feld wird dokumentiert woher der Wert kam. Das ist essentiell fuer Debugging ("warum ist der Betrag 0?") und Feedback ("der Wert kam aus dem falschen Feld").

```json
"normalized_trace": {
    "amount": {"source_field": "brutto", "rule": "mapping", "confidence": 1.0},
    "currency": {"source_field": null, "rule": "default", "confidence": 0.95, "context": "country=DE"},
    "vendor_name": {"source_field": "_meta.absender.firma", "rule": "mapping", "confidence": 1.0},
    "date_issued": {"source_field": "datum", "rule": "mapping", "confidence": 1.0, "converted_from": "01.03.2025"}
}
```

Moegliche `rule` Werte:
- `mapping` — Direkt aus dem Template normalize_mapping
- `default` — Kontextbasierter Default angewendet
- `heuristic` — Automatisch abgeleitet (z.B. Waehrung aus IBAN-Prefix)
- `correction` — Aus extraction_corrections uebernommen

### 3.10 Confidence pro Feld

Die Confidence gibt an wie sicher der Normalizer bei einem Wert ist. Alle Werte sind konfigurierbar ueber die `scoring_weight` Tabelle in der DB:

| Rule | scoring_weight Name | Default | Beschreibung |
|------|-------------------|---------|-------------|
| mapping | confidence_mapping | 1.0 | Direktes Mapping, Feld existiert und hat Wert |
| correction | confidence_correction | 1.0 | Correction Override (User hat korrigiert) |
| default | confidence_default | 0.95 | Kontextbasierter Default (EUR fuer DE) |
| heuristic | confidence_heuristic | 0.8 | Heuristische Ableitung (Waehrung aus IBAN-Prefix) |
| llm | confidence_llm | 0.7 | LLM-Extraktion ohne weitere Validierung |
| fallback | confidence_fallback | 0.3 | Fallback-Template minimale Normalisierung |

---

## 4. Output-Format

### 4.1 Response-Erweiterung

Der bestehende Response bleibt vollstaendig unveraendert. Es kommen fuenf neue Top-Level-Felder hinzu:

- `normalized` — Das harmonisierte Objekt. Consumer lesen nur dieses.
- `normalized_version` — Versions-Hash fuer Nachvollziehbarkeit.
- `normalized_warnings` — Liste von Warnings (fehlende Pflichtfelder, Validierungsfehler, Plausibilitaets-Abweichungen).
- `normalized_trace` — Pro-Feld-Traceability.
- `normalized_context` — Erkannter Kontext (Land, Sprache, Quelle).

**Namenskonvention:** Kein Unterstrich-Prefix. Pydantic v2 behandelt `_` Prefix-Felder als private Attribute — das wuerde die JSON-Serialisierung verhindern. Daher `normalized` statt `normalized`. In `ConvertResponse` (models.py): `normalized: Optional[dict] = None`, `normalized_version: Optional[str] = None`, etc.

**Wann laeuft der Normalizer?** Immer wenn `extracted` nicht None ist UND ein `normalize_mapping` fuer das verwendete Template existiert. Das gilt fuer:
- `auto_extract: true` (Template automatisch bestimmt)
- `template: "invoice"` (Template explizit angegeben)
- NICHT bei `extract_schema` mit Custom-Schema (kein Template → kein Mapping)

Wenn kein Mapping existiert (Template ohne `normalize_mapping`): `normalized = null`, Warning "No mapping for template '{name}'. Use PUT /v1/normalized/mappings/{name} to create one."

Vollstaendiges Beispiel: Siehe Abschnitt 5.

### 4.2 Compact-Flag

`compact: true` als optionaler Parameter auf `/v1/convert` (und allen Endpoints die `normalized` zurueckgeben). Wenn gesetzt:
- Alle Felder mit Wert `null` werden aus `normalized` entfernt
- Alle Felder mit Wert `null` werden aus `_normalized_trace` entfernt
- Default: `false` (vollstaendiger Output mit allen Feldern)

Spart 60%+ Payload bei typischen Dokumenten wo 20+ der 41 Felder `null` sind. Consumer die ein vollstaendiges Schema erwarten nutzen `compact: false`.

**Implementation:** Neues Feld `compact: bool = Field(False, ...)` auf `ConvertRequest` in `models.py`. Wird an den Normalizer durchgereicht der vor Auslieferung die null-Felder entfernt.

### 4.3 Fallback-Template

Wenn ein Dokument keinem spezifischen Template zugeordnet wird (Dokumenttyp `fallback`), hat es kein Template-spezifisches Mapping. Der Normalizer extrahiert nur sicher ableitbare Felder:

- `vendor_name` aus Absender/Header (wenn vorhanden, Confidence 0.7)
- `date_issued` aus dem Dokumentdatum (Confidence 0.7)
- `language` aus Textanalyse (Confidence 0.8)
- **`amount` wird bei Fallback NICHT gesetzt** (zu hohes Risiko einer falschen Zuordnung)
- Alle anderen Felder `null`
- `quality_score` per Definition niedrig (0.2-0.3)
- `completeness_score` nahe 0
- Warning: "Kein Template-Match, minimale Normalisierung"

Die Entscheidung, bei Fallback trotzdem ein `normalized` zu liefern (statt `null`), wurde getroffen weil selbst ein unvollstaendiges Ergebnis besser ist als gar keins. Ein Consumer kann ueber den quality_score filtern.

---

## 5. Vollstaendiges Output-Beispiel

```json
{
    "success": true,
    "markdown": "Mistral AI SAS\nRechnung MSTRL-API-755032-026\n...",
    "extracted": {
        "rechnungsnummer": "MSTRL-API-755032-026",
        "datum": "19.02.2026",
        "brutto": "24,86",
        "netto": "20,89",
        "mwst": "3,97",
        "waehrung": "EUR",
        "positionen": [
            {"beschreibung": "API tokens mistral-embed pp1k_input", "menge": 1, "einzelpreis": "20,89", "gesamt": "20,89", "mwst_satz": 19}
        ],
        "_meta": {
            "absender": {"firma": "Mistral AI SAS", "slug": "mistral", "adresse": {"strasse": "15 rue des Halles", "plz": "75001", "ort": "Paris"}},
            "empfaenger": {"name": "Hans Kuhlen", "adresse": {"strasse": "Berger Heide 6", "plz": "41063", "ort": "Moenchengladbach"}},
            "mwst_satz": "19.00",
            "steuerrelevant": true,
            "steuer_kategorie": "betriebsausgaben"
        }
    },
    "normalized": {
        "amount": 24.86,
        "amount_net": 20.89,
        "amount_tax": 3.97,
        "tax_rate": 19.0,
        "currency": "EUR",
        "iban_vendor": null,
        "iban_recipient": null,
        "bic": null,
        "payment_method": null,
        "mandate_reference": null,
        "tax_relevant": true,
        "tax_category": "betriebsausgaben",
        "tax_deductible_amount": null,
        "invoice_number": "MSTRL-API-755032-026",
        "reference_number": null,
        "order_number": null,
        "customer_number": null,
        "contract_number": null,
        "insurance_number": null,
        "vendor_name": "Mistral AI SAS",
        "vendor_address": {"strasse": "15 rue des Halles", "plz": "75001", "ort": "Paris"},
        "vendor_contact": null,
        "vendor_tax_id": null,
        "vendor_slug": "mistral",
        "recipient_name": "Hans Kuhlen",
        "recipient_address": {"strasse": "Berger Heide 6", "plz": "41063", "ort": "Moenchengladbach"},
        "date_issued": "2026-02-19",
        "date_due": null,
        "date_paid": null,
        "date_period_from": null,
        "date_period_to": null,
        "date_service": null,
        "line_items": [
            {"description": "API tokens mistral-embed pp1k_input", "quantity": 1, "unit": null, "unit_price": 20.89, "total": 20.89, "tax_rate": 19.0, "tax_amount": null}
        ],
        "line_items_count": 1,
        "summary": null,
        "notes": null,
        "language": "de",
        "page_count": null,
        "quality_score": 0.82,
        "completeness_score": 0.75,
        "cancellation_period": null,
        "contract_end": null,
        "auto_renewal": null,
        "phone_number": null,
        "insurance_type": null,
        "reimbursement_rate": null,
        "own_share": null,
        "diagnosis": null,
        "treatment_date": null,
        "vendor_country": "FR",
        "recipient_country": "DE",
        "tax_country": "DE"
    },
    "normalized_version": "v1-a3f8c2",
    "normalized_warnings": [
        {"field": "amount_tax", "type": "plausibility", "message": "amount_tax (3.97) matches amount_net * tax_rate / 100 (3.97) — konsistent", "severity": "info"},
        {"field": "iban_vendor", "type": "required_missing", "message": "Pflichtfeld iban_vendor ist leer", "severity": "warning"}
    ],
    "normalized_trace": {
        "amount": {"source_field": "brutto", "rule": "mapping", "confidence": 1.0, "converted_from": "24,86"},
        "amount_net": {"source_field": "netto", "rule": "mapping", "confidence": 1.0, "converted_from": "20,89"},
        "currency": {"source_field": "waehrung", "rule": "mapping", "confidence": 1.0},
        "vendor_name": {"source_field": "_meta.absender.firma", "rule": "mapping", "confidence": 1.0},
        "date_issued": {"source_field": "datum", "rule": "mapping", "confidence": 1.0, "converted_from": "19.02.2026"},
        "language": {"source_field": null, "rule": "heuristic", "confidence": 0.8, "context": "document text is German"},
        "vendor_country": {"source_field": "_meta.absender.adresse.ort", "rule": "heuristic", "confidence": 0.8, "context": "ort=Paris → FR"},
        "recipient_country": {"source_field": "_meta.empfaenger.adresse.plz", "rule": "heuristic", "confidence": 0.9, "context": "plz=41063 → DE"},
        "tax_country": {"source_field": null, "rule": "derived", "confidence": 0.95, "context": "recipient_country=DE"}
    },
    "normalized_context": {
        "vendor_country": "FR",
        "vendor_country_source": "vendor_address.ort=Paris",
        "recipient_country": "DE",
        "recipient_country_source": "recipient_address.plz=41063",
        "tax_country": "DE",
        "tax_country_source": "recipient_country",
        "language": "de",
        "language_source": "heuristic"
    },
    "meta": {}
}
```

---

## 6. Versionierung

### 6.1 Versions-Hash

```
normalized_version = "v{schema_version}-{hash6}"
```

`schema_version` ist ein Integer-Counter, gespeichert als Eintrag in der `scoring_weight` Tabelle: `name='normalize_schema_version', value=1`. Wird bei jeder Schema-Aenderung (neues Feld, Feld-Umbenennung, Feld-Entfernung) manuell hochgezaehlt. Konfigurierbar aber NICHT automatisch — bewusste Entscheidung wann eine neue Schema-Version vorliegt.

Der `hash6` ist ein 6-Zeichen SHA256-Prefix berechnet aus:
- Neuester `updated_at` Timestamp in `normalized_fields`
- `normalize_mapping.updated_at` des verwendeten Templates
- Neuester `updated_at` Timestamp in `normalized_values`

Aendert sich irgendeine dieser Komponenten, aendert sich der Hash. Consumer koennen damit erkennen ob ihre gecachten Daten noch aktuell sind.

### 6.2 Schema-Evolution

- **Neues Feld hinzufuegen:** Bestehende `normalized` Outputs bleiben gueltig — das neue Feld ist dort einfach nicht vorhanden (bzw. `null` bei Re-Normalisierung). Kein Breaking Change.
- **Feld umbenennen:** Deprecation-Phase in der altes und neues Feld parallel ausgeliefert werden. Nach Uebergangszeit altes Feld entfernen mit Major-Versionssprung.
- **Feld entfernen:** Major-Versionssprung. Consumer muessen vorab informiert werden.
- **Mapping aendern:** Minor-Versionssprung. Bestehende Daten koennen per Batch-Endpoint re-normalisiert werden.

---

## 7. API-Endpoints

### 7.1 Schema-Export

```
GET /v1/normalized/schema
```

Liefert ein JSON Schema fuer `normalized` basierend auf der aktuellen `normalized_fields` Tabelle. Consumer koennen damit eingehende Daten validieren und ihre Datenmodelle automatisch aktualisieren.

### 7.2 Corrections

```
POST /v1/corrections
```

Nimmt Korrekturen von Consumern entgegen. Korrekturen aendern NICHT automatisch das Mapping — sie erzeugen Vorschlaege die manuell reviewed werden.

### 7.3 Coverage Report

```
GET /v1/normalized/coverage
```

Liefert einen Ueberblick ueber den Zustand aller Mappings: Wie viele Templates haben ein vollstaendiges Mapping? Welche Felder fehlen wo? Wie hoch ist die Fixture-Pass-Rate?

### 7.4 Batch Re-Normalisierung

```
POST /v1/normalize/batch
```

Nimmt eine Liste von `{template_name, extracted}` Paaren entgegen und liefert die normalisierten Ergebnisse zurueck. Dieser Endpoint wird von Consumern genutzt um bestehende Daten nach Mapping-Aenderungen neu zu normalisieren. daigestr bietet den Endpoint, der Consumer entscheidet wann und wie er ihn nutzt.

### 7.5 Admin-Endpoints fuer Normalization-Tabellen

CRUD-Endpoints fuer alle Normalization-Tabellen. Alle Endpoints mit vollstaendigen OpenAPI/Swagger-Beschreibungen damit LLMs die API verstehen und nutzen koennen.

```
GET    /v1/normalized/fields                — Alle Felder auflisten
POST   /v1/normalized/fields                — Neues Feld erstellen
PUT    /v1/normalized/fields/{name}         — Feld aktualisieren
DELETE /v1/normalized/fields/{name}         — Feld deaktivieren (nicht loeschen)

GET    /v1/normalized/values                — Alle kanonischen Werte auflisten
POST   /v1/normalized/values                — Neuen Wert + Aliases erstellen
PUT    /v1/normalized/values/{id}           — Wert/Aliases aktualisieren
DELETE /v1/normalized/values/{id}           — Wert deaktivieren

GET    /v1/normalized/categories            — Alle Kategorien auflisten
POST   /v1/normalized/categories            — Neue Kategorie erstellen
PUT    /v1/normalized/categories/{name}     — Kategorie aktualisieren

GET    /v1/normalized/mappings/{template}   — Mapping eines Templates anzeigen
PUT    /v1/normalized/mappings/{template}   — Mapping aktualisieren

GET    /v1/corrections                      — Alle Korrekturen auflisten
POST   /v1/corrections                      — Korrektur einreichen
PUT    /v1/corrections/{id}                 — Korrektur reviewen (applied/rejected)
```

**WICHTIG fuer LLMs:** In `/v1/tips` wird ein `common_mistakes` Eintrag hinzugefuegt: "Do NOT modify normalized_fields, normalized_values or normalized_categories directly in the database. Use the /v1/normalized/* REST endpoints instead. These endpoints validate constraints, update versions, and log changes."

### 7.6 Caching

Normalization-Daten (Felder, Werte, Mappings) werden beim ersten Zugriff in-memory gecacht. Invalidierung: Versions-Hash-Check alle 60 Sekunden gegen die DB. Wenn sich der Hash aendert, wird der Cache geleert. Konfigurierbar via .env:

- `NORMALIZE_CACHE_TTL_SECONDS` (Default: 60) — Wie oft der Hash geprüft wird
- `NORMALIZE_CACHE_ENABLED` (Default: true) — Cache komplett deaktivieren fuer Debugging

Nach dem Warmup: Kein DB-Query pro Request fuer Normalization-Daten.

---

## 8. Seeding

### 8.1 Umfang und Qualitaetsanforderungen

Das Seeding ist der kritischste Teil dieses Features. Es muss akribisch und vollstaendig sein — keine Luecken, keine Abkuerzungen, kein "das machen wir spaeter".

| Tabelle | Eintraege | Methode |
|---------|-----------|---------|
| normalized_categories | ~15 | Manuell definiert (siehe Abschnitt 2.1) |
| normalized_fields | Initial ~50, erweiterbar | Manuell definiert (siehe Abschnitt 2.2) |
| normalized_values | ~200+ | System-Standards (ISO) + manuell gepflegte Aliases |
| normalize_mapping (143 Templates) | 143 × alle Felder | LLM-Batch generiert, manuell geprueft |
| required_normalized_fields (143 Templates) | 143 Arrays | Manuell pro Template definiert |
| normalized_test_fixtures | Mindestens 143 | LLM-generiert aus echten Dokumenten, manuell geprueft |

**Qualitaetsanforderungen vor Go-Live:**
- 100% Template-Coverage: Kein Template ohne vollstaendiges Mapping
- 100% Feld-Coverage: Jedes normalized_field in jedem Mapping erwaehnt (auch wenn `null`)
- 100% Fixture-Coverage: Jedes Template hat mindestens 1 Test-Fixture
- Alle Fixtures muessen bestehen
- Manueller Review aller Mappings — LLM-Output ist Ausgangsbasis, nicht Endprodukt

### 8.2 Seed-Workflow

1. `normalized_categories` manuell definieren und seeden
2. `normalized_fields` manuell definieren und seeden
3. `normalized_values` seeden: Zuerst system-Tier (ISO-Standards), dann managed-Tier (Steuerkategorien, Zahlungsmethoden, etc.)
3b. `scoring_weight` Tabelle: Neue Eintraege fuer Normalization seeden (normalize_weight_completeness=0.5, normalize_weight_validation=0.3, normalize_weight_plausibility=0.2, confidence_mapping=1.0, confidence_correction=1.0, confidence_default=0.95, confidence_heuristic=0.8, confidence_llm=0.7, confidence_fallback=0.3)
4. Per Brix `llm.batch` Brick fuer jedes der 143 Templates das `normalize_mapping` generieren lassen. Prompt pro Template: "Gegeben dieses Template-Schema {schema}, und diese normalisierten Zielfelder {fields}, erstelle ein JSON-Mapping das jedes Zielfeld auf das passende Quellfeld mappt. Nutze Dot-Notation fuer verschachtelte Pfade. Setze null wenn kein passendes Quellfeld existiert."
5. Brix Pipeline `daigestr-seed-mappings` schreibt alle 143 generierten Mappings via `PUT /v1/normalized/mappings/{template}` in die DB.
6. Review-Report generieren: Brix Pipeline die pro Template Auffaelligkeiten markiert (fehlende Pflichtfelder, ungewoehnliche Mappings, Felder die in mehreren Templates auf unterschiedliche Quellen zeigen). Report als Markdown fuer manuellen Review.
7. Manueller Review ALLER 143 Mappings — Report durchgehen, Korrekturen per API einspielen.
8. `required_normalized_fields` pro Template manuell definieren (basierend auf Dokumenttyp-Logik: Rechnungen brauchen amount+invoice_number, Vertraege brauchen contract_number+date_issued, etc.)
9. Per Brix `llm.batch` Brick fuer jedes Template mindestens 1 Test-Fixture generieren: "Gegeben dieses Template-Schema und dieses Mapping, generiere ein realistisches extracted-Objekt und das erwartete normalized Ergebnis."
10. Alle Fixtures ausfuehren via `GET /v1/normalized/coverage` — Fixture-Pass-Rate muss 100% sein.
11. Coverage Report pruefen: Alles 100%. Kein Template ohne Mapping, kein Mapping ohne Fixture.
12. Go-Live

---

## 9. Feedback-Loop

### 9.1 Design-Prinzip

daigestr hat KEINE Abhaengigkeit zum Consumer. Die Feedback-Schnittstelle ist optional — daigestr funktioniert vollstaendig ohne dass je eine Correction eingeht.

### 9.2 Flow

```
Consumer korrigiert Wert → POST /v1/corrections → extraction_corrections Tabelle
                                                           ↓
                        Normalizer prueft bei neuen Extraktionen:
                        "Gibt es Corrections fuer dieses Template/Feld?"
                                                           ↓
                        Wenn mehrere Corrections fuer dasselbe Mapping existieren:
                        → Vorschlag generieren: "Mapping X→Y scheint falsch, 5 Corrections zeigen Z"
                                                           ↓
                        Admin reviewed Vorschlag manuell → Mapping-Update (oder Ablehnung)
```

Korrekturen werden NIEMALS automatisch ins Mapping uebernommen. Immer manueller Review.

---

## 10. Test-Strategie

### 10.1 Unit Tests

- Normalizer-Kernlogik: Feld-Mapping mit Dot-Notation, verschachtelte Pfade
- Wert-Normalisierung: Alias-Aufloesung, kanonische Werte
- Datentyp-Konvertierung: Dezimalkomma, Datumsformate, Booleans
- Validierungsregeln: MOD-97, Regex-Pattern, Bereichspruefung
- Cross-Field-Plausibilisierung: Betragskonsistenz, Datumslogik
- Default-Logik: Kontextabhaengige Defaults, verbotene Defaults

### 10.2 Integration Tests (Fixtures)

Pro Template mindestens 1 Fixture: `input_extracted` durch den Normalizer → Vergleich mit `expected_normalized`. Automatisiert in CI/CD. Bei Mapping-Aenderung muessen betroffene Fixtures weiterhin bestehen.

### 10.3 Coverage Monitoring

Regelmaessig (z.B. taeglich) pruefen:
- Coverage Report: 100% ueberall
- Fixture-Pass-Rate: Alle bestehen
- Alert wenn Coverage sinkt (z.B. neues Template ohne Mapping hinzugefuegt)

---

## 11. Abhaengigkeiten und Abgrenzung

### 11.1 daigestr-intern

- Bestehender `auto_extract` Flow bleibt vollstaendig unveraendert
- Normalizer ist ein ZUSAETZLICHER Schritt nach `auto_extract`
- Wenn Normalisierung fehlschlaegt: `extracted` wird trotzdem ausgeliefert, `normalized` ist `null` mit Fehler-Info
- Keine Aenderung am bestehenden API-Vertrag — nur Erweiterung

### 11.2 Keine externen Abhaengigkeiten

- Kein Import von buddy, Twin, Metabase oder anderen Systemen
- Kein Callback, kein Webhook, kein Event an externe Systeme
- daigestr liefert `normalized` im Response — was der Consumer damit macht ist dessen Sache
- `POST /v1/corrections` ist optional und wird passiv entgegengenommen

### 11.3 Consumer-Verantwortung (separate Tasks, NICHT Teil dieses Features)

- buddy: `normalized` Felder auf buddy-DB-Spalten mappen und speichern
- buddy: Bestehende Dokumente re-normalisieren via Batch-Endpoint
- Metabase: Dashboards auf `normalized` Felder umstellen
- Twin: Queries auf `normalized` anpassen

---

## 12. Entscheidungslog

Dieses Kapitel dokumentiert die getroffenen Entscheidungen und deren Begruendung, damit sie in zukuenftigen Sessions nicht erneut diskutiert werden muessen.

| # | Entscheidung | Begruendung | Entschieden von |
|---|-------------|-------------|-----------------|
| 1 | Technische Feldnamen auf Englisch, Labels auf Deutsch + Englisch | API international lesbar, Dashboards trotzdem deutsch | Hans + Claude |
| 2 | Kein Limit auf Anzahl normalized_fields | Schema muss jederzeit erweiterbar sein ohne kuenstliche Grenzen | Hans |
| 3 | line_items flache Struktur (keine Verschachtelung) | Einfacher in SQL abfragbar, kein Vorteil bei wenigen Feldern pro Position | Hans |
| 4 | Corrections IMMER manueller Review, nie automatisch | Automatische Mapping-Aenderungen koennten fehlerhafte Korrekturen propagieren | Hans |
| 5 | daigestr bietet Batch-Re-Normalisierung-Endpoint | Consumer soll nicht selbst normalisieren muessen, daigestr hat die Logik | Hans + Claude |
| 6 | Validierung warnt, bricht NIEMALS | Unvollstaendiges Ergebnis besser als gar keins. Pflichtfelder sind Qualitaetsindikator | Hans |
| 7 | Defaults nur fuer ableitbare Kontextdaten, nie fuer faktische Daten | Verfaelschung von Betraegen/Nummern/Daten ist inakzeptabel | Hans |
| 8 | normalized_values drei Tiers (system/managed/user) | Verschiedene Aenderungshaeufigkeiten und Verantwortlichkeiten | Claude, bestaetigt Hans |
| 9 | Kontext-Erkennung: Adresse > IBAN > Sprache > Fallback DE | Priorisierung nach Zuverlaessigkeit der Quelle | Claude, bestaetigt Hans |
| 10 | Mapping auf bestehender templates Tabelle, keine separate Tabelle | Performance (kein JOIN bei jeder Extraktion), 1:1 Zuordnung | Claude, bestaetigt Hans |
| 11 | Keine kuenstlichen Limits auf Feldlaengen, Slugs, Markdown etc. | User will keine willkuerlichen Limits die spaeter Probleme machen | Hans |
| 12 | Country-Trennung: vendor_country, recipient_country, tax_country | Mistral-Rechnung: Vendor FR, Empfaenger DE — "country" allein ist mehrdeutig. tax_country als resultierendes Feld fuer Steuer-Zuordnung. Hans wollte die Trennung explizit, Steuer-Relevanz muss zusaetzlich resultierend angegeben werden. | Hans |
| 13 | Compact-Flag fuer null-Felder | 20+ von 41 Feldern sind bei typischen Dokumenten null. 60%+ Payload-Einsparung. Optionaler Parameter, Default false. | Hans, Vorschlag Claude |
| 14 | Seeding der 143 Mappings via Brix llm.batch | Manuelles Mapping fuer 143 Templates × 41 Felder = 5.863 Entscheidungen ist nicht realistisch. LLM generiert Ausgangsbasis, Review-Report markiert Auffaelligkeiten, Hans prueft manuell. Brix orchestriert den Workflow. | Hans |
| 15 | Admin-Endpoints mit Swagger-Beschreibungen | LLMs die daigestr nutzen muessen die Normalization-API verstehen koennen OHNE in der DB rumzupfuschen. Swagger/OpenAPI ist der Standard den jedes LLM lesen kann. Tips-Eintrag warnt explizit vor direktem DB-Zugriff. | Hans |
| 16 | In-Memory Cache mit Version-Hash-Invalidierung | Kein DB-Query pro Request nach Warmup. 60s Check-Intervall konfigurierbar. Performance-Bedenken von Claude, pragmatische Loesung die CPU-only ist. | Claude, bestaetigt Hans |
| 17 | Fallback-Template: KEIN amount ableiten | "Groesster Betrag im Dokument" koennte MwSt statt Brutto sein. Lieber ehrlich null als falsch. Nur sicher ableitbare Felder (vendor_name, date_issued, language) bei Fallback. | Claude Vorschlag, bestaetigt Hans |
| 18 | Quality Score Gewichtungen in scoring_weight Tabelle (DB) | Zentral konfigurierbar, nicht hardcoded. DB hat Vorrang ueber .env. Gleiche Tabelle die schon fuer andere Gewichtungen genutzt wird. | Hans (Regel: nichts hardcoded, alles konfigurierbar) |
| 19 | Confidence-Werte konfigurierbar in scoring_weight Tabelle | 1.0/0.95/0.8/0.7/0.3 als Defaults, aenderbar ohne Code-Deployment. Hans besteht darauf dass ALLES konfigurierbar ist. | Hans |
| 20 | Fallback-Country konfigurierbar (NORMALIZE_FALLBACK_COUNTRY) | Default DE, aber muss aenderbar sein falls daigestr international eingesetzt wird. | Hans (Regel: nichts hardcoded) |
| 21 | Plausibilitaets-Toleranz konfigurierbar (NORMALIZE_PLAUSIBILITY_TOLERANCE) | Default 1%, aber verschiedene Dokumenttypen koennten andere Toleranzen brauchen. | Hans (Regel: nichts hardcoded) |
| 22 | PostgreSQL als Voraussetzung (nicht SQLite) | Normalization Layer braucht SERIAL, TIMESTAMPTZ, TEXT[], JSONB nativ, Foreign Keys mit Constraints. Eigener daigestr-postgres Container, keine externe Abhaengigkeit. Migration als eigenes Epic (E-DAI-POSTGRES) VOR diesem Feature. | Hans |
| 23 | Feedback-Endpoints in Swagger mit Beschreibungen | Feedback wird ausschliesslich manuell durchgefuehrt, aber ein LLM muss den Workflow verstehen koennen. Swagger mit Beschreibungen ist der Weg. Kein automatischer Review, kein auto-apply. | Hans |
| 24 | Kein Unterstrich-Prefix bei Response-Feldern | Pydantic v2 behandelt _ Prefix als private Attribute → JSON-Serialisierung bricht. `normalized` statt `_normalized`. | Claude (technisch), bestaetigt Hans |
| 25 | Normalizer laeuft bei extracted != None UND Mapping existiert | Nicht nur bei auto_extract — auch bei explizitem template Parameter. NICHT bei extract_schema (Custom Schema hat kein Mapping). | Claude Vorschlag, bestaetigt Hans |
| 26 | line_items_count als Computed Field | Wird nach line_items Sub-Normalisierung automatisch als len(line_items) berechnet. Kein Mapping-Eintrag noetig. | Claude |
| 27 | schema_version als manueller Integer-Counter | In scoring_weight Tabelle gespeichert. Wird bewusst manuell hochgezaehlt — keine Automatik. Consumer nutzt Version-Hash fuer Cache-Invalidierung. | Claude |
| 28 | GIN-Index auf normalized_values.aliases | Alias-Suche mit ANY() waere O(N) ohne Index. GIN-Index macht Array-Lookups performant. Bei 200+ Werten relevant. | Claude |
| 29 | normalized_test_fixtures: KEIN Foreign Key auf template | Fixtures sollen auch existieren wenn Template temporaer deaktiviert oder umbenannt wird. Test-Artefakte unabhaengig vom Template-Lifecycle. | Claude, bestaetigt Hans |

---

## 13. Neue .env Variablen (zentrale Uebersicht)

Alle neuen Konfigurationswerte die dieses Feature einfuehrt. Keine hardcodierten Werte im Code — alles ueber .env oder die DB-Tabelle `scoring_weight` konfigurierbar.

| Variable | Default | Typ | Beschreibung |
|----------|---------|-----|-------------|
| `NORMALIZE_CACHE_TTL_SECONDS` | 60 | int | Wie oft der Version-Hash gegen DB geprueft wird (Sekunden) |
| `NORMALIZE_CACHE_ENABLED` | true | bool | Normalization-Cache komplett deaktivieren fuer Debugging |
| `NORMALIZE_FALLBACK_COUNTRY` | DE | str | Fallback-Laendercode wenn kein Land erkennbar (ISO 3166-1 Alpha-2) |
| `NORMALIZE_PLAUSIBILITY_TOLERANCE` | 0.01 | float | Toleranz fuer Cross-Field-Plausibilitaetspruefungen (1% = 0.01) |

**In `scoring_weight` Tabelle (DB, Vorrang ueber .env):**

| Name | Default | Beschreibung |
|------|---------|-------------|
| `normalize_weight_completeness` | 0.5 | Gewichtung Pflichtfeld-Abdeckung im Quality Score |
| `normalize_weight_validation` | 0.3 | Gewichtung Validierungsergebnis im Quality Score |
| `normalize_weight_plausibility` | 0.2 | Gewichtung Plausibilitaetsergebnis im Quality Score |
| `confidence_mapping` | 1.0 | Confidence fuer direktes Feld-Mapping |
| `confidence_correction` | 1.0 | Confidence fuer User-Korrekturen |
| `confidence_default` | 0.95 | Confidence fuer kontextbasierte Defaults |
| `confidence_heuristic` | 0.8 | Confidence fuer heuristische Ableitungen |
| `confidence_llm` | 0.7 | Confidence fuer LLM-Extraktionen ohne Validierung |
| `confidence_fallback` | 0.3 | Confidence fuer Fallback-Template minimale Normalisierung |
