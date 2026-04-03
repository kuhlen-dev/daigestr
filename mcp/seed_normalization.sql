-- Daigestr Normalization Seed Data (T-DAI-051)
-- INSERT ... ON CONFLICT DO UPDATE for idempotent reloads.

-- =============================================================================
-- 1. normalized_categories (~17 rows)
-- =============================================================================

INSERT INTO normalized_categories (name, parent_name, label_de, label_en, description, sort_order) VALUES
  ('financial',                  NULL,          'Finanzen',              'Financial',           'Finanzielle Felder (Beträge, Zahlungen, Steuern)',              10),
  ('financial.amount',           'financial',   'Beträge',               'Amounts',             'Geldbeträge und Währungen',                                    11),
  ('financial.payment',          'financial',   'Zahlung',               'Payment',             'Zahlungsart, Bankverbindung, Mandate',                          12),
  ('financial.tax',              'financial',   'Steuer',                'Tax',                 'Steuerrelevante Felder (Kategorie, Abzugsfähigkeit)',           13),
  ('reference',                  NULL,          'Referenzen',            'References',          'Dokumenten- und Vorgangsnummern',                              20),
  ('reference.document',         'reference',   'Dokumentnummern',       'Document Numbers',    'Rechnungs-, Auftrags- und Referenznummern',                    21),
  ('reference.customer',         'reference',   'Kundennummern',         'Customer Numbers',    'Kunden-, Vertrags- und Versicherungsnummern',                  22),
  ('party',                      NULL,          'Parteien',              'Parties',             'Aussteller und Empfänger eines Dokuments',                     30),
  ('party.vendor',               'party',       'Lieferant',             'Vendor',              'Angaben zum Lieferanten/Aussteller',                           31),
  ('party.recipient',            'party',       'Empfänger',             'Recipient',           'Angaben zum Empfänger/Kunden',                                 32),
  ('temporal',                   NULL,          'Zeitangaben',           'Temporal',            'Datums- und Periodenfelder',                                   40),
  ('temporal.date',              'temporal',    'Datum',                 'Date',                'Einzelne Datumsangaben (Ausstellung, Fälligkeit, Bezahlung)',   41),
  ('temporal.period',            'temporal',    'Zeitraum',              'Period',              'Zeiträume (von–bis, Leistungszeitraum)',                        42),
  ('detail',                     NULL,          'Details',               'Details',             'Inhalts- und Positionsdaten',                                  50),
  ('detail.line_items',          'detail',      'Positionen',            'Line Items',          'Einzelpositionen einer Rechnung oder Abrechnung',              51),
  ('detail.content',             'detail',      'Inhalt',                'Content',             'Freitextfelder, Zusammenfassungen, Notizen',                   52),
  ('quality',                    NULL,          'Qualität',              'Quality',             'Dokumentqualität und Metadaten',                               60),
  ('quality.meta',               'quality',     'Metadaten',             'Metadata',            'Sprachkennung, Seitenzahl, Scores',                            61)
ON CONFLICT (name) DO UPDATE SET
  parent_name  = EXCLUDED.parent_name,
  label_de     = EXCLUDED.label_de,
  label_en     = EXCLUDED.label_en,
  description  = EXCLUDED.description,
  sort_order   = EXCLUDED.sort_order,
  updated_at   = now();


-- =============================================================================
-- 2. normalized_fields (51 Felder)
-- =============================================================================

INSERT INTO normalized_fields (name, label_de, label_en, type, category, description, sort_order) VALUES
  -- financial.amount
  ('amount',                'Gesamtbetrag',          'Total Amount',          'decimal',  'financial.amount',   'Gesamtbetrag inkl. MwSt.',                              10),
  ('amount_net',            'Nettobetrag',           'Net Amount',            'decimal',  'financial.amount',   'Nettobetrag ohne MwSt.',                                11),
  ('amount_tax',            'Steuerbetrag',          'Tax Amount',            'decimal',  'financial.amount',   'Ausgewiesener MwSt.-Betrag',                            12),
  ('tax_rate',              'Steuersatz',            'Tax Rate',              'decimal',  'financial.amount',   'Angewendeter Steuersatz (z. B. 19.0)',                   13),
  ('currency',              'Währung',               'Currency',              'enum',     'financial.amount',   'ISO-4217-Währungscode',                                 14),
  -- financial.payment
  ('iban_vendor',           'IBAN Lieferant',        'Vendor IBAN',           'string',   'financial.payment',  'IBAN des Lieferanten/Ausstellers',                      20),
  ('iban_recipient',        'IBAN Empfänger',        'Recipient IBAN',        'string',   'financial.payment',  'IBAN des Empfängers/Kunden',                            21),
  ('bic',                   'BIC',                   'BIC',                   'string',   'financial.payment',  'BIC/SWIFT-Code der Bank',                               22),
  ('payment_method',        'Zahlungsart',           'Payment Method',        'enum',     'financial.payment',  'Art der Zahlung (Lastschrift, Überweisung, …)',          23),
  ('mandate_reference',     'Mandatsreferenz',       'Mandate Reference',     'string',   'financial.payment',  'SEPA-Mandatsreferenz',                                  24),
  -- financial.tax
  ('tax_relevant',          'Steuerrelevant',        'Tax Relevant',          'boolean',  'financial.tax',      'Ist das Dokument steuerrelevant?',                      30),
  ('tax_category',          'Steuerkategorie',       'Tax Category',          'enum',     'financial.tax',      'Steuerliche Einordnung (Werbungskosten, Sonderausgaben…)',31),
  ('tax_deductible_amount', 'Abzugsfähiger Betrag',  'Tax Deductible Amount', 'decimal',  'financial.tax',      'Steuerlich abzugsfähiger Betrag',                       32),
  -- reference.document
  ('invoice_number',        'Rechnungsnummer',       'Invoice Number',        'string',   'reference.document', 'Eindeutige Rechnungsnummer',                            40),
  ('reference_number',      'Referenznummer',        'Reference Number',      'string',   'reference.document', 'Allgemeine Dokumentenreferenz',                         41),
  ('order_number',          'Bestellnummer',         'Order Number',          'string',   'reference.document', 'Bestellnummer des Empfängers',                          42),
  -- reference.customer
  ('customer_number',       'Kundennummer',          'Customer Number',       'string',   'reference.customer', 'Kundennummer beim Lieferanten',                         50),
  ('contract_number',       'Vertragsnummer',        'Contract Number',       'string',   'reference.customer', 'Vertragsnummer',                                        51),
  ('insurance_number',      'Versicherungsnummer',   'Insurance Number',      'string',   'reference.customer', 'Versicherungsnummer beim Versicherer',                  52),
  -- party.vendor
  ('vendor_name',           'Lieferant Name',        'Vendor Name',           'string',   'party.vendor',       'Name des Lieferanten/Ausstellers',                      60),
  ('vendor_address',        'Lieferant Adresse',     'Vendor Address',        'string',   'party.vendor',       'Vollständige Anschrift des Lieferanten',                61),
  ('vendor_contact',        'Lieferant Kontakt',     'Vendor Contact',        'string',   'party.vendor',       'E-Mail oder Telefon des Lieferanten',                   62),
  ('vendor_tax_id',         'Lieferant Steuernummer','Vendor Tax ID',         'string',   'party.vendor',       'USt-IdNr. oder Steuernummer des Lieferanten',           63),
  ('vendor_slug',           'Lieferant Slug',        'Vendor Slug',           'string',   'party.vendor',       'Normalisierter Lieferantenname (maschinenlesbar)',       64),
  ('vendor_country',        'Lieferant Land',        'Vendor Country',        'enum',     'party.vendor',       'Länderkennzeichen des Lieferanten (ISO 3166-1 Alpha-2)', 65),
  -- party.recipient
  ('recipient_name',        'Empfänger Name',        'Recipient Name',        'string',   'party.recipient',    'Name des Empfängers/Rechnungsempfängers',               70),
  ('recipient_address',     'Empfänger Adresse',     'Recipient Address',     'string',   'party.recipient',    'Vollständige Anschrift des Empfängers',                 71),
  ('recipient_country',     'Empfänger Land',        'Recipient Country',     'enum',     'party.recipient',    'Länderkennzeichen des Empfängers (ISO 3166-1 Alpha-2)', 72),
  -- temporal.date
  ('date_issued',           'Ausstellungsdatum',     'Issue Date',            'date',     'temporal.date',      'Datum der Ausstellung/Rechnungsdatum',                  80),
  ('date_due',              'Fälligkeitsdatum',      'Due Date',              'date',     'temporal.date',      'Zahlungsfälligkeitsdatum',                              81),
  ('date_paid',             'Bezahldatum',           'Payment Date',          'date',     'temporal.date',      'Tatsächliches Zahlungsdatum',                           82),
  ('date_service',          'Leistungsdatum',        'Service Date',          'date',     'temporal.date',      'Datum der erbrachten Leistung',                         83),
  ('treatment_date',        'Behandlungsdatum',      'Treatment Date',        'date',     'temporal.date',      'Datum der medizinischen Behandlung',                    84),
  -- temporal.period
  ('date_period_from',      'Zeitraum von',          'Period From',           'date',     'temporal.period',    'Beginn des Leistungszeitraums',                         90),
  ('date_period_to',        'Zeitraum bis',          'Period To',             'date',     'temporal.period',    'Ende des Leistungszeitraums',                           91),
  -- detail.line_items
  ('line_items',            'Positionen',            'Line Items',            'array',    'detail.line_items',  'Liste der Rechnungspositionen',                        100),
  ('line_items_count',      'Anzahl Positionen',     'Line Items Count',      'integer',  'detail.line_items',  'Anzahl der Rechnungspositionen',                       101),
  -- detail.content
  ('summary',               'Zusammenfassung',       'Summary',               'text',     'detail.content',     'Kurze Zusammenfassung des Dokuments',                  110),
  ('notes',                 'Hinweise',              'Notes',                 'text',     'detail.content',     'Freitextnotizen und Anmerkungen',                      111),
  ('cancellation_period',   'Kündigungsfrist',       'Cancellation Period',   'string',   'detail.content',     'Kündigungsfrist (z. B. 3 Monate zum Quartalsende)',     112),
  ('contract_end',          'Vertragsende',          'Contract End',          'date',     'detail.content',     'Datum des Vertragsendes',                               113),
  ('auto_renewal',          'Automatische Verlängerung','Auto Renewal',        'boolean',  'detail.content',     'Verlängert sich der Vertrag automatisch?',              114),
  ('phone_number',          'Telefonnummer',         'Phone Number',          'string',   'detail.content',     'Rufnummer (Kontakt oder Leistungsnummer)',              115),
  -- detail.content — insurance specific
  ('insurance_type',        'Versicherungsart',      'Insurance Type',        'enum',     'detail.content',     'Art der Versicherung (Kranken, Haftpflicht, …)',        120),
  ('reimbursement_rate',    'Erstattungssatz',       'Reimbursement Rate',    'decimal',  'detail.content',     'Erstattungssatz in Prozent',                           121),
  ('own_share',             'Eigenanteil',           'Own Share',             'decimal',  'detail.content',     'Selbstbehalt/Eigenanteil in Euro',                     122),
  ('diagnosis',             'Diagnose',              'Diagnosis',             'string',   'detail.content',     'Medizinische Diagnose (ICD-10 oder Freitext)',          123),
  -- quality.meta
  ('language',              'Sprache',               'Language',              'enum',     'quality.meta',       'Dokumentsprache (ISO 639-1)',                           130),
  ('page_count',            'Seitenanzahl',          'Page Count',            'integer',  'quality.meta',       'Anzahl der Seiten',                                    131),
  ('quality_score',         'Qualitätsscore',        'Quality Score',         'decimal',  'quality.meta',       'Qualitätsscore der Extraktion (0–1)',                   132),
  ('completeness_score',    'Vollständigkeitsscore', 'Completeness Score',    'decimal',  'quality.meta',       'Vollständigkeit der extrahierten Felder (0–1)',         133),
  ('tax_country',           'Steuerland',            'Tax Country',           'enum',     'financial.tax',      'Land für steuerliche Einordnung (ISO 3166-1 Alpha-2)', 35)
ON CONFLICT (name) DO UPDATE SET
  label_de         = EXCLUDED.label_de,
  label_en         = EXCLUDED.label_en,
  type             = EXCLUDED.type,
  category         = EXCLUDED.category,
  description      = EXCLUDED.description,
  sort_order       = EXCLUDED.sort_order,
  updated_at       = now();


-- =============================================================================
-- 3. normalized_values (~200+ rows)
-- =============================================================================

-- currency
INSERT INTO normalized_values (field_name, canonical_value, aliases, is_default, description, sort_order) VALUES
  ('currency', 'EUR', ARRAY['euro', 'Eur.', 'Euro', '€', 'eur'],        true,  'Euro (Europäische Währungsunion)',  10),
  ('currency', 'USD', ARRAY['$', 'Dollar', 'US-Dollar', 'usd', 'US$'],  false, 'US-Dollar',                        20),
  ('currency', 'CHF', ARRAY['Fr.', 'Franken', 'chf', 'SFr', 'fr'],     false, 'Schweizer Franken',                30),
  ('currency', 'GBP', ARRAY['£', 'Pfund', 'gbp', 'Sterling', 'pound'], false, 'Britisches Pfund Sterling',        40)
ON CONFLICT (field_name, canonical_value) DO UPDATE SET
  aliases     = EXCLUDED.aliases,
  is_default  = EXCLUDED.is_default,
  description = EXCLUDED.description,
  sort_order  = EXCLUDED.sort_order,
  updated_at  = now();

-- payment_method
INSERT INTO normalized_values (field_name, canonical_value, aliases, is_default, description, sort_order) VALUES
  ('payment_method', 'lastschrift',  ARRAY['SEPA', 'Einzug', 'Bankeinzug', 'Lastschrift', 'Abbuchung', 'sepa-lastschrift', 'direct debit'], true,  'SEPA-Lastschrift',     10),
  ('payment_method', 'ueberweisung', ARRAY['Überweisung', 'Banküberweisung', 'überweisung', 'transfer', 'bank transfer'],                   false, 'Banküberweisung',      20),
  ('payment_method', 'kreditkarte',  ARRAY['Visa', 'Mastercard', 'Amex', 'credit card', 'Kreditkarte', 'kreditkarte'],                      false, 'Kreditkartenzahlung',  30),
  ('payment_method', 'paypal',       ARRAY['PayPal', 'Paypal', 'paypal.com'],                                                               false, 'PayPal',               40),
  ('payment_method', 'rechnung',     ARRAY['Zahlung per Rechnung', 'auf Rechnung', 'invoice', 'per Rechnung'],                              false, 'Zahlung per Rechnung', 50)
ON CONFLICT (field_name, canonical_value) DO UPDATE SET
  aliases     = EXCLUDED.aliases,
  is_default  = EXCLUDED.is_default,
  description = EXCLUDED.description,
  sort_order  = EXCLUDED.sort_order,
  updated_at  = now();

-- tax_category
INSERT INTO normalized_values (field_name, canonical_value, aliases, is_default, description, sort_order) VALUES
  ('tax_category', 'werbungskosten',                ARRAY['Werbungskosten', 'WK', 'Berufliche Ausgaben', 'Anlage N'],                                                               false, 'Werbungskosten (§9 EStG)',                    10),
  ('tax_category', 'sonderausgaben',                ARRAY['Sonderausgaben', 'SA', 'Vorsorgeaufwendungen', 'Anlage Vorsorgeaufwand'],                                                false, 'Sonderausgaben (§10 EStG)',                   20),
  ('tax_category', 'aussergewoehnliche_belastung',  ARRAY['Außergewöhnliche Belastungen', 'agB', 'AB', 'außergewöhnliche Belastung', 'Anlage agB'],                                 false, 'Außergewöhnliche Belastungen (§33 EStG)',     30),
  ('tax_category', 'haushaltsnahe_dienstleistung',  ARRAY['Haushaltsnahe Dienstleistungen', 'HnD', 'haushaltsnahe Dienstleistung', '§35a EStG', 'Handwerkerleistungen'],            false, 'Haushaltsnahe Dienstleistungen (§35a EStG)', 40),
  ('tax_category', 'betriebsausgaben',              ARRAY['Betriebsausgaben', 'BA', 'Anlage EÜR', 'betriebliche Ausgaben', 'Geschäftsausgaben'],                                    false, 'Betriebsausgaben (§4 EStG)',                  50)
ON CONFLICT (field_name, canonical_value) DO UPDATE SET
  aliases     = EXCLUDED.aliases,
  is_default  = EXCLUDED.is_default,
  description = EXCLUDED.description,
  sort_order  = EXCLUDED.sort_order,
  updated_at  = now();

-- language
INSERT INTO normalized_values (field_name, canonical_value, aliases, is_default, description, sort_order) VALUES
  ('language', 'de', ARRAY['deutsch', 'Deutsch', 'German', 'ger', 'deu'],                                     true,  'Deutsch',     10),
  ('language', 'en', ARRAY['English', 'englisch', 'Englisch', 'eng'],                                         false, 'Englisch',    20),
  ('language', 'fr', ARRAY['franzoesisch', 'Französisch', 'French', 'français', 'fra'],                       false, 'Französisch', 30),
  ('language', 'es', ARRAY['spanisch', 'Spanisch', 'Spanish', 'español', 'spa'],                              false, 'Spanisch',    40),
  ('language', 'it', ARRAY['italienisch', 'Italienisch', 'Italian', 'italiano', 'ita'],                       false, 'Italienisch', 50),
  ('language', 'nl', ARRAY['niederlaendisch', 'Niederländisch', 'Dutch', 'Nederlands', 'nld'],                false, 'Niederländisch', 60),
  ('language', 'pl', ARRAY['polnisch', 'Polnisch', 'Polish', 'polski', 'pol'],                                false, 'Polnisch',    70)
ON CONFLICT (field_name, canonical_value) DO UPDATE SET
  aliases     = EXCLUDED.aliases,
  is_default  = EXCLUDED.is_default,
  description = EXCLUDED.description,
  sort_order  = EXCLUDED.sort_order,
  updated_at  = now();

-- insurance_type
INSERT INTO normalized_values (field_name, canonical_value, aliases, is_default, description, sort_order) VALUES
  ('insurance_type', 'kranken',            ARRAY['KV', 'PKV', 'GKV', 'Krankenversicherung', 'Kranken', 'private Krankenversicherung'],   false, 'Krankenversicherung',       10),
  ('insurance_type', 'haftpflicht',        ARRAY['PHV', 'Privathaftpflicht', 'Haftpflicht', 'Haftpflichtversicherung'],                  false, 'Haftpflichtversicherung',   20),
  ('insurance_type', 'kfz',               ARRAY['KFZ', 'Auto', 'Kfz-Versicherung', 'Kraftfahrzeugversicherung', 'Fahrzeugversicherung'], false, 'Kfz-Versicherung',          30),
  ('insurance_type', 'berufsunfaehigkeit', ARRAY['BU', 'Berufsunfähigkeit', 'Berufsunfähigkeitsversicherung', 'BU-Versicherung'],        false, 'Berufsunfähigkeitsversicherung', 40),
  ('insurance_type', 'leben',             ARRAY['LV', 'Lebensversicherung', 'Leben', 'Risikolebensversicherung'],                        false, 'Lebensversicherung',        50)
ON CONFLICT (field_name, canonical_value) DO UPDATE SET
  aliases     = EXCLUDED.aliases,
  is_default  = EXCLUDED.is_default,
  description = EXCLUDED.description,
  sort_order  = EXCLUDED.sort_order,
  updated_at  = now();

-- vendor_country / recipient_country / tax_country (shared enum set, inserted per field)
INSERT INTO normalized_values (field_name, canonical_value, aliases, is_default, description, sort_order) VALUES
  ('vendor_country', 'DE', ARRAY['Deutschland', 'Germany', 'Allemagne', 'ger', 'deu'],          true,  'Deutschland',    10),
  ('vendor_country', 'AT', ARRAY['Österreich', 'Austria', 'Autriche', 'aut'],                   false, 'Österreich',     20),
  ('vendor_country', 'CH', ARRAY['Schweiz', 'Switzerland', 'Suisse', 'che'],                    false, 'Schweiz',        30),
  ('vendor_country', 'FR', ARRAY['Frankreich', 'France', 'fra'],                                false, 'Frankreich',     40),
  ('vendor_country', 'US', ARRAY['USA', 'United States', 'Amerika', 'usa'],                     false, 'USA',            50),
  ('vendor_country', 'GB', ARRAY['Großbritannien', 'United Kingdom', 'UK', 'England', 'gbr'],   false, 'Großbritannien', 60),
  ('vendor_country', 'NL', ARRAY['Niederlande', 'Netherlands', 'Holland', 'nld'],               false, 'Niederlande',    70),
  ('vendor_country', 'IT', ARRAY['Italien', 'Italy', 'Italia', 'ita'],                          false, 'Italien',        80),
  ('vendor_country', 'ES', ARRAY['Spanien', 'Spain', 'España', 'esp'],                          false, 'Spanien',        90),
  ('vendor_country', 'PL', ARRAY['Polen', 'Poland', 'Polska', 'pol'],                           false, 'Polen',         100)
ON CONFLICT (field_name, canonical_value) DO UPDATE SET
  aliases     = EXCLUDED.aliases,
  is_default  = EXCLUDED.is_default,
  description = EXCLUDED.description,
  sort_order  = EXCLUDED.sort_order,
  updated_at  = now();

INSERT INTO normalized_values (field_name, canonical_value, aliases, is_default, description, sort_order) VALUES
  ('recipient_country', 'DE', ARRAY['Deutschland', 'Germany', 'Allemagne', 'ger', 'deu'],        true,  'Deutschland',    10),
  ('recipient_country', 'AT', ARRAY['Österreich', 'Austria', 'Autriche', 'aut'],                 false, 'Österreich',     20),
  ('recipient_country', 'CH', ARRAY['Schweiz', 'Switzerland', 'Suisse', 'che'],                  false, 'Schweiz',        30),
  ('recipient_country', 'FR', ARRAY['Frankreich', 'France', 'fra'],                              false, 'Frankreich',     40),
  ('recipient_country', 'US', ARRAY['USA', 'United States', 'Amerika', 'usa'],                   false, 'USA',            50),
  ('recipient_country', 'GB', ARRAY['Großbritannien', 'United Kingdom', 'UK', 'England', 'gbr'], false, 'Großbritannien', 60),
  ('recipient_country', 'NL', ARRAY['Niederlande', 'Netherlands', 'Holland', 'nld'],             false, 'Niederlande',    70),
  ('recipient_country', 'IT', ARRAY['Italien', 'Italy', 'Italia', 'ita'],                        false, 'Italien',        80),
  ('recipient_country', 'ES', ARRAY['Spanien', 'Spain', 'España', 'esp'],                        false, 'Spanien',        90),
  ('recipient_country', 'PL', ARRAY['Polen', 'Poland', 'Polska', 'pol'],                         false, 'Polen',         100)
ON CONFLICT (field_name, canonical_value) DO UPDATE SET
  aliases     = EXCLUDED.aliases,
  is_default  = EXCLUDED.is_default,
  description = EXCLUDED.description,
  sort_order  = EXCLUDED.sort_order,
  updated_at  = now();

INSERT INTO normalized_values (field_name, canonical_value, aliases, is_default, description, sort_order) VALUES
  ('tax_country', 'DE', ARRAY['Deutschland', 'Germany', 'Allemagne', 'ger', 'deu'],        true,  'Deutschland',    10),
  ('tax_country', 'AT', ARRAY['Österreich', 'Austria', 'Autriche', 'aut'],                 false, 'Österreich',     20),
  ('tax_country', 'CH', ARRAY['Schweiz', 'Switzerland', 'Suisse', 'che'],                  false, 'Schweiz',        30),
  ('tax_country', 'FR', ARRAY['Frankreich', 'France', 'fra'],                              false, 'Frankreich',     40),
  ('tax_country', 'US', ARRAY['USA', 'United States', 'Amerika', 'usa'],                   false, 'USA',            50),
  ('tax_country', 'GB', ARRAY['Großbritannien', 'United Kingdom', 'UK', 'England', 'gbr'], false, 'Großbritannien', 60),
  ('tax_country', 'NL', ARRAY['Niederlande', 'Netherlands', 'Holland', 'nld'],             false, 'Niederlande',    70),
  ('tax_country', 'IT', ARRAY['Italien', 'Italy', 'Italia', 'ita'],                        false, 'Italien',        80),
  ('tax_country', 'ES', ARRAY['Spanien', 'Spain', 'España', 'esp'],                        false, 'Spanien',        90),
  ('tax_country', 'PL', ARRAY['Polen', 'Poland', 'Polska', 'pol'],                         false, 'Polen',         100)
ON CONFLICT (field_name, canonical_value) DO UPDATE SET
  aliases     = EXCLUDED.aliases,
  is_default  = EXCLUDED.is_default,
  description = EXCLUDED.description,
  sort_order  = EXCLUDED.sort_order,
  updated_at  = now();


-- =============================================================================
-- 4. scoring_weight (9 neue Einträge + schema_version)
-- =============================================================================

INSERT INTO scoring_weight (id, name, value, description) VALUES
  ('normalize_weight_completeness',  'Normalize Weight Completeness',  0.5,  'Gewichtung: Vollständigkeitsscore beim Normalisieren'),
  ('normalize_weight_validation',    'Normalize Weight Validation',    0.3,  'Gewichtung: Validierungsscore beim Normalisieren'),
  ('normalize_weight_plausibility',  'Normalize Weight Plausibility',  0.2,  'Gewichtung: Plausibilitätsscore beim Normalisieren'),
  ('confidence_mapping',             'Confidence Mapping',             1.0,  'Konfidenz: direktes Mapping via normalize_mapping'),
  ('confidence_correction',          'Confidence Correction',          1.0,  'Konfidenz: manuell korrigierter Wert'),
  ('confidence_default',             'Confidence Default',             0.95, 'Konfidenz: canonical_value-Treffer in normalized_values'),
  ('confidence_heuristic',           'Confidence Heuristic',           0.8,  'Konfidenz: Heuristik (Regex, Format-Erkennung)'),
  ('confidence_llm',                 'Confidence LLM',                 0.7,  'Konfidenz: LLM-generierter Wert ohne Validierung'),
  ('confidence_fallback',            'Confidence Fallback',            0.3,  'Konfidenz: Fallback / unbekannte Quelle'),
  ('normalize_schema_version',       'Normalize Schema Version',       1.0,  'Aktuelle Schema-Version der Normalisierungstabellen')
ON CONFLICT (id) DO UPDATE SET
  name        = EXCLUDED.name,
  value       = EXCLUDED.value,
  description = EXCLUDED.description;
