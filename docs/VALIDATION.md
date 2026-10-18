# Validation — EchoCheck AI v0.2

Development validation completed on 18 October 2026.

## Automated checks

Final command:

```sh
python -m pytest -q -W error -p no:cacheprovider
```

**140 passed in 64.86 seconds. No skipped tests, expected failures or warnings.**

- 4 original local-parser examples, retained for comparison only.
- 78 strict extraction/evaluation and mocked-provider tests.
- 43 authenticated workflow, consent, recording, approval and release checks.
- 5 interrupted/expired processing and audio-cleanup recovery checks.
- 10 explicit trusted-proxy configuration checks.

All database tests use temporary databases and private audio directories. Speech tests use valid synthetic PCM WAV data and an explicit fake provider. Provider HTTP tests use mock transports. No real API key, production account, live provider call or actual workplace recording was used.

The passing workflow test deliberately enables an isolated production-shaped configuration and simulated provider. It verifies release invariants without releasing a real task. Default application settings still disable work release.

## Safety properties exercised

- Anonymous, cross-site and unassigned task/audio access is denied.
- A worker cannot edit supervisor instructions, forge a role or acknowledge discrepancies.
- Origin and CSRF checks protect mutations; consent requires a true boolean and exact current policy snapshot.
- An older consent notice or changed retention policy invalidates an open capture ticket before provider transmission.
- Switching roles in the same browser cannot satisfy the second-device recording requirement.
- Published instructions and successful evidence remain versioned; stale approvals and delayed comparisons cannot apply to changed instructions.
- Discrepancies require supervisor acknowledgment and a new recording. Repeated discrepancies repeat the cycle. Exact audio reuse is rejected.
- Both assigned participants approve the same successful evidence before explicit supervisor release. Concurrent stale approval requests are rejected.
- A fresh recording immediately revokes prior release eligibility. Cancellation, upload/decoding failure, provider error, session loss, timeout and process interruption remain blocked and retryable.
- Missing or expired audio invalidates release and cannot be retrieved.
- Withdrawing approval revokes release. Old v0.1 simulated approvals remain quarantined.
- Oversized uploads, expired/revoked sessions, unauthorized release, forged provider pass flags, incomplete provider coverage, login throttling and concurrent release/revision are blocked.
- Background save cancellation removes untracked audio; the purge command removes aged crash leftovers while preserving current saves and retained evidence.
- Local parsing is never used for production release; all reviewed requirements, evidence, uncertainties and extra actions are checked.

## Browser and source checks

The interface was exercised in the in-app browser against an isolated local QA server seeded with synthetic data. Checks covered sign-in, task creation, information popovers, voice-consent controls, task/version details, supervisor approval and withdrawal, history, disabled development release, logout, and mobile layout without horizontal scrolling. No microphone was activated. Offline/reconnect behavior was checked after stopping and restarting the QA server. See BROWSER-QA.md and the synthetic-data screenshots under docs/screenshots/.

The frontend has no external font dependency or inline event handlers. JavaScript syntax was checked separately. The original source was compared with its review snapshot before installation so unrelated user changes would not be overwritten.

## Boundaries of these results

A strict JSON/quotation validator cannot establish semantic accuracy. A language model can misunderstand negation, numbers or noisy speech even while quoting text correctly. The interface therefore shows reviewed instructions, the full worker transcript/audio and each result before distinct human approvals. Real Hindi/English and other target-language evaluation under actual shop-floor noise is still required.

Browser device identity is a server-issued cookie, not physical hardware attestation. A recording hash detects identical bytes, not malicious re-encoding or physical liveness. Private file permissions are not encryption at rest. Deployment must supply the site's device management, HTTPS, storage protection, backup, retention scheduling and account lifecycle.

Real phone microphones, compressed-audio decoding with deployed FFmpeg, a real provider/model, production TLS and deployment behavior were not validated in this pass. The Dockerfile was prepared but no container build or cloud deployment is claimed. The included synthetic dataset has no measured accuracy claim.
