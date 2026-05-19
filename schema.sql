-- Medical Public Data Intelligence — initial schema
-- Auto-loaded by docker-compose on first Postgres start.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- Especialidades controladas
-- ============================================================
CREATE TABLE specialty (
  id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  code     TEXT UNIQUE NOT NULL,
  name_es  TEXT NOT NULL,
  name_en  TEXT,
  parent   UUID REFERENCES specialty(id)
);

CREATE TABLE specialty_synonym (
  specialty_id UUID REFERENCES specialty(id) ON DELETE CASCADE,
  term         TEXT NOT NULL,
  lang         CHAR(2) NOT NULL DEFAULT 'es',
  UNIQUE(term, lang)
);

-- ============================================================
-- Organizaciones (clínicas, hospitales, universidades, colegios)
-- ============================================================
CREATE TABLE organization (
  id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  name     TEXT NOT NULL,
  kind     TEXT NOT NULL,
  country  CHAR(2) NOT NULL,
  website  TEXT,
  domain   TEXT,
  phones   TEXT[] NOT NULL DEFAULT '{}',
  UNIQUE(name, country, kind)
);

CREATE TABLE domain_email_pattern (
  domain          TEXT PRIMARY KEY,
  pattern         TEXT,
  observed_count  INT NOT NULL DEFAULT 0,
  last_confirmed_at TIMESTAMPTZ
);

-- ============================================================
-- Fuentes
-- ============================================================
CREATE TABLE source (
  id        UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  name      TEXT UNIQUE NOT NULL,
  base_url  TEXT,
  tier      INT NOT NULL,                    -- 1 autoritativa, 2 agregador, 3 social
  is_active BOOLEAN DEFAULT TRUE
);

INSERT INTO source(name, tier, base_url) VALUES
  ('colegio_cr',  1, 'https://www.medicos.cr'),
  ('colegio_pa',  1, 'https://www.consejotecnicodesalud.gob.pa'),
  ('doctoralia',  2, 'https://www.doctoralia.com'),
  ('topdoctors',  2, 'https://www.topdoctors.com'),
  ('google_serp', 2, 'https://www.google.com'),
  ('linkedin',    3, 'https://www.linkedin.com'),
  ('manual_excel',1, 'local'),
  ('inference',   3, 'internal');

-- ============================================================
-- Entidad maestra (gold)
-- ============================================================
CREATE TABLE physician (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  full_name       TEXT NOT NULL,
  family_name_1   TEXT NOT NULL,
  family_name_2   TEXT,
  given_name      TEXT NOT NULL,
  specialty_id    UUID REFERENCES specialty(id),
  country         CHAR(2) NOT NULL,
  name_embedding  vector(384),
  confidence      NUMERIC(4,3) NOT NULL DEFAULT 0.0,
  status          TEXT NOT NULL DEFAULT 'draft',  -- draft | verified | published | merged
  assigned_to     TEXT,
  comment         TEXT,
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX physician_country_specialty ON physician(country, specialty_id);
CREATE INDEX physician_family_idx ON physician(family_name_1, family_name_2);
-- Index para similitud (crear cuando haya datos):
-- CREATE INDEX physician_name_emb_idx ON physician USING ivfflat (name_embedding vector_cosine_ops);

-- ============================================================
-- Observaciones (cada vez que vemos un dato en una fuente)
-- ============================================================
CREATE TABLE observation (
  id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  source_id         UUID NOT NULL REFERENCES source(id),
  source_url        TEXT,
  scraped_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  extractor_version TEXT NOT NULL DEFAULT 'v1',
  raw_payload       JSONB NOT NULL,
  snapshot_path     TEXT,
  physician_id      UUID REFERENCES physician(id),
  status            TEXT NOT NULL DEFAULT 'new'   -- new|matched|merged|rejected
);
CREATE INDEX observation_phys ON observation(physician_id);
CREATE INDEX observation_raw_gin ON observation USING gin (raw_payload);

-- ============================================================
-- Claims (la pieza central: cada atributo con su confianza/evidencia)
-- ============================================================
CREATE TABLE claim (
  id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  physician_id      UUID NOT NULL REFERENCES physician(id) ON DELETE CASCADE,
  attribute         TEXT NOT NULL,           -- email | phone | specialty | affiliation | social
  subkind           TEXT,                    -- direct_personal | clinic_main | whatsapp_business | ...
  value             TEXT NOT NULL,
  value_normalized  TEXT NOT NULL,
  confidence        NUMERIC(4,3) NOT NULL,
  is_inferred       BOOLEAN NOT NULL DEFAULT FALSE,
  inference_method  TEXT,
  validation_status TEXT NOT NULL DEFAULT 'unverified',
  sources           UUID[] NOT NULL DEFAULT '{}',
  evidence_strength JSONB NOT NULL DEFAULT '{}',
  contradicted_by   UUID[] NOT NULL DEFAULT '{}',
  explanation       TEXT,
  first_seen_at     TIMESTAMPTZ DEFAULT NOW(),
  last_verified_at  TIMESTAMPTZ,
  superseded_by     UUID REFERENCES claim(id),
  created_at        TIMESTAMPTZ DEFAULT NOW(),
  CHECK (confidence >= 0 AND confidence <= 1)
);
CREATE INDEX claim_phys_attr ON claim(physician_id, attribute) WHERE superseded_by IS NULL;
CREATE INDEX claim_val_norm ON claim(value_normalized);

-- Vista materializada: "mejor claim activo por atributo"
CREATE MATERIALIZED VIEW physician_current AS
SELECT DISTINCT ON (physician_id, attribute)
  physician_id, attribute, subkind, value, value_normalized,
  confidence, is_inferred, validation_status
FROM claim
WHERE superseded_by IS NULL
ORDER BY physician_id, attribute, confidence DESC, last_verified_at DESC NULLS LAST;

CREATE INDEX physician_current_idx ON physician_current(physician_id, attribute);

-- ============================================================
-- Afiliaciones
-- ============================================================
CREATE TABLE affiliation (
  id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  physician_id UUID NOT NULL REFERENCES physician(id) ON DELETE CASCADE,
  org_id       UUID NOT NULL REFERENCES organization(id),
  role         TEXT,
  since        DATE,
  until        DATE,
  confidence   NUMERIC(4,3) NOT NULL DEFAULT 0.5,
  sources      UUID[] NOT NULL DEFAULT '{}',
  UNIQUE(physician_id, org_id, role)
);

-- ============================================================
-- Cola de revisión humana
-- ============================================================
CREATE TABLE review_task (
  id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  kind         TEXT NOT NULL,    -- claim_low_conf | duplicate_pair | contradiction | inference_check
  payload      JSONB NOT NULL,
  priority     INT NOT NULL DEFAULT 5,
  assigned_to  TEXT,
  status       TEXT NOT NULL DEFAULT 'open',
  decision     JSONB,
  resolved_by  TEXT,
  resolved_at  TIMESTAMPTZ,
  created_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX review_task_open ON review_task(status, priority DESC) WHERE status = 'open';

-- ============================================================
-- Auditoría append-only
-- ============================================================
CREATE TABLE claim_audit (
  id       BIGSERIAL PRIMARY KEY,
  claim_id UUID NOT NULL,
  event    TEXT NOT NULL,
  before   JSONB,
  after    JSONB,
  actor    TEXT NOT NULL,
  reason   TEXT,
  at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX claim_audit_claim ON claim_audit(claim_id);

-- ============================================================
-- Jobs de scraping con cursor (resumability)
-- ============================================================
CREATE TABLE scrape_job (
  id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  source_id   UUID NOT NULL REFERENCES source(id),
  country     CHAR(2),
  specialty   TEXT,
  cursor      JSONB,
  status      TEXT NOT NULL DEFAULT 'queued',
  stats       JSONB NOT NULL DEFAULT '{}',
  started_at  TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- Seed mínimo de especialidades observadas en tu Excel
-- ============================================================
INSERT INTO specialty(code, name_es, name_en) VALUES
  ('pediatria',           'Pediatría',           'Pediatrics'),
  ('medicina_interna',    'Medicina Interna',    'Internal Medicine'),
  ('otorrinolaringologia','Otorrinolaringología','Otolaryngology'),
  ('ginecologia',         'Ginecología',         'Gynecology'),
  ('cardiologia',         'Cardiología',         'Cardiology'),
  ('dermatologia',        'Dermatología',        'Dermatology'),
  ('neurologia',          'Neurología',          'Neurology'),
  ('psiquiatria',         'Psiquiatría',         'Psychiatry'),
  ('oftalmologia',        'Oftalmología',        'Ophthalmology'),
  ('traumatologia',       'Traumatología',       'Orthopedics')
ON CONFLICT (code) DO NOTHING;
