# `sl.ScopedModule`: an experiment in scope-safe module wrapping

This directory holds experimental work not intended for release. The code in this branch is an exploratory implementation of a redesigned module-wrapping strategy for `starlark-pyo3`. It coexists with the existing `sl.Module` — nothing about the existing API changes. The point is to feel a design in isolation before deciding what to do with it.

## Motivation

`starlark-pyo3` currently wraps a live `starlark::environment::Module` behind a Python-side object:

```rust
#[pyclass] struct Module(Mutex<starlark::environment::Module>);
unsafe impl Send for Module {}
unsafe impl Sync for Module {}
```

Two properties of that wrapping are load-bearing:

1. The Rust `Module` outlives any single pyo3 call. Users can `mod['x'] = v`, then `sl.eval(mod, ast, glb)`, then `mod['y']` (the second read sees a script-defined variable), then `mod.freeze()` — all separate boundary crossings against the same underlying `Module`.
2. The wrapping requires manual `unsafe impl Send/Sync`. The comment in `src/lib.rs` calls out that this is a conscious workaround around the `Module`'s inferred non-`Send`-ness and needs re-verification on every dependency update.

Property 1 depends on `starlark::environment::Module::new()` being publicly constructible and the `Module` type having no lifetime parameter — assumptions that hold at our current pinned revision but may not hold at some future revision of the crate we depend on. Property 2 is a permanent maintenance cost.

The `sl.ScopedModule` experiment sketches an alternative where the Python-side object never holds a live `starlark::environment::Module`. Instead, it stores a bag of pending operations (values to set, callables to register, frozen modules to import), and each evaluation materializes a fresh underlying module inside the eval helper, applies pending, runs the eval, and freezes. The frozen product is returned as an `EvalResult.module` field on the result of the evaluation.

The point of this design is to decouple the Python-side object from any `starlark::environment::Module` lifetime, so that the wrapping stays valid across a wider range of upstream API shapes.

## Coexistence, not replacement

Everything currently in `sl.Module`, `sl.eval`, `sl.eval_with`, and `FrozenModule.call_with` is unchanged. The experiment adds new items alongside:

- `sl.ScopedModule` — the new module class
- `sl.eval_scoped(module, ast, globals, /, file_loader=None) -> EvalResult` — bare entry point
- `sl.eval_scoped_with(options, module, ast, globals, /, file_loader=None) -> EvalResult` — options entry point
- `EvalResult.module: FrozenModule | None` — new field, populated by scoped-path evaluations

Existing tests continue to pass without modification. Both APIs live in the same module namespace and can be used interchangeably in the same session.

## Design

### The pyclass

```rust
struct ScopedModuleState {
    values: HashMap<String, Py<PyAny>>,
    callables: HashMap<String, Py<PyAny>>,
    private_imports: Vec<Py<FrozenModule>>,
    reexports: Vec<Py<FrozenModule>>,
}

#[pyclass]
struct ScopedModule(Mutex<ScopedModuleState>);
```

The state is a plain builder. `Py<T>` is `Send + Sync` in pyo3; `Mutex<HashMap<..., Py<...>>>` inherits that; no `unsafe impl` is needed. The class is genuinely `Send + Sync` without hand-waving.

### The public methods

- `sl.ScopedModule()` — construct, empty state
- `mod[name]` — read pending values or callables; returns `None` if unset. Reads **do not** see values produced by evaluation (those are on `EvalResult.module`)
- `mod[name] = value` — record a value to apply at the next evaluation. Late binding: the Python-to-starlark conversion happens at eval time, not at assignment
- `mod.add_callable(name, callable)` — record a Python callable to be exposed as a Starlark function at eval time
- `mod.import_public_symbols(fmod)` — see below (private/load-like composition)
- `mod.reexport_public_symbols(fmod)` — see below (public carry-forward)
- `mod.freeze() -> FrozenModule` — pure function of pending state. Materializes a fresh underlying module, applies pending, freezes, returns. Does not mutate `mod`.

### The evaluation entry points

Both entry points return an `EvalResult`. The bare form is convenience for callers with no evaluator options to configure:

```python
result = sl.eval_scoped(mod, ast, globals)
result.value            # the script's return value
result.module           # a sl.FrozenModule of the evaluation's end state
```

The options form matches the `_with` grammar from the rest of the module:

```python
opts = sl.EvalOptions(check_cancelled=cb, max_callstack_size=100)
result = sl.eval_scoped_with(opts, mod, ast, globals)
```

For symmetry with the rest of the API surface, `mod` argument is positional-only (via `/` in the pyo3 signature).

### `EvalResult.module`

`EvalResult` gains one field:

```rust
struct EvalResult {
    value: Py<PyAny>,
    module: Option<Py<FrozenModule>>,
}
```

Populated by scoped-path evaluations. `None` for `eval_with` on the existing `sl.Module`. From Python, `result.module` is either a `sl.FrozenModule` or `None`.

The design commitment here is that `EvalResult.module` is a *durable*, *inspectable* product of the evaluation — you can freeze the outputs of one evaluation into a value that survives every subsequent Rust-side lifetime constraint, and pass it into further evaluations as an import.

### Two composition methods, distinct semantics

Composing modules — feeding the frozen product of one evaluation into the next — is the core iterative workflow the scoped design has to support well. There are two distinct semantics an embedder might want, and each maps to a separate named method.

#### `import_public_symbols(fmod)` — private, load-like

Matches Starlark's `load()` semantics. The imported symbols enter the receiving module as **private**: they are usable by scripts running against the module, but they are **not** re-exported by the next `freeze()`. If you carry the receiving module's frozen product to a downstream evaluation, the imported symbols are gone from its public namespace.

Concretely, this delegates to the underlying `starlark::environment::Module::import_public_symbols` method, which internally uses `set_private` on each imported name.

Use case: importing a "utility" module for a one-shot evaluation, where you don't want the utility's symbols to leak into whatever the evaluation itself produces.

#### `reexport_public_symbols(fmod)` — public carry-forward

Symbols enter the receiving module as **public**: they survive the next `freeze()` and remain visible to downstream imports. This is the operation that makes iterative "session" workflows work — each evaluation composes on top of a prior `EvalResult.module`, and every hop in the chain preserves the accumulated namespace.

Implementation: iterates public names from the source frozen module and calls `module.set(name, value)` on the destination — which stores each name as public. Also transfers the frozen-heap reference so the source's frozen values remain valid.

Use case: REPL-style flows where each script builds on the state left behind by the previous one.

#### Why two methods, not one

Using the same name (`import_public_symbols`) for both semantics would be misleading — a user familiar with Starlark's `load()` would reasonably expect the identically-named binding method to have equivalent semantics. Reusing an identical name while silently resolving the ambiguity in the opposite direction (private in Starlark, public here) is a footgun.

The naming `reexport_public_symbols` states the visibility consequence directly. Alternatives considered:

- `carry_forward`: describes the workflow but is silent on visibility
- `merge` or `absorb`: too vague; don't distinguish visibility
- `reexport_public_symbols`: clearest — names both the source (public symbols) and the destination effect (re-export)

Splitting into two methods keeps the load-like operation available under its accurate name and makes the iterative continuation behavior explicit and searchable.

### Grammar

The design follows the same grammar as the `_with`-variant work already in the branch:

- Bare entry points return values (`sl.eval`) or `EvalResult` when the return would carry a durable module (`sl.eval_scoped`, `FrozenModule.call_with`)
- `_with` entry points take options positional-first and return `EvalResult`

The one deviation worth flagging: bare `sl.eval_scoped` returns `EvalResult`, not `object`. Scoped evaluations are *defined* by producing a durable module, so a bare form that hid `.module` would defeat the purpose. The tradeoff is one extra `.value` attribute access at every call site vs. the missing ability to reach the frozen product.

## API reference

### `sl.ScopedModule`

```python
class ScopedModule:
    def __init__(self) -> None: ...
    def __getitem__(self, name: str) -> object: ...
    def __setitem__(self, name: str, value: object) -> None: ...
    def add_callable(self, name: str, callable: Callable[..., object]) -> None: ...
    def import_public_symbols(self, fmod: FrozenModule) -> None: ...
    def reexport_public_symbols(self, fmod: FrozenModule) -> None: ...
    def freeze(self) -> FrozenModule: ...
```

- `__getitem__` returns `None` for keys that were never set. It only sees pending state (values and callables recorded via `__setitem__` / `add_callable`) — not values produced by evaluation. For those, inspect `EvalResult.module`.
- `__setitem__` overwrites any prior pending value or callable of the same name.
- `add_callable` overwrites any prior pending value or callable of the same name.
- `import_public_symbols` and `reexport_public_symbols` append; a module may hold arbitrarily many imports and re-exports. Application order at eval time: private imports first, then re-exports, then values, then callables. Later applications shadow earlier ones on name collision.
- `freeze()` is idempotent: calling it twice on the same `ScopedModule` produces two equivalent `FrozenModule`s.

### `sl.eval_scoped(module, ast, globals, /, file_loader=None) -> EvalResult`

Bare evaluation entry point. Convenience form for calls with no evaluator options to configure. Equivalent to `sl.eval_scoped_with(sl.EvalOptions(), module, ast, globals, file_loader=file_loader)`.

### `sl.eval_scoped_with(options, module, ast, globals, /, file_loader=None) -> EvalResult`

Options-taking evaluation entry point. Materializes a fresh underlying `starlark::environment::Module`, applies the pending operations on `module` (in the order: private imports, re-exports, values, callables), configures the evaluator per `options`, runs `eval_module`, freezes into a `FrozenModule`, and returns an `EvalResult` with `.value` (the return value, converted per the standard object-conversion rules) and `.module` (the frozen product).

`module` is not mutated. A second call to `eval_scoped_with` on the same `module` starts fresh from the same pending state.

### `EvalResult.module`

For `eval_scoped` and `eval_scoped_with`, `result.module` is a `sl.FrozenModule` whose public names are:

- Every symbol the script assigned at module scope, minus symbols with a leading underscore (which are private by default in Starlark)
- Every public re-export accumulated via `reexport_public_symbols` on the source `ScopedModule` (this is why re-exports survive the freeze cycle)
- Every value and callable recorded on the source `ScopedModule` (public unless the name begins with underscore)

For `eval_with` on the existing `sl.Module`, `result.module` is `None`. The old `sl.Module` is mutated in place by `eval_with`; there is no separate "frozen product" of that evaluation, so there is nothing to attach.

## Comparison with `sl.Module`

### API mapping

| Operation | `sl.Module` (current) | `sl.ScopedModule` (new) |
|---|---|---|
| Construct | `sl.Module()` | `sl.ScopedModule()` |
| Set value | `mod[name] = v` | Same |
| Add callable | `mod.add_callable(name, cb)` | Same |
| Evaluate | `sl.eval(mod, ast, glb)` | `sl.eval_scoped(mod, ast, glb)` |
| Evaluate with options | `sl.eval_with(opts, mod, ast, glb).value` | `sl.eval_scoped_with(opts, mod, ast, glb).value` |
| Read post-eval variable | `mod[name]` | No equivalent — use follow-up `eval_scoped` |
| Freeze after eval | `mod.freeze()` | `result.module` from the eval |
| Chain evaluations | Implicit — `sl.eval` mutates `mod` in place | Explicit — `mod2.reexport_public_symbols(r1.module)` |
| Load-like import | `mod.freeze().call(...)` (indirect) | `mod.import_public_symbols(fmod)` (direct) |

### Workflow examples

**Define a function and call it.**

`sl.Module`:
```python
mod = sl.Module()
sl.eval(mod, sl.parse("f.star", "def double(x): return x * 2"), glb)
fmod = mod.freeze()
result = fmod.call("double", 21)
```

`sl.ScopedModule`:
```python
mod = sl.ScopedModule()
r = sl.eval_scoped(mod, sl.parse("f.star", "def double(x): return x * 2"), glb)
result = r.module.call("double", 21)
```

**Set values, evaluate, use return value.**

`sl.Module`:
```python
mod = sl.Module()
mod["a"] = 5
result = sl.eval(mod, sl.parse("s.star", "a * 2"), glb)
# result == 10
```

`sl.ScopedModule`:
```python
mod = sl.ScopedModule()
mod["a"] = 5
result = sl.eval_scoped(mod, sl.parse("s.star", "a * 2"), glb)
# result.value == 10
```

**Chain two evaluations, second using state defined by first.**

`sl.Module` (implicit — `sl.eval` mutates the module):
```python
mod = sl.Module()
sl.eval(mod, sl.parse("s1.star", "x = 100"), glb)
r = sl.eval(mod, sl.parse("s2.star", "x + 1"), glb)
# r == 101
```

`sl.ScopedModule` (explicit chain via re-export):
```python
mod = sl.ScopedModule()
r1 = sl.eval_scoped(mod, sl.parse("s1.star", "x = 100"), glb)

mod2 = sl.ScopedModule()
mod2.reexport_public_symbols(r1.module)
r2 = sl.eval_scoped(mod2, sl.parse("s2.star", "x + 1"), glb)
# r2.value == 101
```

**Inspect a script-defined variable after evaluation.**

`sl.Module` (direct read on the module):
```python
mod = sl.Module()
sl.eval(mod, sl.parse("s.star", "x = 42\ny = 'hello'"), glb)
assert mod["x"] == 42
assert mod["y"] == "hello"
```

`sl.ScopedModule` (follow-up eval that references the frozen chain):
```python
mod = sl.ScopedModule()
r = sl.eval_scoped(mod, sl.parse("s.star", "x = 42\ny = 'hello'"), glb)

# mod["x"] is None — mod only sees pending state.
# Use a follow-up eval to inspect:
inspect_mod = sl.ScopedModule()
inspect_mod.reexport_public_symbols(r.module)
x = sl.eval_scoped(inspect_mod, sl.parse("check.star", "x"), glb).value
# x == 42
```

This is the largest ergonomic loss in the scoped design. Inspection requires a script-level indirection.

## Semantic differences

Ranked by impact on user-visible behavior.

### 1. Multi-eval no longer accumulates

`sl.Module`: state accumulates across `sl.eval` calls on the same module. Eval 2 sees values defined by eval 1.

`sl.ScopedModule`: each evaluation starts fresh from the module's current pending state. Eval 2 does **not** see values defined by eval 1. To reach values from a prior evaluation, use `mod2.reexport_public_symbols(r1.module)` explicitly.

Test: `test_scoped_multi_eval_does_not_accumulate`, `test_module_multi_eval_accumulates`.

### 2. Frozen function globals see origin, not current module

Within a single evaluation, function globals resolve normally in both designs — a `def f(): return x` sees whichever `x` is bound in its home module at call time.

Across evaluations via `reexport_public_symbols`, function globals become **frozen at the origin module**. A `def get_x(): return x` carried forward from eval 1 reads the frozen `x` from eval 1's module, even if eval 2 has reassigned `x` to a different value.

```python
mod1 = sl.ScopedModule()
r1 = sl.eval_scoped(mod1, sl.parse("f1.star",
    "x = 1\ndef get_x(): return x"), glb)

mod2 = sl.ScopedModule()
mod2.reexport_public_symbols(r1.module)
r2 = sl.eval_scoped(mod2, sl.parse("f2.star",
    "x = 999\nget_x()"), glb)
# r2.value == 1  (NOT 999 — frozen get_x reads its origin's frozen x)
```

This is a real semantic shift from what a user of `sl.Module` might expect. The pattern arises naturally in REPL-style chained evaluations. Test: `test_scoped_module_frozen_function_globals_are_stale`.

### 3. `mod[name]` post-eval semantics

`sl.Module.__getitem__`: returns values defined by prior evaluation. `mod["x"]` after a script assigning `x = 42` returns 42.

`sl.ScopedModule.__getitem__`: returns pending values or callables only. `mod["x"]` after the same script returns `None` — the script's assignments are on `result.module`, not on `mod`.

Test: `test_module_read_defined_vars_after_eval`, `test_scoped_read_defined_vars_after_eval`.

### 4. `freeze()` is pure

`sl.Module.freeze()` empties the wrapper — after freeze, `mod["a"]` (for a value set before freeze) returns `None`. The internal Rust module is moved out.

`sl.ScopedModule.freeze()` is a pure function of pending state. Calling it twice produces two equivalent `FrozenModule`s. `mod["a"]` still returns `10` after freeze — the pending state is unchanged.

Test: `test_module_freeze_empties_wrapper`, `test_scoped_freeze_is_pure`.

### 5. Transitive frozen-heap retention

Both `import_public_symbols` and `reexport_public_symbols` add heap references to the source frozen module's heap on the receiving module. Chained imports retain a reference to every prior generation's frozen heap. Values from the origin of a 20-generation chain remain accessible in the final generation.

Test: `test_scoped_module_transitive_import_retention`, `test_scoped_module_deep_import_chain_padding_carries_forward`.

### 6. Namespace growth on `reexport_public_symbols`

Distinct from heap retention. Public re-export makes the receiving module's public namespace grow monotonically across chain hops. Old bindings from every prior link remain externally visible; symbols that were intended as one-hop implementation details will propagate indefinitely; name collisions become likelier with chain length.

With one new public symbol added per generation, cumulative name/slot table metadata can grow ~quadratically across a long chain (each generation copies the accumulated name table into its own module before adding its one new name).

`import_public_symbols` doesn't have this problem — private imports don't propagate through freeze cycles.

## The `sl.session` namespace

An extension of the `Session` pattern, exposed as a first-class submodule namespace whose API surface mirrors the top-level `sl.eval` / `sl.Module` shape. Under the hood, the class delegates to the same ScopedModule + FrozenModule chaining logic; from the outside, users write code that looks like `sl.Module` code with a namespace prefix.

```python
mod = sl.session.Module()
mod["a"] = 5
mod.add_callable("triple", lambda x: x * 3)

# Same call shape as sl.eval(mod, ast, glb):
val = sl.session.eval(mod, sl.parse("s1.star", "b = triple(a)"), glb)
# val is None; side effect: session state now has a, b, triple

# Post-eval reads work like sl.Module:
assert mod["b"] == 15
assert mod["a"] == 5

# Multi-eval chains, like sl.Module:
sl.session.eval(mod, sl.parse("s2.star", "c = b + 1"), glb)
assert mod["c"] == 16

# Freeze and call, like sl.Module.freeze():
fmod = mod.freeze()
assert fmod.call("triple", 7) == 21
```

### API mapping

Every function that takes a `sl.Module` has a parallel that takes a `sl.session.Module`:

| Top-level | Under `sl.session` |
|---|---|
| `sl.Module()` | `sl.session.Module()` |
| `sl.eval(module, ast, globals, file_loader=None) -> object` | `sl.session.eval(module, ast, globals, file_loader=None) -> object` |
| `sl.eval_with(options, module, ast, globals, /, file_loader=None) -> EvalResult` | `sl.session.eval_with(options, module, ast, globals, /, file_loader=None) -> EvalResult` |
| `mod[name]` (read post-eval variable) | Same |
| `mod[name] = v` | Same |
| `mod.add_callable(name, cb)` | Same |
| `mod.freeze()` | Same |

Signatures are identical position-by-position and name-by-name. The only difference is the type of `module`: `sl.Module` at the top level, `sl.session.Module` under the namespace.

### The one call-shape gap

You cannot pass a `sl.session.Module` to top-level `sl.eval()` or `sl.eval_with()`, and vice versa — they're different types. If a caller wants to use the parallel namespace, all references to eval and Module must be prefixed with `sl.session.`.

### FrozenModule protocols

`FrozenModule` gains two Python protocols that support the session read pattern (`mod[name]` looking through to the frozen product) and are useful independently:

- **`FrozenModule.__contains__(name) -> bool`** — is `name` a public symbol?
- **`FrozenModule.__getitem__(name) -> object`** — get the value, raising `KeyError` if missing or `StarlarkError` if not Python-convertible (matches the semantics of `sl.Module.__getitem__`).

Both are natural Python protocols. They also make `sl.FrozenModule` behave like a read-only mapping for its public symbols:

```python
if "x" in fmod:
    val = fmod["x"]
```

### Inherent semantic differences

The session namespace hides most of the ScopedModule semantics, but three differences from `sl.Module` remain visible because they come from the underlying Starlark value model:

1. **Frozen closure globals across evaluations.** A `def f(): return x` defined in eval 1 reads eval 1's frozen `x`, not eval 2's reassigned `x`. Under `sl.Module` the function sees the mutated module slot. Verified in `test_session_module_frozen_function_globals_across_evals`.
2. **`freeze()` doesn't empty the wrapper.** `sl.Module.freeze()` moves state out; `sl.session.Module.freeze()` is a pure function of session state. Session state remains intact after freeze.
3. **Type incompatibility.** `sl.session.Module` is not an `sl.Module`; call sites for `sl.eval` and `sl.eval_with` require the top-level type.

Everything else — accumulating state, post-eval variable reads, FileLoader integration, callable registration, freezing to a callable FrozenModule — behaves identically.

### Implementation

`sl.session` is defined in pure Python at ``python/session.py`` as a class-used-as-namespace with a nested ``Module`` class and static ``eval`` / ``eval_with`` methods. At extension import time, ``src/lib.rs`` reads that Python source (via ``include_str!`` so the source is embedded in the compiled ``.so``), executes it with the underlying scoped-module primitives (``ScopedModule``, ``eval_scoped``, ``eval_scoped_with``) pre-populated in globals, extracts the resulting ``session`` class, and attaches it to the ``starlark`` module.

The wrapper's ``Module`` stores ``{pending_values, pending_callables, last_frozen}`` as plain Python dicts and an optional ``FrozenModule`` reference. On each eval:

1. Build a fresh ``sl.ScopedModule`` from the pending state:
    a. If ``last_frozen`` exists, ``reexport_public_symbols(last_frozen)``
    b. Apply pending values via ``scoped[name] = v``
    c. Apply pending callables via ``scoped.add_callable(name, cb)``
2. Call ``sl.eval_scoped(scoped, ast, globals, file_loader)`` (or ``sl.eval_scoped_with(...)``)
3. Store the resulting ``FrozenModule`` as the new ``last_frozen``, clear pending
4. Return the value (or the ``EvalResult``)

Reads via ``__getitem__`` check pending values, then pending callables, then look through to ``last_frozen`` via ``FrozenModule.__contains__`` / ``__getitem__``.

Because it's a class-as-namespace and not a real Python submodule, ``import starlark.session`` doesn't work — only ``from starlark import session`` (which returns the class). Users who write ``sl.session.eval(...)`` see identical behavior to a submodule; only the import syntax is affected.

Total Rust footprint for this feature: ~15 lines in the pymodule init (read the file, execute with globals, attach the class). All the wrapper logic lives in ~50 lines of Python. The Rust primitives (``ScopedModule``, ``eval_scoped``, ``eval_scoped_with``, ``FrozenModule.__contains__``, ``FrozenModule.__getitem__``) are shared with the base experiment.

## The pure-Python `Session` prototype

This was the pre-`sl.session` sketch: a pure-Python convenience wrapper that made the iterative REPL-style flow feel like `sl.Module`'s implicit accumulation, at the cost of the semantic caveats catalogued above. The `sl.session` namespace above is essentially this pattern promoted into the binding as a first-class Rust `#[pyclass]` with matching top-level function signatures. The Python sketch is kept in the tests as a reference and to show that any user could write the same wrapper without binding support:

```python
class Session:
    def __init__(self, globals):
        self._globals = globals
        self._frozen = None

    def eval(self, ast, options=None):
        mod = sl.ScopedModule()
        if self._frozen is not None:
            mod.reexport_public_symbols(self._frozen)
        opts = options or sl.EvalOptions()
        result = sl.eval_scoped_with(opts, mod, ast, self._globals)
        self._frozen = result.module
        return result
```

Usage:

```python
session = Session(sl.Globals.standard())
session.eval(sl.parse("s1.star", "x = 10"))
session.eval(sl.parse("s2.star", "y = x + 5"))
r = session.eval(sl.parse("s3.star", "x + y"))
# r.value == 25
```

This is 14 lines of user-space Python. It doesn't need to be a binding primitive — it's a composition of `sl.ScopedModule` + `sl.eval_scoped_with`. Whether to promote it into the binding as `sl.Session` is an open question. Ergonomically it's nice; it hides the explicit chain wiring and lets a user think in terms of "steps in a session" rather than "modules and imports." It also inherits every semantic caveat of `reexport_public_symbols` (frozen function globals, namespace growth, transitive heap retention).

Test: `test_session_wrapper_chains_evals`, `test_session_wrapper_frozen_globals_staleness`.

## Implementation notes

### Coexistence

Nothing in the existing `sl.Module`, `sl.eval`, `sl.eval_with`, `sl.FrozenModule.call`, or `sl.FrozenModule.call_with` code changes. All existing tests continue to pass unchanged. The added Rust surface is entirely in a new `// {{{ ScopedModule (Direction A experiment)` region of `src/lib.rs`, plus one field added to `EvalResult` and one method (`eval_scoped_with` + `eval_scoped`) added to the eval section.

### The internal `apply_pending_ops` helper

All materialization goes through one helper:

```rust
fn apply_pending_ops(
    py: Python<'_>,
    module: &starlark::environment::Module,
    state: &ScopedModuleState,
) -> PyResult<()> {
    // 1. Private imports (upstream-style)
    // 2. Public re-exports
    // 3. Pending values
    // 4. Pending callables
    ...
}
```

The helper is called from both `ScopedModule.freeze()` and `eval_scoped_with`. Application order matters for shadowing: names set by later steps override names set by earlier steps.

### `unsafe impl Send/Sync` not needed

Every field of `ScopedModuleState` is `Send + Sync` (Rust's `HashMap` + `Vec` inherit the property from their type parameters; pyo3's `Py<T>` is `Send + Sync`). The class is genuinely `Send + Sync`. This removes one hand-rolled `unsafe` compared to the existing `sl.Module` wrapping.

### Late binding

Values set via `mod[name] = v` are stored as `Py<PyAny>` — no Python-to-starlark conversion happens at assignment time. Conversion runs during `apply_pending_ops`, at eval time, in the freshly-materialized module's heap. This means:

- Assignment is fast (no work done)
- Errors from unconvertible values show up at eval time, not at assignment time
- The same `ScopedModule` can be evaluated multiple times with the same pending values, and each evaluation gets its own converted values in its own heap

### Chain memory characteristics

Every `reexport_public_symbols(fmod)` call causes the receiving module's `frozen_heap` to `add_reference(fmod.frozen_heap())`. When the receiving module is later frozen and re-exported downstream, the downstream module's heap in turn references the receiving module's heap — including the transitive reference to the original source's heap. Heap references form a chain.

The starlark crate at the pinned revision provides no APIs to introspect the retained-byte size of chained heap references. Tests in this experiment structurally verify the reference chain exists (origin values remain accessible after 20 chained imports) but cannot measure the retained byte count.

## Open questions and areas not explored

- **Performance.** Every scoped evaluation pays the cost of materializing a fresh underlying module and applying pending operations. No benchmarks yet. Probably negligible for one-shot evaluations, may matter for hot loops.
- **Concurrency.** The `Mutex<ScopedModuleState>` around the state is honest; contention behavior under free-threaded builds or heavy concurrent use is untested.
- **Retention observability.** Whether the transitive heap-chain retention (§5) grows to problematic sizes in practice depends on values retained by intermediate frozen heaps; no way to measure at the pinned starlark rev.
- **Selective re-export.** `reexport_public_symbols(fmod)` is whole-namespace. A selective form `reexport_public_symbols(fmod, names=["a", "b"])` would let users carry only what they need through a long chain — mitigating the namespace-growth problem. Not implemented.
- **`Session` productization.** The 14-line Python wrapper feels clean. Whether to promote it into the binding as `sl.Session` is a separate design decision.
- **`ScopedModule.freeze()` without any eval.** Currently produces a `FrozenModule` reflecting just the pending state (no computed values). Use case unclear from the tests. Could remove to simplify the API.

## Tests

Three test files exercise the new API:

- `test/test_scoped_module.py` — 19 tests focused on `sl.ScopedModule` alone. Covers basic construction, pending values and callables, eval-then-call, iterative chaining via `reexport_public_symbols`, the private-vs-public distinction between `import` and `reexport`, the transitive heap retention structure, FileLoader integration, the `Session` convenience wrapper, and the frozen function globals semantic
- `test/test_direction_a_comparison.py` — 19 tests running the same conceptual workflow against both `sl.Module` and `sl.ScopedModule`, one pair per workflow. Test names use the prefix `test_module_` and `test_scoped_` for easy visual pairing
- `test/test_starlark.py` — the existing test suite, unchanged. Continues to pass, demonstrating that the experiment coexists cleanly

79 tests total pass (18 decimal + 19 scoped-only + 19 comparison + 23 existing starlark). All existing CI checks (ruff, typos, basedpyright, mypy stubtest, Sphinx, examples) continue to pass.
