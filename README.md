# Echoff




[![PyPI](https://img.shields.io/pypi/v/echoff.svg)](https://pypi.org/project/echoff/)
[![Python](https://img.shields.io/pypi/pyversions/echoff.svg)](https://pypi.org/project/echoff/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/KoljaB/echoff/blob/main/LICENSE)
[![Typed](https://img.shields.io/badge/typing-py.typed-2f74c0)](https://peps.python.org/pep-0561/)
[![Windows capture](https://img.shields.io/badge/live_capture-Windows-0078d4)](https://github.com/KoljaB/echoff/blob/main/docs/platforms.md)




**Echo off. Clean microphone on.**




Stops your voice agent from transcribing its own voice.




When a voice agent talks through your speakers, the microphone can pick
up its voice and feed it back to speech recognition as if you had said it.
Echoff synchronizes Windows system-audio loopback and microphone capture, then
uses WebRTC acoustic echo cancellation (AEC) to reduce that playback before
your application receives the microphone stream. Applications get the matched
reference, raw microphone, and echo-reduced microphone PCM.




## Hear the difference

This synchronized 20-second proof clip compares the same six-second moment in
three states: raw microphone, Echoff's echo-reduced output, and the computer-
audio reference it matches against. Listen with headphones.

<video src="https://github.com/user-attachments/assets/ed13f50e-a774-4378-acab-1ee5935bca09" controls></video>

The microphone tracks use the same +8 dB monitor gain so quiet details remain
audible. The computer-audio reference is unmodified.

> **Project status:** Echoff 0.1 is alpha software. Built-in live capture is
> physically tested on Windows. The processor APIs are designed for
> application-owned aligned PCM on other platforms where the LiveKit dependency
> installs, but those paths are not CI- or hardware-qualified here and Linux and
> macOS capture backends are not implemented. APIs and artifact schemas may
> change before 1.0. Echoff is licensed under the MIT License.




## Support at a glance




| Platform | Built-in live capture | Processor-only use with aligned PCM |
|---|---|---|
| Windows 10/11 | **Supported**: WASAPI loopback + WASAPI microphone, with WDM-KS microphone fallback | Supported |
| Linux | Planned: PipeWire backend | Designed for application-owned PCM where LiveKit installs; not qualified here |
| macOS | Not implemented | Designed for application-owned PCM where LiveKit installs; not qualified here |




Python 3.11 or newer is required. See [Platform support](https://github.com/KoljaB/echoff/blob/main/docs/platforms.md)
for the exact boundary between portable processing and platform-specific
capture.




## Three-minute Windows quickstart




Create an isolated environment and install the published package:




```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install echoff
```




List the endpoints Echoff can select:




```powershell
.\.venv\Scripts\python.exe -m echoff devices
```




Then start a 20-second evidence-preserving recording:




```powershell
.\.venv\Scripts\python.exe -m echoff record --duration 20
```




While it runs, play continuous speech or music through the normal speakers for
at least ten seconds. Readiness requires 7.5 seconds of active, correctly paired
reference audio plus measured suppression of the microphone signal. Speak for part of the run only if you
also want to check that near-end speech survives. The command prints the
artifact directory and writes:
