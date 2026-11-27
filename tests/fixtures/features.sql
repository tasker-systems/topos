-- A synthetic schema that exercises each construct the structural extractor reads.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA app;

CREATE FUNCTION app.whoami() RETURNS text
LANGUAGE sql STABLE AS $$ SELECT current_user::text $$;

CREATE FUNCTION app.is_short(body text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$ SELECT length(body) < 280 $$;

CREATE DOMAIN app.positive AS integer CHECK (VALUE > 0);
CREATE TYPE app.status AS ENUM ('open', 'closed');

CREATE TABLE app.accounts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL CHECK (kind = ANY (ARRAY['person', 'robot'])),
    tier text CHECK (tier IN ('free', 'paid')),
    balance app.positive NOT NULL,
    status app.status NOT NULL DEFAULT 'open',
    created_by text DEFAULT app.whoami()
);

CREATE TABLE app.notes (
    id bigint PRIMARY KEY,
    owner uuid NOT NULL,
    reviewer uuid,
    body text CHECK (app.is_short(body)),
    FOREIGN KEY (owner) REFERENCES app.accounts (id),
    FOREIGN KEY (reviewer) REFERENCES app.accounts (id)
);

CREATE TABLE app.events (id bigint, at date, account uuid REFERENCES app.accounts (id))
    PARTITION BY RANGE (at);
CREATE TABLE app.events_2026 PARTITION OF app.events
    FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');

CREATE FUNCTION app.describe(who uuid) RETURNS text
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN (SELECT kind FROM app.accounts WHERE id = who);
END
$$;

CREATE FUNCTION app.describe(who uuid, verbose boolean) RETURNS text
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN (SELECT kind || tier FROM app.accounts WHERE id = who);
END
$$;

CREATE FUNCTION app.summary(who uuid) RETURNS text
LANGUAGE plpgsql STABLE SECURITY DEFINER AS $$
DECLARE
    label text;
BEGIN
    label := app.describe(who);
    RETURN label || encode(gen_random_bytes(4), 'hex');
END
$$;

CREATE FUNCTION app.recent(who uuid) RETURNS bigint
LANGUAGE plpgsql STABLE AS $$
DECLARE
    n bigint;
BEGIN
    WITH mine AS (SELECT * FROM app.events WHERE account = who)
    SELECT count(*) INTO n FROM mine;
    RETURN n;
END
$$;

CREATE FUNCTION app.scratch() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    CREATE TEMP TABLE scratch_rows (id bigint);
    INSERT INTO scratch_rows SELECT id FROM app.notes;
    DELETE FROM app.notes WHERE id IN (SELECT id FROM scratch_rows);
END
$$;

CREATE FUNCTION app.purge(tbl text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    EXECUTE format('DELETE FROM %I', tbl);
END
$$;

CREATE FUNCTION app.broken() RETURNS bigint
LANGUAGE plpgsql AS $$
BEGIN
    RETURN (SELECT count(*) FROM app.missing_table);
END
$$;

CREATE FUNCTION public.uses_path() RETURNS bigint
LANGUAGE plpgsql STABLE SET search_path = app AS $$
BEGIN
    RETURN (SELECT count(*) FROM accounts);
END
$$;

CREATE FUNCTION app.count_accounts() RETURNS bigint
LANGUAGE sql STABLE
BEGIN ATOMIC
    SELECT count(*) FROM app.accounts;
END;

CREATE FUNCTION app.upsert(who uuid) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    MERGE INTO app.accounts a USING (SELECT who AS id) s ON a.id = s.id
    WHEN MATCHED THEN UPDATE SET status = 'closed';
END
$$;

CREATE PROCEDURE app.settle()
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE app.accounts SET balance = balance;
    TRUNCATE app.events;
END
$$;

CREATE FUNCTION app.stamp() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO app.events (id, at, account) VALUES (NEW.id, current_date, NEW.owner);
    RETURN NEW;
END
$$;

CREATE TRIGGER notes_stamp BEFORE INSERT OR DELETE ON app.notes
FOR EACH STATEMENT EXECUTE FUNCTION app.stamp();

CREATE VIEW app.mine AS SELECT * FROM app.accounts WHERE created_by = app.whoami();
CREATE MATERIALIZED VIEW app.totals AS SELECT kind, count(*) AS n FROM app.accounts GROUP BY kind;

ALTER TABLE app.notes ENABLE ROW LEVEL SECURITY;
CREATE POLICY notes_owner ON app.notes FOR UPDATE
    USING (owner IN (SELECT id FROM app.accounts WHERE created_by = app.whoami()));
