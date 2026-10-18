# EchoCheck v0.2 frontend checks

The three frontend files were implemented and checked against the live, local QA server using the in-app browser. The server used temporary test accounts, a separate QA database, synthetic recordings, a synthetic provider, and a development release block. No actual microphone was activated and no external AI request was made during browser checks.

Verified in the browser:

- Supervisor sign-in, assigned task listing, task detail, and new task creation.
- A new draft cannot publish before its supervisor recording exists. Recording is disabled until explicit consent.
- Canonical recording disclosure, exact configured retention and recording limits are shown.
- An explanatory information popover expands and shows the approval/release explanation.
- Supervisor approval requires its explicit checkbox and applies to the displayed version. Approval and withdrawal saved successfully; the audit history reflected both actions.
- Release remains disabled in development, even with a passing voice check.
- Worker sign-in hides new-task, supervisor publication, supervisor correction, and release controls. The recording transcript is read-only; there is no typed worker understanding submission.
- Worker consent enables the recording button; worker acknowledgment enables its approval button. Neither was submitted as a real recording or work release.
- A deliberate QA server outage made the current detail status unavailable/work blocked. Approval, consent, release, and revision controls were disabled. The last successful check time remained visible and no green released indicator remained.
- Restarting the QA server restored trusted status through the normal poll, even though the task version was unchanged. No mutation was retried.
- At 390-pixel mobile width, task and consent views had no horizontal overflow. Desktop layout was also inspected at the default 1280-pixel viewport.
- Sign-out cleared task title, instruction, transcript, audit text, and credentials from the DOM, and removed playback sources. Browser viewport override was reset. The test account was signed out at the end.
- Browser console had no error or warning entries during the final stable worker check. The intentional server outage generated expected network failures before recovery.

Static checks passed: JavaScript syntax, HTML parsing, 90 unique IDs, all literal JavaScript DOM references, no inline handlers or styles, no remote stylesheet/assets, no browser persistence, and no legacy typed-worker endpoints.

Final defensive edits also neutralize list release badges until an individual task is opened and checked; unavailable task-list fetches clear stale cards. A released state with missing current release eligibility renders as work blocked. These small branches were checked in source; no released task was fabricated in development.

Limits: actual phone microphone hardware, real second-device browser capture, codec handling on physical iOS/Android, interruption during actual hardware recording, external provider behavior, and production deployment were not exercised by browser QA. Server/API tests cover the workflow rules separately.

Screenshots, with synthetic QA data only:

- supervisor-desktop.jpg
- supervisor-mobile.jpg
- worker-mobile.jpg
- offline-mobile.jpg
