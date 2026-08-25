# Echoff 0.3.0 release handoff

Date: 2026-08-20  
Repository: `D:\Projekte\echoff`  
Branch: `main`  
HEAD: `639b312a66f42e1392f30a17522615becff0fcb1` (`Improve capture alignment handling`)  
Status: partial; release is blocked and the worktree is intentionally uncommitted

## Goal

Qualify and release Echoff after `v0.2.0` without rebuilding or overwriting that
release. The requested release process includes compatibility review, automated
and physical Windows/Linux qualification, a preserved three-track listening
probe, one clean build, clean-wheel installation, strict artifact checks,
TestPyPI/PyPI publication of identical bytes, and exact commit/tag/hash
verification.

## User constraints

- Existing applications matter; preserve the `v0.2.0` public API unless a
  breaking change is explicitly reviewed.
- Suggest newly required behavior-code changes before implementing them so the
  user can review them.
- Luna Max subagents may be used for implementation and hostile review.
- Do not upload, publish, push, tag, or otherwise mutate a registry/repository
  without an explicit gate.
- Do not touch or remove `.codex-remote-attachments/`; it is untracked user data.
- Hardware artifacts contain private microphone/system audio. Preserve them and
  never package or upload them.

The last response proposed four additional Windows behavior fixes. The user
replied only by asking for this handoff. That is not explicit approval to apply
those fixes. The next session must obtain approval before editing behavior code.

## Work completed

### Capture and alignment

- Startup joining now validates a bounded contiguous microphone tail without
  crossing a sequence or discontinuity barrier.
- Leading microphone blocks that have no real reference counterpart are retired
  and counted in `startup_unpaired_microphone_blocks`; they no longer create a
  synthetic zero-reference processed slot.
- Only real, confirmed `process_pair(reference, microphone)` pairs reach APM,
  callbacks, and the three processed tracks.
- A proven discontinuity invalidates the old reference mapping, remains pending
  until the first real new-epoch pair, and resets APM exactly once per episode.
- Same-epoch temporary stalls can recover without an alignment reset.
- Raw ingress queues, internal pairing buffers, and Windows callback queues are
  finite. Unsafe backlog/overflow becomes explicit instead of growing without
  bound.
- Degraded live buffers are bounded and retired with cause accounting whether or
  not artifacts are enabled. Raw payload preservation occurs only when artifacts
  are enabled.
- Terminal capture status now distinguishes `completed`, `incomplete`,
  `degraded`, and `failed`. `run_probe()` rejects a non-`completed` summary while
  preserving the artifacts.
- Processor reset paths are hard boundaries that discard partial adapter tails.
  Legacy `v0.2.0` custom processors remain usable because `reset_echo_path()` is
  additive rather than a required `AecProcessor` protocol member.

### Windows backend work already present

- Restored the hardware-qualified native callback cadence to fixed 100 ms
  packets while retaining fixed 20 ms downstream blocks.
- Moved WDM-KS callback decoding away from the realtime callback.
- Added finite callback queues, queue packet/block telemetry, backend-attempt
  diagnostics, fallback metadata, and synchronized diagnostic snapshots.
- Improved start failure cleanup, shared-context release retryability, active
  metadata publication, generic open-error retry/fallback, user-requested stop,
  and partial-start handling.
- Added extensive Windows backend regression tests.

This Windows work is **not frozen** because the final hostile audit reproduced
four remaining stop-ship defects listed below.

### API, version, changelog, and documentation

- Top-level exports and the existing `AecCapture` constructor signature remain
  identical to `v0.2.0`.
- Existing config defaults and backend values match `v0.2.0`, including
  `reference_stall_grace_s=3.0`. The temporary 15-second value existed only in
  post-tag commit `639b312` and was not part of `v0.2.0`.
- Additive API includes `AecCapture.reset_echo_path()`, reset telemetry,
  per-source observed frame times, capture status fields, and new event/status
  values.
- Existing `AecFrame.reference_ended_monotonic` and
  `microphone_ended_monotonic` retain their `v0.2.0` canonical shared-timeline
  meaning. The new nullable `*_observed_end_monotonic` fields hold per-source
  observations; `pair_skew_s` uses valid observed timing.
- `pyproject.toml` and `src/echoff/__init__.py` now say `0.3.0`.
- `CHANGELOG.md` has a dated `0.3.0 - 2026-08-20` entry.
- README and docs now describe the real startup/reset/status behavior, the
  three-second reserve, exact backends, correct Linux CLI flags, correct track
  names, canonical/observed timing, and additive artifact semantics.
- Artifact schema remains `echoff-capture-artifacts-v2`; the changes are additive
  nested fields/events, and track names are unchanged.
- Exact `0.3.0` endpoints returned 404 on both PyPI and TestPyPI on 2026-08-20,
  while the project endpoints returned 200. Recheck immediately before upload.

Supported built-in capture backends to preserve/document:

- Windows: WASAPI loopback reference and WASAPI microphone through
  PyAudioWPatch, with an optional hidden WDM-KS microphone fallback through
  `sounddevice` only when the selected physical microphone can be proven.
- Linux: PipeWire sink-monitor reference and ordinary microphone source through
  `pactl`, `pw-dump`, and `pw-record`.
- macOS: no built-in capture backend; processor-only use with application-owned
  aligned PCM.

## Automated evidence

Primary-agent results before the documentation-only patch:

- Three consecutive full runs: `124 passed, 30 subtests passed` each.
- Focused capture suite: `35 passed, 2 subtests passed`.
- Windows backend suite: `29 passed, 5 subtests passed`.
- `python -m ruff check .`: passed.
- `python -m mypy src`: passed.
- `git diff --check`: passed.

Independent Luna Max capture re-audit:

- Four consecutive full runs passed (`124 + 30 subtests`).
- Reproduced bounded fatal spooling, one-reset reference-only barrier recovery,
  truthful degraded shutdown, and complete accepted-block accounting.
- Reported no remaining capture/alignment stop-ship blocker.

Independent Luna Max Windows re-audit:

- Repeated Windows tests and the complete suite passed, but hostile
  reproductions found the four real defects below. Green tests therefore do not
  make the Windows patch releasable yet.

The local `.venv` uses Python 3.14.0. CI declares Python 3.11 and 3.12 on Windows
and Ubuntu. Those declared environments still need final qualification. The
actual mypy contract is `mypy src`; `mypy src tests` exposes substantial
pre-existing test-typing debt and is not the configured gate.

After the documentation/version edits, `git diff --check` passed and both
version sources were confirmed at `0.3.0`. Rerun every automated gate after the
remaining behavior patch; do not reuse the earlier green results as final
release evidence.

## Four unresolved Windows stop-ship defects

No fix below has been applied.

1. **WDM-KS can select the wrong physical microphone.**
   `_device_name_key()` removes generic words and the selector accepts substring
   matches. `"USB Microphone"` normalizes to `"usb"`, which incorrectly matches
   `"USB Headset"` (`"usbheadset"`). Proposed fix: require exactly one exact
   normalized-name equality. Prefer a safe fallback refusal over wrong-device
   capture. Add the hostile USB microphone/headset regression while retaining a
   valid exact-match case.

2. **A partial native start can leak an active stream when the activity probe
   also fails.**
   WASAPI and WDM cleanup currently may close an active stream without stopping
   it if `start*()` activates and raises, then `is_active()`/`.active` raises.
   Proposed fix: treat an unknown post-start state as “stop required,” attempt
   stop before close, and preserve the original start exception if probing or
   cleanup also fails. Cover both WASAPI and WDM fakes and assert one stop,
   inactive close, and original-error precedence.

3. **A final callback queue overflow during native shutdown can be swallowed.**
   The current final callback-error check occurs before `stop_stream()`. A final
   callback injected during stop can overflow the bounded queue after that check,
   letting capture falsely finish as `completed`. Proposed fix: inspect callback
   errors after native stop, decoder drain, close, and sample-clock flush. Promote
   a late callback error when no earlier primary exists; otherwise preserve the
   earlier primary and report the late error as secondary. Add a deterministic
   one-packet-queue shutdown-overflow test and a capture-level assertion that the
   result is `failed`, never `completed`.

4. **`AecCapture.stop()` cannot retry failed cleanup/finalization safely.**
   It marks `_running=False` before cleanup, so a second call returns immediately
   after a transient shared-context release failure. `_stop_processing()` also
   discards its thread reference before a successful join. Proposed fix: a
   serialized private stop state machine that permits retry until cleanup and
   finalization complete, retains the first error/status, retries pending source
   cleanup and worker join, and guarantees exactly one stop event, summary, and
   set of WAV frames. Artifact finalization must be safely retryable: close flags
   only become final after successful closes, an atomic-summary failure cleans
   up its own temporary file, and an already completed summary is not rewritten.
   Add a transient source-release plus transient-finalization regression and
   assert two attempts, one event, one summary, and unchanged frame counts.

The first three fixes are small and backend-local. The fourth crosses capture
lifecycle and artifact finalization and needs a separate hostile review after
implementation.

## Hardware evidence so far

Windows enumeration previously identified:

- microphone device index `9`: Arctis microphone, 48 kHz mono;
- reference device index `10`: Realtek loopback, 48 kHz stereo.

Preserved diagnostic runs live under `captures/release-0.3.0/` and are ignored by
Git. Do not delete, overwrite, package, or upload them.

- `windows-repaired-smoke-001` correctly failed to produce loopback blocks when
  there was no system-audio stimulus; preserve it as diagnostic evidence, not a
  pass.
- `windows-repaired-stimulus-001` completed before the latest Windows changes:
  725 pairs over about 14.5 seconds, WDM fallback, mean skew 0.789 ms, maximum
  skew 1.994 ms, no reported failures/timeouts/discontinuities/overflows, and
  12.49 dB aggregate suppression. It is only a smoke result and cannot qualify
  the current tree.
- Other existing Windows directories must also be treated as preserved private
  artifacts, not reusable output locations.

Still required on the final exact code:

- fresh device enumeration;
- at least 10 cold starts and 10 repeated restarts per platform;
- microphone/reference synchronization;
- discontinuity and physical overflow recovery;
- far-end-only suppression (target aggregate at least 10 dB and weakest useful
  window at least 6 dB with sufficient raw exposure);
- near-end speech retention and manual intelligibility review;
- at least a 10-minute long run, preferably 30 minutes;
- device disconnect and clean shutdown without hangs;
- preserved three-track recording/listening probe.

The listening judgment is human-required and cannot be replaced by unit tests or
aggregate metrics.

## Linux execution boundary

Before any Linux command, read:

- `D:\Projekte\LinuxRemote\AGENTS.md`
- `D:\Projekte\remote-codex_cli\remote-codex_cli\AGENTS_REMOTE.md`

Those files were read in this session. Prefer the wrapper in
`D:\Projekte\remote-codex_cli\remote-codex_cli`, run one remote command per call,
use explicit `--cwd` and `--reason`, and begin with `--hello`, `whoami`, `pwd`,
and `ls -la` checks. Use the documented SSH fallback only if the controlled
terminal is broken.

The existing remote `/home/lon/Dev/echoff` tree is known to be stale/dirty. Do
not alter it. Use a separate clean checkout/worktree of the exact approved
release commit for Linux tests and hardware capture. A remote-session handoff
note is required by the remote workflow after Linux work.

## Current worktree

The worktree is deliberately dirty and uncommitted. Modified files at handoff:

- release surface: `CHANGELOG.md`, `README.md`, `pyproject.toml`,
  `src/echoff/__init__.py`;
- docs: `docs/README.md`, `docs/architecture.md`,
  `docs/capture-artifacts.md`, `docs/cli.md`,
  `docs/getting-started-linux.md`, `docs/integration.md`,
  `docs/platforms.md`, `docs/python-api.md`, `docs/troubleshooting.md`;
- behavior: `src/echoff/alignment.py`, `src/echoff/backends/windows.py`,
  `src/echoff/capture.py`, `src/echoff/config.py`, `src/echoff/models.py`,
  `src/echoff/probe.py`, `src/echoff/processor.py`;
- tests: `tests/test_alignment.py`, `tests/test_capture.py`,
  `tests/test_config.py`, `tests/test_processor.py`,
  `tests/test_recording_analysis.py`, `tests/test_windows_backend.py`;
- untracked user data: `.codex-remote-attachments/` (preserve untouched).

There is no release commit. No build was made from this worktree. Existing
`dist/` content is stale and must never be reused.

## Next session sequence

1. Read this file, `AGENTS.md`, and the current diff. Confirm that the user has
   explicitly approved all four proposed Windows fixes; otherwise ask and stop
   before behavior edits.
2. Implement the fixes test-first in two review units: backend fixes 1-3, then
   retryable lifecycle/artifact fix 4. Use `apply_patch`; preserve unrelated
   worktree content.
3. Have separate Luna Max critics reproduce the old failures and attack the new
   ordering, error precedence, retry, and exactly-once guarantees.
4. Run focused tests, then at least three complete suites with
   `-p no:cacheprovider`, Ruff, `mypy src`, and `git diff --check`.
5. Freeze behavior only after all hostile reviews are clean. Then run fresh
   uniquely named Windows hardware runs. Never overwrite existing capture dirs.
6. Qualify Linux from an isolated clean checkout of the exact candidate commit.
7. Reconcile hardware evidence, public API/backend docs, version sources, and
   changelog. If code semantics changed, update docs before the release commit.
8. Create the exact release commit only after all pre-build gates pass.
9. From that clean commit, build the wheel and sdist once into a fresh explicit
   staging directory on `D:`. Do not run speculative package builds beforehand.
10. Run strict metadata/content checks, hashes, a clean-wheel install, and fresh
    capture smoke tests. Verify the wheel/sdist contain no captures, attachments,
    credentials, caches, or development-only artifacts.
11. Stop and obtain explicit approval before uploading to TestPyPI. Verify the
    downloaded TestPyPI wheel is byte-identical and installs correctly.
12. Stop again for explicit PyPI/tag authorization. Publish the verified bytes,
    tag the exact release commit, and verify commit/tag/artifact hashes.

Do not claim the release is complete until every applicable gate above has
passed on the exact released bytes.
