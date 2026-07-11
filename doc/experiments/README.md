# Experiments

This directory holds experimental designs and prototypes. Documents here describe work that lives on a specific branch and is not intended for release as-is. They are checked into the branch for future reference and for anyone reviewing the experiment to have a self-contained writeup.

## Contents

- **[`scoped-module.md`](scoped-module.md)** — an alternative wrapping for `sl.Module` (branch `direction-a-experiment`). Decouples the Python-side object from any live `starlark::environment::Module` lifetime; adds `sl.ScopedModule`, `sl.eval_scoped`, `sl.eval_scoped_with`, and `EvalResult.module`. Coexists with the existing `sl.Module`.
