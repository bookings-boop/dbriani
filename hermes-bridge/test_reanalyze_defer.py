#!/usr/bin/env python3
"""Fix C — F2 cooldown deferred re-enqueue (2026-06-14).
A 2nd inbound inside the cooldown window must DEFER (not drop) and re-surface
exactly once on the next drain after the window. Throttle preserved.
Run: python3 hermes-bridge/test_reanalyze_defer.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import routes  # noqa: E402


class FakeRedis:
    def __init__(self):
        self.kv = {}; self.lists = {}; self.now = 1000.0
    def advance(self, s): self.now += s
    def _live(self, k):
        v = self.kv.get(k)
        if v is None: return None
        val, exp = v
        if exp is not None and exp <= self.now:
            del self.kv[k]; return None
        return val
    def __call__(self, args, timeout=8):
        c = args[0].upper()
        if c == "SET":
            k, val = args[1], args[2]; nx = "NX" in args
            ex = float(args[args.index("EX")+1]) if "EX" in args else None
            if nx and self._live(k) is not None: return ("", None)
            self.kv[k] = (val, (self.now+ex) if ex else None); return ("OK", None)
        if c == "RPUSH": self.lists.setdefault(args[1], []).append(args[2]); return ("1", None)
        if c == "LTRIM": return ("OK", None)
        if c == "LPOP":
            k = args[1]; n = int(args[2]) if len(args)>2 else 1
            lst = self.lists.get(k, []); p, self.lists[k] = lst[:n], lst[n:]
            return ("\n".join(p), None)
        if c == "DEL":
            for k in args[1:]: self.kv.pop(k, None)
            return ("1", None)
        if c == "EXISTS": return ("1" if self._live(args[1]) is not None else "0", None)
        if c == "SADD":
            cur = set(self.lists.get(args[1], []))
            for m in args[2:]: cur.add(m)
            self.lists[args[1]] = list(cur); return ("1", None)
        if c == "SMEMBERS": return ("\n".join(self.lists.get(args[1], [])), None)
        if c == "SREM":
            self.lists[args[1]] = [m for m in self.lists.get(args[1], []) if m not in args[2:]]
            return ("1", None)
        raise AssertionError(f"unhandled redis cmd: {args}")


def _setup():
    fr = FakeRedis(); routes._redis = fr
    os.environ["REANALYZE_DEFER_ENABLED"] = "1"
    os.environ["REANALYZE_DEDUP_TTL"] = "1800"
    return fr


def test_second_enqueue_in_cooldown_is_deferred_not_dropped():
    fr = _setup(); routes._enqueue_reanalyze("eva@c.us")
    assert fr.lists.get(routes._REANALYZE_QUEUE) == ["eva@c.us"]
    routes._enqueue_reanalyze("eva@c.us")
    assert fr.lists.get(routes._REANALYZE_QUEUE) == ["eva@c.us"], "no double-push"
    assert "eva@c.us" in fr.lists.get(routes._REANALYZE_PENDING, []), "must defer"


def test_deferred_does_NOT_surface_before_window():
    fr = _setup(); routes._enqueue_reanalyze("eva@c.us")
    fr.lists[routes._REANALYZE_QUEUE] = []; routes._enqueue_reanalyze("eva@c.us")
    fr.advance(60); routes._promote_deferred_reanalyze(12)
    assert fr.lists.get(routes._REANALYZE_QUEUE) == [], "must not surface early"
    assert "eva@c.us" in fr.lists.get(routes._REANALYZE_PENDING, [])


def test_deferred_surfaces_on_next_drain_after_window():
    fr = _setup(); routes._enqueue_reanalyze("eva@c.us")
    fr.lists[routes._REANALYZE_QUEUE] = []; routes._enqueue_reanalyze("eva@c.us")
    fr.advance(1801); routes._promote_deferred_reanalyze(12)
    assert fr.lists.get(routes._REANALYZE_QUEUE) == ["eva@c.us"], "must surface"


def test_deferred_surfaces_exactly_once():
    fr = _setup(); routes._enqueue_reanalyze("eva@c.us")
    fr.lists[routes._REANALYZE_QUEUE] = []; routes._enqueue_reanalyze("eva@c.us")
    fr.advance(1801); routes._promote_deferred_reanalyze(12)
    fr.lists[routes._REANALYZE_QUEUE] = []; routes._promote_deferred_reanalyze(12)
    assert fr.lists.get(routes._REANALYZE_QUEUE) == [], "exactly once"


def test_flag_off_is_old_behavior_drop():
    fr = _setup(); os.environ["REANALYZE_DEFER_ENABLED"] = "0"
    routes._enqueue_reanalyze("eva@c.us"); routes._enqueue_reanalyze("eva@c.us")
    assert fr.lists.get(routes._REANALYZE_PENDING, []) == [], "flag off: no defer"
    assert fr.lists.get(routes._REANALYZE_QUEUE) == ["eva@c.us"]


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try: fn(); print("PASS", fn.__name__)
        except AssertionError as e: failed += 1; print("FAIL", fn.__name__, "-", e)
        except Exception as e: failed += 1; print("ERROR", fn.__name__, "-", repr(e))
    print(f"{len(fns)-failed}/{len(fns)} passed"); sys.exit(1 if failed else 0)
