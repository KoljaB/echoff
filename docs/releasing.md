# Releasing Echoff

[Documentation home](README.md)

PyPI filenames and versions are immutable. Build once, prove the TestPyPI wheel
is byte-identical after download, then upload those same local artifact bytes to
PyPI.

The examples below use `VERSION` as a placeholder. Replace it once and keep the
resolved file paths fixed throughout the release.

## 1. Prepare

- update `pyproject.toml` and `echoff.__version__` to the same unused version;
- update README installation/status text and changelog date;
- confirm the version is unused on TestPyPI and PyPI;
- start from a clean release commit; and
- never use `--skip-existing` to conceal a collision.

Create a release-capable environment. This example uses Python 3.12; substitute
another supported interpreter deliberately rather than whichever `python`
happens to be first on `PATH`:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,release]"
```

## 2. Validate

```powershell
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

Never upload with a wildcard from a directory that may contain another
version.

## 4. TestPyPI

```powershell
.\.venv\Scripts\python.exe -m twine upload `
  --repository testpypi --non-interactive $wheel $sdist
if ($LASTEXITCODE -ne 0) { throw "TestPyPI upload failed" }

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

Only after every TestPyPI check passes, upload the same resolved local paths:

```powershell
if ((Get-FileHash -Algorithm SHA256 $wheel).Hash -ne $wheelHash) { throw "wheel changed" }
if ((Get-FileHash -Algorithm SHA256 $sdist).Hash -ne $sdistHash) { throw "sdist changed" }
.\.venv\Scripts\python.exe -m twine upload `
  --repository pypi --non-interactive $wheel $sdist
if ($LASTEXITCODE -ne 0) { throw "PyPI upload failed" }
```

Verify PyPI's JSON file hashes against `$wheelHash` and `$sdistHash`.

## 6. GitHub

Push the release commit to `main`, create annotated tag `vVERSION` at that exact
commit, push the tag, and verify remote `main` and tag refs. Record test results,
artifact hashes, registry URLs, commit, and tag in the handoff.

Never publish credentials, `.pypirc`, private capture artifacts, or a release
built from a different tree than the tagged commit.
