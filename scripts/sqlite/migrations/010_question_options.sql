-- Keep answer choices as first-class question content.  Legacy prompts may
-- continue to contain inline choices, but new structured writes use this table
-- so the stem, choices, reference answer, and explanation remain distinct.
CREATE TABLE IF NOT EXISTS question_options (
    question_id TEXT NOT NULL
        REFERENCES questions(question_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    option_key TEXT NOT NULL COLLATE NOCASE
        CHECK (trim(option_key) <> ''),
    option_text TEXT NOT NULL
        CHECK (trim(option_text) <> ''),
    position INTEGER NOT NULL
        CHECK (position >= 1),
    verification_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (verification_status IN ('unknown', 'candidate', 'verified', 'generated')),
    source_reference TEXT,
    metadata_json TEXT
        CHECK (metadata_json IS NULL OR json_valid(metadata_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (question_id, option_key),
    UNIQUE (question_id, position)
);

INSERT OR IGNORE INTO schema_migrations(version, name)
VALUES (10, 'structured question options');

INSERT INTO system_meta(key, value, updated_at)
VALUES ('schema_version', '10', CURRENT_TIMESTAMP)
ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;

INSERT INTO system_meta(key, value, updated_at)
VALUES ('question_option_policy', 'structured-options-with-legacy-inline-compatibility-v1', CURRENT_TIMESTAMP)
ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;

PRAGMA user_version = 10;
