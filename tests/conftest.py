"""Dashboard application plugged into the dev Immich and a copy of the dev database, with a simulated
identification (no model loaded): shared by test_api.py and test_ui.py."""

import os
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
ENV, DB = ROOT / "dev" / ".env.dev", ROOT / "data" / "dev" / "taxonero.sqlite"
DEV_READY = ENV.exists() and DB.exists()


@pytest.fixture
def dev_app(tmp_path, monkeypatch):
    """(Settings, Worker) on a copy of the dev database."""
    from taxonero.config import Settings
    from taxonero.pipeline import Candidate
    from taxonero.taxonomy import Taxonomy
    from taxonero.worker import Worker

    class FakeClf:
        def classify(self, images):
            return [[Candidate("Vulpes vulpes", 0.9, {"kingdom": "Animalia", "family": "Canidae", "genus": "Vulpes"}),
                     Candidate("Martes foina", 0.1, {"kingdom": "Animalia", "family": "Mustelidae", "genus": "Martes"})]]

    class FakeAnalyzer:
        classifier = FakeClf()

        def analyze(self, data, lat=None, lon=None, original=None, fallback=None, groups=None):
            from taxonero.pipeline import Analysis, Box, Decision
            c = Candidate("Vulpes vulpes", 0.9, {"kingdom": "Animalia", "family": "Canidae"}, 5219243, "Vulpes vulpes",
                          posterior=0.9, prior=0.9)
            return [Analysis(Box(0.1, 0.1, 0.5, 0.5, 0.9, "animal"), Decision(5219243, "Vulpes vulpes", "species", 0.9, 0.8, "unknown", [c]))]

    # Simulated GBIF observation service: its state must not decide the test results. GBIF_LIVE=1: the real one.
    if not os.environ.get("GBIF_LIVE"):
        real = Taxonomy._fetch
        monkeypatch.setattr(Taxonomy, "_fetch", lambda self, url, retries=6: {"count": 500, "results": [], "endOfRecords": True}
                            if "/occurrence/" in url else real(self, url, retries))
    # Light copy (without the network cache or the range maps): /tmp is often in RAM.
    src, dst = sqlite3.connect(DB), sqlite3.connect(tmp_path / "taxonero.sqlite")
    src.backup(dst)
    dst.execute("DELETE FROM http_cache")
    dst.execute("DROP TABLE IF EXISTS taxon_range")
    dst.commit()
    dst.execute("VACUUM")
    src.close()
    dst.close()
    for line in ENV.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            monkeypatch.setenv(k, v)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DASHBOARD_NO_AUTH", "true")
    s = Settings()
    w = Worker(s, analyzer=FakeAnalyzer())
    w.check()
    return s, w
