# Echoff 0.3.0 test handoff

## Current state

- Local release commit: `0763be330fe7890a37e230b46e2c048a6607254a`
- The candidate is usable for practical testing.
- Nothing was pushed, uploaded, published, or tagged.
- Existing captures, private recordings, stale `dist/`, and
  `.codex-remote-attachments/` were preserved.

## Evidence

- Focused suite: 84 passed, 16 subtests passed.
- Complete Windows and Linux suites: 138 passed, 35 subtests passed each.
- Ruff, `mypy src`, `git diff --check`, v0.2.0 API compatibility, strict package
  contents/metadata, `twine check --strict`, clean Python 3.12 installation,
  `pip check`, and installed-wheel `process_pair` smoke passed.
- The stimulated installed-wheel Windows capture completed with 725 pairs,
  equal 696000-frame primary tracks, one `capture_stopped` event, and no
  overflow, discontinuity, or reset.

## Built artifacts

- Wheel: `D:\Temp\User\echoff-release-0.3.0-0763be3-dist\echoff-0.3.0-py3-none-any.whl`
  - SHA-256: `a2804bc5750d68cb0ad7919756bd13769eb3456d3ca20032029427a123e8d6c4`
- sdist: `D:\Temp\User\echoff-release-0.3.0-0763be3-dist\echoff-0.3.0.tar.gz`
  - SHA-256: `537048284030cb2a84186566e68a081251480aeea37b2903df075399b77a4b23`

## Known limits

- AEC quality is not formally qualified because raw microphone exposure stayed
  below the configured minimum in the clean hardware runs.
- Human listening remains required; do not infer listening quality from metrics.
- The shortened qualification omitted the exhaustive start/restart matrix,
  long run, disconnect/physical-overflow tests, a third full local test run, and
  a final independent Unit 2 re-audit.
- On Windows, play actual system audio during capture. A silent WASAPI loopback
  may produce no reference blocks, causing the intentional three-second failure.

## Next step

Test the local wheel and listen to the preserved three-track captures. Obtain
explicit approval before any TestPyPI upload, push, tag, or publication.
