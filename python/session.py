"""The starlark.session namespace — sl.Module-like ergonomics backed by
sl.ScopedModule internals.

This module is executed from Rust at extension import time, with
``ScopedModule``, ``eval_scoped``, and ``eval_scoped_with`` pre-populated
in globals. It defines a class-as-namespace `session` and attaches it to
the ``starlark`` extension module.

Kept in Python (not Rust) to make the experiment easier to iterate on:
adjusting the wrapper doesn't require a Rust rebuild. See
``doc/experiments/scoped-module.md``.
"""


class session:  # noqa: N801 -- class used as a Python namespace
    """Namespace holding the sl.Module-like API surface for scoped-module
    session workflows.

    Access as :class:`sl.session.Module`, :func:`sl.session.eval`, and
    :func:`sl.session.eval_with`. This is a class used as a namespace; don't
    instantiate it directly.
    """

    class Module:
        """A module wrapper providing :class:`sl.Module`-like ergonomics on top
        of the scope-safe internals of :class:`sl.ScopedModule`.

        Instances hold pending values, callables, and the frozen product of
        the most recent evaluation. Each :func:`sl.session.eval` materializes a
        fresh scoped module with pending applied over the prior frozen state,
        runs the eval, and updates the session's frozen state.

        See ``doc/experiments/scoped-module.md`` for the semantic differences
        from :class:`sl.Module` that are inherent to the scoped-module
        backing.
        """

        def __init__(self):
            self._pending_values = {}
            self._pending_callables = {}
            self._last_frozen = None

        def __getitem__(self, name):
            if name in self._pending_values:
                return self._pending_values[name]
            if name in self._pending_callables:
                return self._pending_callables[name]
            if self._last_frozen is not None and name in self._last_frozen:
                return self._last_frozen[name]
            return None

        def __setitem__(self, name, value):
            self._pending_callables.pop(name, None)
            self._pending_values[name] = value

        def add_callable(self, name, callable):
            self._pending_values.pop(name, None)
            self._pending_callables[name] = callable

        def freeze(self):
            return self._materialize().freeze()

        def _materialize(self):
            # ScopedModule, eval_scoped, eval_scoped_with come from module globals
            # injected by the Rust loader.
            scoped = ScopedModule()  # noqa: F821 -- injected global
            if self._last_frozen is not None:
                scoped.reexport_public_symbols(self._last_frozen)
            for name, v in self._pending_values.items():
                scoped[name] = v
            for name, cb in self._pending_callables.items():
                scoped.add_callable(name, cb)
            return scoped

    @staticmethod
    def eval(module, ast, globals, file_loader=None):
        """Evaluate *ast* against *module* (a :class:`sl.session.Module`).

        Mirrors the top-level :func:`sl.eval`: returns the value produced by
        the evaluation, and updates *module*'s internal state.
        """
        scoped = module._materialize()
        result = eval_scoped(scoped, ast, globals, file_loader)  # noqa: F821
        module._last_frozen = result.module
        module._pending_values.clear()
        module._pending_callables.clear()
        return result.value

    @staticmethod
    def eval_with(options, module, ast, globals, /, file_loader=None):
        """Like :func:`sl.session.eval`, but takes an :class:`sl.EvalOptions`
        bundle and returns an :class:`sl.EvalResult`. Mirrors the top-level
        :func:`sl.eval_with`.
        """
        scoped = module._materialize()
        result = eval_scoped_with(  # noqa: F821
            options, scoped, ast, globals, file_loader
        )
        module._last_frozen = result.module
        module._pending_values.clear()
        module._pending_callables.clear()
        return result
