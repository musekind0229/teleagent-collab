Local-only ordinary JSON/JSONL report collector and offline viewer

Run synthetic regression from this directory:
  python -B -m unittest discover -s tests -v

Use the module CLI --help for manifest arguments. Explicit manifest entries and
allowlisted roots are mandatory. Only documented normalized JSON and ordinary
Claude/Codex/generic dialogue JSONL shapes are supported, not all producer logs.
DB/WAL, unsupported suffixes and path reparse ancestors are rejected. This is
not an OS sandbox or complete semantic/credential detector. Reports are private;
never commit actual manifests, dialogue, source indexes or generated reports.

Origin: real collab AGY collector/parser repairs and hash-CSP/display/layout
implementations, mechanically integrated and independently synthetic-tested.
No private source/report is included in this package. Inference and role labels
are heuristics, not verified personal facts. Time/context/source are inert text.
The exporter writes CSV as an inert side artifact; spreadsheet formula safety
is NOT verified: do not open real private CSV in spreadsheet applications.
HTML uses exact trusted-script CSP hashes and safe JSON/textContent rendering.

Known delivery boundaries: native implementation succeeded; original product
review failure/incomplete verdict is retained and not rewritten by packaging.
Synthetic tests do not prove full chat coverage, stable autonomous orchestration,
account isolation, all races, all unknown formats or secret absence. No memory
writeback, network, deployment, automatic source opener or remote upload exists.
Runtime glue failures (review material caps/scope, review transport completion,
source-selector completeness and busy notification handoffs) are separate from
this standalone tested collector. Do not vendor task-specific launchers or logs.
