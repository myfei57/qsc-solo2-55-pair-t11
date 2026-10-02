"""极小 pytest 兼容运行时：approx / raises / mark，供无 pytest 环境跑测试。"""


class Approx:
    def __init__(self, expected, rel=1e-6, abs=1e-6):
        self.expected = expected
        self.rel = rel
        self.abs = abs

    def __eq__(self, actual):
        return abs(actual - self.expected) <= max(self.rel * abs(self.expected), self.abs)

    def __ne__(self, actual):
        return not self.__eq__(actual)

    def __repr__(self):
        return f"approx({self.expected})"


def approx(expected, rel=1e-6, abs=1e-6):
    return Approx(expected, rel, abs)


class raises:
    def __init__(self, exc):
        self.exc = exc
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f"期望抛出 {self.exc.__name__}，但没有抛出")
        self.value = exc
        return issubclass(exc_type, self.exc)


class _Mark:
    def __getattr__(self, name):
        def deco(obj=None, *a, **k):
            if obj is None:
                return lambda o: o
            return obj
        return deco

    def parametrize(self, *a, **k):
        def deco(func):
            return func
        return deco

    def skipif(self, condition, reason=""):
        def deco(func):
            return func
        return deco


mark = _Mark()
fixture = lambda *a, **k: (lambda f: f)


class MonkeyPatch:
    def __init__(self):
        self._undo = []

    def setattr(self, target, name=None, value=None):
        if isinstance(target, str):
            module, _, attr = target.rsplit(".", 1)
            obj = __import__(module, fromlist=[attr])
        else:
            obj, attr = target, name
        old = getattr(obj, attr)
        setattr(obj, attr, value)
        self._undo.append((obj, attr, old))

    def undo(self):
        for obj, attr, old in reversed(self._undo):
            setattr(obj, attr, old)
        self._undo.clear()
