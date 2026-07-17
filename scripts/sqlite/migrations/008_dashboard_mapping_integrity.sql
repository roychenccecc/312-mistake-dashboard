CREATE TABLE IF NOT EXISTS exam_frequency_audits (
    mastery_unit_id TEXT PRIMARY KEY
        REFERENCES chapter_mastery_units(mastery_unit_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    status TEXT NOT NULL
        CHECK (status IN ('partial', 'complete', 'blocked')),
    year_start INTEGER
        CHECK (year_start IS NULL OR year_start BETWEEN 2007 AND 2026),
    year_end INTEGER
        CHECK (year_end IS NULL OR year_end BETWEEN 2007 AND 2026),
    audit_note TEXT,
    blocker_reason TEXT,
    evidence_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(evidence_json)),
    audited_date TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((year_start IS NULL) = (year_end IS NULL)),
    CHECK (year_start IS NULL OR year_start <= year_end),
    CHECK (status <> 'partial' OR year_start IS NOT NULL),
    CHECK (
        status <> 'complete'
        OR (
            year_start IS NOT NULL AND year_start = 2007
            AND year_end IS NOT NULL AND year_end = 2026
        )
    ),
    CHECK (status <> 'blocked' OR COALESCE(blocker_reason, '') <> '')
);

CREATE INDEX IF NOT EXISTS exam_frequency_audits_by_status
    ON exam_frequency_audits(status, updated_at);

-- attempts.mastery_unit_id predates chapter_mastery_units, so it cannot gain a
-- normal foreign key without rebuilding a populated table. These triggers give
-- the nullable column the same referential guard while migration/backfill work
-- audits and clears legacy orphan values.
CREATE TRIGGER IF NOT EXISTS attempt_mastery_unit_exists_insert
BEFORE INSERT ON attempts
WHEN NEW.mastery_unit_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM chapter_mastery_units u
     WHERE u.mastery_unit_id = NEW.mastery_unit_id
 )
BEGIN
    SELECT RAISE(ABORT, 'attempt mastery_unit_id must reference chapter_mastery_units');
END;

CREATE TRIGGER IF NOT EXISTS attempt_mastery_unit_exists_update
BEFORE UPDATE OF mastery_unit_id ON attempts
WHEN NEW.mastery_unit_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM chapter_mastery_units u
     WHERE u.mastery_unit_id = NEW.mastery_unit_id
 )
BEGIN
    SELECT RAISE(ABORT, 'attempt mastery_unit_id must reference chapter_mastery_units');
END;

CREATE TRIGGER IF NOT EXISTS referenced_mastery_unit_cannot_be_deleted
BEFORE DELETE ON chapter_mastery_units
WHEN EXISTS (
    SELECT 1 FROM attempts a
    WHERE a.mastery_unit_id = OLD.mastery_unit_id
)
BEGIN
    SELECT RAISE(ABORT, 'mastery unit is referenced by attempts');
END;

CREATE TRIGGER IF NOT EXISTS referenced_mastery_unit_id_is_immutable
BEFORE UPDATE OF mastery_unit_id ON chapter_mastery_units
WHEN NEW.mastery_unit_id IS NOT OLD.mastery_unit_id
 AND EXISTS (
     SELECT 1 FROM attempts a
     WHERE a.mastery_unit_id = OLD.mastery_unit_id
 )
BEGIN
    SELECT RAISE(ABORT, 'mastery unit id is referenced by attempts');
END;

INSERT OR IGNORE INTO schema_migrations(version, name)
VALUES (8, 'dashboard mapping integrity and exam frequency audit state');

INSERT INTO system_meta(key, value, updated_at)
VALUES ('schema_version', '8', CURRENT_TIMESTAMP)
ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;

INSERT INTO system_meta(key, value, updated_at)
VALUES ('attempt_mastery_unit_policy', 'nullable-verified-unit-only', CURRENT_TIMESTAMP)
ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;

INSERT INTO system_meta(key, value, updated_at)
VALUES ('exam_frequency_audit_policy', '2007-2026-explicit-audit-v1', CURRENT_TIMESTAMP)
ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;

PRAGMA user_version = 8;
