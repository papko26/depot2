"""Project hooks: the one way a project's own checks reach depot2.

depot2 knows nothing about the project it plans and merges. Whatever is specific to that project (how its smoke
check's output is read, what to report after a merge, which test owns a snapshot file, extra context for a team
round) comes from plugins: Python modules named in the config's `plugins` list, each a dotted module name on the
path or a path to a `.py` file. A plugin defines any of the functions below; depot2 calls them in the order the
plugins are listed. A plugin that defines none of them is loaded and ignored.

    smoke_verdict(code, out)        -> (ok, summary) or None
        The merge queue's smoke check (`merge.smoke`, after the deploy) exited with `code` (None: it timed out)
        and printed `out`. The first plugin that returns a pair decides; with none, exit 0 passes.
    after_merge(cfg, entry)         -> a dict {"line": str, ...} or None
        The merge queue's last step, after a merge and its checks: anything the owner should see beside the
        merge (minutes left, storage, a link). Every plugin's dict is kept in the entry's `usage`.
    snapshot_test(path)             -> a test file (str) or None
        `merge.auto.snapshots`: the test whose snapshot file `path` is, so its `{test}` placeholder can be filled.
    round_context(cfg, kind)        -> [(title, text), ...] or None
        Extra parts for a team round's bundle (`prep`), after the depot's own.

Plugins run in the depot's process with its rights: they are the project's code, reviewed like it.
"""

import importlib
import importlib.util
import os

HOOKS = ("smoke_verdict", "after_merge", "snapshot_test", "round_context")
_cache = {}


class HookError(RuntimeError):
    pass


def _load_one(spec):
    if spec in _cache:
        return _cache[spec]
    if spec.endswith(".py") or os.sep in spec:
        path = os.path.abspath(os.path.expanduser(spec))
        if not os.path.isfile(path):
            raise HookError("plugin %s: no such file" % spec)
        name = "depot2_plugin_%s" % os.path.splitext(os.path.basename(path))[0]
        mspec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(mspec)
        mspec.loader.exec_module(mod)
    else:
        try:
            mod = importlib.import_module(spec)
        except ImportError as e:
            raise HookError("plugin %s: %s" % (spec, e))
    _cache[spec] = mod
    return mod


def plugins(cfg):
    """The config's plugins, loaded (once per process each)."""
    out = []
    for spec in (cfg or {}).get("plugins") or []:
        if not isinstance(spec, str) or not spec:
            raise HookError("plugins: every entry is a module name or a .py path, got %r" % (spec,))
        out.append(_load_one(spec))
    return out


def call_all(cfg, name, *args, **kw):
    """Every plugin's answer to hook `name` that is not None, in the plugins' order."""
    if name not in HOOKS:
        raise HookError("unknown hook %s (known: %s)" % (name, ", ".join(HOOKS)))
    out = []
    for mod in plugins(cfg):
        fn = getattr(mod, name, None)
        if fn is None:
            continue
        res = fn(*args, **kw)
        if res is not None:
            out.append(res)
    return out


def call_first(cfg, name, *args, **kw):
    """The first plugin's answer to hook `name` that is not None (None when no plugin answers)."""
    res = call_all(cfg, name, *args, **kw)
    return res[0] if res else None


def has(cfg, name):
    return any(getattr(m, name, None) is not None for m in plugins(cfg))
