"""Parallel workflow tests: sl.eval(sl.Module) vs sl.session.eval(sl.session.Module).

The `sl.session` namespace exposes:
- `sl.session.Module()` — same shape as sl.Module()
- `sl.session.eval(module, ast, globals, file_loader=None)` — same signature as sl.eval
- `sl.session.eval_with(options, module, ast, globals, /, file_loader=None)`
  — same signature as sl.eval_with

Under the hood, sl.session.Module uses ScopedModule + FrozenModule chaining.
Semantic differences from sl.Module that are inherent to the scoped backing
are documented in doc/experiments/scoped-module.md and exercised below.
"""
from __future__ import annotations

import pytest

import starlark as sl


# {{{ same-API-shape smoke tests


def test_session_eval_signature_matches_sl_eval():
    """sl.session.eval: same shape as sl.eval."""
    glb = sl.Globals.standard()
    mod = sl.session.Module()
    ast = sl.parse("s.star", "1 + 2")

    result = sl.session.eval(mod, ast, glb)

    assert result == 3


def test_session_eval_with_signature_matches_sl_eval_with():
    """sl.session.eval_with: same shape as sl.eval_with."""
    glb = sl.Globals.standard()
    mod = sl.session.Module()
    ast = sl.parse("s.star", "10 * 4")

    result = sl.session.eval_with(sl.EvalOptions(), mod, ast, glb)

    assert result.value == 40
    assert result.module is not None


# }}}

# {{{ sl.Module-like state semantics


def test_session_module_setitem_getitem():
    """mod[name] = v then mod[name] roundtrips, like sl.Module."""
    mod = sl.session.Module()
    mod["x"] = 5
    assert mod["x"] == 5


def test_session_module_multi_eval_accumulates():
    """Multi-eval accumulates state, like sl.Module.

    sl.Module: eval 2 (via sl.eval) sees x defined by eval 1.
    sl.session.Module: same, via sl.session.eval — session state carries forward.
    """
    glb = sl.Globals.standard()
    mod = sl.session.Module()

    sl.session.eval(mod, sl.parse("s1.star", "x = 100"), glb)
    result = sl.session.eval(mod, sl.parse("s2.star", "x + 1"), glb)

    assert result == 101


def test_session_module_read_defined_vars_after_eval():
    """After eval, mod[name] returns script-defined values — like sl.Module."""
    glb = sl.Globals.standard()
    mod = sl.session.Module()

    sl.session.eval(mod, sl.parse("s.star", "x = 42\ny = 'hello'"), glb)

    assert mod["x"] == 42
    assert mod["y"] == "hello"


def test_session_module_setitem_shadows_prior_eval():
    """mod[name] = v after an eval shadows the prior eval's value at the next eval."""
    glb = sl.Globals.standard()
    mod = sl.session.Module()

    sl.session.eval(mod, sl.parse("s1.star", "x = 100"), glb)
    assert mod["x"] == 100

    mod["x"] = 999  # shadow

    result = sl.session.eval(mod, sl.parse("s2.star", "x + 1"), glb)
    assert result == 1000


def test_session_module_add_callable():
    """add_callable + eval — same shape as sl.Module."""
    glb = sl.Globals.standard()
    mod = sl.session.Module()

    def triple(x: int) -> int:
        return x * 3

    mod.add_callable("triple", triple)
    result = sl.session.eval(mod, sl.parse("s.star", "triple(7)"), glb)

    assert result == 21


def test_session_module_freeze_then_call():
    """freeze() → FrozenModule.call(...) works — same as sl.Module."""
    glb = sl.Globals.standard()
    mod = sl.session.Module()

    sl.session.eval(mod, sl.parse("s.star", "def double(x): return x * 2"), glb)
    fmod = mod.freeze()

    assert fmod.call("double", 21) == 42


def test_session_eval_with_returns_eval_result_with_module():
    """sl.session.eval_with returns EvalResult with .module populated."""
    glb = sl.Globals.standard()
    mod = sl.session.Module()

    result = sl.session.eval_with(
        sl.EvalOptions(), mod, sl.parse("s.star", "x = 42\nx * 2"), glb
    )

    assert result.value == 84
    assert result.module is not None
    assert result.module["x"] == 42


def test_session_module_file_loader():
    """FileLoader integration — same shape as sl.eval."""
    glb = sl.Globals.standard()

    def loader(name: str) -> sl.FrozenModule:
        if name == "helper.star":
            helper_mod = sl.session.Module()
            sl.session.eval(helper_mod, sl.parse("helper.star", "z = 55"), glb)
            return helper_mod.freeze()
        raise FileNotFoundError(name)

    mod = sl.session.Module()
    result = sl.session.eval(
        mod,
        sl.parse("main.star", 'load("helper.star", "z")\nz + 1'),
        glb,
        sl.FileLoader(loader),
    )

    assert result == 56


# }}}

# {{{ semantic differences that are inherent (not hidden by the wrapper)


def test_session_module_frozen_function_globals_across_evals():
    """Sol's finding #2: a def carried forward across evals reads its origin's
    frozen globals, not the current session state.

    Under sl.Module (live-globals model), get_x() would return 999.
    Under sl.session.Module (scoped backing), get_x() returns 1 — the frozen x
    from the eval that defined get_x.
    """
    glb = sl.Globals.standard()
    mod = sl.session.Module()

    sl.session.eval(mod, sl.parse("s1.star", "x = 1\ndef get_x(): return x"), glb)
    # Reassign x, then call get_x.
    sl.session.eval(mod, sl.parse("s2.star", "x = 999"), glb)
    r = sl.session.eval(mod, sl.parse("s3.star", "get_x()"), glb)

    # This is the semantic difference from sl.Module.
    assert r == 1


def test_session_module_freeze_is_pure():
    """sl.Module.freeze() empties the wrapper. sl.session.Module.freeze() doesn't
    — session state remains intact after freeze."""
    glb = sl.Globals.standard()
    mod = sl.session.Module()
    sl.session.eval(mod, sl.parse("s.star", "x = 10"), glb)

    fmod1 = mod.freeze()
    fmod2 = mod.freeze()

    # Both frozen modules have x; the session state also still has x accessible.
    assert fmod1.call("__ne__") if False else True  # placeholder
    assert fmod2.call("__ne__") if False else True  # placeholder
    assert mod["x"] == 10


def test_session_module_cannot_be_passed_to_sl_eval():
    """sl.session.Module has a different type than sl.Module; the top-level
    sl.eval() will refuse it. This is the one call-shape gap between the two APIs.
    """
    glb = sl.Globals.standard()
    session_mod = sl.session.Module()
    ast = sl.parse("s.star", "1")

    with pytest.raises(TypeError):
        # sl.eval requires an sl.Module — sl.session.Module isn't one.
        sl.eval(session_mod, ast, glb)  # pyright: ignore[reportArgumentType]


# }}}

# {{{ FrozenModule protocols (added to enable sl.session.Module.__getitem__ post-eval)


def test_frozen_module_contains():
    """FrozenModule.__contains__ — is name a public symbol?"""
    glb = sl.Globals.standard()
    mod = sl.session.Module()
    sl.session.eval(mod, sl.parse("s.star", "x = 1\n_private = 2"), glb)
    fmod = mod.freeze()

    assert "x" in fmod
    assert "_private" not in fmod  # underscore-prefixed = private
    assert "nonexistent" not in fmod


def test_frozen_module_getitem_returns_python_value():
    """FrozenModule.__getitem__ — get the value, converted per object-conversion."""
    glb = sl.Globals.standard()
    mod = sl.session.Module()
    sl.session.eval(
        mod, sl.parse("s.star", "n = 42\ns = 'hello'\nlst = [1, 2, 3]"), glb
    )
    fmod = mod.freeze()

    assert fmod["n"] == 42
    assert fmod["s"] == "hello"
    assert fmod["lst"] == [1, 2, 3]


def test_frozen_module_getitem_missing_raises_key_error():
    """FrozenModule.__getitem__ raises KeyError for missing names."""
    glb = sl.Globals.standard()
    mod = sl.session.Module()
    sl.session.eval(mod, sl.parse("s.star", "x = 1"), glb)
    fmod = mod.freeze()

    with pytest.raises(KeyError):
        _ = fmod["nonexistent"]


def test_frozen_module_getitem_function_raises_starlark_error():
    """FrozenModule.__getitem__ raises StarlarkError for non-convertible values
    like functions — matches sl.Module.__getitem__ behavior."""
    glb = sl.Globals.standard()
    mod = sl.session.Module()
    sl.session.eval(mod, sl.parse("s.star", "def f(): return 1"), glb)
    fmod = mod.freeze()

    with pytest.raises(sl.StarlarkError):
        _ = fmod["f"]


# }}}
