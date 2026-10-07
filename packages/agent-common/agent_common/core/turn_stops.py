"""How the results that stop an agent's step begin — one definition for producers and readers.

The loop guard and the approval layer write these; the answer-after-refusal middleware and
the stopped-turn reply read them. A reader matching its own copy of the wording would stop
working, silently, the day a producer is reworded.
"""

#: A call loop detection blocked (``RepeatedToolCallMiddleware._build_error_message``).
BLOCKED_LEAD = "BLOCKED: '"

#: An identical retry of a call the user refused this turn, answered without asking again
#: (``conditional_hitl._REFUSED_AGAIN``).
REFUSED_AGAIN_LEAD = "NOT RUN: the user rejected this exact call"
