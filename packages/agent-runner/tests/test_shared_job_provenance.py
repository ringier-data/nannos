"""The provenance footer a delivered run of a SHARED scheduled job carries (ADR-0010).

The scheduler puts the line in the dispatch metadata; the footer is appended here,
where every run's output is composed, so one seam covers every delivery channel instead
of the same paragraph living in three clients.
"""

from agent.core import _with_provenance


class TestWithProvenance:
    def test_appends_the_line_to_a_result(self):
        assert _with_provenance("Sales were up 4%.", "(You receive this because Bo shared it.)") == (
            "Sales were up 4%.\n\n(You receive this because Bo shared it.)"
        )

    def test_an_unshared_run_is_untouched(self):
        # The scheduler sends None for a job nobody else runs, which is most of them.
        assert _with_provenance("Sales were up 4%.", None) == "Sales were up 4%."

    def test_a_run_with_no_output_has_nothing_to_explain(self):
        assert _with_provenance(None, "(shared)") is None
        assert _with_provenance("", "(shared)") == ""

    def test_a_non_string_or_blank_provenance_is_ignored(self):
        # Metadata arrives from the wire: a Struct can hand us anything.
        assert _with_provenance("result", 42) == "result"
        assert _with_provenance("result", "   ") == "result"
