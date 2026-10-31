import os
import sys
import tempfile

# Must be set before importing the machine package.
_TMP = tempfile.mkdtemp(prefix="cm-test-")
os.environ["MACHINE_OFFLINE"] = "1"
os.environ["MACHINE_DATA"] = _TMP
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import hashlib  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples" / "packages"
FIX = Path(__file__).resolve().parent / "fixtures"


def put_source(url: str, text: str) -> None:
    """Offline stand-in for an official page (used by the fact check)."""
    from machine import config
    d = config.DATA_DIR / "offline_sources"
    d.mkdir(parents=True, exist_ok=True)
    (d / (hashlib.sha1(url.encode()).hexdigest() + ".txt")).write_text(text, encoding="utf-8")


def put_feed(url: str, raw: bytes) -> None:
    from machine import config
    d = config.DATA_DIR / "offline_feeds"
    d.mkdir(parents=True, exist_ok=True)
    (d / (hashlib.sha1(url.encode()).hexdigest() + ".dat")).write_bytes(raw)


def load_sample(name: str) -> dict:
    return json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))


SSA_COLA_URL = "https://www.ssa.gov/news/en/press/releases/2026-10-14.html"
SSA_COLA_TEXT = """Social Security Announces 3.6 Percent Benefit Increase for 2027.
Social Security and Supplemental Security Income (SSI) benefits for nearly 72 million Americans
will increase 3.6 percent in 2027. Increased payments to approximately 7.3 million SSI recipients
will begin on December 31, 2026. Increased payments to Social Security beneficiaries will begin in January 2027.
The average retirement benefit will increase by $75 per month. Create an account and opt in by November 18."""


@pytest.fixture(autouse=True)
def _sources():
    put_source(SSA_COLA_URL, SSA_COLA_TEXT)
    yield


@pytest.fixture
def clean_db():
    from machine import db
    c = db.conn()
    for t in ("packages", "events", "radar_items", "source_status", "kv"):
        c.execute(f"DELETE FROM {t}")
    yield c
