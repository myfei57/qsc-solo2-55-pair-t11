#!/usr/bin/env python3
"""无 pytest 环境下的测试运行器：发现 tests/test_*.py 并按 fixture 约定执行。"""

import importlib.util
import inspect
import sys
import tempfile
import traceback
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import pytest_shim  # noqa: E402

sys.modules["pytest"] = pytest_shim


class CapSys:
    def __init__(self):
        self.buf_out = StringIO()
        self.buf_err = StringIO()
        self._old_out = None
        self._old_err = None

    def __enter__(self):
        self._old_out = sys.stdout
        self._old_err = sys.stderr
        sys.stdout = self.buf_out
        sys.stderr = self.buf_err
        return self

    def __exit__(self, *exc):
        sys.stdout = self._old_out
        sys.stderr = self._old_err

    def readouterr(self):
        class R:
            def __init__(s, out, err):
                s.out = out
                s.err = err
        return R(self.buf_out.getvalue(), self.buf_err.getvalue())


class TmpPath:
    def __init__(self):
        self.path = Path(tempfile.mkdtemp())

    def __truediv__(self, other):
        return self.path / other

    def __str__(self):
        return str(self.path)


def collect(module):
    return [
        (name, obj)
        for name, obj in vars(module).items()
        if name.startswith("test_") and callable(obj)
    ]


def build_kwargs(params):
    from cspplant.app import build_plant
    from cspplant.clock import ManualClock
    from cspplant.config import default_config

    kwargs = {}
    patches = []
    tmp = None
    capsys = None
    if "clock" in params:
        kwargs["clock"] = ManualClock()
    if "platform" in params:
        tmp = TmpPath()
        kwargs["platform"] = build_plant(
            default_config(),
            clock=kwargs.get("clock") or ManualClock(),
            storage_root=str(tmp / "state"),
        )
    if "online" in params:
        tmp = tmp or TmpPath()
        online = build_plant(
            default_config(),
            clock=kwargs.get("clock") or ManualClock(),
            storage_root=str(tmp / "state"),
        )
        online.start()
        online.track(800.0, 720.0)
        online.circulate()
        online.dispatch(100.0, "fixture")
        kwargs["online"] = online
    if "tmp_path" in params:
        kwargs["tmp_path"] = tmp or TmpPath()
    if "monkeypatch" in params:
        mp = pytest_shim.MonkeyPatch()
        patches.append(mp)
        kwargs["monkeypatch"] = mp
    if "capsys" in params:
        capsys = CapSys()
        kwargs["capsys"] = capsys
    return kwargs, patches, capsys


def main() -> int:
    files = [Path(a) for a in sys.argv[1:]]
    if not files:
        files = sorted((ROOT / "tests").glob("test_*.py"))

    total = passed = 0
    failures = []
    for path in files:
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name, func in collect(module):
            total += 1
            kwargs, patches, capsys = build_kwargs(list(inspect.signature(func).parameters))
            try:
                if capsys is not None:
                    with capsys:
                        func(**kwargs)
                else:
                    func(**kwargs)
                passed += 1
            except Exception:
                failures.append((path.name, name, traceback.format_exc()))
            finally:
                for mp in patches:
                    mp.undo()

    for file, name, tb in failures:
        print(f"FAIL {file}::{name}\n{tb}", file=sys.stderr)
    print(f"{passed}/{total} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
