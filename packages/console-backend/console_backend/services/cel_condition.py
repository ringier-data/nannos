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

Three extension functions are registered. One is for authoring; two exist so the SQL
migration off the old JSONPath conditions could be a string rewrite rather than a
parser, and are deliberately not advertised in CEL_SYNTAX_HINT:

  * ``strftime(t, "%Y-%m-%d")`` — format a timestamp. Load-bearing, and advertised:
    dynamic check arguments are usually a formatted date, and CEL has no native
    formatting (``string(t)`` gives ISO 8601 and nothing narrower).
  * ``jsonpath(value, "$.a[*].b")`` — extract with a JSONPath. Returns null on no
    match, the value on one match, a list on several: the old extractor's exact
    contract, which is the point. Native CEL covers all of this except recursive
    descent over unknown depth (``$..n``) and regex filters, and a condition is
    written against one known tool response, so new work should use ``map``/``filter``.
  * ``eq_ci(a, b)`` — the old rules' comparison: both sides as text, case-insensitive.
    ``matches("(?i)^x$")`` is the standard spelling and what the hint teaches. (Note
    ``lowerAscii()`` is *not* available: it belongs to the CEL strings extension, which
    celpy does not register.) eq_ci needs no regex escaping, so it is still the safer
    choice for a value full of metacharacters — one reason it stays registered rather
    than being dropped.

Not advertising the latter two is about portability: they are the only part of a stored
condition that a conformant CEL engine would not understand, so every new expression
that avoids them is one an engine change would not have to re-migrate.

CEL rather than JSONPath filters or a JS sandbox on purpose: it is non-Turing-complete
(evaluation always terminates), has native timestamp/duration arithmetic, and an
expression can only touch the variables above — there is no I/O to reach.

The remaining risk is resource exhaustion, not escape, and it is not the timeout that
bounds it. The timeout cannot actually stop the work: `asyncio.wait_for` cancels the
coroutine that is waiting, while the interpreter keeps running in its worker thread to
completion — Python cannot kill a thread. So the limits that do the real work are these,
the first applied *during* evaluation and the rest before it begins:

  * evaluation is metered at MAX_CEL_STEPS AST-node visits, and stops itself at
    CEL_EVAL_DEADLINE_SECONDS — the only ceilings that can stop work already running.
    The input caps below bound the inputs, and cost is not a function of size alone: a
    short expression over a small payload can still be quadratic. Both exist because
    they answer different questions — how much work is too much, which must not vary
    with how busy the host is, and how long a worker thread may be held, which is
    exactly a wall-clock question. See MeteredEvaluator, and note it is celpy's
    interpreter that makes either possible — no CEL implementation available to Python
    offers a cost or gas limit of its own;
  * the expression is capped at MAX_CEL_EXPR_LENGTH before the parser ever sees it, so
    a megabyte of nested parens cannot exhaust memory during the parse;
  * the payload bound to `result`/`prev` is capped at MAX_CEL_PAYLOAD_BYTES, because
    cost is a function of the data as much as the expression — a comprehension over ten
    items is free, the same one over a million is not;
  * evaluation runs on a small dedicated executor, so however slow one expression is it
    cannot starve every other `to_thread` caller in the process, nor the scheduler tick;
  * only `result`, `now` and `prev` are ever bound — nothing else is reachable by name.

The timeout stays as a backstop that fails closed (an error, never a quiet "not met"),
and it does return the request promptly; it simply is not what makes this safe. It should
now be the ceiling that never fires: the in-interpreter deadline is set below it, so a
slow evaluation ends with an error naming what happened rather than a bare timeout.

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
from time import monotonic
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import celpy
import lark
from celpy import CELEvalError, CELParseError, celtypes
from celpy.evaluation import Activation, Evaluator
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

#: Ceiling on how many AST nodes one evaluation may visit. This is the only limit that
#: can stop work already under way — the timeout cannot, and the size caps above only
#: bound the *inputs*, which is not the same thing: an expression well under
#: MAX_CEL_EXPR_LENGTH over a payload well under MAX_CEL_PAYLOAD_BYTES can still cost
#: quadratically, e.g. `result.items.filter(i, result.items.exists(j, j == i))`.
#: Counted per node visit rather than per comprehension because celpy builds a macro's
#: nested evaluator once and calls it per item: counting macros would count the same
#: `filter` once whether it runs over ten items or a million, while counting node visits
#: prices the iteration itself. See MeteredEvaluator.
#:
#: Sized from measurement (2026-08-25): the realistic worst case is a filter over a large
#: tool response, which costs ~65 steps per item — 32.5k steps over 500 events, 130k over
#: 2000, about the most a response under MAX_CEL_PAYLOAD_BYTES can carry. 300k leaves that
#: headroom. Deliberately a count and not a duration: how long a step takes depends on
#: what else the host is doing (~2us idle, 7-10us under load), so a ceiling expressed in
#: seconds would reject different expressions on a busy machine than on an idle one. The
#: wall-clock bound is CEL_EVAL_DEADLINE_SECONDS, and the two are independent on purpose.
#: Re-tune from the warnings CEL_STEP_WARN_RATIO emits, not from this comment.
MAX_CEL_STEPS = 300_000

#: Wall-clock bound checked *inside* the interpreter, unlike CEL_EVAL_TIMEOUT_SECONDS
#: which only bounds the caller's wait. Set below that timeout so this fires first: the
#: thread then stops itself rather than running on unobserved after the request has already
#: failed, and the author gets an error naming what happened instead of a bare timeout.
#: Which of this and MAX_CEL_STEPS trips first is load-dependent, which is why both exist —
#: the step count is the load-independent statement of how much work is too much, and this
#: is the promise that the thread is released whatever the host is doing.
CEL_EVAL_DEADLINE_SECONDS = 1.5

#: Fraction of MAX_CEL_STEPS above which an evaluation is logged. Nothing is rejected
#: here — this is how the ceiling gets re-tuned from real conditions instead of guesses.
CEL_STEP_WARN_RATIO = 0.25

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
    "fields with has(), e.g. has(e.attendees) && e.attendees.exists(a, ...), and "
    "guard map keys with in, e.g. \'code\' in result && result[\'code\'] == 200 "
    "(the optional forms .? and [?] are not available). Guard division against a zero "
    "denominator, and divide doubles unless you mean integer division, e.g. "
    "result.total != 0 && double(result.hits) / double(result.total) > 0.5. Where a "
    "field may hold either a string or a list, check before using it, e.g. "
    "type(result.tags) == list && \'urgent\' in result.tags. "
    "Compare text case-insensitively with matches, e.g. "
    'result.status.matches("(?i)^failed$"). '
    "strftime(t, '%Y-%m-%d') formats a timestamp (string(t) renders ISO 8601). "
    "Evaluation is metered, so filter a large collection down before mapping over it "
    "rather than nesting a comprehension inside another comprehension."
)


class CelSyntaxError(ValueError):
    """The expression is not valid CEL (or exceeds the size cap)."""


class CelEvaluationError(RuntimeError):
    """The expression is valid but could not be evaluated against this payload.

    Typically a field the payload does not have or a type mismatch. Distinct from
    "condition not met": a condition that cannot see its subject must fail the run,
    not silently read as false.
    """


class CelDeadlineExceededError(CelEvaluationError):
    """Evaluation ran past CEL_EVAL_DEADLINE_SECONDS and stopped itself.

    Distinct from the `asyncio.wait_for` timeout around it, which only ends the wait:
    this one ends the work. Same reasoning as CelBudgetExceededError about not being a
    CELEvalError.
    """


class CelBudgetExceededError(CelEvaluationError):
    """The expression visited more AST nodes than MAX_CEL_STEPS allows.

    Deliberately *not* a CELEvalError subclass. celpy turns those into values rather
    than propagating them — macros catch CELEvalError per item and hand it back as the
    item's result, and `||`/`&&`/`?:` bottle them up to implement short-circuiting — so
    a budget error spelled that way would be swallowed and evaluation would continue,
    which is the one thing this must never do.
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


class _StepMeter:
    """One evaluation's step count, shared by every evaluator that evaluation creates.

    Shared rather than per-evaluator because macros evaluate their body in a *nested*
    evaluator: a counter that started at zero for each `filter` item would never reach
    the ceiling however many items there were, which is exactly the case the ceiling is
    for.
    """

    __slots__ = ("steps", "limit", "deadline")

    def __init__(self, limit: int, deadline: float) -> None:
        self.steps = 0
        self.limit = limit
        self.deadline = deadline


class MeteredEvaluator(Evaluator):
    """celpy's interpreter, counting node visits and stopping at MAX_CEL_STEPS.

    This is the only limit that acts on work in progress. It is worth the coupling to
    celpy's internals because the alternative is none: no CEL implementation available
    to Python exposes a cost or gas limit — not celpy, not the cel-cpp binding
    (`cel-expr/cel-python`, whose Options carries a single parser flag), not the Rust
    one — although cel-cpp's own runtime has `comprehension_max_iterations` one layer
    below its bindings.
    """

    def __init__(self, ast: lark.Tree, activation: Activation, meter: _StepMeter) -> None:
        super().__init__(ast, activation=activation)
        self.meter = meter

    def sub_evaluator(self, ast: lark.Tree) -> "MeteredEvaluator":
        """Extend the superclass to carry the meter into macro bodies.

        The superclass hardcodes `Evaluator(...)` here, so without this override the
        meter would be dropped at the entrance to every map/filter/all/exists — the
        only places where an expression's cost depends on the payload's size.
        """
        return type(self)(ast, activation=self.activation, meter=self.meter)

    def _visit_tree(self, tree: lark.Tree) -> Any:
        """Extend lark's dispatch — the one point every node visit passes through.

        Not `__default__`: lark reaches that only via `__getattr__`, for node types the
        visitor has no method for, and celpy defines a method for nearly every CEL
        production. Metering there would meter almost nothing.
        """
        meter = self.meter
        meter.steps += 1
        if meter.steps > meter.limit:
            raise CelBudgetExceededError(
                f"Evaluation visited more than {meter.limit} expression nodes. This is "
                "usually a comprehension inside a comprehension over a large response; "
                "filter the outer collection down first, or narrow what the check tool "
                "returns."
            )
        # Clock reads are the expensive part of metering — the step count alone is two
        # integer ops — so the deadline is checked every 1024 nodes. That granularity
        # costs at most ~10ms of overshoot even at the slow end of the per-step range.
        if not meter.steps & 1023 and monotonic() > meter.deadline:
            raise CelDeadlineExceededError(
                f"Evaluation ran longer than {CEL_EVAL_DEADLINE_SECONDS}s and was "
                "stopped. Simplify the expression, or narrow what the check tool returns."
            )
        return super()._visit_tree(tree)


class MeteredRunner(celpy.InterpretedRunner):
    """Runs a program with a fresh MeteredEvaluator, so each evaluation gets its budget.

    Note this deliberately extends the *interpreted* runner. celpy also ships a
    CompiledRunner that transpiles to Python and exec()s it; it never touches the
    visitor, so switching to it for speed would silently remove this ceiling.
    """

    def evaluate(self, context: Any) -> celtypes.Value:
        meter = _StepMeter(MAX_CEL_STEPS, deadline=monotonic() + CEL_EVAL_DEADLINE_SECONDS)
        evaluator = MeteredEvaluator(
            ast=self.ast, activation=self.new_activation(), meter=meter
        )
        try:
            return evaluator.evaluate(context)
        finally:
            self.last_steps = meter.steps
            if meter.steps > MAX_CEL_STEPS * CEL_STEP_WARN_RATIO:
                logger.warning(
                    "CEL evaluation used %d of %d permitted steps", meter.steps, MAX_CEL_STEPS
                )
            else:
                logger.debug("CEL evaluation used %d steps", meter.steps)


#: One Environment, and one parse per distinct expression. celpy re-runs its Lark parse
#: on every compile, and for the small expressions a condition actually is, that parse is
#: the dominant cost — paid once per poll per job, once per dynamic argument per run, and
#: once per debounced /validate-condition keystroke. Expressions are capped at
#: MAX_CEL_EXPR_LENGTH and the cache is bounded, so it cannot grow without limit. The AST
#: is cached rather than the Program because a Program is bound to a function set and is
#: cheap to build from a parsed AST.
_ENV = celpy.Environment(
    annotations=_EXTENSION_ANNOTATIONS, runner_class=MeteredRunner
)
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
        except (CelBudgetExceededError, CelDeadlineExceededError) as exc:
            raise type(exc)(f"argument {key!r}: {exc}") from exc
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
