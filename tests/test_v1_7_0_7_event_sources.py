"""Execute the panel's real source-label logic rather than checking map names."""
import json
from pathlib import Path
import shutil
import subprocess


def test_detailed_sources_and_unknowns_are_visible():
    root = Path(__file__).resolve().parents[1]
    source = (root / "hermes-kame-api-rotation/desktop/plugin.js").read_text(encoding="utf-8")
    table = source[source.index("const SIZED_BY_LABELS ="):source.index("function EventRow(")]
    # Before the fix, execute exactly the inline lookup used by EventRow.
    function = "sizedByLabel" if "function sizedByLabel(" in table else "(source => SIZED_BY_LABELS[source] ?? null)"
    inputs = ["header.retry-after-ms", "body.resets_at", "exception.retry_delay", "text.reset_at",
              "window", "new-provider-signal", "__proto__", "", None, "retained"]
    node = shutil.which("node")
    assert node, "Node is required for the actual panel logic gate"
    result = subprocess.run([node, "-e", table+"\nconsole.log(JSON.stringify("+
        json.dumps(inputs)+".map("+function+")));"], capture_output=True, text=True, check=True)
    labels = json.loads(result.stdout)
    assert labels[:4] == [["header", "good"], ["payload", "good"], ["sdk", "good"], ["message", "weak"]]
    assert labels[4] == ["our default", "weak"]
    assert labels[5:7] == [["unknown source", "weak"]]*2
    assert labels[7:9] == [None, None]
    assert labels[9] == ["existing hold", "weak"]
