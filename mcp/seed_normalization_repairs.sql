-- Idempotent normalization repairs for existing PostgreSQL installations.

UPDATE template
SET
    normalize_mapping = '{
      "amount": "endsaldo",
      "amount_tax": "_meta.mwst_betrag",
      "auto_renewal": "_meta.automatische_verlaengerung",
      "bic": "bic",
      "contract_number": "_meta.vertragsnummer",
      "currency": "währung",
      "date_due": "_meta.faelligkeitsdatum",
      "date_issued": "datum",
      "date_period_from": "zeitraum.von",
      "date_period_to": "zeitraum.bis",
      "iban_recipient": "_meta.empfaenger_iban",
      "iban_vendor": "iban",
      "invoice_number": "auszugsnummer",
      "line_items": "buchungen",
      "mandate_reference": "_meta.mandatsreferenz",
      "order_number": "_meta.bestellnummer",
      "payment_frequency": "_meta.zahlungsweise",
      "payment_method": "_meta.zahlungsart",
      "phone_number": "_meta.absender.telefon",
      "recipient_address": "_meta.empfaenger.adresse",
      "recipient_country": "_meta.empfaenger.adresse.land",
      "recipient_name": "_meta.empfaenger.name",
      "summary": "_meta.zusammenfassung",
      "tax_category": "_meta.steuer_kategorie",
      "tax_rate": "_meta.mwst_satz",
      "tax_relevant": "_meta.steuerrelevant",
      "vendor_address": "_meta.absender.adresse",
      "vendor_country": "_meta.absender.adresse.land",
      "vendor_email": "_meta.absender.email",
      "vendor_name": "_meta.absender.name",
      "vendor_phone": "_meta.absender.telefon",
      "vendor_slug": "_meta.absender.slug",
      "vendor_tax_id": "_meta.absender.ust_id"
    }'::jsonb,
    updated_at = now()
WHERE id = 'bank_statement';
