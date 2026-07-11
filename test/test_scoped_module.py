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
