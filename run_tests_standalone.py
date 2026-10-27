"""Fallback runner for machines without pytest. On your VM use:  pytest -v"""
import importlib, pkgutil, sys, traceback
import tests
passed = failed = 0
for m in pkgutil.iter_modules(tests.__path__):
    if not m.name.startswith("test_"):
        continue
    mod = importlib.import_module(f"tests.{m.name}")
    for name in sorted(dir(mod)):
        if name.startswith("test_"):
            try:
                getattr(mod, name)(); passed += 1; print(f"PASS {m.name}::{name}")
            except Exception:
                failed += 1; print(f"FAIL {m.name}::{name}"); traceback.print_exc()
print(f"\n{passed} passed, {failed} failed"); sys.exit(1 if failed else 0)
