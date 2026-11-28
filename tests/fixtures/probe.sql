-- A small synthetic system for the probe's tests. Users belong to groups with a role; notes belong
-- to groups; a share grants a user flags on a note or a group through a polymorphic reference.

CREATE TYPE member_role AS ENUM ('lead', 'member', 'viewer');

CREATE TABLE users (id uuid PRIMARY KEY, name text NOT NULL);
CREATE TABLE groups (id uuid PRIMARY KEY, name text NOT NULL);
CREATE TABLE memberships (
    group_id uuid NOT NULL REFERENCES groups (id),
    user_id uuid NOT NULL REFERENCES users (id),
    role member_role NOT NULL,
    PRIMARY KEY (group_id, user_id)
);
CREATE TABLE notes (id uuid PRIMARY KEY, group_id uuid NOT NULL REFERENCES groups (id));
CREATE TABLE shares (
    subject_table text NOT NULL CHECK (subject_table IN ('notes', 'groups')),
    subject_id uuid NOT NULL,
    user_id uuid NOT NULL REFERENCES users (id),
    can_read boolean NOT NULL,
    can_edit boolean NOT NULL,
    CHECK (can_edit <= can_read)
);
CREATE TABLE audit (n integer);

-- A member or lead of a note's group may edit it.
CREATE FUNCTION can_edit_note(p_user uuid, p_note uuid) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM notes n JOIN memberships m ON m.group_id = n.group_id
        WHERE n.id = p_note AND m.user_id = p_user AND m.role IN ('lead', 'member')
    )
$$;

-- An explicit share, read through a polymorphic reference.
CREATE FUNCTION user_shared(p_user uuid, p_action text, p_subject_table text, p_subject_id uuid)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM shares s
        WHERE s.subject_table = p_subject_table AND s.subject_id = p_subject_id
          AND s.user_id = p_user
          AND CASE p_action WHEN 'read' THEN s.can_read WHEN 'edit' THEN s.can_edit ELSE false END
    )
$$;

CREATE FUNCTION holds_role(p_user uuid, p_role member_role) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT 1 FROM memberships m WHERE m.user_id = p_user AND m.role = p_role)
$$;

-- Raises for any existing user.
CREATE FUNCTION strict_check(p_user uuid) RETURNS boolean LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM users u WHERE u.id = p_user) THEN
        RAISE EXCEPTION 'strict check refused';
    END IF;
    RETURN false;
END
$$;

-- NULL for any existing user.
CREATE FUNCTION unknown_for(p_user uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT CASE WHEN EXISTS (SELECT 1 FROM users u WHERE u.id = p_user) THEN NULL ELSE false END
$$;

-- Writes as it answers.
CREATE FUNCTION noted(p_user uuid) RETURNS boolean LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO audit VALUES (1);
    RETURN EXISTS (SELECT 1 FROM users u WHERE u.id = p_user);
END
$$;

-- Excluded: a jsonb argument no axis supplies.
CREATE FUNCTION payload_ok(p jsonb) RETURNS boolean LANGUAGE sql IMMUTABLE AS $$ SELECT p ? 'ok' $$;

-- Excluded: free text the schema yields no values for.
CREATE FUNCTION titled(p_user uuid, p_title text) RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT 1 FROM users u WHERE u.id = p_user AND length(p_title) > 0)
$$;

-- Excluded: no argument reaches the fixture.
CREATE FUNCTION is_level(p_role member_role) RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT p_role = 'lead'
$$;

-- Not a predicate: it does not return boolean.
CREATE FUNCTION note_count(p_user uuid) RETURNS bigint LANGUAGE sql STABLE AS $$
    SELECT count(*) FROM memberships m WHERE m.user_id = p_user
$$;
