"""The vendored routing stays byte-identical to Devika's original.

Guards against the copy drifting silently. If the original is intentionally
updated, refresh the copy and this test passes again; if the copy is edited
on its own, this fails and says so.
"""
import difflib
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ORIGINAL = REPO_ROOT / "users" / "devika" / "src" / "river_aoi.py"
VENDORED = REPO_ROOT / "eddie_aoi" / "eddie_aoi" / "routing.py"


def _select_route_source(text):
    match = re.search(r"^def select_route\(.*?(?=^\S|\Z)", text, re.S | re.M)
    assert match, "select_route not found"
    return match.group(0).rstrip("\n")


@pytest.mark.skipif(
    not ORIGINAL.exists(),
    reason="Devika's module is not present in this checkout",
)
def test_select_route_is_byte_identical_to_the_original():
    original = _select_route_source(ORIGINAL.read_text())
    vendored = _select_route_source(VENDORED.read_text())

    if original != vendored:
        diff = "\n".join(
            difflib.unified_diff(
                original.splitlines(),
                vendored.splitlines(),
                "users/devika/src/river_aoi.py",
                "eddie_aoi/eddie_aoi/routing.py",
                lineterm="",
            )
        )
        pytest.fail(f"Vendored select_route has drifted from the original:\n{diff}")


def test_vendored_block_records_its_provenance():
    text = VENDORED.read_text()

    assert "users/devika/src/river_aoi.py" in text
    assert "fa5d58bfb62679ba3f7320707c67e3689e3f6c05" in text
    assert "BEGIN VENDORED" in text and "END VENDORED" in text
