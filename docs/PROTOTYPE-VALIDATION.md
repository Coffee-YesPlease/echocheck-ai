# Prototype validation — v0.3

18 October 2026.

Final automated run: **157 passed in 72.41 seconds**, with warnings treated as errors. This includes the previous 140 tests and 17 new prototype regressions. The focused prototype run also passed all 17 cases.

The new cases verify:

- Exactly three preset accounts and the requested simple passwords/public role names.
- Explicit reset replaces the prior supervisor password, preserves its ID, and revokes older sessions.
- Startup/restart does not silently reset later password changes or duplicate accounts.
- Admin can switch Project manager → Supervisor → Admin while retaining its signed-in session.
- Admin overview is read-only; effective task actions follow the selected perspective.
- Both perspectives see the same assigned task; ordinary logins cannot impersonate another account.
- Switch requests reject invalid/foreign/non-demo/inactive targets and stale CSRF tokens.
- A switch cancels unfinished capture; late responses cannot overwrite the next perspective's task state.
- The full same-browser voice handoff and approvals work with simulated speech; prototype release remains disabled.
- Localhost/127.0.0.1/IPv6 loopback aliases require the same configured scheme and port. Unrelated origins remain rejected.
- Prototype profiles and switching are not accepted in production mode.

Live checks against the actual local app at http://127.0.0.1:8000 confirmed all three logins, their public roles, admin perspective switching with an unchanged session cookie, and the local-address fix. Each verification session was signed out afterward. No tasks were created by these live checks.

JavaScript syntax and frontend role checks passed. Static checks covered 102 unique HTML IDs, DOM references, local assets, cache-busted v0.3 assets and unchanged production role labels. No real microphone, external speech service or production deployment was used for these changes.
