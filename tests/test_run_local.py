import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


spec = importlib.util.spec_from_file_location("run_local", Path(__file__).resolve().parents[1] / "scripts/run-local.py")
run_local = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_local)


@pytest.mark.parametrize("requested,available,expected", [
    ("auto", True, "cuda"),
    ("auto", False, "cpu"),
    ("cuda", True, "cuda"),
])
def test_selects_available_ocr_device(monkeypatch, requested, available, expected):
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: available)))
    assert run_local.resolve_ocr_device(requested) == expected


def test_explicit_cpu_does_not_import_torch(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)
    assert run_local.resolve_ocr_device("cpu") == "cpu"


def test_auto_without_pytorch_falls_back_to_cpu_but_explicit_cuda_fails(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)
    assert run_local.resolve_ocr_device("auto") == "cpu"
    with pytest.raises(RuntimeError, match="PyTorch is missing"):
        run_local.resolve_ocr_device("cuda")


def test_explicit_cuda_fails_when_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    with pytest.raises(RuntimeError, match="cannot access CUDA"):
        run_local.resolve_ocr_device("cuda")
