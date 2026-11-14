"""The design rules the modules are meant to obey, checked rather than described.

A layering that only exists in a README stops being true on the first busy
afternoon. These read the import graph out of the source and assert the three
rules the design rests on:

    1. No cycles. Every module can be understood, and tested, without its callers.
    2. The pure layer stays pure: parsing, money and time never touch the database,
       so they are testable without one and reusable away from it.
    3. HTTP lives only in the api package. Every other module can be called from the worker,
       the CLI or a test with no request in sight.
"""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).parent.parent / "paysleuth"

# Modules that must not know the database exists. The first group is pure value
# handling; the second is parsing, which is deliberately separable from storage
# so a statement can be read and checked before anything is written.
PURE = {"money", "periods", "errors", "names", "actors", "webhooks", "logging_setup",
        "statements", "continuity", "banks", "banks.common", "banks.sbi", "banks.hdfc",
        "banks.icici", "banks.axis", "banks.canara"}

DATABASE_NAMES = {"psycopg", "psycopg_pool", "db"}
HTTP_NAMES = {"fastapi", "starlette", "pydantic", "uvicorn"}


def _module_name(path: Path) -> str:
    if path.name == "__init__.py":
        # A subpackage is named for its own folder; only the root package's
        # __init__ is "paysleuth".
        return "paysleuth" if path.parent.name == "paysleuth" else path.parent.name
    return path.stem if path.parent.name == "paysleuth" else f"{path.parent.name}.{path.stem}"


def _imports(path: Path) -> tuple[set[str], set[str]]:
    """(modules of this package, outside packages) imported by a file."""
    inside, outside = set(), set()
    tree = ast.parse(path.read_text())
    # Inside a subpackage, `from .x import y` means that subpackage's x.
    prefix = "" if path.parent.name == "paysleuth" else f"{path.parent.name}."
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            outside.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if not node.level:                       # absolute: another package
                outside.add((node.module or "").split(".")[0])
            elif node.module:                        # from .x import y
                inside.add(prefix + node.module.split(".")[0] if node.level == 1 and prefix
                           else node.module.split(".")[0])
            else:                                    # from . import x, y
                inside.update(prefix + alias.name for alias in node.names)
    return inside, outside


def _graph() -> dict[str, set[str]]:
    graph = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        inside, _ = _imports(path)
        graph[_module_name(path)] = inside
    known = set(graph)
    return {module: {dep for dep in deps if dep in known} for module, deps in graph.items()}


def test_the_import_graph_has_no_cycles():
    """Peel off modules whose dependencies are already placed. A cycle leaves some stuck."""
    graph = _graph()
    placed, layers, remaining = set(), [], dict(graph)
    while remaining:
        layer = sorted(m for m, deps in remaining.items() if deps <= placed)
        assert layer, f"import cycle among {sorted(remaining)}"
        layers.append(layer)
        placed |= set(layer)
        remaining = {m: deps for m, deps in remaining.items() if m not in placed}
    # The shape matters as much as the absence of cycles: nothing but the ASGI
    # entry point sits above the HTTP package.
    #
    # The earlier form of this asserted api.py occupied the second layer from the
    # top. That stopped being the right statement when api.py became a package:
    # api.schemas imports only money and errors, so by dependency *depth* it
    # belongs near the bottom, and depth is not architectural rank. What actually
    # matters is that the package is a sink -- the domain can be called by the
    # worker, the CLI and the tests because none of it depends on the web layer.
    assert layers[-1] == ["main"]
    http = {m for m in graph if m == "api" or m.startswith("api.")}
    assert http, "the api package vanished"
    reaching_up = {m: sorted(deps & http) for m, deps in graph.items()
                   if m not in http and m != "main" and deps & http}
    assert reaching_up == {}, f"the domain depends on the web layer: {reaching_up}"

    # allocation.py is in the bottom layer though it is not pure: it writes SQL, but
    # it takes the connection as a parameter and imports no module of this package,
    # so nothing it does is decided by what sits above it.
    assert "allocation" in layers[0]


def test_the_pure_layer_is_closed():
    """A pure module may depend only on other pure modules.

    Depth is not the test: the pure chain is legitimately five deep
    (banks.common -> each adapter -> banks -> statements -> continuity). What
    matters is that nothing in it reaches upward, because that is what keeps
    statement parsing usable with no database and no institution in sight.
    """
    graph = _graph()
    leaks = {module: sorted(graph[module] - PURE - {"paysleuth"})
             for module in sorted(PURE) if graph.get(module, set()) - PURE - {"paysleuth"}}
    assert leaks == {}, f"the pure layer now depends on {leaks}"


@pytest.mark.parametrize("module", sorted(PURE))
def test_the_pure_modules_do_not_touch_the_database(module):
    path = PACKAGE / (module.replace(".", "/") + ".py")
    if not path.exists():
        path = PACKAGE / module.replace(".", "/") / "__init__.py"
    inside, outside = _imports(path)
    assert not (inside | outside) & DATABASE_NAMES, f"{module} reaches the database"


def test_only_the_api_module_knows_about_http():
    offenders = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        name = _module_name(path)
        if name == "main" or name == "api" or name.startswith("api."):
            continue
        _, outside = _imports(path)
        if outside & HTTP_NAMES:
            offenders[name] = sorted(outside & HTTP_NAMES)
    assert offenders == {}, f"HTTP leaked out of the api package into {offenders}"


def test_the_domain_raises_its_own_errors_rather_than_http_ones():
    """A 404 is a presentation choice. Business code raises NotFound and the api package maps it,
    so the same function can be called from the worker or a test."""
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        name = _module_name(path)
        if name == "main" or name == "api" or name.startswith("api."):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                name = node.exc.func
                raised = getattr(name, "id", None) or getattr(name, "attr", None)
                if raised == "HTTPException":
                    offenders.append(f"{name}:{node.lineno}")
    assert offenders == [], f"HTTPException raised outside the api package at {offenders}"


def test_every_module_says_what_it_is_for():
    """A module docstring is where the next person looks first, so there is always one."""
    missing = [_module_name(p) for p in sorted(PACKAGE.rglob("*.py"))
               if not ast.get_docstring(ast.parse(p.read_text()))]
    assert missing == [], f"no module docstring in {missing}"
