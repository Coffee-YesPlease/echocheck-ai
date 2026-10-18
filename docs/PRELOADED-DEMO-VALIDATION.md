# EchoCheck AI v0.5 prerecorded prototype

The user explicitly approved a demo-only exception allowing prerecorded sample voices and prepared comparisons. This implements the requested click-through prototype without pretending that examples are fresh human recordings or live AI decisions. Existing live recording and production rules remain separate.

## Seven preloaded tasks

| Task | What the examples demonstrate |
| --- | --- |
| Cartons and bottles | Reversed carton/bottle quantities; the same overall total does not satisfy the requested packing. |
| Check every carton | An omitted inspection requirement, checking only the first carton and doing steps in the wrong order. |
| Label colour and lot | A wrong colour, transposed lot identifier and keeping lots separate. |
| Order and handoff time | Sequence, delivery bay and deadline. |
| Destination and extra actions | A wrong destination and an additional action beyond the instruction. |
| Sorting and exclusions | Negation, separation and excluding items from a count. |
| A quantity is still uncertain | An incorrect quantity followed by an explicitly uncertain explanation that still needs correction. |

Each task has one project-manager instruction and four distinct worker responses: correct, incorrect, corrected, still incorrect. All 35 audio files contain local synthetic speech in English. The project manager and worker have different sample voices. Audio is mono, 16-bit PCM at 16 kHz; clips run approximately 6–12 seconds, with about 9.8 MiB of audio in total. Every clip has a checksum, transcript checksum and duration in the asset manifest.

## Workflow and boundaries

Seven examples belong to the preset Project manager and assigned Supervisor. They are visible alongside ordinary tasks and require no microphone, external provider or new login. Demo labels and information popovers explain their source.

The Supervisor submits a correct or incorrect example. An incorrect example requires an explicit Supervisor correction acknowledgment before a follow-up response is accepted. A still-incorrect follow-up needs another acknowledgment. Each submission creates a new versioned demo attempt linked to the current correction. Listening alone changes nothing. A passing attempt still requires separate Project manager and Supervisor confirmations before the demo completes. Repeat starts a new demo cycle and retains the history.

Demo storage, confirmations and audio are separate from real worker consent and evidence. Playback requires the assigned authenticated account. State changes require the current task version, authorization and CSRF protection. Admin perspective actions retain the real Admin identity. Production does not expose these demo actions, and no demo can release real work. Existing accounts, recordings, task history and provider placeholders are preserved.

## Verification

On 18 October 2026, the full suite passed **220 tests in 125.92 seconds**, with warnings treated as errors. This includes **45 new demo cases** plus all 175 previous cases. The targeted demo run passed 45/45. Frontend checks passed **38 demo contracts and 22 retained live-flow contracts**, together with JavaScript syntax, HTML IDs/references, content-security and cache-version checks.

The backend tests exercise all seven initial-correct paths and all seven incorrect → acknowledgment → still incorrect → acknowledgment → corrected paths, followed by both confirmations. They verify that the provider is never called, no real consent/recording/approval evidence is created, all three accounts are retained, stale and unauthorized actions are rejected, production demo routes are unavailable, history survives repeat, and missing or modified audio blocks a decision.

A browser walkthrough against an isolated database verified all seven task cards, actual instruction audio playback (duration 5.99 seconds with no media error), the full repeated-correction loop, separate confirmations via Admin perspective changes, completion and repeat. At a 390 × 844 viewport, the page stayed within the viewport width. The temporary viewport was reset and the test session signed out. A screenshot of the seven Project-manager tasks accompanies the delivery.

After installing the checked source and media, live local HTTP checks passed for all three existing logins, all seven preloaded tasks and all 35 WAV assets. The installed app also completed the incorrect → acknowledgment → still incorrect → acknowledgment → corrected → both confirmations → repeat cycle. All seven examples were left ready to begin. Snapshots of ordinary task, account, recording, consent, transcript, instruction, acknowledgment and approval tables matched before and after the demo checks. Verification sessions were signed out. No password reset or real-worker recording was performed.

The example comparisons are curated fixtures validated against the app's strict comparison schema. They demonstrate workflow behavior; they do not measure live transcription or AI accuracy in plant noise.
