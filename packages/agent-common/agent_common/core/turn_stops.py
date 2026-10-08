"""How the results that stop an agent's step begin — one definition for producers and readers.

The loop guard and the approval layer write these; the stopped-turn reply reads them. A reader matching its own copy of the wording would stop
working, silently, the day a producer is reworded.
"""

#: A call loop detection blocked (``RepeatedToolCallMiddleware._build_error_message``).
BLOCKED_LEAD = "BLOCKED: '"

#: An identical retry of a call the user refused this turn, answered without asking again
#: (``conditional_hitl._REFUSED_AGAIN``).
REFUSED_AGAIN_LEAD = "NOT RUN: the user rejected this exact call"

#: A call whose in-task authorization the user declined
#: (``AuthErrorDetectionMiddleware._refusal_message``).
DECLINED_AUTH_LEAD = "The user DECLINED the authorization required by"

#: A call that never ran because loop detection force-stopped the run over another call
#: (``loop_detection_middleware._STOPPED_BEFORE_EXECUTION``).
STOPPED_SIBLING_LEAD = "BLOCKED: the run was stopped"
