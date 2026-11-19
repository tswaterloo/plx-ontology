"""Loaded by every Python child of a guarded eval (``scripts/eval_guard.py`` puts this
folder first on ``PYTHONPATH``): with ``PLX_EVAL_GUARD=1`` the child gets the same write
wall over the live state as its parent. Then the sitecustomize this one shadows runs."""
import importlib.util as _util
import os as _os
import sys as _sys

if _os.environ.get("PLX_EVAL_GUARD") == "1":
    _path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                          "eval_guard.py")
    try:
        _spec = _util.spec_from_file_location("_plx_eval_guard", _path)
        _mod = _util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _mod.install_from_env()
    except BaseException as _exc:  # noqa: BLE001 - Python would warn and run on unguarded
        _sys.stderr.write(f"eval_guard: refused: the write wall did not install ({_exc!r})\n")
        _os._exit(2)

_here = _os.path.dirname(_os.path.abspath(__file__))
for _dir in _sys.path:
    if not _dir or _os.path.abspath(_dir) == _here:
        continue
    _next = _os.path.join(_dir, "sitecustomize.py")
    if _os.path.isfile(_next):
        _spec = _util.spec_from_file_location("_plx_next_sitecustomize", _next)
        try:
            _spec.loader.exec_module(_util.module_from_spec(_spec))
        except Exception:  # noqa: BLE001 - the shadowed hook is the system's, never ours
            pass
        break
