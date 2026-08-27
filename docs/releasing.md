# Releasing Echoff

[Documentation home](README.md)

PyPI filenames and versions are immutable. Build once, prove the TestPyPI wheel
is byte-identical after download, then upload those same local artifact bytes to
PyPI.

The examples below use `VERSION` as a placeholder. Replace it once and keep the
resolved file paths fixed throughout the release.

## 0. Mandatory release guard

The release guard is the required bridge between the installed Miep runtime and
the published Echoff artifacts. It is intentionally a cheap hash/metadata check
and does not access live audio devices or repeat the test suite.

Resolve these paths before building:

```powershell
$guard = (Resolve-Path .\tools\release_guard.py).Path
$miepPython = "PATH\TO\MIEP\.venv\Scripts\python.exe"
$miepEchoff = "PATH\TO\MIEP\.venv\Lib\site-packages\echoff"
$signingKey = "C:\Users\Start\.codex\release-state\keys\echoff_ed25519"
$allowedSigners = "C:\Users\Start\.codex\release-state\allowed_signers"
```

Pass the Miep runtime's exact Python interpreter and imported package directory
to `attest`, so the recorded distribution version and bytes belong to the
runtime being released. A missing or unresolvable runtime is a blocker; do not
substitute the Echoff build venv.

Every registry upload, including TestPyPI, must use `release_guard.py publish`
with the wheel and artifact paths recorded by `attest`. Direct `twine upload`,
wildcard uploads, and rebuilt artifacts are not valid release paths.

## 1. Prepare

- update `pyproject.toml` and `echoff.__version__` to the same unused version;
- update README installation/status text and changelog date;
- confirm the version is unused on TestPyPI and PyPI;
- start from a clean release commit; and
- never use `--skip-existing` to conceal a collision.
- confirm the Miep runtime paths above resolve to the runtime being released.

Create a release-capable environment. This example uses Python 3.12; substitute
another supported interpreter deliberately rather than whichever `python`
happens to be first on `PATH`:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,release]"
$guardPython = (Resolve-Path .\.venv\Scripts\python.exe).Path
```

## 2. Validate

```powershell
& .\.venv\Scripts\python.exe $guard check-worktrees --repo (Get-Location).Path
if ($LASTEXITCODE -ne 0) { throw "release guard worktree check failed" }
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\mypy.exe src
git status --short
```

## 3. Build once outside the repository

```powershell
$version = "VERSION"
$parent = Split-Path -Parent (Get-Location).Path
$release = Join-Path $parent "echoff-release-$version"
if (Test-Path -LiteralPath $release) { throw "release staging already exists: $release" }
New-Item -ItemType Directory -Path "$release\dist" -ErrorAction Stop | Out-Null

.\.venv\Scripts\python.exe -m build --outdir "$release\dist"
if ($LASTEXITCODE -ne 0) { throw "build failed" }

$wheel = Join-Path $release "dist\echoff-$version-py3-none-any.whl"
$sdist = Join-Path $release "dist\echoff-$version.tar.gz"
$files = @(Get-ChildItem -LiteralPath "$release\dist" -File)
if ($files.Count -ne 2 -or -not (Test-Path $wheel) -or -not (Test-Path $sdist)) {
  throw "expected exactly the wheel and sdist"
}

.\.venv\Scripts\python.exe -m twine check --strict $wheel $sdist
if ($LASTEXITCODE -ne 0) { throw "twine check failed" }
$wheelHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $wheel).Hash
$sdistHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $sdist).Hash
Write-Output "wheel $wheelHash"
Write-Output "sdist $sdistHash"
```

Install `$wheel` into `$miepPython`, run the short import/AEC smoke, then attest
that exact runtime as the final step before any upload. Keep the manifest,
detached signature, and artifact paths unchanged. Re-attest after 30 minutes or
after any runtime/install change:

```powershell
& $miepPython -m pip install --no-deps --force-reinstall $wheel
if ($LASTEXITCODE -ne 0) { throw "Miep runtime install failed" }
& $miepPython -I -c "import echoff, importlib.metadata as m; assert m.version('echoff') == '$version' == echoff.__version__; from echoff import WebRtcAecProcessor; p=WebRtcAecProcessor(); z=(0.0,)*480; assert len(p.process_pair(z,z)) == 480; print(echoff.__file__)"
if ($LASTEXITCODE -ne 0) { throw "Miep runtime smoke failed" }

$manifest = Join-Path $release "release-guard.json"
& $guardPython $guard attest `
  --repo (Get-Location).Path `
  --component echoff `
  --distribution echoff `
  --package-dir echoff `
  --source-package-dir src/echoff `
  --wheel $wheel `
  --sdist $sdist `
  --runtime-package-dir $miepEchoff `
  --runtime-module echoff `
  --runtime-python $miepPython `
  --runtime-label "Miep runtime" `
  --signer echoff-windows `
  --signing-key-file $signingKey `
  --output $manifest
if ($LASTEXITCODE -ne 0) { throw "release guard attestation failed" }
$signature = "$manifest.sig"
if (-not (Test-Path -LiteralPath $signature)) { throw "release signature missing" }
```

Never upload with a wildcard from a directory that may contain another
version.

Before the first registry upload, push the release commit to `main`, create and
push annotated tag `v$version`, and verify both remote refs. `publish` repeats
that remote check and refuses any mismatch.

## 4. TestPyPI

Publish through the guard; it re-verifies the attested runtime, source, wheel,
and artifact hashes immediately before invoking Twine:

```powershell
& $guardPython $guard publish `
  --repo (Get-Location).Path `
  --package-dir echoff `
  --source-package-dir src/echoff `
  --wheel $wheel `
  --sdist $sdist `
  --manifest $manifest `
  --signature-file $signature `
  --allowed-signers-file $allowedSigners `
  --signer echoff-windows `
  --repository testpypi `
  --remote origin `
  --branch main `
  --tag "v$version"
if ($LASTEXITCODE -ne 0) { throw "TestPyPI upload failed" }
```

$download = Join-Path $release "testpypi-download"
New-Item -ItemType Directory -Path $download -ErrorAction Stop | Out-Null
.\.venv\Scripts\python.exe -m pip download `
  --no-cache-dir --no-deps --only-binary=:all: `
  --index-url https://test.pypi.org/simple/ `
  --dest $download "echoff==$version"
if ($LASTEXITCODE -ne 0) { throw "TestPyPI download failed" }

$fetched = Join-Path $download "echoff-$version-py3-none-any.whl"
$fetchedHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $fetched).Hash
if ($fetchedHash -ne $wheelHash) { throw "TestPyPI wheel differs from built wheel" }
```

Install that downloaded wheel into a fresh supported-Python environment while
resolving dependencies from production PyPI:

```powershell
$verifyVenv = Join-Path $release "verify-venv"
py -3.12 -m venv $verifyVenv
$verifyPython = Join-Path $verifyVenv "Scripts\python.exe"
$verifyCli = Join-Path $verifyVenv "Scripts\echoff.exe"

& $verifyPython -m pip install `
  --no-cache-dir --index-url https://pypi.org/simple/ $fetched
if ($LASTEXITCODE -ne 0) { throw "fresh install failed" }
& $verifyPython -m pip check
if ($LASTEXITCODE -ne 0) { throw "pip check failed" }
& $verifyPython -I -c "import echoff, importlib.metadata as m; assert m.version('echoff') == '$version' == echoff.__version__; print(echoff.__file__)"
if ($LASTEXITCODE -ne 0) { throw "version/import check failed" }
& $verifyCli --help
if ($LASTEXITCODE -ne 0) { throw "CLI check failed" }
& $verifyPython -I -c "from echoff import WebRtcAecProcessor; p=WebRtcAecProcessor(); z=(0.0,)*480; assert len(p.process_pair(z,z)) == 480"
if ($LASTEXITCODE -ne 0) { throw "processor smoke test failed" }
```

## 5. Production PyPI

Only after every TestPyPI check passes, publish the same resolved local paths
through the guard. It rechecks the manifest and refuses a changed runtime or
artifact:

```powershell
if ((Get-FileHash -Algorithm SHA256 $wheel).Hash -ne $wheelHash) { throw "wheel changed" }
if ((Get-FileHash -Algorithm SHA256 $sdist).Hash -ne $sdistHash) { throw "sdist changed" }
& $guardPython $guard publish `
  --repo (Get-Location).Path `
  --package-dir echoff `
  --source-package-dir src/echoff `
  --wheel $wheel `
  --sdist $sdist `
  --manifest $manifest `
  --signature-file $signature `
  --allowed-signers-file $allowedSigners `
  --signer echoff-windows `
  --repository pypi `
  --remote origin `
  --branch main `
  --tag "v$version"
if ($LASTEXITCODE -ne 0) { throw "PyPI upload failed" }
```

`publish` succeeds only after PyPI's JSON API reports `$wheel` and `$sdist`
with the exact attested hashes.

## 6. GitHub

Attach the same immutable `$wheel` and `$sdist` to the GitHub release for the
already-pushed `vVERSION` tag. Record test results, artifact hashes, registry
URLs, commit, and tag in the handoff.

Never publish credentials, `.pypirc`, private capture artifacts, or a release
built from a different tree than the tagged commit.
