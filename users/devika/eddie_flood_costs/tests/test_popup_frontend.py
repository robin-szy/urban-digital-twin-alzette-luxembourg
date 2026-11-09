"""Execute dependency-free JS behaviour tests using an existing Node or JSC."""
from pathlib import Path
import os
import shutil
import subprocess

import pytest


def test_popup_frontend_behaviour():
    module = Path(__file__).resolve().parents[1]
    repo = module.parents[2]
    adapter = repo / "EDDIE/eddie_antarctica/terriajs/flood-cost-popup.js"
    if not adapter.is_file():
        pytest.skip("Shared frontend adapter is not present in this standalone package checkout.")
    node = shutil.which("node")
    jsc = os.environ.get("EDDIE_POPUP_JS_ENGINE") or shutil.which("jsc")
    tests = Path(__file__).with_name("popup_frontend_checks.js")
    if node:
        # Node executes one file; the runner loads the adapter through fs/vm.
        command = [node, str(tests), str(adapter)]
    elif jsc and Path(jsc).is_file():
        command = [jsc, str(adapter), str(tests)]
    else:
        pytest.skip("No existing Node or JavaScriptCore executable; nothing is installed automatically.")
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "popup checks passed" in result.stdout, result.stdout + result.stderr
