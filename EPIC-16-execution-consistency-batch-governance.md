# Epic 16: Execution Consistency, Batch, Policy, Governance

## Intent

Daigestr soll nicht länger aus mehreren nebeneinander gewachsenen Realitäten bestehen. Heute gibt es fachlich denselben Dokumentprozess, aber technisch unterschiedliche Ausführungs- und Beobachtungspfade: direkte Requests, Async-Jobs, Audit-Log, Progress-Snapshots, Debug-Snapshots, Retry-Logik und Brix-Artefakte. Das führt zu Inkonsistenz, schlechter Auswertbarkeit, unklarer Fehlersemantik und unnötig fragiler Integration.

Dieses Epic hat deshalb nicht das Ziel, "auch noch Batch" einzubauen. Das Ziel ist größer und sauberer: Daigestr soll ein einheitliches, dauerhaft belastbares Ausführungsmodell bekommen, auf dem Direct, Async und Batch konsistent aufsetzen. Status, Progress, Resultate, Retrys, Policies, Historie, Governance und Dokumentation sollen dabei dieselbe Wahrheit sprechen.

Die Architektur dieses Epics folgt deshalb bewusst dieser Reihenfolge:

1. zuerst kanonisches Execution-Modell
2. dann Contract und Policy
3. dann Progress, History und Observability
4. dann Input- und Idempotenzmodell
5. erst darauf Batch und Queue
6. danach Mistral-Batch-Integration
7. anschließend Artefakte, Replay, Governance und Dokumentation
8. zum Schluss ein harter Gatekeeper-Abschlussring

Es geht ausdrücklich nicht um ein MVP, nicht um Quick Wins, nicht um Workarounds und nicht um einen "erstmal pragmatischen Batch". Es geht um ein konsistentes Zielsystem.

## Leitprinzipien

- Es gibt genau ein kanonisches Laufmodell.
- Direct, Async und Batch unterscheiden sich nur in Start- und Konsumform, nicht in der fachlichen Wahrheit.
- Polling ist immer leichtgewichtig. Ergebnisse werden bewusst separat abgeholt.
- Mistral Batch ist ein interner Ausführungspfad, kein externer Contract.
- Alle relevanten Policies, Timeouts, Parallelitätsgrenzen und Schwellwerte sind über `.env` konfigurierbar.
- Es gibt keine neuen Parallelstrukturen ohne explizite Zuständigkeit.
- Es gibt keine Brix-Workarounds für Daigestr-Architekturlücken.
- Debugging, Replay, Drift-Erkennung und Governance sind Erstklassiges, kein Zusatz.
- Neue Fähigkeiten gelten erst dann als fertig, wenn Code, Tips, OpenAPI und Doku dieselbe Wahrheit sprechen.

## Wave 16.1: Kanonisches Execution-Modell

### T16.1.1 Kanonische Laufentitäten definieren

Intent:

Der Kernfehler der aktuellen Architektur ist das Fehlen eines einzigen kanonischen Laufobjekts. Solange Direct, Async und später Batch verschiedene Lebensrealitäten haben, werden Status, Progress, History und Resultate weiter auseinanderlaufen. Dieser Task definiert daher die fundamentalen Entitäten, auf denen der gesamte Dienst künftig aufbaut.

Umsetzung:

- Definition einer persistierten Entität `execution`
- Definition einer persistierten Entität `execution_attempt`
- Definition einer persistierten Entität `execution_result`
- klare Verantwortlichkeiten:
  - `execution` beschreibt den logischen Lauf
  - `execution_attempt` beschreibt einzelne Ausführungsversuche, z. B. `default` und später `full`
  - `execution_result` beschreibt das finale oder attempt-bezogene Ergebnis
- Definition der Kernfelder für `execution`:
  - `execution_id`
  - `request_id`
  - `source_type`
  - `source_ref`
  - `status`
  - `current_stage`
  - `created_at`
  - `updated_at`
  - `started_at`
  - `finished_at`
  - `document_identity`
  - `policy_context`
  - `warning_summary`
  - `error_summary`
- Definition der Kernfelder für `execution_attempt`:
  - `attempt_number`
  - `attempt_mode`
  - `attempt_reason`
  - `started_at`
  - `finished_at`
  - `status`
  - `quality_score`
  - `retry_trigger`
  - `error`
- Definition der Kernfelder für `execution_result`:
  - `result_status`
  - `success`
  - `meta`
  - `extracted`
  - `normalized`
  - `artifact_refs`
  - `warnings`
  - `error`

Erwartetes Ergebnis:

- es gibt ein schriftlich und technisch belastbares Ausführungsmodell
- jeder Dokumentlauf kann eindeutig als Execution beschrieben werden
- spätere Waves bauen nicht auf impliziten Annahmen, sondern auf einer klaren Laufidentität

### T16.1.2 Direct- und Async-Pfade auf dasselbe Execution-Modell ziehen

Intent:

Direct und Async sind fachlich derselbe Dokumentprozess. Der Unterschied darf künftig nur noch in der Art liegen, wie der Client das Ergebnis konsumiert, nicht in einer getrennten Persistenz- oder Beobachtungswelt.

Umsetzung:

- Direct-Requests erzeugen künftig ebenfalls eine `execution`
- Async-Requests erzeugen ebenfalls eine `execution`
- Async darf zusätzlich einen asynchronen Zugriffspfad haben, aber keine eigene fachliche Datenwelt
- der bestehende `job`-Pfad wird funktional auf das Execution-Modell abgebildet oder systematisch daruntergelegt
- Direct und Async teilen sich dieselbe Attempt- und Result-Logik

Erwartetes Ergebnis:

- dieselben Kerninformationen existieren für beide Startarten
- Reporting und Debugging müssen nicht mehr zwischen `job` und "nicht-job" unterscheiden
- Batch kann später auf eine stabile Basis aufsetzen

### T16.1.3 Einheitliche Result-Struktur über alle Laufarten herstellen

Intent:

Ein kanonisches Ausführungsmodell ist wertlos, wenn Ergebnisse je nach Pfad anders modelliert oder referenziert werden. Dieser Task stellt sicher, dass das Ergebnis eines Dokumentlaufs unabhängig von seiner Startart dieselbe Struktur und dieselben Referenzpunkte hat.

Umsetzung:

- finales Ergebnis wird für jede Execution in einheitlicher Form referenziert
- attempt-bezogene Ergebnisse bleiben nachvollziehbar
- finaler Result-Pointer wird explizit modelliert
- derselbe Result-Contract gilt für:
  - Direct
  - Async
  - später Batch-Items

Erwartetes Ergebnis:

- ein Dokumentergebnis ist technisch immer derselbe Objekttyp
- Brix, Diagnose, Debugging und spätere Batch-Verarbeitung arbeiten auf derselben Ergebnisform

### T16.1.4 Bestehende Tabellen und Pfade sauber einordnen

Intent:

Die bestehende Architektur soll nicht durch unkontrollierte Parallelstrukturen weiter verschlechtert werden. Dieser Task entscheidet bewusst, was mit `job`, `audit_log`, Snapshots und bestehenden Response-Wegen geschieht.

Umsetzung:

- `job`-Tabelle entweder als Legacy-View auf `execution` behandeln oder als technische Async-Hülle unter `execution`
- `audit_log` explizit als Ereignisstrom definieren, nicht als primäre Ergebnisquelle
- bestehende Snapshots an `execution_id` koppeln
- klare Regel:
  - `execution` ist kanonisch
  - `audit_log` ist beobachtender Eventstream
  - Logs sind Diagnose, nicht Contract

Erwartetes Ergebnis:

- keine unklaren Doppelzuständigkeiten mehr
- alle späteren Features landen an der richtigen Schicht

## Wave 16.2: Contract und Policy

### T16.2.1 Kanonische Pflichtfelder für alle Ausführungsarten festschreiben

Intent:

Direct, Async und später Batch dürfen keine unterschiedlichen Contract-Wahrheiten haben. Dieser Task definiert verbindlich, welche Felder für jeden Dokumentlauf kanonisch sind und wie mit `null`, Warnungen und Fehlern umzugehen ist.

Umsetzung:

- Festlegung der Pflichtfelder auf Ergebnis- und Metadatenebene
- eindeutige `null`-Semantik
- Trennung zwischen:
  - Pflichtfeld ohne Wert
  - optionalem Feld
  - fehlendem Feld als Fehler
- Definition der kanonischen Felder:
  - `meta.document_type`
  - `meta.document_type_confidence`
  - `meta.template_used`
  - `meta.template_version`
  - `meta.quality_score`
  - `meta.quality_grade`
  - `meta.retry_applied`
  - `meta.retry_reason`
  - `meta.initial_mode`
  - `meta.final_mode`
  - `meta.initial_quality_score`
  - `meta.final_quality_score`
  - `meta.pipeline_steps`
  - `meta.contract_version`

Erwartetes Ergebnis:

- jeder Client weiß exakt, worauf er sich verlassen kann
- keine Interpretation über Logs oder Nebenspiegelungen mehr

### T16.2.2 Default-Klassifizierung als Policy-Modell einführen

Intent:

Klassifizierung ist fachlich nahezu immer sinnvoll. Sie darf deshalb nicht weiter implizit oder requestabhängig zufällig sein. Dieser Task macht Klassifizierung zu einer klaren, env-gesteuerten Default-Policy.

Umsetzung:

- Einführung von `DEFAULT_CLASSIFY=true` über `.env`
- Request-Override bleibt möglich
- `auto_extract=true` erzwingt weiterhin Klassifizierung
- Policy-Auflösung wird zentral statt verteilt implementiert
- Tests für:
  - Default ohne Request-Feld
  - explizit `classify=false`
  - `auto_extract=true`

Erwartetes Ergebnis:

- dokumenttypbezogene Informationen werden konsistenter erzeugt
- Nicht-PDFs durchlaufen denselben fachlichen Standard
- Policy statt Altverhalten

### T16.2.3 Retry-, Long-Document- und Normalization-Policy explizit modellieren

Intent:

Zu viele zentrale Entscheidungen passieren heute noch "irgendwo im Code". Dieser Task macht die wichtigsten Verhaltensregeln explizit, konfigurierbar und nachvollziehbar.

Umsetzung:

- Definition einer klaren Retry-Policy
- Definition einer klaren Long-Document-Policy
- Definition einer klaren Normalization-Policy
- `.env`-gesteuerte Steuergrößen
- einheitliche Materialisierung der gewählten Policy im Execution-/Result-Kontext

Erwartetes Ergebnis:

- Verhalten ist bewusst und nachvollziehbar
- keine diffusen Seiteneffekte mehr bei langen oder grenzwertigen Dokumenten

### T16.2.4 Standardisierte Warnings in den Contract aufnehmen

Intent:

Nicht jeder auffällige Zustand ist ein Fehler. Viele betriebswichtige Zustände müssen als kanonische Warnings sichtbar werden, damit Brix und andere Systeme darauf reagieren können, ohne Logs zu parsen.

Umsetzung:

- Einführung eines standardisierten `warnings`-Blocks
- mögliche Warning-Typen:
  - `normalizer_missing_mapping`
  - `used_retry`
  - `used_markdown_reconstruction`
  - `partial_output`
  - `policy_override`
  - `upstream_retry_occurred`
- Definition, wo Warnings leben:
  - Ergebnis
  - Execution
  - Item-Kontext später im Batch

Erwartetes Ergebnis:

- wichtige Zustände werden maschinenverständlich
- weniger Blindflug downstream

### T16.2.5 Contract-Versionierung einführen

Intent:

Sobald Execution, Batch und Result Retrieval sauberer werden, braucht der Contract eine sichtbare Version. Sonst sind Breaking Changes und Migrationspfade nicht sauber steuerbar.

Umsetzung:

- Einführung von `meta.contract_version`
- Versionierung der späteren Batch-Endpunkte
- Migrationsregeln für Brix und andere Konsumenten dokumentieren

Erwartetes Ergebnis:

- bewusste Contract-Evolution statt impliziter Verhaltensänderungen

## Wave 16.3: Progress, History, Observability

### T16.3.1 Progress-Modell für alle Laufarten vereinheitlichen

Intent:

Progress darf nicht je nach Pfad, Tabelle oder Logqualität unterschiedlich sein. Dieser Task definiert einen einzigen Progress-Contract für Direct, Async und später Batch.

Umsetzung:

- Definition von:
  - `status`
  - `current_stage`
  - `percent`
  - `attempt_number`
  - `attempt_mode`
  - `attempt_count`
  - `active_subjobs`
  - `upstream_status`
- Direct-Läufe bekommen denselben internen Progress wie Async-Läufe
- APIs lesen den Progress aus derselben Wahrheit

Erwartetes Ergebnis:

- Progress ist nicht mehr mal im Log, mal im Job, mal veraltet
- Clients können sich auf dieselben Felder verlassen

### T16.3.2 `/v1/jobs`-Konsistenz vollständig reparieren

Intent:

Der bestehende Job-Status ist sichtbar inkonsistent. Dieser Task beseitigt den Zustand, dass abgeschlossene Jobs noch `processing` spiegeln oder Filter falsch arbeiten.

Umsetzung:

- Progress-Normalisierung überarbeiten
- Query-Filter auf tatsächliche Zustände bringen
- Finalisierung des Status bei Erfolg, Fehler und Abbruch konsistent machen
- Tests gegen bekannte Fehlzustände

Erwartetes Ergebnis:

- `GET /v1/jobs` und `GET /v1/jobs/{id}` sind wieder belastbar
- kein Zustand "completed mit processing-Payload" mehr

### T16.3.3 History-/Audit-Responses um fachliche Kernfelder erweitern

Intent:

Bisher lassen sich vergangene Läufe nicht sauber auswerten, weil im Audit-Response zu wenig fachliche Daten landen. Dieser Task behebt das.

Umsetzung:

- Anreicherung des persistierten Response-/History-Modells um:
  - `document_type`
  - `template_used`
  - `quality_score`
  - `retry_*`
  - `contract_version`
  - `result_status`
- gleiche Mindestdaten für Direct und Async

Erwartetes Ergebnis:

- vergangene Extraktionen lassen sich sachlich auswerten
- Tabellen und Reports benötigen keine Log-Kettenanalyse mehr

### T16.3.4 Diagnose- und Operator-Sicht einführen

Intent:

Ein produktionsfähiges System braucht eine technische Operator-Sicht, nicht nur Enduser-Endpunkte.

Umsetzung:

- Diagnose-Endpunkte oder Admin-Sichten für:
  - aktive Executions
  - stuck Executions
  - Queue-Tiefe später
  - letzte Fehlerklassen
  - Seed-/Drift-Status
  - Snapshot-Retention-Zustand
- ohne dabei PII unnötig breit offenzulegen

Erwartetes Ergebnis:

- Betrieb und Fehlersuche werden systematisch statt improvisiert

## Wave 16.4: Input, Idempotenz, Storage

### T16.4.1 Input-Fingerprint pro Dokument einführen

Intent:

Ein Lauf muss reproduzierbar sein. Dafür reicht ein Pfad oder eine Dateibezeichnung nicht. Wir brauchen einen stabilen Fingerprint des Eingangs.

Umsetzung:

- Persistenz von:
  - `path`
  - `filename`
  - `size`
  - `mtime`
  - optional `hash`
  - `source_ref`
- Input-Fingerprint wird Teil der Execution und später des Batch-Items

Erwartetes Ergebnis:

- bessere Reproduzierbarkeit
- bessere Dedup-/Idempotenzlogik
- nachvollziehbare Input-Identität

### T16.4.2 Idempotenz auf Batch- und Item-Ebene modellieren

Intent:

Bei Massendurchläufen dürfen identische Beauftragungen nicht zu unkontrollierten Doppelläufen führen.

Umsetzung:

- Einführung von `idempotency_key` auf Batch-Ebene
- stabile `item_key` oder `document_identity`
- Regeln für:
  - Wiederholung desselben Batchs
  - Wiederholung desselben Items
  - forced rerun
  - reuse existing result

Erwartetes Ergebnis:

- keine stillen Doppelverarbeitungen
- bessere Steuerbarkeit aus Brix

### T16.4.3 Storage- und Zugriffsgrenzen festziehen

Intent:

Wenn Daigestr Pfade verarbeitet, muss klar sein, welche Pfade es verarbeiten darf und unter welchen Bedingungen.

Umsetzung:

- erlaubte Root-Pfade
- Verhalten bei Symlinks
- Verhalten bei gelöschten/geänderten Dateien
- Verhalten bei nicht lesbaren Dateien
- Umgang mit Shared Storage

Erwartetes Ergebnis:

- sichere und reproduzierbare Dateiverarbeitung
- keine unkontrollierten Pfadübernahmen

### T16.4.4 Input-Snapshot-Unveränderlichkeit sicherstellen

Intent:

Ein Batch darf nicht von sich während der Laufzeit ändernden Dateien abhängen, ohne das zu merken.

Umsetzung:

- Einfrieren der Item-Liste beim Batch-Create
- Dokumentation, ob Inhalte gesnapshottet oder per Fingerprint referenziert werden
- klare Fehler-/Warning-Semantik bei Input-Drift

Erwartetes Ergebnis:

- Batchs bleiben reproduzierbar und auditierbar

## Wave 16.5: Batch- und Queue-Ausführung

### T16.5.1 Persistierte Queue und Worker-Pool bauen

Intent:

Batch-Verarbeitung darf nicht durch ungebremstes Task-Spawning entstehen. Dieser Task baut eine echte Queue mit kontrollierter Parallelität.

Umsetzung:

- persistierte Queue
- Worker-Pool
- `.env`-gesteuerte Concurrency
- claim/release/retry-Semantik
- faire Abarbeitung und Backpressure

Erwartetes Ergebnis:

- keine unkontrollierte Parallelität mehr
- besseres Verhalten unter Last

### T16.5.2 Batch-Entitäten und `POST /v1/batches` bauen

Intent:

Brix soll Daigestr eine explizite Dokumentliste übergeben können. Dieser Task schafft den Batch-Einstieg.

Umsetzung:

- `batch`
- `batch_item`
- `POST /v1/batches`
- Speicherung von:
  - `batch_ref`
  - `idempotency_key`
  - Item-Liste
  - Eingangsmetadaten
- sofortige Antwort mit:
  - `batch_id`
  - `status=queued`
  - `item_count`

Erwartetes Ergebnis:

- kein Tausend-Einzelrequest-Muster mehr für große Ordnerläufe

### T16.5.3 Leichtgewichtige Batch-Status-Endpunkte bauen

Intent:

Polling darf nicht große Payloads zurückliefern. Der Status eines Batchs muss leichtgewichtig bleiben.

Umsetzung:

- `GET /v1/batches/{batch_id}`
- aggregierte Counts und Fortschritt
- keine Vollergebnisse im Polling
- optional aktive Items und Fehlerübersicht

Erwartetes Ergebnis:

- Polling bleibt günstig
- Brix muss keine großen Payloads handhaben

### T16.5.4 Item- und Result-Retrieval bauen

Intent:

Ergebnisse müssen bewusst und kontrolliert abgeholt werden, nicht im Statuspolling stecken.

Umsetzung:

- `GET /v1/batches/{batch_id}/items`
- `GET /v1/batches/{batch_id}/items/{item_id}/result`
- `GET /v1/batches/{batch_id}/result`
- Pagination
- Sortierung
- Filterung
- optionale Export-/Download-Strategien

Erwartetes Ergebnis:

- große Batch-Ergebnisse bleiben beherrschbar
- Brix kann selektiv oder gesammelt abholen

### T16.5.5 Cancel, Resume und Retry im Batch-Kontext modellieren

Intent:

Massenverarbeitung ohne Cancel/Resume/Retry ist nicht betriebstauglich.

Umsetzung:

- Batch abbrechen
- einzelnes Item abbrechen
- Batch/Item wiederaufnehmen
- Retry pro Item
- partielle Batch-Fortsetzung
- Tests für Störfälle

Erwartetes Ergebnis:

- Batchs sind operativ kontrollierbar
- fehlerhafte Items zerstören nicht den ganzen Auftrag

## Wave 16.6: Mistral-Batch-Integration

### T16.6.1 Mistral-Batch als internen Subjob-Typ modellieren

Intent:

Mistral Batch darf nicht als lose Zusatzidee existieren. Es muss als interner Ausführungstyp sauber modelliert sein.

Umsetzung:

- Definition eines Subjob-Modells
- Verknüpfung zwischen Item und Upstream-Batch-Subjobs
- persistierte Referenzen:
  - `upstream_batch_id`
  - `upstream_item_id`
  - `subjob_status`

Erwartetes Ergebnis:

- Mistral Batch ist architektonisch sauber anschlussfähig

### T16.6.2 Entscheidungspfad direct vs queued vs mistral-batch bauen

Intent:

Nicht jeder Lauf soll in Mistral Batch. Die Wahl muss policy-gesteuert und nachvollziehbar sein.

Umsetzung:

- Heuristiken und Policies für Batch-Nutzung
- env-gesteuerte Schwellen
- Dokumentation der Auswahlpfade

Erwartetes Ergebnis:

- Batch-Nutzung ist zielgerichtet, nicht blind

### T16.6.3 Polling, Result-Mapping und Partial-Failure-Semantik für Mistral Batch bauen

Intent:

Upstream-Batches erzeugen partielle Fehler und zeitversetzte Ergebnisse. Daigestr muss diese sauber in sein eigenes Item-/Execution-Modell übersetzen.

Umsetzung:

- Upstream-Polling
- Result-Zuordnung
- partielle Fehlerbehandlung
- Retry isoliert auf fehlgeschlagene Subjobs
- Merge zurück auf Item- und Dokumentebene

Erwartetes Ergebnis:

- Mistral Batch bleibt intern transparent beherrschbar
- externer Daigestr-Contract bleibt stabil

### T16.6.4 `.env`-Konfiguration für Mistral-Batch sauber einführen

Intent:

Batch-Nutzung muss vollständig konfigurierbar sein.

Umsetzung:

- Batch-Schwellwerte
- Polling-Intervalle
- Max-Aktivität
- Feature-Toggles

Erwartetes Ergebnis:

- kein Hardcoding, keine implizite Aktivierung

## Wave 16.7: Ergebnisse, Artefakte, Replay

### T16.7.1 Finale Ergebnisse klar von Artefakten trennen

Intent:

Finale Dokumentergebnisse und Debug-/Replay-Artefakte dürfen nicht vermischt werden.

Umsetzung:

- kanonischer Result-Contract
- getrennte Artefakt-Referenzen
- keine implizite Vermischung von Debugdaten und Produkt-Output

Erwartetes Ergebnis:

- saubere Konsumierbarkeit für Brix
- saubere Diagnose für Engineering

### T16.7.2 Replay execution- und batch-item-fähig machen

Intent:

Problemfälle sollen ohne neue Upstream-Kosten reproduzierbar bleiben, auch im Batch-Kontext.

Umsetzung:

- Replay einzelner Executions
- Replay einzelner Batch-Items
- Nutzung vorhandener Snapshots
- klarer Replay-Status und Ergebnisweg

Erwartetes Ergebnis:

- Debugging wird günstiger, schneller und deterministischer

### T16.7.3 Result- und Snapshot-Retention modellieren

Intent:

Mit zunehmender Persistenz braucht das System klare TTL- und Löschregeln.

Umsetzung:

- TTL für Resultate
- TTL für Snapshots
- TTL für Artefakte
- Cleanup-Prozesse
- Tests dafür

Erwartetes Ergebnis:

- keine unkontrolliert wachsende Persistenz
- bessere Governance

## Wave 16.8: Governance und Datenschutz

### T16.8.1 Persistenz- und Retention-Regeln verbindlich festziehen

Intent:

Ein produktionsfähiges Dokumentintelligenzsystem braucht klare Regeln, was wie lange aufbewahrt werden darf.

Umsetzung:

- TTL-Regeln pro Datenklasse
- klare Einordnung:
  - execution metadata
  - result payload
  - debug snapshot
  - replay artifact

Erwartetes Ergebnis:

- keine implizite Aufbewahrung ohne Policy

### T16.8.2 PII- und Payload-Policy modellieren

Intent:

Mit strukturierten Outputs, Snapshots und Batch-Artefakten steigt das Datenschutzrisiko. Dieses Risiko muss architektonisch adressiert werden.

Umsetzung:

- Definition PII-sensitiver Inhalte
- Regeln, was dauerhaft, temporär oder nie gespeichert werden darf
- Konfigurations- und Cleanup-Regeln

Erwartetes Ergebnis:

- systematische Datenhygiene statt Einzelfallentscheidungen

### T16.8.3 Sichere Betriebsgrenzen dokumentieren und erzwingen

Intent:

Governance endet nicht bei Retention. Auch Storage, Zugriff und Admin-Sichten brauchen klare Grenzen.

Umsetzung:

- Zugriffsbeschränkungen
- Operator-Rechte
- Exportgrenzen
- Auditierbarkeit von Löschung und Wiederholung

Erwartetes Ergebnis:

- nachhaltiger Betrieb ohne spätere Governance-Nachrüstung

## Wave 16.9: Doku- und Maschinenleitfaden

### T16.9.1 `get_tips` als normativen Leitfaden auf den neuen Stand ziehen

Intent:

LLMs und Integratoren müssen neue Fähigkeiten, Regeln und Grenzen über denselben Leitfaden lernen.

Umsetzung:

- Execution-Modell
- Direct/Async/Batch
- Polling vs Result Retrieval
- Contract-Felder
- Warnings
- Policy-Verhalten
- Idempotenz
- Batch-Nutzung

Erwartetes Ergebnis:

- `get_tips` ist wieder die normative Maschinenwahrheit

### T16.9.2 OpenAPI/Swagger auf den vollständigen Contract ziehen

Intent:

API-Dokumentation muss formaler und vollständiger Contract sein, nicht nur lose Endpoint-Beschreibung.

Umsetzung:

- Execution-Modelle
- Batch-Endpunkte
- Statusmodelle
- Result-Modelle
- Pagination
- Fehlersemantik
- Contract-Versionierung

Erwartetes Ergebnis:

- Integratoren können sich auf OpenAPI stützen, ohne Dinge zu erraten

### T16.9.3 README und Betriebsdoku vollständig synchronisieren

Intent:

Menschen, Agenten und externe Tools dürfen nicht aus widersprüchlichen Dokumentationen lernen.

Umsetzung:

- README
- Betriebsdoku
- Architekturabschnitte
- Konfigurationsübersicht

Erwartetes Ergebnis:

- Doku und Laufzeitmodell sprechen dieselbe Sprache

### T16.9.4 Doku-/Tips-/API-Konsistenz testbar machen

Intent:

Synchronität darf nicht vom Gedächtnis abhängen.

Umsetzung:

- Tests oder Prüfmechanismen, die zentrale Contract-Felder und Capability-Hinweise abgleichen
- keine neue Capability ohne Dokumentationsupdate

Erwartetes Ergebnis:

- Drift zwischen Code, API und Leitfaden wird früh erkannt

## Wave 16.10: Niederschwellige, aber wichtige Konsistenzmaßnahmen

### T16.10.1 `DEFAULT_CLASSIFY=true` einführen

Intent:

Klassifizierung soll nicht zufällig oder historisch schwankend sein, sondern bewusst Standard.

### T16.10.2 Audit-/History-Responses mit fachlichen Kernfeldern anreichern

Intent:

Vergangene Läufe sollen auswertbar werden, ohne Response-Artefakte oder Logs zu parsen.

### T16.10.3 `/v1/jobs`-Konsistenz hart reparieren

Intent:

Der bestehende Async-Zugriffspfad muss wieder verlässlich sein.

### T16.10.4 Normalizer-Coverage/Seed-Drift in Diagnose und Health sichtbar machen

Intent:

Gerade der gefundene Drift zeigt, dass diese Information nicht unsichtbar bleiben darf.

### T16.10.5 Standardisierte Warnings im Result-Contract materialisieren

Intent:

Maschinen sollen auffällige, aber nicht-fehlerschwere Zustände sauber sehen können.

Erwartetes Ergebnis der gesamten Wave:

- sofort spürbare Konsistenzgewinne
- ohne das Zielmodell zu verwässern
- als integraler Teil der Architektur, nicht als Quick-Win-Sonderweg

## Wave 16.11: Tests, Gatekeeper, Live-Abnahme

### T16.11.1 Regressionsringe pro Wave bauen

Intent:

Jede Wave muss eigenständig gegen Regressionen abgesichert sein.

### T16.11.2 End-to-End-Ringe für Direct, Async und Batch bauen

Intent:

Das System soll nicht nur in Einzeltests, sondern im Zusammenspiel belastbar sein.

### T16.11.3 Drift-, Idempotenz-, Retention- und Replay-Tests einführen

Intent:

Die besonders gefährlichen Betriebsaspekte müssen explizit testbar sein.

### T16.11.4 Gatekeeper-Abschlussring für das Epic

Intent:

Nicht nur Tests, sondern bewusste Architektur- und Konsistenzprüfung.

### T16.11.5 Live-Rollout und Verifikation

Intent:

Das Epic gilt erst als fertig, wenn der aktuelle Stand live läuft, konsistent beobachtbar ist und die dokumentierten Fähigkeiten tatsächlich bereitstellt.

## Zusätzliche Punkte, die ausdrücklich berücksichtigt werden

Diese Punkte sind bewusst zusätzlich aufgenommen worden, weil sie leicht übersehen werden, aber für ein nachhaltig funktionierendes System zwingend sind:

- Input-Fingerprint pro Dokument und pro Batch-Item
- Batch- und Item-Idempotenz mit stabilen Schlüsseln
- Pfad- und Storage-Grenzen für lokale oder gemeinsam gemountete Dateien
- Input-Unveränderlichkeit und Drift-Erkennung
- klare Trennung zwischen finalem Ergebnis und Debug-/Replay-Artefakten
- standardisierte Warnings als eigener Contract-Bestandteil
- Contract-Versionierung
- Admin-/Diagnose-Sicht
- Retention- und PII-Policy
- verpflichtende Synchronisierung von `get_tips`, OpenAPI, README und Betriebsdoku

## Wichtige Architekturregeln für das gesamte Epic

- kein neuer Sonderpfad ohne Anschluss an `execution`
- kein Hardcoding von Policies, Timeouts oder Parallelität
- alles Relevante über `.env`
- kein Batch als Abkürzung über die bestehende Inkonsistenz
- keine Workarounds in Brix für Daigestr-Architekturlücken
- keine Dokuänderung ohne Codebezug und keine Codeänderung ohne Dokusynchronisierung
- keine "temporären" Parallelmodelle, die später aufgeräumt werden sollen

## Abhängigkeiten und Reihenfolge

Die sinnvolle Reihenfolge ist:

1. `W16.1` Kanonisches Execution-Modell
2. `W16.2` Contract und Policy
3. `W16.3` Progress, History, Observability
4. `W16.4` Input, Idempotenz, Storage
5. `W16.10` Niederschwellige Konsistenzmaßnahmen
6. `W16.5` Batch- und Queue-Ausführung
7. `W16.6` Mistral-Batch-Integration
8. `W16.7` Ergebnisse, Artefakte, Replay
9. `W16.8` Governance und Datenschutz
10. `W16.9` Doku- und Maschinenleitfaden
11. `W16.11` Tests, Gatekeeper, Live-Abnahme

## Erwarteter Zielzustand nach Epic 16

Nach Abschluss dieses Epics soll Daigestr ein System sein, bei dem:

- jeder Dokumentlauf ein kanonisches `execution`-Objekt ist
- Direct, Async und Batch auf derselben Laufwahrheit beruhen
- Status, Progress, History und Resultate dieselbe Sprache sprechen
- Batchs über explizite Dokumentlisten stabil und idempotent verarbeitet werden
- Mistral Batch intern sauber integrierbar ist, ohne den externen Contract zu brechen
- Ergebnisse nicht im Polling transportiert werden, sondern bewusst abgeholt werden
- Debugging, Replay und Drift-Erkennung systematisch möglich sind
- Doku, Tips und API dieselbe Wahrheit sprechen
- Policies, Retention und Governance nicht nachträglich, sondern architektonisch eingebaut sind

## Brix-Cutover nach Epic 16

Nach Epic 16 gilt fuer Brix und andere Orchestratoren ausdruecklich:

- `execution` ist die kanonische Laufwahrheit; `job` bleibt nur Async-Kompatibilitaet.
- `POST /v1/batches` ist der kanonische Batch-Einstieg fuer explizite Dokumentlisten.
- `GET /v1/executions/{id}`, `GET /v1/executions/{id}/result`, `GET /v1/batches/{id}` und `GET /v1/batches/{id}/items` sind die relevanten Konsumenten-Surfaces.
- `BRIX_URL` und Brix-Verfuegbarkeit sind nur Hints fuer Integration und Routing, nicht fuer Status- oder Result-Truth.
- Es werden keine Brix-Workarounds fuer Daigestr-Architekturluecken gebaut.
- Cutover und Migration sollen ueber OpenAPI, `get_tips`, und die kanonischen REST-Surfaces erfolgen, nicht ueber Logs oder DB-Mirrors.
