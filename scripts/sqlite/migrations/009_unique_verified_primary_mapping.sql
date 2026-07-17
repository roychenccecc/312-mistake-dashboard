-- A question may have multiple verified secondary/entity links, but the
-- dashboard denominator requires exactly one verified primary knowledge point.
CREATE UNIQUE INDEX IF NOT EXISTS one_verified_primary_mastery_unit_per_question
    ON question_mastery_unit_links(question_id)
    WHERE semantic_verification_status = 'verified'
      AND tested_dimension = 'primary';

INSERT OR IGNORE INTO schema_migrations(version, name)
VALUES (9, 'unique verified primary mastery mapping');

INSERT INTO system_meta(key, value, updated_at)
VALUES ('schema_version', '9', CURRENT_TIMESTAMP)
ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;

INSERT INTO system_meta(key, value, updated_at)
VALUES ('primary_mastery_mapping_policy', 'one-verified-primary-per-question', CURRENT_TIMESTAMP)
ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;

PRAGMA user_version = 9;
