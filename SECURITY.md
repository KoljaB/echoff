# Security and privacy

## Sensitive recordings

Echoff artifacts can contain private microphone speech, calls, notifications,
media, and any other audio routed to the selected output endpoint. The default
`captures/` tree and general WAV/JSONL/log patterns are ignored by this
repository; only the two reviewed demo WAVs under `assets/` are explicitly
exempt. Custom output locations and JSON metadata are not guaranteed to be ignored.
Artifacts are not encrypted, anonymized, or safe to publish by default. Check
`git status` before committing.

Before sharing diagnostics:

1. review all three WAV files and the human-readable log;
2. remove unrelated application metadata and paths where possible;
3. share only the minimum interval needed to reproduce the issue; and
4. prefer structured `summary.json` and redacted event rows when raw PCM is not
   required.

## Reporting a vulnerability

GitHub private vulnerability reporting is currently disabled for this
repository, and no public security email is listed. Open a minimal public issue
asking the owner for a private contact channel; do not include exploit details,
credentials, or recordings in that issue. If private vulnerability reporting
is enabled later, prefer the repository's **Security** tab.

Include affected Echoff/Python/OS versions, impact, reproduction prerequisites,
and whether the report contains private audio. Do not attach raw recordings to
a public issue.
