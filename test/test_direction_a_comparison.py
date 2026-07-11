"""Parallel workflow tests: sl.Module (current) vs sl.ScopedModule (Direction A).

Each pair of tests runs the same conceptual workflow both ways. The names
prefix `_module_` and `_scoped_` to make side-by-side comparison obvious.
The point is to catalog differences — behavior, ergonomic, and semantic —
in one place for the maintainer discussion.

Findings synthesized in .tmp/direction-a-findings.md.
"""
from __future__ import annotations

import pytest

import starlark as sl


# {{{ Workflow: set values + eval + read result


def test_module_set_eval_read():
    """Old sl.Module: script's result is the eval return; module reads
    show the script's defined variables."""
    glb = sl.Globals.standard()
    mod = sl.Module()
    mod["a"] = 5
    ast = sl.parse("w1.star", "b = a * 2\nb + 1")

    result = sl.eval(mod, ast, glb)

    assert result == 11
    # sl.Module: after eval, mod["b"] shows the script-defined value
    assert mod["b"] == 10


def test_scoped_set_eval_read():
    """New sl.ScopedModule: script's result is EvalResult.value; result.module
    gives access to script-defined variables via .call() (no direct subscript)."""
    glb = sl.Globals.standard()
    mod = sl.ScopedModule()
    mod["a"] = 5
    ast = sl.parse("w1.star", "b = a * 2\nb + 1")

    result = sl.eval_scoped(mod, ast, glb)

    assert result.value == 11
    # sl.ScopedModule: mod["b"] returns None — mod is only the setup builder
    assert mod["b"] is None
    # To inspect script-defined vars, use result.module (a FrozenModule).
    # FrozenModule has no __getitem__ — use `result.module.call(...)` for functions.
    # For values, use a follow-up eval that references them via the frozen chain.


# }}}

# {{{ Workflow: define function + freeze + call


def test_module_define_freeze_call():
    """Old sl.Module: eval populates mod; freeze produces a callable
    FrozenModule."""
    glb = sl.Globals.standard()
    mod = sl.Module()
    ast = sl.parse("w2.star", "def add(x, y): return x + y")

    sl.eval(mod, ast, glb)
    fmod = mod.freeze()

    assert fmod.call("add", 3, 4) == 7


def test_scoped_define_freeze_call():
    """New sl.ScopedModule: eval returns EvalResult with .module ready."""
    glb = sl.Globals.standard()
    mod = sl.ScopedModule()
    ast = sl.parse("w2.star", "def add(x, y): return x + y")

    result = sl.eval_scoped(mod, ast, glb)

    assert result.module is not None
    assert result.module.call("add", 3, 4) == 7
    # No separate freeze() step — the frozen module is delivered by eval_scoped.


# }}}

# {{{ Workflow: Python callable exposed to Starlark


def test_module_add_callable():
    """Old sl.Module: add_callable, then eval."""
    glb = sl.Globals.standard()
    mod = sl.Module()

    def triple(x: int) -> int:
        return x * 3

    mod.add_callable("triple", triple)
    ast = sl.parse("w3.star", "triple(7)")

    assert sl.eval(mod, ast, glb) == 21


def test_scoped_add_callable():
    """New sl.ScopedModule: same shape."""
    glb = sl.Globals.standard()
    mod = sl.ScopedModule()

    def triple(x: int) -> int:
        return x * 3

    mod.add_callable("triple", triple)
    ast = sl.parse("w3.star", "triple(7)")

    result = sl.eval_scoped(mod, ast, glb)
    assert result.value == 21


# }}}

# {{{ Workflow: multi-eval accumulate (key semantic difference)


def test_module_multi_eval_accumulates():
    """Old sl.Module: state accumulates across evals. Eval 2 sees eval 1's x."""
    glb = sl.Globals.standard()
    mod = sl.Module()

    ast1 = sl.parse("m1.star", "x = 100")
    sl.eval(mod, ast1, glb)

    ast2 = sl.parse("m2.star", "x + 1")
    assert sl.eval(mod, ast2, glb) == 101


def test_scoped_multi_eval_does_not_accumulate():
    """New sl.ScopedModule: each eval starts fresh from mod's pending state.
    Eval 2 does NOT see eval 1's x. Users must import explicitly."""
    glb = sl.Globals.standard()
    mod = sl.ScopedModule()

    ast1 = sl.parse("s1.star", "x = 100")
    sl.eval_scoped(mod, ast1, glb)

    ast2 = sl.parse("s2.star", "x + 1")
    with pytest.raises(sl.StarlarkError, match="not found"):
        sl.eval_scoped(mod, ast2, glb)


def test_scoped_multi_eval_via_explicit_import():
    """Scoped equivalent of accumulate: use result.module + reexport_public_symbols."""
    glb = sl.Globals.standard()
    mod = sl.ScopedModule()

    ast1 = sl.parse("s1.star", "x = 100")
    r1 = sl.eval_scoped(mod, ast1, glb)
    assert r1.module is not None

    mod2 = sl.ScopedModule()
    mod2.reexport_public_symbols(r1.module)
    ast2 = sl.parse("s2.star", "x + 1")
    assert sl.eval_scoped(mod2, ast2, glb).value == 101


# }}}

# {{{ Workflow: FileLoader with load()


def test_module_file_loader():
    """Old sl.Module: FileLoader returns a FrozenModule for load() statements."""
    glb = sl.Globals.standard()

    def loader(name: str) -> sl.FrozenModule:
        if name == "helper.star":
            helper_mod = sl.Module()
            sl.eval(helper_mod, sl.parse("helper.star", "z = 55"), glb)
            return helper_mod.freeze()
        raise FileNotFoundError(name)

    mod = sl.Module()
    ast = sl.parse("main.star", 'load("helper.star", "z")\nz + 1')
    result = sl.eval(mod, ast, glb, sl.FileLoader(loader))

    assert result == 56


def test_scoped_file_loader():
    """New sl.ScopedModule: FileLoader integration is a natural fit — the
    loader function returns a FrozenModule from a nested eval_scoped."""
    glb = sl.Globals.standard()

    def loader(name: str) -> sl.FrozenModule:
        if name == "helper.star":
            helper_mod = sl.ScopedModule()
            r = sl.eval_scoped(helper_mod, sl.parse("helper.star", "z = 55"), glb)
            assert r.module is not None
            return r.module
        raise FileNotFoundError(name)

    mod = sl.ScopedModule()
    ast = sl.parse("main.star", 'load("helper.star", "z")\nz + 1')
    result = sl.eval_scoped(mod, ast, glb, sl.FileLoader(loader))

    assert result.value == 56


# }}}

# {{{ Workflow: closure semantics across evals (sol's finding #2)


def test_module_closure_reads_live_globals():
    """Old sl.Module: a def in eval 1 reads the live module slot when
    called from eval 2 (which reassigns the global)."""
    glb = sl.Globals.standard()
    mod = sl.Module()

    sl.eval(mod, sl.parse("m1.star", "x = 1\ndef get_x(): return x"), glb)

    # eval 2 reassigns x, then calls get_x. Live-globals model: sees new x.
    fmod = mod.freeze()
    # Note: after freeze, mod is emptied; can't rerun sl.eval on mod here.
    # The old-Module semantic being tested is: within ONE freeze cycle,
    # a def reads the live module slot. This is what breaks in Direction A.

    # For a strict apples-to-apples: do two evals with different x values,
    # both defining get_x, and confirm the second one sees the second x.
    mod = sl.Module()
    sl.eval(mod, sl.parse("m1b.star", "x = 1\ndef get_x(): return x\nx = 2"), glb)
    fmod = mod.freeze()
    # After eval, x=2, get_x() should see 2 (live-globals model within one eval).
    assert fmod.call("get_x") == 2


def test_scoped_closure_globals_are_frozen_at_origin():
    """New sl.ScopedModule: within a single eval, live-globals works too
    (functions read the live module slot). Difference only shows up when
    a function is CARRIED FORWARD via freeze+import."""
    glb = sl.Globals.standard()
    mod = sl.ScopedModule()
    # Same within-one-eval reassignment: also works fine in scoped model.
    ast = sl.parse("s1b.star", "x = 1\ndef get_x(): return x\nx = 2")
    r = sl.eval_scoped(mod, ast, glb)
    assert r.module is not None
    assert r.module.call("get_x") == 2

    # But when get_x is carried forward via import and x is shadowed in a
    # later eval, get_x reads its FROZEN origin, not the current module.
    mod2 = sl.ScopedModule()
    mod2.reexport_public_symbols(r.module)
    r2 = sl.eval_scoped(mod2, sl.parse("s2b.star", "x = 999\nget_x()"), glb)
    assert r2.value == 2  # frozen origin, NOT 999


# }}}

# {{{ Workflow: read-after-eval on mod (semantic difference)


def test_module_read_defined_vars_after_eval():
    """Old sl.Module: script-defined variables are readable via mod[name]
    after eval. This is a key affordance of the old API."""
    glb = sl.Globals.standard()
    mod = sl.Module()
    sl.eval(mod, sl.parse("r1.star", "x = 42\ny = 'hello'"), glb)

    assert mod["x"] == 42
    assert mod["y"] == "hello"


def test_scoped_read_defined_vars_after_eval():
    """New sl.ScopedModule: mod['x'] after eval returns None. Users must
    inspect via a follow-up eval that references the frozen chain."""
    glb = sl.Globals.standard()
    mod = sl.ScopedModule()
    r = sl.eval_scoped(mod, sl.parse("r1.star", "x = 42\ny = 'hello'"), glb)
    assert r.module is not None

    # mod['x'] does NOT return 42 — mod is just the setup builder.
    assert mod["x"] is None

    # To read x, do a follow-up eval that references it via the frozen module.
    inspect_mod = sl.ScopedModule()
    inspect_mod.reexport_public_symbols(r.module)
    r_inspect = sl.eval_scoped(inspect_mod, sl.parse("check.star", "x"), glb)
    assert r_inspect.value == 42


# }}}

# {{{ Workflow: freeze semantic


def test_module_freeze_empties_wrapper():
    """Old sl.Module: mod.freeze() moves the internal Module out;
    subsequent operations on mod see an empty module."""
    glb = sl.Globals.standard()
    mod = sl.Module()
    mod["a"] = 10
    sl.eval(mod, sl.parse("f1.star", "b = 20"), glb)

    fmod = mod.freeze()
    # After freeze, mod is "empty" — a fresh internal module. Setting new
    # values on it works, but the previously-populated state is gone.
    assert mod["a"] is None
    # The fmod has both.
    _ = fmod  # exercised


def test_scoped_freeze_is_pure():
    """New sl.ScopedModule: freeze() is a pure function of pending state;
    it doesn't mutate the ScopedModule."""
    mod = sl.ScopedModule()
    mod["a"] = 10

    fmod1 = mod.freeze()
    fmod2 = mod.freeze()

    # Two calls to freeze produce equivalent FrozenModules (both have a=10).
    # More importantly, mod still has 'a' after freeze.
    assert mod["a"] == 10
    _ = fmod1, fmod2  # exercised


# }}}

# {{{ Workflow: check_cancelled / options


def test_module_eval_with_check_cancelled_aborts():
    """Old sl.Module: options routed via eval_with, aborts as expected."""
    glb = sl.Globals.standard()
    mod = sl.Module()
    ast = sl.parse(
        "loop.star",
        "def busy():\n    for _ in range(1000000000):\n        pass\nbusy()",
    )

    def cancel():
        return True

    with pytest.raises(sl.StarlarkError):
        sl.eval_with(sl.EvalOptions(check_cancelled=cancel), mod, ast, glb)


def test_scoped_eval_with_check_cancelled_aborts():
    """New sl.ScopedModule: same shape via eval_scoped_with."""
    glb = sl.Globals.standard()
    mod = sl.ScopedModule()
    ast = sl.parse(
        "loop.star",
        "def busy():\n    for _ in range(1000000000):\n        pass\nbusy()",
    )

    def cancel():
        return True

    with pytest.raises(sl.StarlarkError):
        sl.eval_scoped_with(sl.EvalOptions(check_cancelled=cancel), mod, ast, glb)


# }}}
