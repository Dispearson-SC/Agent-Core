CREATE TABLE IF NOT EXISTS sample_install_notes (
    note TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
