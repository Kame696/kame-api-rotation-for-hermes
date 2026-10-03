"""Execute the exact Desktop formatter; both display sites use it."""
from pathlib import Path
import json
import shutil
import subprocess
import pytest
SOURCE=Path(__file__).resolve().parents[1]/'hermes-kame-api-rotation/desktop/plugin.js'

def test_both_desktop_display_sites_use_the_null_aware_formatter():
    text=SOURCE.read_text(encoding='utf-8')
    assert text.count('(${resumeProgress(activity)})')==2
    assert '${activity.resume}/${activity.budget}' not in text
    assert '${activity.resume} of ${activity.budget}' not in text

def test_actual_js_formatter_auto_explicit_zero_and_missing_values():
    node=shutil.which('node')
    if not node:pytest.skip('Node runtime not available for exact JS execution')
    text=SOURCE.read_text(encoding='utf-8');start=text.index('function resumeProgress(activity) {')
    fn=text[start:text.index('\n}',start)+2]
    script=fn+'\nconsole.log(JSON.stringify([resumeProgress({resume:12,budget:null}),resumeProgress({resume:3,budget:10}),resumeProgress({resume:0,budget:0}),resumeProgress({resume:2})]));'
    result=subprocess.run([node,'-e',script],check=True,capture_output=True,text=True,timeout=15)
    assert json.loads(result.stdout)==['12 (automatic)','3 of 10','0 of 0','2 (automatic)']
