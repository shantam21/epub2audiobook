"""Worker sizing and the worker-side render contract."""

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from epub2audiobook import render
from epub2audiobook.config import SAMPLE_RATE


class TestWorkerSizing:
    def test_never_returns_zero(self, monkeypatch):
        monkeypatch.setattr(render.os, "cpu_count", lambda: 1)
        monkeypatch.setattr(render, "memory_budget_gb", lambda: 0.1)
        assert render.default_workers() == 1

    def test_is_capped_by_free_ram_not_just_cores(self, monkeypatch):
        monkeypatch.setattr(render.os, "cpu_count", lambda: 32)
        monkeypatch.setattr(render, "memory_budget_gb", lambda: 1.6)
        # 32 logical cores would suggest plenty, but only ~1 worker fits in RAM.
        assert render.default_workers() == 1

    def test_uses_cores_when_ram_is_plentiful(self, monkeypatch):
        monkeypatch.setattr(render.os, "cpu_count", lambda: 12)
        monkeypatch.setattr(render, "memory_budget_gb", lambda: 32.0)
        assert render.default_workers() == 3

    def test_has_an_upper_bound(self, monkeypatch):
        monkeypatch.setattr(render.os, "cpu_count", lambda: 128)
        monkeypatch.setattr(render, "memory_budget_gb", lambda: 256.0)
        assert render.default_workers() == 4

    def test_available_gb_returns_something_usable(self):
        assert render._available_gb() > 0
        assert render._total_gb() > 0

    def test_budget_falls_back_to_a_share_of_total_ram(self, monkeypatch):
        """Free RAM alone is too pessimistic -- most of what looks used is
        reclaimable cache, and two workers ran fine on a box showing 1.4 GB free."""
        monkeypatch.setattr(render, "_available_gb", lambda: 0.5)
        monkeypatch.setattr(render, "_total_gb", lambda: 16.0)
        assert render.memory_budget_gb() == pytest.approx(16.0 * render.BUDGET_FRACTION)

    def test_a_small_busy_machine_still_gets_two_workers(self, monkeypatch):
        """The real case this was tuned on: 9.8 GB total, 1.3 GB free, 6 physical
        cores. Two workers measured 2.2x faster with no swapping."""
        monkeypatch.setattr(render.os, "cpu_count", lambda: 12)
        monkeypatch.setattr(render, "_available_gb", lambda: 1.3)
        monkeypatch.setattr(render, "_total_gb", lambda: 9.8)
        assert render.default_workers() == 2

    def test_budget_uses_free_ram_when_it_is_larger(self, monkeypatch):
        monkeypatch.setattr(render, "_available_gb", lambda: 12.0)
        monkeypatch.setattr(render, "_total_gb", lambda: 16.0)
        assert render.memory_budget_gb() == 12.0


class TestThreadsPerWorker:
    def test_splits_physical_cores_across_workers(self, monkeypatch):
        monkeypatch.setattr(render.os, "cpu_count", lambda: 12)  # 6 physical
        assert render.threads_per_worker(1) == 6
        assert render.threads_per_worker(3) == 2

    def test_never_returns_zero(self, monkeypatch):
        monkeypatch.setattr(render.os, "cpu_count", lambda: 2)
        assert render.threads_per_worker(8) == 1


class TestRenderChunk:
    def test_returns_errors_as_data_rather_than_raising(self, tmp_path, monkeypatch):
        """A worker that raises would poison the pool; failures must come back
        as a result the parent can record against that one chunk."""
        monkeypatch.setattr(render, "_ENGINE", None)
        job = render.ChunkJob(0, 0, "hello", tmp_path / "x.wav")
        result = render.render_chunk(job)
        assert result.error is not None
        assert result.chapter_idx == 0 and result.chunk_idx == 0
        assert result.path is None

    def test_returns_path_and_duration_on_success(self, tmp_path, monkeypatch):
        class FakeEngine:
            def synthesize_to_file(self, text, path):
                path.parent.mkdir(parents=True, exist_ok=True)
                sf.write(path, np.zeros(SAMPLE_RATE // 2, np.float32), SAMPLE_RATE)
                return 0.5

        monkeypatch.setattr(render, "_ENGINE", FakeEngine())
        out = tmp_path / "ch0000" / "00003.wav"
        result = render.render_chunk(render.ChunkJob(0, 3, "hello", out))

        assert result.error is None
        assert result.chunk_idx == 3
        assert Path(result.path) == out and out.exists()
        assert result.duration == pytest.approx(0.5)


class TestJobIsPicklable:
    def test_chunk_job_survives_a_round_trip(self, tmp_path):
        """Jobs cross a process boundary, so they must pickle cleanly."""
        import pickle

        job = render.ChunkJob(1, 2, "some text", tmp_path / "a.wav")
        back = pickle.loads(pickle.dumps(job))
        assert (back.chapter_idx, back.chunk_idx, back.text) == (1, 2, "some text")
        assert back.path == job.path

    def test_chunk_result_survives_a_round_trip(self):
        import pickle

        r = render.ChunkResult(1, 2, "p.wav", 1.5, None)
        assert pickle.loads(pickle.dumps(r)) == r


class TestNoWorkerRecycling:
    """`max_tasks_per_child` deadlocks on Python 3.12 / Windows / spawn.

    Because the pool spreads work evenly, every worker reaches the limit on the
    same chunk, so the pool tries to replace all of them at once and hangs. It
    stalled a real 941-chunk book at exactly chunk 300 (2 workers x 150). A
    30-line reproduction with time.sleep as the payload hangs 3 runs out of 3.
    """

    def test_the_pool_is_built_without_it(self):
        import inspect

        from epub2audiobook import cli

        source = inspect.getsource(cli._render)
        # Comments are allowed to name it -- there is one explaining why it is
        # absent. Only real code counts.
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        assert "ProcessPoolExecutor" in code, "test is watching the wrong function"
        assert "max_tasks_per_child" not in code, (
            "max_tasks_per_child deadlocks on Windows/spawn -- see render.py"
        )

    def test_the_constant_is_gone(self):
        from epub2audiobook import render

        assert not hasattr(render, "MAX_TASKS_PER_WORKER")


class TestDeviceDetection:
    """Kokoro picks CUDA on its own when torch reports it. These cover what we
    report about that -- especially the silent case: an NVIDIA machine running
    a CPU-only PyTorch, which is what `pip install torch` gives you on Windows.
    """

    def _fake_torch(self, monkeypatch, *, available, built_cuda, name="RTX 4090"):
        import sys
        import types

        mod = types.ModuleType("torch")
        mod.cuda = types.SimpleNamespace(
            is_available=lambda: available,
            get_device_name=lambda i: name,
        )
        mod.version = types.SimpleNamespace(cuda=built_cuda)
        monkeypatch.setitem(sys.modules, "torch", mod)

    def test_reports_the_gpu_when_cuda_is_available(self, monkeypatch):
        self._fake_torch(monkeypatch, available=True, built_cuda="12.1")
        device, desc, warning = render.detect_device()
        assert device == "cuda"
        assert "RTX 4090" in desc
        assert warning is None

    def test_warns_when_an_nvidia_box_has_a_cpu_only_torch(self, monkeypatch):
        self._fake_torch(monkeypatch, available=False, built_cuda=None)
        monkeypatch.setattr(render.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
        device, _, warning = render.detect_device()
        assert device == "cpu"
        assert warning is not None
        assert "CPU-only" in warning
        assert "torch-backend" in warning

    def test_stays_quiet_on_a_machine_with_no_gpu(self, monkeypatch):
        self._fake_torch(monkeypatch, available=False, built_cuda=None)
        monkeypatch.setattr(render.shutil, "which", lambda name: None)
        device, desc, warning = render.detect_device()
        assert (device, desc, warning) == ("cpu", "CPU", None)

    def test_no_warning_when_cuda_is_built_but_no_card_is_present(self, monkeypatch):
        """A CUDA build with no device is a normal laptop situation, not a
        misconfiguration worth shouting about."""
        self._fake_torch(monkeypatch, available=False, built_cuda="12.1")
        monkeypatch.setattr(render.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
        assert render.detect_device()[2] is None

    def test_survives_torch_failing_to_import(self, monkeypatch):
        import builtins

        real = builtins.__import__

        def boom(name, *a, **k):
            if name == "torch":
                raise ImportError("no torch")
            return real(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", boom)
        device, _, warning = render.detect_device()
        assert device == "cpu" and "could not query" in warning


class TestGpuWorkerSizing:
    def test_one_worker_on_cuda(self, monkeypatch):
        """Extra workers on a GPU queue for the same device and each load
        another copy of the model into VRAM."""
        monkeypatch.setattr(render.os, "cpu_count", lambda: 32)
        monkeypatch.setattr(render, "memory_budget_gb", lambda: 128.0)
        assert render.default_workers("cuda") == 1

    def test_cpu_sizing_is_unchanged(self, monkeypatch):
        monkeypatch.setattr(render.os, "cpu_count", lambda: 12)
        monkeypatch.setattr(render, "memory_budget_gb", lambda: 32.0)
        assert render.default_workers("cpu") == 3

    def test_defaults_to_cpu_when_not_told(self, monkeypatch):
        monkeypatch.setattr(render.os, "cpu_count", lambda: 12)
        monkeypatch.setattr(render, "memory_budget_gb", lambda: 32.0)
        assert render.default_workers() == render.default_workers("cpu")
