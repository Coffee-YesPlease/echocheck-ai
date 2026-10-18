# EchoCheck AI v0.4 worker handoff

The project manager issues the task. A shop-floor worker explains it in their own words on a phone or shared device operated by the assigned supervisor. The worker has no app account and needs no personal device. The supervisor identifies the worker, explains the recording notice and confirms the worker agreed before capture.

AI compares the published requirements with the worker's recorded words. The comparison shows what the project manager asked, what the worker said and which requirements conflict, are missing or remain uncertain. The supervisor explains discrepancies and acknowledges the correction. The same worker must provide a fresh recording after that acknowledgment. A passing attempt still needs the project manager's confirmation and the supervisor's review of the worker's understanding.

## Changes and preserved boundaries

- Three preset accounts and the admin perspective switcher are unchanged. The real admin principal remains in the audit trail when demonstrating another role.
- Speaker and device operator are separate records. A worker name or badge is required, together with explicit witnessed worker consent and the current recording policy.
- The first validated, stored worker recording fixes that worker reference for the task. Canceling capture, denying microphone access or uploading invalid audio before storage does not freeze a typo. A different worker requires a new task.
- The assigned supervisor acknowledges discrepancies in prototype mode. The project manager cannot acknowledge on their behalf. The supervisor cannot change the project manager's published intent.
- Acknowledgment alone cannot approve work. Replayed audio, changed workers, stale evidence, changed consent and processing failures cannot bypass the fresh-recording requirement.
- Older recordings without worker attribution remain historical. They cannot be relabeled as worker voice or approved; the task requests a fresh, named worker recording. The exact earlier instruction-consent policy remains valid only for the project manager's instruction when its provider, model, retention and version still match. It never substitutes for worker consent.
- New tables are additive. Existing accounts, task history, recordings and environment files are preserved. No password reset or provider credential is included in this update.
- Release to real work remains disabled in prototype mode. Production-mode behavior retains its existing separate-account and device requirements; this update changes the prototype handoff the user clarified.

## Verification

On 18 October 2026, the full suite passed **175 tests in 87.19 seconds**, with warnings treated as errors. This includes **18 new worker-handoff cases** and the existing 157 cases. A separate targeted run passed all 35 prototype and handoff cases. The frontend passed **22 DOM contract checks**, JavaScript syntax validation, HTML ID/reference checks and an independent review of role-specific controls.

After installation, live local HTTP checks passed for the v0.4 assets and recording notice, all three existing logins, admin perspective changes and disabled real-work release. These checks created no tasks or recordings, and their temporary sessions were signed out.

Tests use temporary databases, synthetic WAV audio and a fake speech/comparison provider. The new regression cases cover the correction cycle, actor attribution, strict consent, worker identity changes, invalid uploads, legacy recovery and provider failures alongside the existing suite.

Browser/DOM checks cover labels, role visibility and capture requests. They do not establish microphone performance, speaker identity, speech recognition accuracy or comparison accuracy in plant noise. Provider credentials and deployment configuration remain placeholders, so real external speech processing is not exercised. The local environment still needs the configured provider and compressed-audio decoder to process browser recordings.
