# Learning cards (2026-10-24)

The UI reads program-owned data; it never infers stored notes or wrong answers from response wording.

## Transport

POST `/turn` with `conversation` and `learning_action`. Actions: `read`, `records`, `compile`, `retry`, `reveal`, `edit`. Include a fresh event_id per user operation; preserve it for retrying an uncertain submission. Responses contain `learning_protocol: 1`, `ok`, `conversation`, `view`, `message`, and optional `result`.

Both local `backend/proxy.py` and Space `space/app.py` forward these actions through the existing authenticated Responses conversation as reserved `/learning <JSON>` control messages. The pipeline handles them under its session lock, before invoking models. Prior control requests/results are removed from teaching history. The existing host determines the memory owner, not a learner_id supplied by the browser. Only compile invokes the notes model. A host running old code returns `learning_protocol_unavailable`; do not guess records from its text.

## Records

SQLite adds learning_cards, retry_events and learning_reveals without rewriting old records. Disabling records hides cards and stops new writes; deletion removes all three tables' rows for the owner. Independent wrong answers save the original question and private grading key in the existing grading transaction. Initial card views omit keys, explanations and prior selections. Retry checks the owner, recording setting and option, grades deterministically and deduplicates event IDs. Retries never increase mastery. Viewing an explanation, or receiving it after a correct retry, marks subsequent retries as assisted.

Knowledge notes are an optional backward-compatible study_notes addition to NOTES_SCHEMA. Teaching source turns are checked before the entire note transaction commits. They are separate from learner evidence and mastery. User edits are marked unverified and cannot edit source IDs or grading data. UI control messages do not consume teaching turn numbers after they are removed from history.

## Validation and deployment

Run `agent/.venv/bin/python -m unittest discover -s tests` and `npm run build --prefix frontend`. Test learning_cards covers grading-to-card flow, retries/deduplication, disabled records, owner isolation, editing, source validation, compilation cursor, proxy envelope and hosted session boundaries. Local browser QA uses temporary SQLite, fixture model results and the production frontend; it does not verify live model note quality.

Deploy the agent and both relevant proxy/UI build together. This change has not deployed itself. Historical wrong-answer records without an original question snapshot cannot be reconstructed into retry cards. Original picture links can expire; the UI disables submitting image-dependent questions whose image cannot load and offers a variant request.
