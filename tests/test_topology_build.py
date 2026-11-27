"""Parts of the topology builder that need no database."""

from __future__ import annotations

from topos.extract.topology import _check_values, _function_search_path, _trigger_shape


def test_check_constraints_that_enumerate_values_are_state_variables() -> None:
    any_form = "CHECK ((role = ANY (ARRAY['member'::text, 'librarian'::text])))"
    assert _check_values(any_form) == ("role", ["member", "librarian"])
    assert _check_values("CHECK ((tier IN ('free', 'paid')))") == ("tier", ["free", "paid"])


def test_other_check_constraints_are_not() -> None:
    assert _check_values("CHECK ((balance > 0))") is None
    assert _check_values("CHECK (is_short(body))") is None
    assert _check_values("CHECK (((a = 'x'::text) OR (b IS NULL)))") is None


def test_trigger_type_bits() -> None:
    assert _trigger_shape(1 | 2 | 4) == ("before", ["insert"], "row")
    assert _trigger_shape(16) == ("after", ["update"], "statement")
    assert _trigger_shape(1 | 64 | 8) == ("instead_of", ["delete"], "row")


def test_a_function_search_path_overrides_the_default() -> None:
    assert _function_search_path(['search_path=app, "$user"'], ["public"]) == ["app"]
    assert _function_search_path(["work_mem=1MB"], ["public"]) == ["public"]
