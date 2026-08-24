"""Evaluating a watch job's CEL condition.

One CEL expression serves two purposes at once: it *extracts* the evidence a condition
is about (the matching events, the failing rows) and it *gates* whether the job
triggers. The gate is derived from the result's type rather than declared separately,
so the two readings can never disagree:

  * a boolean result is the gate itself;
  * any other result gates on "non-empty" (see `is_empty` for what counts as empty).

The expression is evaluated against three variables:

  * ``result`` — the check tool's response;
  * ``now``    — the current time in the job's timezone, so time-relative conditions
                 ("starts within the hour") are deterministic instead of judged by a
                 model guessing the date;
  * ``prev``   — the previous check result (null on the first run), which makes
                 change-detection gates (``result != prev``) possible without state
                 beyond what the job already keeps.

Two extension functions exist mainly so the SQL migration off the old JSONPath
conditions could be a string rewrite rather than a parser, but both remain available
to authors:

  * ``jsonpath(value, "$.a[*].b")`` — extract with a JSONPath, for path styles CEL
    cannot express (regex filters, recursive descent). Returns null on no match, the
    value on one match, a list on several — the old extractor's exact contract.
  * ``eq_ci(a, b)`` — the old rules' comparison: both sides as text, case-insensitive.

CEL rather than JSONPath filters or a JS sandbox on purpose: it is non-Turing-complete
(evaluation always terminates), has native timestamp/duration arithmetic, and an
expression can only touch the variables above — there is no I/O to reach. The remaining
risk is resource exhaustion, not escape, so expressions are capped in length and
evaluated under a timeout that fails closed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import celpy
from celpy import CELEvalError, CELParseError, celtypes
from jsonpath_ng.ext import parse as jsonpath_parse

logger = logging.getLogger(__name__)

#: Long expressions are how CEL's guaranteed termination stops being cheap termination:
#: cost grows with expression size, and nothing a watch condition expresses needs this
#: much room.
MAX_CEL_EXPR_LENGTH = 2000

#: Hard ceiling on one evaluation. CEL cannot loop forever, but a deeply nested
#: comprehension over a large payload can still be slow, and the scheduler tick must
#: not wait on it. Exceeding it is an error (fail closed), not a quiet "not met".
CEL_EVAL_TIMEOUT_SECONDS = 2.0

#: Shown wherever an expression is rejected, phrased for the person (or model) writing it.
CEL_SYNTAX_HINT = (
    "The condition is a CEL (Common Expression Language) expression over three "
    "variables: `result` (the tool response), `now` (the current time in the job's "
    "timezone), and `prev` (the previous check result, null on the first run). "
    "Return a boolean to gate directly, e.g. size(result.items) > 3 — or return the "
    "matching items themselves, e.g. "
    "result.events.filter(e, timestamp(e.start.dateTime) - now < duration('1h')), "
    "and the condition is met when the result is non-empty. What the expression "
    "returns is also what is recorded on the run and handed to the model or agent, "
    "so prefer returning the evidence over returning a bare boolean. Guard optional "
    "fields with has(), e.g. has(e.attendees) && e.attendees.exists(a, ...). "
    'For path styles CEL cannot express, jsonpath(result, "$.a[*].b") extracts with '
    "a JSONPath; eq_ci(a, b) compares as text, case-insensitively; "
    "strftime(t, '%Y-%m-%d') formats a timestamp (string(t) renders ISO 8601)."
)


class CelSyntaxError(ValueError):
    """The expression is not valid CEL (or exceeds the size cap)."""


class CelEvaluationError(RuntimeError):
    """The expression is valid but could not be evaluated against this payload.

    Typically a field the payload does not have or a type mismatch. Distinct from
    "condition not met": a condition that cannot see its subject must fail the run,
    not silently read as false.
    """


@dataclass
class CelEvaluation:
    """Outcome of one CEL evaluation: the value and the gate derived from it."""

    #: What the expression returned, converted to plain JSON-serialisable Python.
    value: Any
    #: The trigger decision derived from `value` (see module docstring).
    gate: bool
    #: True when the expression returned a boolean, so callers (and the preview) can
    #: say which gating rule applied.
    is_boolean: bool


def is_empty(value: Any) -> bool:
    """Whether a value counts as absent for the non-boolean gate.

    Mirrors what "the value is not null" has always meant to watch jobs: None, empty
    string, empty collection, zero and False were all treated as "nothing there".
    """
    if value is None:
        return True
    if isinstance(value, (str, list, dict, tuple, set)):
        return len(value) == 0
    return value in (0, False)


def to_python(value: Any) -> Any:
    """Convert a celpy result into plain JSON-serialisable Python.

    Needed because celtypes subclass Python builtins in ways json.dumps mishandles:
    BoolType subclasses int (so it would serialise as 1), and Timestamp/Duration are
    not serialisable at all.
    """
    if value is None or isinstance(value, celtypes.NullType):
        return None
    if isinstance(value, celtypes.BoolType):
        return bool(value)
    if isinstance(value, celtypes.MapType | dict):
        return {to_python(k): to_python(v) for k, v in value.items()}
    if isinstance(value, celtypes.ListType | list):
        return [to_python(v) for v in value]
    if isinstance(value, celtypes.StringType | str):
        return str(value)
    if isinstance(value, celtypes.DoubleType | float):
        return float(value)
    if isinstance(value, celtypes.IntType | celtypes.UintType | int):
        return int(value)
    if isinstance(value, celtypes.TimestampType | celtypes.DurationType):
        return str(value)
    if isinstance(value, celtypes.BytesType | bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _cel_jsonpath(value: Any, path: Any) -> Any:
    """The `jsonpath(value, path)` extension: extract with a JSONPath expression.

    Keeps the old extractor's contract — null for no match, the value for one match, a
    list for several — so a condition migrated from a stored JSONPath means exactly
    what it meant. A path that does not parse raises, which CEL surfaces as an
    evaluation error: the run fails rather than quietly reading as "not met".
    """
    try:
        expr = jsonpath_parse(str(path))
    except Exception as exc:  # noqa: BLE001 - the lexer raises a bare Exception subclass
        raise ValueError(f"jsonpath(): {path!r} is not a valid JSONPath: {exc}") from exc
    matches = expr.find(to_python(value))
    if not matches:
        return None
    if len(matches) == 1:
        return celpy.json_to_cel(matches[0].value)
    return celpy.json_to_cel([m.value for m in matches])


def _cel_strftime(value: Any, fmt: Any) -> celtypes.StringType:
    """The `strftime(t, fmt)` extension: format a timestamp for a tool argument.

    `string(now)` renders ISO 8601, but reporting tools commonly want a bare date
    ('%Y-%m-%d') or another fixed layout, and CEL has no formatting of its own.
    """
    if not isinstance(value, datetime):
        raise ValueError(f"strftime(): expected a timestamp, got {type(value).__name__}")
    return celtypes.StringType(value.strftime(str(fmt)))


def _cel_eq_ci(left: Any, right: Any) -> celtypes.BoolType:
    """The `eq_ci(a, b)` extension: the old rules' equality, kept bug-for-bug.

    Both sides as text, case-insensitive, with dicts and lists serialised with sorted
    keys. Conditions migrated from `expected_value` rows compare through this so their
    verdicts do not change out from under the jobs that rely on them.
    """

    def as_text(value: Any) -> str:
        plain = to_python(value)
        if plain is None:
            return ""
        if isinstance(plain, (dict, list)):
            return json.dumps(plain, sort_keys=True)
        return str(plain)

    return celtypes.BoolType(as_text(left).lower() == as_text(right).lower())


#: Extension functions available in every condition, declared once so compile-time
#: validation and evaluation cannot disagree about what exists.
_EXTENSION_ANNOTATIONS = {
    "jsonpath": celtypes.FunctionType,
    "eq_ci": celtypes.FunctionType,
    "strftime": celtypes.FunctionType,
}
_EXTENSION_FUNCTIONS = {
    "jsonpath": _cel_jsonpath,
    "eq_ci": _cel_eq_ci,
    "strftime": _cel_strftime,
}


def validate_cel_expression(expr: str | None) -> None:
    """Check that an expression compiles, without a payload to try it against.

    An expression that cannot compile is a job that can never fire, and that must
    surface when it is typed, not on the first poll.

    Raises:
        CelSyntaxError: the expression does not compile or exceeds the size cap.
    """
    if not expr:
        return
    if len(expr) > MAX_CEL_EXPR_LENGTH:
        raise CelSyntaxError(
            f"CEL expression is {len(expr)} characters; the maximum is {MAX_CEL_EXPR_LENGTH}."
        )
    try:
        celpy.Environment(annotations=_EXTENSION_ANNOTATIONS).compile(expr)
    except CELParseError as exc:
        raise CelSyntaxError(str(exc)) from exc


def _evaluate_sync(expr: str, result: Any, now: datetime, prev: Any) -> CelEvaluation:
    env = celpy.Environment(annotations=_EXTENSION_ANNOTATIONS)
    try:
        program = env.program(env.compile(expr), functions=_EXTENSION_FUNCTIONS)
    except CELParseError as exc:
        raise CelSyntaxError(str(exc)) from exc

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    activation = {
        # Round-trip through json so a payload carrying non-JSON scalars (datetimes a
        # client library deserialised, Decimals) cannot crash json_to_cel.
        "result": celpy.json_to_cel(json.loads(json.dumps(result, default=str))),
        "now": celtypes.TimestampType(now),
        "prev": celpy.json_to_cel(json.loads(json.dumps(prev, default=str))),
    }
    try:
        raw = program.evaluate(activation)
    except CELEvalError as exc:
        raise CelEvaluationError(str(exc)) from exc

    if isinstance(raw, celtypes.BoolType):
        return CelEvaluation(value=bool(raw), gate=bool(raw), is_boolean=True)
    value = to_python(raw)
    return CelEvaluation(value=value, gate=not is_empty(value), is_boolean=False)


def _evaluate_arg_exprs_sync(exprs: dict[str, str], now: datetime, prev: Any) -> dict[str, Any]:
    env = celpy.Environment(annotations=_EXTENSION_ANNOTATIONS)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    activation = {
        # Deliberately no `result`: the args are computed BEFORE the tool is called,
        # so an expression reaching for the response fails with a clear error instead
        # of a mystifying null.
        "now": celtypes.TimestampType(now),
        "prev": celpy.json_to_cel(json.loads(json.dumps(prev, default=str))),
    }

    resolved: dict[str, Any] = {}
    for key, expr in exprs.items():
        try:
            program = env.program(env.compile(expr), functions=_EXTENSION_FUNCTIONS)
        except CELParseError as exc:
            raise CelSyntaxError(f"argument {key!r}: {exc}") from exc
        try:
            raw = program.evaluate(activation)
        except CELEvalError as exc:
            raise CelEvaluationError(f"argument {key!r}: {exc}") from exc
        value = to_python(raw)
        # celpy quirk: an unbound variable becomes a lazy error VALUE, and string()
        # coerces it into its repr instead of raising — which would send that garbage
        # to an external tool as an argument. The repr carries an unmistakable marker.
        if "undeclared reference to" in json.dumps(value, default=str):
            raise CelEvaluationError(
                f"argument {key!r} references a variable that does not exist here — "
                "only `now` and `prev` are available (`result` is not: the arguments "
                "are computed before the tool is called)"
            )
        resolved[key] = value
    return resolved


async def evaluate_arg_exprs(
    exprs: dict[str, str],
    now: datetime,
    prev: Any = None,
) -> dict[str, Any]:
    """Evaluate dynamic tool arguments: per argument name, a CEL expression over
    `now` and `prev` whose value becomes that argument.

    Merged over the job's static check_args before the check tool is called — this is
    how a rolling time window ("the last 7 days") reaches a tool argument without a
    literal date being stored on the job:

        {"start_date": "strftime(now - duration('168h'), '%Y-%m-%d')"}

    Raises:
        CelSyntaxError: an expression does not compile.
        CelEvaluationError: an expression failed to evaluate, or the batch timed out.
    """
    for key, expr in exprs.items():
        if len(expr) > MAX_CEL_EXPR_LENGTH:
            raise CelSyntaxError(
                f"argument {key!r}: expression is {len(expr)} characters; "
                f"the maximum is {MAX_CEL_EXPR_LENGTH}."
            )
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_evaluate_arg_exprs_sync, exprs, now, prev),
            timeout=CEL_EVAL_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise CelEvaluationError(
            f"Evaluation exceeded {CEL_EVAL_TIMEOUT_SECONDS}s — simplify the expressions."
        ) from exc


async def evaluate_cel(
    expr: str,
    result: Any,
    now: datetime,
    prev: Any = None,
) -> CelEvaluation:
    """Evaluate a CEL condition against one payload.

    Runs in a worker thread under a timeout: celpy is a pure-Python interpreter, and
    however rare a slow expression is, the scheduler tick must not block on one.

    Raises:
        CelSyntaxError: the expression does not compile.
        CelEvaluationError: evaluation failed against this payload, or timed out.
    """
    if len(expr) > MAX_CEL_EXPR_LENGTH:
        raise CelSyntaxError(
            f"CEL expression is {len(expr)} characters; the maximum is {MAX_CEL_EXPR_LENGTH}."
        )
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_evaluate_sync, expr, result, now, prev),
            timeout=CEL_EVAL_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise CelEvaluationError(
            f"Evaluation exceeded {CEL_EVAL_TIMEOUT_SECONDS}s — simplify the expression "
            "or narrow what the check tool returns."
        ) from exc
