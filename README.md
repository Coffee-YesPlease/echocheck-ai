# EchoCheck AI v0.5 — prototype

Seven preloaded demo tasks include spoken project-manager instructions and four worker responses each: correct, incorrect, corrected, and still incorrect. These examples work without a microphone, API key or external speech service. They use fictional sample voices and prepared comparison results.

The prototype starts with three ready-to-use accounts. No account-creation command or password selection is needed.

| View | Username | Password |
| --- | --- | --- |
| Admin | admin | admin123 |
| Project manager | manager | manager123 |
| Supervisor | supervisor | super123 |

Open **http://localhost:8000** or **http://127.0.0.1:8000**. Both local addresses work on the same port.

Admin has a **View as** selector for Admin, Project manager and Supervisor. Switching changes the current perspective without signing out. The project manager gives instructions. A shop-floor worker retells them on a device operated by the supervisor. The worker needs no account, phone or laptop. The admin overview is read-only until a task perspective is selected. No fourth login is created.

## Try the preloaded tasks

Sign in as Supervisor, or sign in as Admin and choose the Supervisor view. Open a task marked **Demo** and listen to the project-manager instruction. Choose a correct or incorrect sample response, then check the understanding.

For an incorrect response, review the comparison, explain the suggested correction and acknowledge it. You can then choose either the corrected response or another incorrect response. Another incorrect response returns to the correction step. A passing response still needs the Supervisor and Project manager to confirm from their own views; the final state is **Demo complete**. Repeat the demo to start again. Repeating preserves its history and does not reset other tasks.

The seven examples cover carton quantities, checking every carton, colour and lot labels, task order and deadline, destination and extra actions, sorting exclusions, and an uncertain quantity. All seven have both passing and failing paths.

Demo attempts and confirmations are separate from live worker evidence. Listening to an audio clip alone does not submit a response. A demo never records worker consent or releases real work. Live recording remains available through the existing task workflow below.

## Start

From this folder, use the existing virtual environment:

```sh
.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

For a fresh source checkout, first create the environment and install the dependencies:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

The included `.prototype-enabled` marker selects prototype mode automatically. The three accounts are prepared on startup. Their passwords are not reset on every restart. Updating this version preserves existing accounts and data.

Admin can demonstrate both sides in the same browser. A perspective change cancels unfinished capture, clears the previous view and reloads current task data. The signed-in admin remains recorded behind actions performed through another perspective.

## Prototype behavior

- Simple preset passwords and one-click login shortcuts.
- Project manager creates instructions and assigns the Supervisor.
- Supervisor identifies the worker by name or badge, explains the recording notice, and confirms that the worker agreed.
- The worker speaks in their own words. The supervisor operates their phone or a shared shop-floor device.
- AI compares the project manager's instructions with the worker's words and shows the supervisor the differences.
- Supervisor explains the corrections and acknowledges the discrepancies. The same worker must then provide a fresh recording; acknowledgment alone never passes verification.
- A passing recording still needs the project manager's task confirmation and the supervisor's review of the worker's understanding.
- The instruction, comparison, recording consent and information popovers remain available.
- External speech processing still requires the provider settings in `.env.example`; credentials remain placeholders.
- This mode demonstrates the workflow; it cannot issue a production work release.

The first stored worker recording fixes the worker name or badge for that task. Canceling before a valid recording is stored lets the supervisor correct an entry mistake. Use a new task for a different worker. Earlier recordings without worker attribution remain in the history and require a fresh, consented worker recording before approval.

To restore the three preset passwords later:

```sh
ECHOCHECK_MODE=prototype .venv/bin/python -m backend.admin bootstrap-prototype --reset
```

## Tests

```sh
.venv/bin/python -m pytest -q -W error -p no:cacheprovider
```

Tests use temporary databases and mocked processing. The suite covers the original workflow, preloaded demo scenarios, preset logins, perspectives, consent, worker attribution, the supervisor's correction cycle, stale approvals and failure handling. See `docs/PRELOADED-DEMO-VALIDATION.md` for this change's measured results. Earlier validation reports under `docs/` describe their respective versions.

## Other environments

An explicit `ECHOCHECK_MODE` overrides the marker. In production mode the preset accounts and perspective switcher are disabled; existing production checks remain. Previous architecture/validation notes are retained under `docs/` as historical v0.2 records. SQLite data is preserved, and the three prototype accounts are linked to explicit demo profiles. No cloud deployment is included.
