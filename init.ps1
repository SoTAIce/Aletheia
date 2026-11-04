# Engineering health check. Runs static checks and the deterministic pytest suite.
# No models, MCP, DB, installs or repo writes (pytest basetemp goes to a fresh TEMP dir).
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
$previousBytecodeSetting = $env:PYTHONDONTWRITEBYTECODE
try {
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        $python = (Get-Command python -ErrorAction Stop).Source
    }
    $check = @'
import json, pathlib, sys
root = pathlib.Path.cwd()
assert (3, 11) <= sys.version_info[:2] < (3, 14), 'Python 3.11-3.13 required'
for name in ('AGENTS.md', 'CLAUDE.md', 'progress.md', 'session-handoff.md',
             'docs/PROJECT_HANDOVER.md', 'docs/DOCUMENT_INDEX.md', 'docs/harness/README.md',
             'docs/DECISIONS.md', 'docs/harness/clean-state-checklist.md',
             'docs/harness/evaluator-rubric.md', 'docs/harness/quality-document.md',
             'docs/harness/baseline.md', 'tests/test_harness_smoke.py'):
    assert (root / name).is_file(), f'Missing management file: {name}'
state = json.loads((root / 'feature_list.json').read_text(encoding='utf-8-sig'))
features = state['features']
assert isinstance(features, list) and features, 'Missing feature records'
ids = [f['id'] for f in features]
assert len(ids) == len(set(ids)), 'Duplicate feature IDs'
for feature in features:
    for key in ('id', 'name', 'description', 'status'):
        assert isinstance(feature[key], str) and feature[key].strip(), f'Invalid {key}'
    assert feature['status'] in {'planned', 'in_progress', 'blocked', 'partial', 'done', 'deferred'}, 'Unknown status'
    assert isinstance(feature.get('dependencies', []), list), 'Dependencies must be a list'
    assert all(dep in ids for dep in feature.get('dependencies', [])), 'Unknown dependency'
    if feature['status'] == 'done':
        assert feature.get('evidence'), f'Missing completion evidence: {feature["id"]}'
active = state.get('active_feature')
assert active is None or active in ids, 'Unknown active feature'
running = [f['id'] for f in features if f['status'] == 'in_progress']
assert running == ([] if active is None else [active]), 'Active feature/status mismatch'
sources = sorted((root / 'app').rglob('*.py'))
assert sources, 'Missing application sources'
for source in sources:
    compile(source.read_bytes(), str(source), 'exec')
print(f'Management state valid; syntax PASS: {len(sources)} Python files')
'@
    $check | & $python -B -
    if ($LASTEXITCODE -ne 0) { throw 'Static health check failed' }
    & $python -B -m app.agent --help
    if ($LASTEXITCODE -ne 0) { throw 'CLI import/help check failed' }
    # Deterministic test suite (tests/ is in-memory only; see tests/test_harness_smoke.py).
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'tests'))) { throw 'tests/ missing' }
    $baseTemp = Join-Path ([IO.Path]::GetTempPath()) ('aletheia-pytest-' + [guid]::NewGuid().ToString('N'))
    try {
        & $python -B -m pytest -q -p no:cacheprovider --basetemp $baseTemp
        if ($LASTEXITCODE -ne 0) { throw 'pytest failed' }
    }
    finally {
        # Clean up immediately; \\?\ prefix handles paths beyond MAX_PATH.
        if (Test-Path -LiteralPath $baseTemp) {
            Remove-Item -LiteralPath ('\\?\' + $baseTemp) -Recurse -Force -Confirm:$false
        }
    }
    Write-Output 'Health check PASS (static/import + deterministic pytest suite; historical 1093-test regression and live services NOT verified)'
    # Print-only, never executed here (course init: show the standard paths).
    Write-Output 'START (needs live model/MCP + authorization): .venv/Scripts/python.exe -m app.agent "<goal>" --filesystem-root . --no-knowledge'
    Write-Output 'VERIFY: .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp <fresh TEMP dir>'
}
finally {
    $env:PYTHONDONTWRITEBYTECODE = $previousBytecodeSetting
    Pop-Location
}
