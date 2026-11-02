-- Existing personal and administrator flows and sessions retain NULL hosts.
ALTER TABLE admin_sessions.flows ADD COLUMN host text CHECK (host IS NULL);
ALTER TABLE admin_sessions.sessions ADD COLUMN host text CHECK (host IS NULL);
ALTER TABLE gateway_sessions.flows ADD COLUMN host text
    CHECK (host IS NULL OR octet_length(host) BETWEEN 1 AND 253);
ALTER TABLE gateway_sessions.sessions ADD COLUMN host text
    CHECK (host IS NULL OR octet_length(host) BETWEEN 1 AND 253);
