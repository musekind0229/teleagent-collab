# Local collector reliability helpers

Small provider-neutral utilities for read-only, local artifact/memory surveys.
This is an isolated engineering candidate, not an agent platform, credential
service, or replacement for a previously frozen report.

## Implemented fixes

- Reject parent-traversal input before filesystem probes; missing sources fail
  closed. Inspect ancestors without resolving symbolic links or reparse points.
  A trusted caller must supply explicit allowed roots. Source text never grants
  additional roots or operations.
- Parse Python without executing it. Capture SyntaxWarning counts and structured
  parse errors instead of leaking inspected source diagnostics to the terminal.
- Distinguish recognizable runtime-component locations from system/install
  locations whose product relevance is unknown. A path is not authorship proof.
- Capture native subprocess output as bytes, including non-UTF8 output.
- Keep saved-memory claims, user-role statements, requests, questions, quoted
  third-party text and historical environment claims distinct. Identity/current
  truth is not certified by a log role or a saved profile.
- Retain verbatim contiguous prose spans and source line numbers; omitted code
  fences must not join separate passages into fabricated quotations.
- Attach captured source-file mtime as snapshot metadata, never as claim creation
  time. Preserve raw event-time tokens, including malformed ones.
- Derive a queue locator/status pointer from its authoritative queue; queued,
  foreground and live native execution are different. PID liveness still needs
  a process-birth check. Multiple files are not a global atomic transaction.
- Localize owned report UI script only, keeping private JSON data separate.

## Modules

`survey_reliability.py`: boundary, parser, binary capture, queue snapshot helpers.
`memory_claim_rules.py`: conservative local text attribution and source-time hints.
`report_labels.py`: readable Chinese report chrome, independent of claim data.

The caller owns source discovery, privacy screening, output scope and lifecycle.
No module opens a database, browser profile, account pool, remote endpoint or
external model. Credential screening is heuristic and may conservatively exclude
ordinary material; it is not an absolute secret-free certification. Topic labels
are local-rule inferences, not new user facts. Semantic contradictions and current
validity still require source-aware review. Nothing writes back user memories.

## Synthetic tests

Python 3.12+; standard library only. Set `TEST_WORKSPACE` to an owned disposable
fixture directory, not a source/project/backup directory, then run:

    python -B -m unittest discover -v

The fixture directory must exist. Tests create a handful of clearly synthetic
files and an owned redirect target/junction (Windows) or symlink (other systems).
No real protected drive, private transcript, memory document or credential is a
fixture. All actual survey reports, indexes, source paths and personal text must
remain outside this Git repository. Test diagnostics belong outside it too.

## Publication boundary

Commit only these helper modules, synthetic tests and minimal documentation.
Never add personal reports, real session sources, source-path inventories,
LOCAL-ONLY material, credentials or data captures. A remote repository, account
and permission must be confirmed before a development-branch push; no default
main merge, force push, visibility change or deployment is part of this code.
