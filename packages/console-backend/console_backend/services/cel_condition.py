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
expression can only touch the variables above — there is no I/O to reach.

The remaining risk is resource exhaustion, not escape, and it is bounded by *upfront
constraints* rather than by the timeout. This matters because the timeout cannot
actually stop the work: `asyncio.wait_for` cancels the coroutine that is waiting, while
the interpreter keeps running in its worker thread to completion — Python cannot kill a
thread. So the limits that do the real work are applied before evaluation begins:

  * the expression is capped at MAX_CEL_EXPR_LENGTH before the parser ever sees it, so
    a megabyte of nested parens cannot exhaust memory during the parse;
  * the payload bound to `result`/`prev` is capped at MAX_CEL_PAYLOAD_BYTES, because
    cost is a function of the data as much as the expression — a comprehension over ten
    items is free, the same one over a million is not;
  * evaluation runs on a small dedicated executor, so however slow one expression is it
    cannot starve every other `to_thread` caller in the process, nor the scheduler tick;
  * only `result`, `now` and `prev` are ever bound — nothing else is reachable by name.

The timeout stays as a backstop that fails closed (an error, never a quiet "not met"),
and it does return the request promptly; it simply is not what makes this safe.

celpy (pure Python) rather than the Rust-backed `common-expression-language`, measured
2026-08-24 on the payload shape this actually sees — a list of events under a filter
comprehension. The Rust binding is ~3x faster on a small payload (2.9ms vs 9.2ms at 50
items, precompiled with a reused Context) and then inverts sharply: 206ms vs celpy's
~65ms at 500 items, and 3.7s vs 257ms at 2000. Its documented "no GIL-blocking, safe
concurrent evaluation" did not materialise either — four threads over the same program
measured a 1.00x speedup, so there is no multi-core win to trade for the regression. It
is also ~80% CEL-conformant by its own README, and migration 083 rewrote live conditions
onto jsonpath()/eq_ci() promising no verdict would change, which a different engine
reopens. Revisit if payloads get small and conformance gets complete; the numbers, not
the architecture, are the reason.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
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

#: Ceiling on how long a caller waits for one evaluation. Exceeding it is an error
#: (fail closed), not a quiet "not met". Note this bounds the *wait*, not the work: the
#: worker thread runs to completion regardless, which is why the limits below exist.
CEL_EVAL_TIMEOUT_SECONDS = 2.0

#: Ceiling on the JSON size of the data bound to `result` and `prev`, each. Evaluation
#: cost is a function of the payload as much as of the expression — `result.items.map(...)`
#: is trivial over ten items and ruinous over a million — and a check tool's response is
#: not something the person writing the condition controls. Over this, the evaluation
#: fails closed rather than being attempted; 1 MiB is far above any real tool response
#: (the invoke endpoint already truncates its own preview well below it).
MAX_CEL_PAYLOAD_BYTES = 1_048_576

#: Evaluation runs here rather than on asyncio's default thread pool. Since a slow
#: expression cannot be interrupted, the only protection is to bound how much of the
#: process it can occupy: a handful of threads that are all its own, so a burst of
#: pathological /validate-condition requests degrades condition previews and nothing
#: else — not the scheduler tick, not any other to_thread caller.
_CEL_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cel")

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


#: One Environment, and one parse per distinct expression. celpy re-runs its Lark parse
#: on every compile, and for the small expressions a condition actually is, that parse is
#: the dominant cost — paid once per poll per job, once per dynamic argument per run, and
#: once per debounced /validate-condition keystroke. Expressions are capped at
#: MAX_CEL_EXPR_LENGTH and the cache is bounded, so it cannot grow without limit. The AST
#: is cached rather than the Program because a Program is bound to a function set and is
#: cheap to build from a parsed AST.
_ENV = celpy.Environment(annotations=_EXTENSION_ANNOTATIONS)
#: celpy's parser is shared mutable state and these compiles happen in `to_thread`
#: workers, so serialise them — a parse is short, and the cache means it is rare.
_COMPILE_LOCK = threading.Lock()


@lru_cache(maxsize=512)
def _compile_cached(expr: str) -> Any:
    """Parse `expr`, reusing the result across evaluations. Raises CELParseError."""
    with _COMPILE_LOCK:
        return _ENV.compile(expr)


def _bind_payload(value: Any, name: str) -> Any:
    """JSON-round-trip a payload for CEL, refusing one too large to evaluate over.

    The round-trip is so a payload carrying non-JSON scalars (datetimes a client library
    deserialised, Decimals) cannot crash json_to_cel. The size ceiling is here because
    this is the one place a payload enters the evaluator, and the serialised form is what
    there is to measure.
    """
    encoded = json.dumps(value, default=str)
    if len(encoded) > MAX_CEL_PAYLOAD_BYTES:
        raise CelEvaluationError(
            f"`{name}` is {len(encoded)} bytes; the maximum an expression can be "
            f"evaluated over is {MAX_CEL_PAYLOAD_BYTES}. Narrow what the check tool "
            "returns."
        )
    return celpy.json_to_cel(json.loads(encoded))


def _check_length(expr: str, what: str = "CEL expression") -> None:
    """One spelling of the size cap, so the three entry points cannot drift apart."""
    if len(expr) > MAX_CEL_EXPR_LENGTH:
        raise CelSyntaxError(
            f"{what} is {len(expr)} characters; the maximum is {MAX_CEL_EXPR_LENGTH}."
        )


def validate_cel_expression(expr: str | None) -> None:
    """Check that an expression compiles, without a payload to try it against.

    An expression that cannot compile is a job that can never fire, and that must
    surface when it is typed, not on the first poll.

    Raises:
        CelSyntaxError: the expression does not compile or exceeds the size cap.
    """
    if not expr:
        return
    _check_length(expr)
    try:
        _compile_cached(expr)
    except CELParseError as exc:
        raise CelSyntaxError(str(exc)) from exc


def _evaluate_sync(expr: str, result: Any, now: datetime, prev: Any) -> CelEvaluation:
    try:
        program = _ENV.program(_compile_cached(expr), functions=_EXTENSION_FUNCTIONS)
    except CELParseError as exc:
        raise CelSyntaxError(str(exc)) from exc

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    # Only these three names are ever bound; nothing else is reachable from an
    # expression.
    activation = {
        "result": _bind_payload(result, "result"),
        "now": celtypes.TimestampType(now),
        "prev": _bind_payload(prev, "prev"),
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
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    activation = {
        # Deliberately no `result`: the args are computed BEFORE the tool is called,
        # so an expression reaching for the response fails with a clear error instead
        # of a mystifying null.
        "now": celtypes.TimestampType(now),
        "prev": _bind_payload(prev, "prev"),
    }

    resolved: dict[str, Any] = {}
    for key, expr in exprs.items():
        try:
            program = _ENV.program(_compile_cached(expr), functions=_EXTENSION_FUNCTIONS)
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
        _check_length(expr, f"argument {key!r}: expression")
    try:
        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                _CEL_EXECUTOR, _evaluate_arg_exprs_sync, exprs, now, prev
            ),
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
    _check_length(expr)
    try:
        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                _CEL_EXECUTOR, _evaluate_sync, expr, result, now, prev
            ),
            timeout=CEL_EVAL_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise CelEvaluationError(
            f"Evaluation exceeded {CEL_EVAL_TIMEOUT_SECONDS}s — simplify the expression "
            "or narrow what the check tool returns."
        ) from exc
