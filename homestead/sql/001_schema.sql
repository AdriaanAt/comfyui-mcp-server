-- Homestead registry: the structured half of the household knowledge base.
--
-- Scope: facts that need querying, joining or date arithmetic - what a thing
-- is, where it came from, when it expires, what it read this month. Scanned
-- paper (manuals, receipts, warranty cards) lives in Paperless-ngx and is
-- referenced here by document id; the ebook/how-to corpus lives in Open WebUI
-- Knowledge for semantic search. This file deliberately stores none of that
-- content itself.
--
--   psql -U homestead -d homestead -f homestead/sql/001_schema.sql

BEGIN;

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ---------------------------------------------------------------------------
-- Places
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS properties (
    id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    name         text NOT NULL UNIQUE,
    kind         text NOT NULL DEFAULT 'home'
                 CHECK (kind IN ('home', 'office', 'other')),
    address      text,
    notes        text,
    created_at   timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE properties IS
    'Physical sites. The home and the office are separate rows so equipment '
    'and readings never get silently mixed between them.';

-- Rooms, and outdoor areas like "front garden bed" that irrigation cares about.
CREATE TABLE IF NOT EXISTS locations (
    id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id  uuid NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    name         text NOT NULL,
    indoor       boolean NOT NULL DEFAULT true,
    notes        text,
    UNIQUE (property_id, name)
);

-- ---------------------------------------------------------------------------
-- Equipment: the answer to "what is it, where did it come from, is it covered"
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS equipment (
    id                uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id       uuid NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    location_id       uuid REFERENCES locations(id) ON DELETE SET NULL,

    name              text NOT NULL,
    category          text,              -- 'irrigation', 'security', 'network'
    manufacturer      text,
    model             text,
    serial_number     text,

    -- How it talks. Populated for anything on the automation network, and the
    -- basis of the compatibility checks below.
    protocol          text CHECK (protocol IN (
                          'zigbee', 'thread', 'matter', 'wifi', 'bluetooth',
                          'zwave', 'rf433', 'ethernet', 'modbus', 'none')),
    ecosystem         text,              -- 'ewelink', 'tuya', 'tasmota', ...
    integration_path  text,              -- how it actually reaches the platform

    purchased_on      date,
    purchase_price    numeric(12, 2),
    currency          char(3) NOT NULL DEFAULT 'ZAR',
    supplier          text,
    warranty_expires  date,

    status            text NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'spare', 'faulty',
                                        'returned', 'retired')),
    notes             text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

COMMENT ON COLUMN equipment.integration_path IS
    'How this device actually reaches the platform, e.g. "zigbee2mqtt via '
    'MG24 coordinator" or "flashed to Tasmota, native MQTT". Left null until '
    'proven working - an empty value is the signal that a device is bought '
    'but not yet reachable.';

-- Serial numbers are unique per manufacturer where present, but plenty of
-- cheap devices have none, so this is a partial index rather than a column
-- constraint.
CREATE UNIQUE INDEX IF NOT EXISTS equipment_serial_key
    ON equipment (manufacturer, serial_number)
    WHERE serial_number IS NOT NULL;

CREATE INDEX IF NOT EXISTS equipment_property_idx  ON equipment (property_id);
CREATE INDEX IF NOT EXISTS equipment_warranty_idx  ON equipment (warranty_expires)
    WHERE warranty_expires IS NOT NULL;
CREATE INDEX IF NOT EXISTS equipment_unreachable_idx ON equipment (property_id)
    WHERE integration_path IS NULL;

-- ---------------------------------------------------------------------------
-- Documents: pointers into Paperless-ngx, never the file itself
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS documents (
    id             uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    title          text NOT NULL,
    kind           text NOT NULL CHECK (kind IN (
                       'manual', 'receipt', 'warranty', 'invoice',
                       'certificate', 'guide', 'other')),
    paperless_id   integer,            -- document id in Paperless-ngx
    external_url   text,               -- vendor manual PDF, if online
    document_date  date,
    notes          text,
    created_at     timestamptz NOT NULL DEFAULT now(),

    -- A document that is neither in Paperless nor reachable online cannot be
    -- retrieved, which defeats the point of recording it.
    CONSTRAINT documents_locatable
        CHECK (paperless_id IS NOT NULL OR external_url IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS documents_paperless_key
    ON documents (paperless_id) WHERE paperless_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS equipment_documents (
    equipment_id  uuid NOT NULL REFERENCES equipment(id)  ON DELETE CASCADE,
    document_id   uuid NOT NULL REFERENCES documents(id)  ON DELETE CASCADE,
    PRIMARY KEY (equipment_id, document_id)
);

-- ---------------------------------------------------------------------------
-- People, and the things that quietly lapse
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS people (
    id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    name         text NOT NULL,
    relationship text,               -- 'self', 'spouse', 'child'
    birth_date   date,
    notes        text
);

CREATE TABLE IF NOT EXISTS health_records (
    id          uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    person_id   uuid NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    kind        text NOT NULL CHECK (kind IN (
                    'vaccination', 'checkup', 'prescription', 'allergy',
                    'condition', 'dental', 'optical', 'other')),
    description text NOT NULL,
    occurred_on date,
    next_due_on date,
    provider    text,
    document_id uuid REFERENCES documents(id) ON DELETE SET NULL,
    notes       text
);

CREATE INDEX IF NOT EXISTS health_due_idx ON health_records (next_due_on)
    WHERE next_due_on IS NOT NULL;

-- Renewals with a deadline: licences, insurance, service intervals, TV licence.
CREATE TABLE IF NOT EXISTS important_dates (
    id            uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id   uuid REFERENCES properties(id) ON DELETE CASCADE,
    person_id     uuid REFERENCES people(id)     ON DELETE CASCADE,
    equipment_id  uuid REFERENCES equipment(id)  ON DELETE CASCADE,
    title         text NOT NULL,
    category      text,
    due_on        date NOT NULL,
    -- Months between recurrences; null means one-off.
    recurs_months integer CHECK (recurs_months IS NULL OR recurs_months > 0),
    notes         text,
    completed_on  date
);

CREATE INDEX IF NOT EXISTS important_dates_due_idx
    ON important_dates (due_on) WHERE completed_on IS NULL;

-- ---------------------------------------------------------------------------
-- Accounts
--
-- Deliberately NOT a password store. Rows record which service, which
-- username, and WHERE the secret is kept - a vault entry, a sealed envelope in
-- the safe. The custodian agent can then tell a family member exactly how to
-- get in without any secret ever sitting in a database an LLM can read, or in
-- a backup, or in a query log.
--
-- If a secret genuinely must live here, add a pgcrypto column and keep the key
-- outside the agent's reach - never plaintext.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS accounts (
    id              uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    service         text NOT NULL,     -- 'Netflix', 'home wifi', 'municipality'
    category        text,              -- 'streaming', 'utility', 'network'
    username        text,
    secret_location text NOT NULL,     -- 'Bitwarden > Home > Netflix'
    recovery_notes  text,
    property_id     uuid REFERENCES properties(id) ON DELETE CASCADE,
    renews_on       date,
    monthly_cost    numeric(12, 2),
    currency        char(3) NOT NULL DEFAULT 'ZAR',
    notes           text,
    UNIQUE (service, username)
);

COMMENT ON COLUMN accounts.secret_location IS
    'Where the password lives - NOT the password. Required, because an '
    'account nobody can get into is not recorded, it is lost.';

-- ---------------------------------------------------------------------------
-- Utilities and solar
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS meter_readings (
    id          uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id uuid NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    utility     text NOT NULL CHECK (utility IN ('water', 'electricity', 'gas')),
    read_on     date NOT NULL,
    reading     numeric(14, 3) NOT NULL,
    unit        text NOT NULL,          -- 'kL', 'kWh'
    cost        numeric(12, 2),
    source      text NOT NULL DEFAULT 'manual'
                CHECK (source IN ('manual', 'mqtt', 'bill')),
    notes       text,
    UNIQUE (property_id, utility, read_on, source)
);

CREATE INDEX IF NOT EXISTS meter_readings_lookup_idx
    ON meter_readings (property_id, utility, read_on DESC);

-- Panels are tracked individually so each addition's contribution is visible.
-- NOTE: per-panel output requires panel-level hardware (microinverters or
-- optimisers). With a plain string inverter, readings are per MPPT string -
-- record one panel row per string in that case and say so in notes.
CREATE TABLE IF NOT EXISTS solar_panels (
    id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id  uuid NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    label        text NOT NULL,
    watts_peak   integer CHECK (watts_peak > 0),
    installed_on date,
    orientation  text,
    equipment_id uuid REFERENCES equipment(id) ON DELETE SET NULL,
    notes        text,
    UNIQUE (property_id, label)
);

CREATE TABLE IF NOT EXISTS solar_readings (
    id            uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    panel_id      uuid NOT NULL REFERENCES solar_panels(id) ON DELETE CASCADE,
    read_on       date NOT NULL,
    kwh_generated numeric(10, 3) NOT NULL CHECK (kwh_generated >= 0),
    UNIQUE (panel_id, read_on)
);

-- ---------------------------------------------------------------------------
-- Plants, so irrigation can be answered from record rather than guesswork
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS plants (
    id                uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    location_id       uuid REFERENCES locations(id) ON DELETE SET NULL,
    common_name       text NOT NULL,
    species           text,
    planted_on        date,
    -- Litres per watering and how often, kept separate so a schedule can be
    -- derived rather than restated in prose.
    water_litres      numeric(8, 2) CHECK (water_litres > 0),
    water_every_days  integer CHECK (water_every_days > 0),
    sun               text CHECK (sun IN ('full', 'partial', 'shade')),
    notes             text
);

-- ---------------------------------------------------------------------------
-- Compatibility: institutional memory, so a mistake is only made once
--
-- Seeded from what has already been learned the hard way. Scout consults this
-- before a purchase is made.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS compatibility_rules (
    id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    subject      text NOT NULL,          -- device, chip or ecosystem
    verdict      text NOT NULL CHECK (verdict IN (
                     'works', 'works_with_caveats', 'blocked', 'unknown')),
    requires     text,                   -- what must be in place first
    caveat       text,
    evidence_url text,
    checked_on   date NOT NULL DEFAULT CURRENT_DATE,
    notes        text
);

CREATE INDEX IF NOT EXISTS compatibility_subject_idx
    ON compatibility_rules (lower(subject));

-- Keep updated_at honest on equipment.
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS equipment_touch ON equipment;
CREATE TRIGGER equipment_touch
    BEFORE UPDATE ON equipment
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

COMMIT;
