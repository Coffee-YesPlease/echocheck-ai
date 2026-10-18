# Approved v0.2 changes

C1: Named accounts provisioned by a local administrator, authenticated sessions, role/site/task assignment checks, distinct participants and server-issued device identities.

C2: Supervisor acknowledgment is separate from final approval. Every discrepancy cycle requires a new consented worker recording. Typed worker text cannot pass verification.

C3: Immutable instruction revisions, recordings, attempts, consent and approvals; atomic version checks reject stale operations and late results. API history is append-only, with database protection/backup remaining an installation responsibility.

C4: Explicit release, blocked pending/failed states, immediate invalidation of old approvals when a recording starts, operation leases, session cancellation, retry recovery, withdrawal and expired-evidence invalidation.

C5: Generic supervisor-reviewed requirements replace six-field matching in the release path. Strict provider schemas, exact requirement coverage and transcript quotations, uncertainty/extra-action handling, bounded requests and sanitized failures. The legacy parser remains demonstrational only.

C6: Supervisor and worker voice capture with explicit policy-bound consent, stop/cancel controls, private bounded audio, server-side transcription, separate-device handoff and replay checks.

Interface: simple mobile-first presentation, system fonts, accessible information popovers, clear blocked states, connection freshness checks and no default or prefilled credentials.

Deployment: placeholders, development release disabled, non-root Docker instructions, private persistent-volume paths, explicit proxy trust, named-account provisioning and audio purge tools.

Existing v0.1 database and simulated approvals are preserved and never automatically trusted.
