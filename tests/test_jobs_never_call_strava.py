"""No queued job may reach the Strava client (issue #455).

``traxjourney_strava_rate_limit_usage`` is a scrape-time gauge read from the
limiter of the process serving ``/metrics``: the API. That is the whole count
only because the API is the only process that calls Strava (request handlers
and their FastAPI BackgroundTasks). A job moved onto an RQ worker would call
Strava through the worker's own limiter, invisible to the gauge, and the quota
alert would under-report without any error.

Static, so it needs no Redis and no imports. Every callable handed to the
enqueue family is traced to the module defining it, and every import in that
module, including ones inside functions, is followed. Fail-closed: an
enqueue-family call whose callable can't be resolved to a module fails the
test ("unsupported enqueue style"), rather than being skipped.

Styles resolved:
- ``src.jobs.queue.enqueue(queue_name, func, ...)`` (the app's wrapper):
  ``func`` is the second positional argument or ``func=``;
- rq's own ``Queue.enqueue(f, ...)``, ``enqueue_call(func, ...)``,
  ``prepare_data(func, ...)`` (first argument), ``enqueue_in(delta, func, ...)``
  and ``enqueue_at(when, f, ...)`` (second), or the keyword;
- the callable as a local function, an imported name (aliases included),
  ``module.func``, ``functools.partial(func, ...)``, or a ``"module.func"``
  string. ``enqueue_many`` is always unsupported: its jobs are built elsewhere.

``src/jobs/queue.py``'s wrapper forwards through rq as
``queue.enqueue(_run_with_level_refresh, func, ...)``: the rq job there is the
forwarder ``_run_with_level_refresh``, and the real jobs are resolved at the
wrapper's own call sites.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_STRAVA = "src.api.strava_client"
_APP_SOURCES = ("src", "api")
_WRAPPER_MODULE = "src.jobs.queue"
_FORWARDERS = {_WRAPPER_MODULE: {"_run_with_level_refresh"}}

# method name -> (positional index of the callable, keywords that may carry it)
_RQ_METHODS = {
    "enqueue": (0, ("f",)),
    "enqueue_call": (0, ("func",)),
    "prepare_data": (0, ("func",)),
    "enqueue_in": (1, ("func", "f")),
    "enqueue_at": (1, ("f", "func")),
    "enqueue_many": (None, ()),
}
_WRAPPER = (1, ("func",))


def _module_path(module: str, roots) -> Path | None:
    if not module:
        return None
    for root in roots:
        base = root.joinpath(*module.split("."))
        for candidate in (base.with_suffix(".py"), base / "__init__.py"):
            if candidate.is_file():
                return candidate
    return None


def _module_of(path: Path, root: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = rel.parts[:-1] if rel.name == "__init__" else rel.parts
    return ".".join(parts)


def _absolute(node: ast.ImportFrom, module: str, is_package: bool) -> str:
    if not node.level:
        return node.module or ""
    package = module if is_package else module.rpartition(".")[0]
    parts = package.split(".")
    if node.level > 1:
        parts = parts[: len(parts) - (node.level - 1)]
    return ".".join(parts + ([node.module] if node.module else []))


def _imports(module: str, roots) -> set[str]:
    """Modules under *roots* that *module* imports, anywhere in its body."""
    path = _module_path(module, roots)
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = _absolute(node, module, path.name == "__init__.py")
            found.add(base)
            found |= {f"{base}.{alias.name}" for alias in node.names}
    return {m for m in found if _module_path(m, roots)}


def _import_path(start: set[str], target: str, roots) -> list[str] | None:
    """An import chain from one of *start* to *target*, or None."""
    parent: dict[str, str] = {}
    seen: set[str] = set()
    todo = sorted(start)
    while todo:
        module = todo.pop()
        if module in seen:
            continue
        seen.add(module)
        if module == target:
            chain = [module]
            while chain[-1] in parent:
                chain.append(parent[chain[-1]])
            return chain[::-1]
        for imported in _imports(module, roots):
            if imported not in seen:
                parent.setdefault(imported, module)
                todo.append(imported)
    return None


class _File:
    """Name bindings of one source file, for resolving a callable."""

    def __init__(self, path: Path, module: str, roots):
        self.module = module
        self.roots = roots
        self.tree = ast.parse(path.read_text(encoding="utf-8"))
        self.local_defs = {
            n.name for n in ast.walk(self.tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.aliases: dict[str, str] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self.aliases[alias.asname] = alias.name
                    else:
                        head = alias.name.split(".")[0]
                        self.aliases[head] = head
            elif isinstance(node, ast.ImportFrom):
                base = _absolute(node, module, path.name == "__init__.py")
                for alias in node.names:
                    self.aliases[alias.asname or alias.name] = f"{base}.{alias.name}"

    def _is_module(self, dotted: str) -> bool:
        return _module_path(dotted, self.roots) is not None

    def dotted(self, expr) -> str | None:
        """``a.b.c`` with its head alias expanded, or None."""
        if isinstance(expr, ast.Name):
            return self.aliases.get(expr.id)
        if isinstance(expr, ast.Attribute):
            base = self.dotted(expr.value)
            return f"{base}.{expr.attr}" if base else None
        return None

    def defining_module(self, expr) -> str | None:
        """The module that defines the callable *expr* names, or None."""
        if isinstance(expr, ast.Name):
            if expr.id in self.aliases:
                target = self.aliases[expr.id]
                owner = target.rpartition(".")[0]
                if not self._is_module(target) and self._is_module(owner):
                    return owner
                return None
            return self.module if expr.id in self.local_defs else None
        if isinstance(expr, ast.Attribute):
            owner = self.dotted(expr.value)
            return owner if owner and self._is_module(owner) else None
        if isinstance(expr, ast.Call):
            called = self.dotted(expr.func) or getattr(expr.func, "id", "")
            if called in ("functools.partial", "partial") and expr.args:
                return self.defining_module(expr.args[0])
            return None
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            owner = expr.value.rpartition(".")[0]
            return owner if self._is_module(owner) else None
        return None

    def enqueue_calls(self):
        """(line, callable expression or None) for each enqueue-family call."""
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "enqueue":
                position, keywords = _WRAPPER
            elif isinstance(func, ast.Attribute) and func.attr in _RQ_METHODS:
                if func.attr == "enqueue" and self.dotted(func.value) == _WRAPPER_MODULE:
                    position, keywords = _WRAPPER
                else:
                    position, keywords = _RQ_METHODS[func.attr]
            else:
                continue
            target = None
            if position is not None and len(node.args) > position:
                target = node.args[position]
            for kw in node.keywords:
                if kw.arg in keywords:
                    target = kw.value
            yield node.lineno, target


def queued_job_modules(scan_roots, resolve_roots) -> dict[str, set[str]]:
    """{module defining a queued callable: {how it was named}}. Raises
    AssertionError on any enqueue-family call it can't resolve."""
    out: dict[str, set[str]] = {}
    unsupported: list[str] = []
    for root, folders in scan_roots:
        for folder in folders:
            for path in sorted((root / folder).rglob("*.py")):
                source = _File(path, _module_of(path, root), resolve_roots)
                for line, target in source.enqueue_calls():
                    module = source.defining_module(target) if target is not None else None
                    if module is None:
                        unsupported.append(f"{path.relative_to(root)}:{line}")
                        continue
                    out.setdefault(module, set()).add(ast.unparse(target))
    assert not unsupported, (
        "unsupported enqueue style (callable not resolvable to a module) at "
        + ", ".join(unsupported)
        + "; teach tests/test_jobs_never_call_strava.py the style"
    )
    return out


def _strava_chain(scan_roots, resolve_roots) -> list[str] | None:
    start = set(queued_job_modules(scan_roots, resolve_roots)) | {
        _WRAPPER_MODULE, "src.jobs.worker"}
    return _import_path(start, _STRAVA, resolve_roots)


# ── The real repository ──────────────────────────────────────────────────────

def test_finds_the_queued_jobs():
    """Guards the scanner: a scan that finds nothing passes trivially."""
    modules = queued_job_modules([(_ROOT, _APP_SOURCES)], [_ROOT])
    names = set().union(*modules.values())
    assert {"_resolve_route_job", "run_poster_job", "_refresh_share_tiles"} <= names, modules
    assert modules.get("src.poster.poster_job_runner") == {"run_poster_job"}
    # The wrapper's own rq call enqueues the forwarder, never a parameter name.
    assert modules.get(_WRAPPER_MODULE) == _FORWARDERS[_WRAPPER_MODULE], modules


def test_the_import_trace_can_reach_strava():
    """Guards the tracer: the API's own Strava router does import the client."""
    assert _import_path({"api.strava"}, _STRAVA, [_ROOT])


def test_no_queued_job_reaches_the_strava_client():
    chain = _strava_chain([(_ROOT, _APP_SOURCES)], [_ROOT])
    assert chain is None, (
        "a module run by an RQ worker imports the Strava client: "
        + " -> ".join(chain)
        + ". The Strava usage gauge only sees the API process' limiter "
        "(src/utils/metrics.py, STRAVA_RATE_LIMIT_USAGE); move the limiter "
        "somewhere shared before moving Strava work into a job."
    )


# ── The guard itself, on scratch jobs that do reach Strava ───────────────────

_STRAVA_JOB = "from src.api.strava_client import StravaAPI  # noqa\n\n\ndef run(*args):\n    pass\n"
_HEAD = (
    "import functools\n"
    "import scratchjobs.strava_job as module\n"
    "from scratchjobs.strava_job import run as f\n"
    "from src.jobs.queue import QUEUE_DEFAULT, enqueue, get_queue\n\n\n"
    "def schedule():\n"
)


@pytest.mark.parametrize("call", [
    "enqueue(QUEUE_DEFAULT, module.run, 1)",                        # D1 attribute
    "enqueue(QUEUE_DEFAULT, func=f)",                               # D2 keyword
    "get_queue(QUEUE_DEFAULT).enqueue(f, 1)",                       # D3 rq direct
    "enqueue(QUEUE_DEFAULT, functools.partial(f, 1), 2)",           # D4 partial
    "get_queue(QUEUE_DEFAULT).enqueue_in(60, f, 1)",                # D5 time first
    "enqueue(QUEUE_DEFAULT, f, 1)",                                 # D6 plain name
    'get_queue(QUEUE_DEFAULT).enqueue("scratchjobs.strava_job.run")',  # dotted string
    "get_queue(QUEUE_DEFAULT).enqueue_call(func=f, args=(1,))",
    "get_queue(QUEUE_DEFAULT).enqueue_at(when, f)",
])
def test_the_guard_catches_every_enqueue_style(tmp_path, call):
    package = tmp_path / "scratchjobs"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "strava_job.py").write_text(_STRAVA_JOB)
    (package / "caller.py").write_text(_HEAD + f"    {call}\n")

    chain = _strava_chain([(tmp_path, ("scratchjobs",))], [tmp_path, _ROOT])
    assert chain and chain[0] == "scratchjobs.strava_job" and chain[-1] == _STRAVA, chain


@pytest.mark.parametrize("call", [
    "enqueue(QUEUE_DEFAULT, lambda: f(1))",
    "get_queue(QUEUE_DEFAULT).enqueue_many([])",
    "enqueue(QUEUE_DEFAULT, jobs[0])",
    "enqueue(QUEUE_DEFAULT)",
])
def test_an_unresolvable_style_fails_closed(tmp_path, call):
    package = tmp_path / "scratchjobs"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "strava_job.py").write_text(_STRAVA_JOB)
    (package / "caller.py").write_text(_HEAD + f"    {call}\n")

    with pytest.raises(AssertionError, match="unsupported enqueue style"):
        _strava_chain([(tmp_path, ("scratchjobs",))], [tmp_path, _ROOT])
