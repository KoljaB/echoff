# Development rules

- Keep capture, alignment, processing, recording, analysis, and CLI code in
  separate modules.
- The core package must not contain VAD, ASR, TTS, or conversation policy.
- Preserve the atomic `process_pair(reference, microphone)` contract.
- Do not mix system audio into the microphone before AEC.
- Capture backends must emit fixed-size mono blocks with monotonically
  increasing end timestamps.
- Runtime discontinuities are realigned and reported. Do not silently pair
  stale frames and do not terminate a healthy session merely because the two
  callback counters temporarily diverge.
- A realignment resets the WebRTC APM once per episode.
- The library must not configure the root logger. Applications and the CLI own
  handlers and log levels.
- Unit tests never access physical audio devices. Hardware tests live under
  `examples/` or the `echoff record` command and always preserve artifacts.
- Keep source modules focused. Split a module before it becomes responsible for
  unrelated concerns.

## Release

- Before building a release, run the cheap worktree guard:
  `python tools/release_guard.py check-worktrees --repo .`.
  A dirty main checkout or linked worktree is a hard release blocker.
- The release wheel must be attested against the exact Miep runtime that will
  use Echoff. Run `release_guard.py attest` with `--package-dir echoff`,
  `--source-package-dir src/echoff`, that runtime's Python/import path, the
  mandatory wheel and sdist, and the local private signing key. The detached
  signature expires for publication after 30 minutes.
- Every TestPyPI or PyPI upload must go through
  `python tools/release_guard.py publish`; invoke neither `twine upload` nor a
  wildcard upload directly. If the guard fails, stop and reconcile the runtime
  before publishing.
- Do not rebuild or replace the attested wheel or sdist after `attest`. A
  changed source commit, linked worktree, runtime import/file, artifact hash,
  signature, remote `main`/tag, or package-index roundtrip must block the
  release.
