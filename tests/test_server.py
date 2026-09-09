"""API tests. No conversions are started -- subprocess launching is stubbed."""

import json
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from epub2audiobook.config import RenderSettings  # noqa: E402
from epub2audiobook.server.app import create_app  # noqa: E402
from epub2audiobook.server.jobs import JobManager  # noqa: E402
from epub2audiobook.store import JobStore, render_key  # noqa: E402


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path)), tmp_path


def seed_job(root, name="A Book", chunks_done=1):
    """Create a job directory that looks like a partly-finished conversion."""
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "source.epub").write_bytes(b"PK\x03\x04fake")
    store = JobStore(path / "job.db")
    settings = RenderSettings()
    store.init_job(
        epub_path=path / "source.epub", epub_hash="h", title=name, author="An Author",
        settings=settings.to_dict(), options={}, llm_model=None,
    )
    store.replace_chapters([(0, "One", "c0", "text one"), (1, "Two", "c1", "text two")])
    store.set_selection(None)
    for idx in (0, 1):
        store.sync_chunks(idx, [(f"chunk {idx}", render_key(f"chunk {idx}", settings.key()))])
    if chunks_done:
        store.finish_chunk(0, 0, str(path / "a.wav"), 3.0)
    store.close()
    return path


class TestMeta:
    def test_lists_voices_and_a_worker_suggestion(self, client):
        c, _ = client
        body = c.get("/api/voices").json()
        assert any(v["id"] == "af_heart" for v in body["voices"])
        assert body["suggested_workers"] >= 1
        assert body["threads_per_worker"] >= 1


class TestJobListing:
    def test_empty_to_start(self, client):
        c, _ = client
        assert c.get("/api/jobs").json()["jobs"] == []

    def test_reports_progress_from_the_database(self, client):
        c, root = client
        seed_job(root)
        jobs = c.get("/api/jobs").json()["jobs"]
        assert len(jobs) == 1
        job = jobs[0]
        assert job["chunks"] == 2 and job["chunks_done"] == 1
        assert job["percent"] == pytest.approx(50.0)
        assert job["author"] == "An Author"

    def test_detail_includes_per_chapter_rows(self, client):
        c, root = client
        seed_job(root)
        job = c.get("/api/jobs/A Book").json()
        assert [r["idx"] for r in job["chapter_rows"]] == [0, 1]
        assert job["chapter_rows"][0]["chunks_done"] == 1
        assert job["chapter_rows"][1]["chunks_done"] == 0

    def test_unknown_job_is_404(self, client):
        c, _ = client
        assert c.get("/api/jobs/nope").status_code == 404

    def test_ignores_directories_that_are_not_jobs(self, client):
        c, root = client
        (root / "just-a-folder").mkdir()
        assert c.get("/api/jobs").json()["jobs"] == []


class TestUploadValidation:
    def test_rejects_a_non_epub_extension(self, client):
        c, _ = client
        r = c.post("/api/jobs", files={"file": ("notes.txt", b"hello", "text/plain")})
        assert r.status_code == 400

    def test_rejects_something_that_is_not_a_zip(self, client):
        c, _ = client
        r = c.post("/api/jobs", files={"file": ("book.epub", b"not a zip", "application/epub+zip")})
        assert r.status_code == 400
        assert "zip" in r.json()["detail"].lower()

    def test_rejects_an_empty_file(self, client):
        c, _ = client
        r = c.post("/api/jobs", files={"file": ("book.epub", b"", "application/epub+zip")})
        assert r.status_code == 400


class TestPathTraversal:
    @pytest.mark.parametrize("bad", ["..", "../secrets", "..\\secrets", ".hidden"])
    def test_job_ids_cannot_escape_the_data_directory(self, tmp_path, bad):
        manager = JobManager(tmp_path)
        with pytest.raises(ValueError):
            manager.job_dir(bad)

    def test_a_normal_id_resolves_inside(self, tmp_path):
        manager = JobManager(tmp_path)
        assert manager.job_dir("A Book").parent == tmp_path.resolve()


class TestDownload:
    def test_404_before_the_audiobook_exists(self, client):
        c, root = client
        seed_job(root)
        assert c.get("/api/jobs/A Book/download").status_code == 404

    def test_serves_the_m4b_once_present(self, client):
        c, root = client
        path = seed_job(root)
        (path / "A Book.m4b").write_bytes(b"\x00\x00\x00\x20ftypM4A ")
        r = c.get("/api/jobs/A Book/download")
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/mp4"

    def test_a_preview_file_is_not_treated_as_the_output(self, client):
        c, root = client
        path = seed_job(root)
        (path / "PREVIEW - A Book.m4b").write_bytes(b"x")
        assert c.get("/api/jobs/A Book").json()["output"] is None


class TestState:
    def test_a_recently_stamped_job_reads_as_running(self, client):
        """A conversion started from the terminal has no tracked process here,
        so freshness of the database is what marks it live."""
        c, root = client
        seed_job(root)
        assert c.get("/api/jobs/A Book").json()["state"] == "running"

    def test_a_stale_job_reads_as_idle(self, client):
        c, root = client
        path = seed_job(root)
        import sqlite3

        con = sqlite3.connect(path / "job.db")
        con.execute("UPDATE job SET updated_at = ?", (time.time() - 600,))
        con.commit()
        con.close()
        assert c.get("/api/jobs/A Book").json()["state"] == "idle"

    def test_a_finished_job_reads_as_done(self, client):
        c, root = client
        path = seed_job(root)
        (path / "A Book.m4b").write_bytes(b"x")
        assert c.get("/api/jobs/A Book").json()["state"] == "done"


class TestLifecycle:
    def test_start_is_refused_when_the_source_is_missing(self, client):
        c, root = client
        path = seed_job(root)
        (path / "source.epub").unlink()
        r = c.post("/api/jobs/A Book/start", json={"voice": "af_heart"})
        assert r.status_code == 404

    def test_start_launches_the_cli_with_the_chosen_settings(self, client, monkeypatch):
        c, root = client
        seed_job(root)
        captured = {}

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = cmd
                self.pid = 1234
                self.returncode = None

            def poll(self):
                return None

        monkeypatch.setattr("epub2audiobook.server.jobs.subprocess.Popen", FakePopen)
        r = c.post(
            "/api/jobs/A Book/start",
            json={"voice": "bm_george", "speed": 0.9, "only": "0-3", "workers": 2},
        )
        assert r.status_code == 200
        cmd = captured["cmd"]
        assert "convert" in cmd
        assert cmd[cmd.index("--voice") + 1] == "bm_george"
        assert cmd[cmd.index("--speed") + 1] == "0.9"
        assert cmd[cmd.index("--only") + 1] == "0-3"
        assert cmd[cmd.index("--workers") + 1] == "2"

    def test_delete_removes_the_job_directory(self, client):
        c, root = client
        seed_job(root)
        assert c.delete("/api/jobs/A Book").status_code == 200
        assert not (root / "A Book").exists()
        assert c.get("/api/jobs").json()["jobs"] == []


class TestEvents:
    def test_stream_emits_a_json_frame(self, client):
        c, root = client
        path = seed_job(root)
        (path / "A Book.m4b").write_bytes(b"x")  # 'done' ends the stream promptly
        with c.stream("GET", "/api/jobs/A Book/events") as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers["content-type"]
            for line in r.iter_lines():
                if line.startswith("data: "):
                    payload = json.loads(line[6:])
                    assert payload["id"] == "A Book"
                    assert payload["state"] == "done"
                    break
