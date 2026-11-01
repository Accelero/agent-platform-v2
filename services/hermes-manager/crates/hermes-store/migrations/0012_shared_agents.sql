-- Shared assignments retain the existing instance, material and lifecycle model.
ALTER TABLE manager.assignments
    ALTER COLUMN identity_id DROP NOT NULL,
    ADD COLUMN kind text NOT NULL DEFAULT 'personal' CHECK (kind IN ('personal', 'shared')),
    ADD COLUMN name text UNIQUE,
    ADD COLUMN display_name text,
    ADD COLUMN grants_version bigint NOT NULL DEFAULT 1 CHECK (grants_version >= 1),
    ADD CONSTRAINT assignment_binding CHECK (
        (kind = 'personal' AND identity_id IS NOT NULL AND name IS NULL AND display_name IS NULL)
        OR (kind = 'shared' AND identity_id IS NULL AND name IS NOT NULL AND display_name IS NOT NULL)),
    ADD CONSTRAINT shared_assignment_name CHECK (name ~ '^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$'),
    ADD CONSTRAINT shared_assignment_display_name CHECK (length(display_name) BETWEEN 1 AND 128);

-- Workload version and audience version advance independently. An audience-only
-- write cannot change any other assignment field, including its timestamp.
CREATE OR REPLACE FUNCTION manager.guard_assignment_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.identity_id IS DISTINCT FROM OLD.identity_id
        OR NEW.name IS DISTINCT FROM OLD.name OR NEW.display_name IS DISTINCT FROM OLD.display_name
    THEN
        RAISE EXCEPTION 'manager: assignment binding is immutable';
    END IF;
    IF NOT (
        (NEW.version = OLD.version + 1 AND NEW.grants_version = OLD.grants_version)
        OR (NEW.version = OLD.version AND NEW.grants_version = OLD.grants_version + 1
            AND (to_jsonb(NEW) - 'grants_version') = (to_jsonb(OLD) - 'grants_version'))
    ) THEN
        RAISE EXCEPTION 'manager: assignment and audience versions advance independently';
    END IF;
    RETURN NEW;
END $$;

CREATE FUNCTION manager.guard_assignment_kind() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.kind IS DISTINCT FROM OLD.kind THEN
        RAISE EXCEPTION 'manager: assignment kind is immutable';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER assignment_kind_immutable BEFORE UPDATE ON manager.assignments
    FOR EACH ROW EXECUTE FUNCTION manager.guard_assignment_kind();

CREATE TABLE manager.endpoints (
    assignment_id uuid PRIMARY KEY REFERENCES manager.assignments (id),
    label text NOT NULL UNIQUE CHECK (label ~ '^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$' AND strpos(label, '--') = 0),
    enabled boolean NOT NULL,
    version bigint NOT NULL CHECK (version >= 1)
);

CREATE TABLE manager.access_groups (
    id uuid PRIMARY KEY,
    name text NOT NULL UNIQUE CHECK (name ~ '^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$'),
    display_name text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 128),
    version bigint NOT NULL CHECK (version >= 1)
);

CREATE TABLE manager.access_group_members (
    group_id uuid NOT NULL REFERENCES manager.access_groups (id) ON DELETE CASCADE,
    identity_id uuid NOT NULL REFERENCES manager.identities (id),
    PRIMARY KEY (group_id, identity_id)
);
CREATE INDEX access_group_members_by_identity ON manager.access_group_members (identity_id, group_id);

CREATE TABLE manager.agent_grants (
    assignment_id uuid NOT NULL REFERENCES manager.assignments (id),
    identity_id uuid REFERENCES manager.identities (id),
    group_id uuid REFERENCES manager.access_groups (id),
    CHECK ((identity_id IS NULL) <> (group_id IS NULL)),
    UNIQUE (assignment_id, identity_id),
    UNIQUE (assignment_id, group_id)
);
CREATE INDEX agent_grants_by_group ON manager.agent_grants (group_id) WHERE group_id IS NOT NULL;

CREATE FUNCTION manager.guard_shared_assignment_reference() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM manager.assignments WHERE id = NEW.assignment_id AND kind = 'shared') THEN
        RAISE EXCEPTION 'manager: endpoints and agent grants require a shared assignment';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER endpoint_shared_assignment BEFORE INSERT OR UPDATE ON manager.endpoints
    FOR EACH ROW EXECUTE FUNCTION manager.guard_shared_assignment_reference();
CREATE TRIGGER grant_shared_assignment BEFORE INSERT OR UPDATE ON manager.agent_grants
    FOR EACH ROW EXECUTE FUNCTION manager.guard_shared_assignment_reference();

CREATE FUNCTION manager.guard_endpoint_binding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'manager: endpoint rows cannot be deleted';
    END IF;
    IF NEW.assignment_id IS DISTINCT FROM OLD.assignment_id THEN
        RAISE EXCEPTION 'manager: endpoint assignment is immutable';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER endpoint_binding_immutable BEFORE UPDATE OR DELETE ON manager.endpoints
    FOR EACH ROW EXECUTE FUNCTION manager.guard_endpoint_binding();
