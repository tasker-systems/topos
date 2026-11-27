-- The lending library's database, as a synthetic target for the structural extractor.
-- `topology.json` is this schema as `topos.extract.postgres` reads it once applied.

CREATE TYPE book_status AS ENUM ('available', 'on_loan', 'lost');

CREATE TABLE members (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    role text NOT NULL CHECK (role IN ('member', 'librarian'))
);

CREATE TABLE books (
    id uuid PRIMARY KEY,
    title text NOT NULL,
    status book_status NOT NULL DEFAULT 'available'
);

CREATE TABLE loans (
    book_id uuid PRIMARY KEY REFERENCES books (id),
    borrower uuid NOT NULL REFERENCES members (id),
    lent_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit_log (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    book_id uuid NOT NULL,
    note text NOT NULL
);

CREATE FUNCTION is_librarian(actor uuid) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT 1 FROM members WHERE id = actor AND role = 'librarian')
$$;

CREATE FUNCTION can_return(actor uuid, book uuid) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT 1 FROM loans WHERE book_id = book AND borrower = actor)
        OR is_librarian(actor)
$$;

CREATE FUNCTION lend(actor uuid, book uuid, borrower uuid) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT is_librarian(actor) THEN
        RAISE EXCEPTION 'only a librarian lends';
    END IF;
    INSERT INTO loans (book_id, borrower) VALUES (book, borrower);
    UPDATE books SET status = 'on_loan' WHERE id = book AND status = 'available';
END
$$;

CREATE FUNCTION return_book(actor uuid, book uuid) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT can_return(actor, book) THEN
        RAISE EXCEPTION 'not allowed';
    END IF;
    DELETE FROM loans WHERE book_id = book;
    UPDATE books SET status = 'available' WHERE id = book;
END
$$;

CREATE FUNCTION mark_lost(actor uuid, book uuid) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT can_return(actor, book) THEN
        RAISE EXCEPTION 'not allowed';
    END IF;
    UPDATE books SET status = 'lost' WHERE id = book;
END
$$;

CREATE FUNCTION touch_book() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO audit_log (book_id, note) VALUES (NEW.id, 'status ' || NEW.status);
    RETURN NEW;
END
$$;

CREATE TRIGGER books_audit AFTER UPDATE OF status ON books
FOR EACH ROW EXECUTE FUNCTION touch_book();

CREATE VIEW books_on_loan AS
    SELECT b.id, b.title, l.borrower FROM books b JOIN loans l ON l.book_id = b.id;

ALTER TABLE loans ENABLE ROW LEVEL SECURITY;
CREATE POLICY loans_visible ON loans FOR SELECT USING (is_librarian(current_setting('app.actor')::uuid)
    OR borrower = current_setting('app.actor')::uuid);
