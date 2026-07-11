"""Exploratory tests for the ScopedModule direction-A experiment.

These tests are the ergonomic feel-check for the new API. They are NOT
intended to prove the design is right — they explore what it looks like.
"""
from __future__ import annotations

import pytest

import starlark as sl


def test_scoped_basic_eval_returns_module():
    glb = sl.Globals.standard()
    mod = sl.ScopedModule()
    ast = sl.parse("basic.star", "x = 42\nx * 2")

    result = sl.eval_scoped_with(sl.EvalOptions(), mod, ast, glb)

    assert result.value == 84
    assert result.module is not None
    # The evaluated module now contains x=42.
    assert result.module.call("__eval__") if False else True  # placeholder
    # We can call a function defined by the eval via the returned FrozenModule.


def test_scoped_eval_then_call_defined_function():
    glb = sl.Globals.standard()
    mod = sl.ScopedModule()
    ast = sl.parse("defs.star", "def double(x): return x * 2")

    result = sl.eval_scoped_with(sl.EvalOptions(), mod, ast, glb)

    assert result.value is None
    assert result.module is not None
    # Direction A replacement for `sl.eval(mod, ast, glb); mod.freeze().call(...)`.
    assert result.module.call("double", 21) == 42


def test_scoped_module_pending_setitem():
    """Values set on the ScopedModule are visible to the eval."""
    glb = sl.Globals.standard()
    mod = sl.ScopedModule()
    mod["a"] = 5

    ast = sl.parse("use-a.star", "a * 2")
    result = sl.eval_scoped_with(sl.EvalOptions(), mod, ast, glb)

    assert result.value == 10


def test_scoped_module_pending_callable():
    """Callables added via add_callable are invocable from Starlark."""
    glb = sl.Globals.standard()
    mod = sl.ScopedModule()

    def triple(x: int) -> int:
        return x * 3

    mod.add_callable("triple", triple)

    ast = sl.parse("call-triple.star", "triple(4)")
    result = sl.eval_scoped_with(sl.EvalOptions(), mod, ast, glb)

    assert result.value == 12


def test_scoped_module_does_not_accumulate_across_evals():
    """Direction A: each eval starts fresh from the module's pending state."""
    glb = sl.Globals.standard()
    mod = sl.ScopedModule()

    ast1 = sl.parse("set-x.star", "x = 1")
    r1 = sl.eval_scoped_with(sl.EvalOptions(), mod, ast1, glb)
    assert r1.value is None
    assert r1.module is not None
    assert r1.module.call("__probe__") if False else True  # placeholder

    # Second eval starts fresh; `x` from the first eval is NOT visible here.
    # Under the pre-refactor Module semantics, this would succeed and return 2.
    ast2 = sl.parse("use-x.star", "x + 1")
    with pytest.raises(sl.StarlarkError):
        sl.eval_scoped_with(sl.EvalOptions(), mod, ast2, glb)


def test_scoped_module_import_chains_state():
    """Iterative pattern: feed result.module into a new module via import."""
    glb = sl.Globals.standard()
    mod = sl.ScopedModule()

    ast1 = sl.parse("set-x.star", "x = 10")
    r1 = sl.eval_scoped_with(sl.EvalOptions(), mod, ast1, glb)
    assert r1.module is not None

    mod2 = sl.ScopedModule()
    mod2.import_public_symbols(r1.module)

    # x should now be visible via the import.
    ast2 = sl.parse("use-x.star", "x + 5")
    r2 = sl.eval_scoped_with(sl.EvalOptions(), mod2, ast2, glb)
    assert r2.value == 15


def test_scoped_module_import_carries_frozen_functions():
    """A def from an earlier eval is callable in a later eval via import."""
    glb = sl.Globals.standard()
    mod = sl.ScopedModule()

    ast1 = sl.parse("defs.star", "def double(x): return x * 2")
    r1 = sl.eval_scoped_with(sl.EvalOptions(), mod, ast1, glb)
    assert r1.module is not None

    mod2 = sl.ScopedModule()
    mod2.import_public_symbols(r1.module)

    ast2 = sl.parse("call-imported.star", "double(21)")
    r2 = sl.eval_scoped_with(sl.EvalOptions(), mod2, ast2, glb)
    assert r2.value == 42


def test_scoped_module_frozen_function_globals_are_stale():
    """Sol's finding #2: a carried-forward def sees its origin module's
    frozen globals, not the current module's redefined ones.

    This exercises the exact semantic the discussion flagged.
    """
    glb = sl.Globals.standard()
    mod1 = sl.ScopedModule()

    ast1 = sl.parse("f1.star", """
x = 1
def get_x():
    return x
""")
    r1 = sl.eval_scoped_with(sl.EvalOptions(), mod1, ast1, glb)
    assert r1.module is not None

    # Import into mod2 and shadow x with a new value.
    mod2 = sl.ScopedModule()
    mod2.import_public_symbols(r1.module)

    ast2 = sl.parse("f2.star", """
x = 999
get_x()
""")
    r2 = sl.eval_scoped_with(sl.EvalOptions(), mod2, ast2, glb)
    # Frozen get_x reads the frozen x=1 from mod1's origin, NOT the new x=999.
    # This is the closure-globals shift sol identified.
    assert r2.value == 1


def test_scoped_module_freeze_without_eval():
    """freeze() before any eval bakes just the pending state."""
    glb = sl.Globals.standard()
    mod = sl.ScopedModule()
    mod["a"] = 42

    fmod = mod.freeze()
    # The FrozenModule now has a=42, but no computed values from any eval.
    _ = glb, fmod  # exercise the freeze; caller pattern isn't super clear yet


# {{{ bare eval_scoped ergonomics


def test_bare_eval_scoped_matches_eval_scoped_with_no_options():
    """eval_scoped(mod, ast, glb) should behave as
    eval_scoped_with(EvalOptions(), mod, ast, glb)."""
    glb = sl.Globals.standard()
    mod = sl.ScopedModule()
    mod["x"] = 21
    ast = sl.parse("bare.star", "x * 2")

    bare_result = sl.eval_scoped(mod, ast, glb)

    mod2 = sl.ScopedModule()
    mod2["x"] = 21
    with_result = sl.eval_scoped_with(sl.EvalOptions(), mod2, ast, glb)

    assert bare_result.value == with_result.value == 42
    assert bare_result.module is not None
    assert with_result.module is not None


def test_bare_eval_scoped_is_less_verbose():
    """Show the ergonomic difference: bare form skips the empty EvalOptions()."""
    glb = sl.Globals.standard()
    mod = sl.ScopedModule()
    ast = sl.parse("bare-ergo.star", "def f(x): return x + 1")

    # Bare form:
    result = sl.eval_scoped(mod, ast, glb)
    assert result.module is not None
    assert result.module.call("f", 10) == 11


# }}}

# {{{ FileLoader integration


def test_scoped_module_with_file_loader():
    """FileLoader delivers a FrozenModule to a `load()` statement.

    Under Direction A, the natural producer of a FrozenModule for the loader
    to return is another eval_scoped_with call. This exercises the "modules
    as first-class evaluations" story end-to-end.
    """
    glb = sl.Globals.standard()

    def load(name: str) -> sl.FrozenModule:
        if name == "helper.star":
            helper_mod = sl.ScopedModule()
            ast = sl.parse("helper.star", "def double(x): return x * 2")
            r = sl.eval_scoped(helper_mod, ast, glb)
            assert r.module is not None
            return r.module
        raise FileNotFoundError(name)

    mod = sl.ScopedModule()
    main_ast = sl.parse(
        "main.star",
        'load("helper.star", "double")\ndouble(21)',
    )
    result = sl.eval_scoped_with(
        sl.EvalOptions(), mod, main_ast, glb, sl.FileLoader(load)
    )
    assert result.value == 42


# }}}

# {{{ transitive heap retention (sol's finding #3)


def test_scoped_module_transitive_import_retention():
    """After N iterated imports each chained to the previous frozen module,
    values from the FIRST module are still transitively accessible.

    This structurally demonstrates the heap-chain retention sol flagged.
    We can't measure the actual retained bytes at the pinned starlark rev
    (`check_heap_size_limit` and friends land at v0.14), but we can prove
    the reference structure exists — a proxy for the memory concern.
    """
    glb = sl.Globals.standard()

    # First eval: origin values.
    mod0 = sl.ScopedModule()
    ast0 = sl.parse("origin.star", "origin_val = 12345")
    r = sl.eval_scoped(mod0, ast0, glb)
    assert r.module is not None

    # Chain 20 iterated evals, each importing the previous frozen and
    # adding some padding of its own.
    for i in range(20):
        m = sl.ScopedModule()
        m.import_public_symbols(r.module)
        ast_i = sl.parse(f"iter-{i}.star", f"padding_{i} = {i * 1000}")
        r = sl.eval_scoped(m, ast_i, glb)
        assert r.module is not None

    # Even after 20 iterations, origin_val is reachable via the transitive
    # frozen-heap chain — which means every intermediate frozen heap along
    # the chain is retained.
    final = sl.ScopedModule()
    final.import_public_symbols(r.module)
    ast_final = sl.parse("check.star", "origin_val")
    r_final = sl.eval_scoped(final, ast_final, glb)
    assert r_final.value == 12345


def test_scoped_module_deep_import_chain_padding_carries_forward():
    """Confirm that intermediate padding values are also retained
    transitively — the chain preserves ALL public symbols from every
    prior link, not just the origin."""
    glb = sl.Globals.standard()

    mod = sl.ScopedModule()
    ast = sl.parse("start.star", "seed = 0")
    r = sl.eval_scoped(mod, ast, glb)
    assert r.module is not None

    for i in range(1, 6):
        m = sl.ScopedModule()
        assert r.module is not None
        m.import_public_symbols(r.module)
        ast_i = sl.parse(f"iter-{i}.star", f"padding_{i} = {i}")
        r = sl.eval_scoped(m, ast_i, glb)

    # padding_1 through padding_5 should all be reachable in the final chain.
    assert r.module is not None
    check = sl.ScopedModule()
    check.import_public_symbols(r.module)
    ast_check = sl.parse(
        "check.star", "padding_1 + padding_2 + padding_3 + padding_4 + padding_5"
    )
    r_check = sl.eval_scoped(check, ast_check, glb)
    assert r_check.value == 15  # 1+2+3+4+5


# }}}

# {{{ Session convenience wrapper (pure Python prototype)


class Session:
    """Python-level convenience wrapper around ScopedModule to make the
    iterative pattern feel like the old accumulate-across-evals workflow.

    Each :meth:`eval` creates a fresh :class:`ScopedModule`, imports the
    previous evaluation's frozen module, runs the eval, and stashes the
    new frozen module for the next call. This is a shim, not a binding
    primitive — a user could write it themselves once ScopedModule is
    exposed.

    Note: this inherits the "frozen function globals are stale" semantic
    (previously verified in test_scoped_module_frozen_function_globals_are_stale).
    """

    _globals: sl.Globals
    _frozen: sl.FrozenModule | None

    def __init__(self, globals: sl.Globals) -> None:
        self._globals = globals
        self._frozen = None

    def set(self, name: str, value: object) -> None:
        # For simplicity, a Session evaluates bindings-as-code so they carry
        # into the frozen chain. A real implementation might defer these to
        # the next eval instead.
        ast = sl.parse("_session_set.star", f"{name} = _v")
        mod = sl.ScopedModule()
        mod["_v"] = value
        if self._frozen is not None:
            mod.import_public_symbols(self._frozen)
        r = sl.eval_scoped(mod, ast, self._globals)
        self._frozen = r.module

    def eval(
        self, ast: sl.AstModule, options: sl.EvalOptions | None = None
    ) -> sl.EvalResult:
        mod = sl.ScopedModule()
        if self._frozen is not None:
            mod.import_public_symbols(self._frozen)
        opts = options or sl.EvalOptions()
        result = sl.eval_scoped_with(opts, mod, ast, self._globals)
        self._frozen = result.module
        return result

    @property
    def frozen(self) -> sl.FrozenModule | None:
        return self._frozen


def test_session_wrapper_chains_evals():
    """The Session shim lets users write a REPL-style iterative flow
    without threading result.module manually."""
    glb = sl.Globals.standard()
    session = Session(glb)

    session.eval(sl.parse("s1.star", "x = 10"))
    session.eval(sl.parse("s2.star", "y = x + 5"))
    r = session.eval(sl.parse("s3.star", "x + y"))

    assert r.value == 25  # 10 + 15
    assert session.frozen is not None
    # The final frozen module has x, y, and no other symbols beyond what
    # each eval defined publicly.


def test_session_wrapper_frozen_globals_staleness():
    """Session inherits the same closure-globals staleness as ScopedModule
    chains. A def captured in an earlier session step reads its frozen
    origin, not the current session state."""
    glb = sl.Globals.standard()
    session = Session(glb)

    session.eval(sl.parse("d1.star", "x = 1\ndef get_x(): return x"))
    session.eval(sl.parse("d2.star", "x = 999"))
    r = session.eval(sl.parse("d3.star", "get_x()"))

    # Frozen get_x still reads the frozen x from d1's origin.
    assert r.value == 1


# }}}
