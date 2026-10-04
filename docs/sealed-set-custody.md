# Sealed qualification-set custody

Credential-free tooling that can later import two externally supplied opaque
bundles and record a content-free custody manifest. This track does not
generate qualification tasks, read task catalogs, contact a provider, or
run inference.

SHA-256 digests provide integrity, not confidentiality. The manifest is not
encryption and is not a cryptographic seal. Key management is outside this
track. Replay protection applies only to the selected custody directory.
This tool has no global anti-replay registry. An owner-controlled registry
outside this repository would be required before any global guarantee could
be claimed, and this track does not claim one.

The public manifest schema is
[sealed-set-manifest.schema.json](../schemas/sealed-set-manifest.schema.json),
version 1.2.0 (`private_layout: stage-separated`). The decision record is
[D-0023](decision-log.md); the stage-separated layout and the execution
binding are recorded as D-0024.

## Invocation

The controller is a module. It is not registered as a console script.

```bash
python -m blackwell_lab.sealed_sets.controller validate-synthetic
python -m blackwell_lab.sealed_sets.controller approval-phrase
python -m blackwell_lab.sealed_sets.controller prepare \
  --repo /absolute/canonical/checkout \
  --commit <40-lowercase-hex> \
  --development /absolute/external/development-bundle \
  --holdout /absolute/external/holdout-bundle
python -m blackwell_lab.sealed_sets.controller import-bundles \
  --repo /absolute/canonical/checkout \
  --commit <40-lowercase-hex> \
  --controller-digest sha256:<64-lowercase-hex> \
  --request-digest sha256:<64-lowercase-hex> \
  --development /absolute/external/development-bundle \
  --holdout /absolute/external/holdout-bundle \
  --output /absolute/external/empty-output \
  --approve '<exact approval phrase>'
python -m blackwell_lab.sealed_sets.controller verify \
  --repo /absolute/canonical/checkout \
  --commit <40-lowercase-hex> \
  --output /absolute/external/output
python -m blackwell_lab.sealed_sets.controller receipt \
  --repo /absolute/canonical/checkout \
  --commit <40-lowercase-hex> \
  --output /absolute/external/output
```

Failures print `BLOCKED: <reason>` on stderr and exit 1. They do not print
task bodies, accepted answers, task identifiers, private paths, or directory
listings. Unexpected errors print `BLOCKED: internal`.

## Operations

`validate-synthetic` checks twenty built-in artificial placeholder payloads
per stage in memory. It writes nothing and does not finalize a set.
`controller_commit` in that receipt is empty because no checkout was frozen.

`prepare` prints one content-free import request. It does not write a
custody set and it does not approve import. The request contains no paths,
task identifiers, task bodies, or filenames.

`approval-phrase` prints the unbound phrase template. Formatting or printing
the phrase does not approve import.

`import-bundles` is the later owner-authorized import. It accepts two
external opaque directories that already exist. It does not create tasks.
It recomputes both stage aggregates and the import-request digest
immediately before writing the manifest, and it re-reads the written blob
bytes before that manifest exists.

`verify` and `receipt` recompute digests and return a content-free receipt.
Verification is read-only. The stored receipt keeps `operation` equal to
`import`. The returned receipt uses `verify` or `receipt`.

## Commit-versus-worktree verification

The supplied repository must be a real directory. `git status` and
`git diff` are not the check. The algorithm is:

1. The supplied commit matches `^[0-9a-f]{40}$`, and `git rev-parse HEAD`
   equals it. Otherwise the reason is `commit-mismatch`.
2. `git ls-files -v -z` is read. Every record must be the tag `H`, one
   space, and a path. Tag `S` (skip-worktree), a lowercase tag
   (assume-unchanged), and any other tag are `dirty-checkout`.
3. `git ls-tree -r -z <commit>` and `git ls-files -s -z` must record the
   same path, mode, object kind, and object id. Every index stage must be
   `0`. Mode `160000` is a gitlink. Every other mode is a blob. A mismatch
   is `dirty-checkout`.
4. Each committed path is checked in the worktree. Absolute paths, `.git`,
   empty components, and `..` fail. A parent component that is a symlink
   fails. A missing path fails. Mode `120000` must be a symlink whose
   target bytes equal `git cat-file blob <oid>`. Mode `100644` or `100755`
   must be a regular file rather than a symlink, the executable bit must
   be set exactly when the mode is `100755`, and the raw file bytes must
   equal that blob. Any other mode fails closed as `dirty-checkout`.
5. `git ls-files -o --exclude-standard -z` must be empty. Any other
   untracked path is `dirty-checkout`.

A finalized manifest is not written when this check fails.

## Commit-bound controller digest

The ordered controller-source set, and the preimage order, is:

1. `src/blackwell_lab/sealed_sets/__init__.py`
2. `src/blackwell_lab/sealed_sets/controller.py`
3. `src/blackwell_lab/sealed_sets/custody.py`
4. `src/blackwell_lab/sealed_sets/model.py`

For each path, in that order, the commit tree must contain a regular blob
of mode `100644` or `100755`. A missing path is `controller-absent`. Any
other object kind or mode is `controller-digest-mismatch`. The blob bytes
are read with `git cat-file blob <oid>`.

The preimage is the concatenation, for each entry, of the UTF-8 path, a
NUL, the ASCII git mode, a NUL, the raw 32-byte SHA-256 of the blob bytes,
and a NUL. The published digest is `sha256:` plus the hexadecimal SHA-256
of that preimage. A different path order is a different digest. A
different mode is a different digest.

The running digest uses the same paths and the same preimage. The bytes
and mode come from the files that are executing: mode `100755` when any
executable bit is set, otherwise `100644`. The running digest must equal
the commit-bound digest. Each executing file's resolved path must be the
worktree file at that canonical path in the supplied repository. A
byte-identical copy in another clean repository is `controller-location`.
Executing this code against a clean repository whose supplied commit does
not contain those paths is `controller-absent`.

## Import request and approval phrase

`prepare` calculates one content-free request. Its fields are:

| Field | Meaning |
| --- | --- |
| `request_version` | Request schema and tool version. The value is `1.0.0`. |
| `controller_commit` | Frozen canonical commit. |
| `controller_digest` | Commit-bound controller digest, `sha256:` plus 64 hex characters. |
| `set_identity` | Order-sensitive identity of the stage content digests. |
| `development_task_count` | `20`. |
| `holdout_task_count` | `20`. |
| `development_aggregate_digest` | Id-sorted development aggregate. |
| `holdout_aggregate_digest` | Id-sorted holdout aggregate. |
| `import_request_digest` | SHA-256 of the canonical request with this field excluded. |

Canonical request bytes are JSON with sorted keys, separators `,` and `:`,
and a trailing newline. The set-identity preimage is UTF-8 text: the line
`development`, each development content digest in the supplied order, the
line `holdout`, and each holdout content digest in the supplied order,
including the final newline. Identifiers and bodies are not part of that
preimage. Reordering the same tasks leaves the id-sorted aggregates
unchanged and changes `set_identity` and `import_request_digest`.

The approval phrase binds that exact request digest:

```text
I approve sealed qualification-set import using request sha256:{request_digest}
```

`{request_digest}` is sixty-four lowercase hexadecimal characters, without
the `sha256:` prefix. Import accepts the phrase only when it matches the
request just recomputed from the supplied commit, the commit-bound
controller digest, and the supplied bytes. A phrase that names only a
commit and a controller digest does not authorize import. Changed,
substituted, or reordered input produces a different request digest and is
refused. An altered count is refused. An altered commit or controller
digest is refused.

## Custody invariants

- Output must be an absolute path outside every Git repository, including
  the supplied checkout and this repository. Relative paths,
  repository-internal paths, and any symlink component are rejected.
- The destination must be absent, or an empty directory with mode `0700`.
  New directories are mode `0700`. New files are mode `0600`.
- If the destination manifest already records the same `set_identity` or
  the same `import_request_digest`, the reason is `replay` and existing
  files are left unchanged. A different nonempty destination is
  `destination-nonempty` and is not overwritten.
- The same approved bytes may be imported into a different empty external
  directory. That is not a global anti-replay guarantee.
- Each stage contains exactly twenty opaque tasks. Identifiers match
  `^[a-z0-9][a-z0-9-]{7,63}$`, are unique within and across stages, and
  are not written to the public manifest.
- Content digests are unique within and across stages. Development bytes
  must not reappear in holdout.
- Each file has a SHA-256 digest. Each stage aggregate covers the sorted
  `identifier`, tab, content-digest lines. The custody manifest digest is
  SHA-256 of the canonical JSON document with `custody_manifest_digest`
  removed.
- The public manifest schema forbids additional properties. It has no
  fields for task bodies, accepted answers, scenario names, private
  filenames, or task identifiers.
- If any invariant fails before the manifest is finalized, the attempt is
  scrubbed when this tool created the destination. A failed import does
  not leave a schema-valid finalized manifest. A later verification
  failure rejects the set and does not rewrite it.
- Hashes bind bytes. They do not conceal those bytes.

The public manifest is `manifest.json`. The content-free receipt is
`receipt.json`. Private material is stage-separated:

```text
manifest.json
receipt.json
private/
  development/
    index.json
    blobs/<64-lowercase-hex>   (twenty files)
  holdout/
    index.json
    blobs/<64-lowercase-hex>   (twenty files)
```

Each stage index records that stage's `stage`, `task_count`,
`aggregate_digest`, identifier-sorted `(task_id, content_digest)` rows, and
the content-digest order. It contains nothing about the other stage. The
public manifest carries `development_index_digest` and
`holdout_index_digest`, the SHA-256 of each stage index's canonical bytes,
so a stage reader can authenticate its own index against the manifest
without touching the other stage. Indexes and blobs are mode `0600` and
are never printed. Blob paths are derived only from the selected stage's
index, never from a directory listing.

Full-custody `verify` and `receipt` read both stages: they recompute both
aggregates, check cross-stage identifier and digest disjointness, the set
identity, `file_digests`, and the exact directory contents. The execution
adapter (`blackwell-cloud qualify-agent`, including `--validate-only`)
never calls full-custody verification; see the execution binding below.

### Combined-index packages (schema 1.1.0)

Packages written under schema version 1.1.0 used one combined
`private/index.json` and one shared `private/blobs/` directory, so a
development reader necessarily decoded holdout identifiers, digests, order,
and blob filenames. That layout is no longer written and is not accepted
for execution. `validate_public_manifest` rejects a 1.1.0 manifest as
`layout-version-unsupported` before any private file is opened, and the
execution adapter fails before the model client is constructed.

Existing 1.1.0 packages are **not** migrated or rewritten in place. Any
previously finalized real package remains an immutable historical custody
object. Before any P2 execution, the real development and holdout bundles
must be re-prepared and re-imported into a **new, empty external
directory** under the stage-separated layout from a frozen canonical
commit that contains this change. That produces a new commit-bound
controller digest, a new import-request digest, a new custody-manifest
digest, and therefore requires a new owner approval phrase bound to the
new request digest. The task source bundles themselves are unchanged, and
the stage aggregate and set-identity algorithms are unchanged, so the two
stage aggregates and the set identity of a faithful re-import equal those
of the historical package; only the layout, the index digests, and the
digests derived from the controller source differ.

## Later sequence

This sequence is documentation only. It is not executed by this change.

Real development and holdout sets may be imported only after all of the
following are true:

1. The P2 implementation is merged to canonical `main`.
2. Post-merge CI on that commit passes.
3. The code is frozen at one exact canonical commit.
4. A local `prepare` prints the content-free import request for the exact
   externally supplied bundles.
5. The owner provides the approval phrase bound to that import-request
   digest.

The later local operation then imports exactly twenty development tasks and
exactly twenty holdout tasks into an external directory. It returns only
the two counts, the two aggregate digests, the controller commit, the
controller digest, the set identity, the import-request digest, the
custody-manifest digest, and pass/fail status.

## Execution binding (D-0024)

The values returned by the import are the only things that enter the
frozen P2 development and holdout configs, as a path-free `sealed_set`
block (schema version, custody-manifest SHA-256, controller digest,
import-request digest, set identity, stage, stage aggregate digest, task
count, payload schema version). The custody directory itself is passed to
`blackwell-cloud qualify-agent` as `--custody-dir` at run time; it is
never printed, never persisted, and never committed.

The adapter is stage-specific. It reads `manifest.json`, `receipt.json`,
`private/<stage>/index.json`, and the twenty blobs named by that index,
each at an exact path, with `lstat` and `open` only. It never lists,
walks, globs, or scans any directory, and it never constructs a path under
the other stage's directory, so a development run does not observe
holdout-private identifiers, digests, order, blob filenames, or bytes, and
vice versa. Damage to the other stage (missing, corrupt, unreadable, or
mode-invalid index or blob, or a missing or unreadable stage directory)
has no effect on the selected stage. The adapter does not call
full-custody verification. Each blob must satisfy the versioned payload
contract in
[sealed-task-payload.schema.json](../schemas/sealed-task-payload.schema.json).

A sealed run records `workload.task_source =
{"kind": "sealed", "digest": <stage aggregate>, "sealed_set": {...}}` in
its private run manifest and carries no `workload.catalog_digest`; a
catalog run records `task_source = {"kind": "catalog"}` together with the
in-repository `catalog_digest`. The run-manifest schema and semantic
validation make the two mutually exclusive, so a stage aggregate can never
pose as a catalog digest or the reverse. The binding, loading, provenance,
and fail-closed rules are recorded as D-0024 in the
[decision log](decision-log.md).

## Materialization of historical source entries (D-0025)

Historical external source entries carry a `task_id` and a scenario
document but no D-0024 `instance` object. The offline materializer
produces the frozen qualification schedule as D-0024 payloads and writes
ordinary bundle directories for the unchanged custody tool above. It is not
a custody operation and is not part of the commit-bound controller digest.

```bash
python -m blackwell_lab.cloud.sealed_materialize approval-phrase
python -m blackwell_lab.cloud.sealed_materialize prepare-materialization \
  --repo /absolute/canonical/checkout \
  --commit <40-lowercase-hex> \
  --development /absolute/external/development-source \
  --holdout /absolute/external/holdout-source
python -m blackwell_lab.cloud.sealed_materialize materialize-bundles \
  --repo /absolute/canonical/checkout \
  --commit <40-lowercase-hex> \
  --implementation-digest sha256:<64-lowercase-hex> \
  --request-digest sha256:<64-lowercase-hex> \
  --development /absolute/external/development-source \
  --holdout /absolute/external/holdout-source \
  --output /absolute/external/new-empty-path \
  --approve 'I approve sealed qualification-task materialization using request sha256:<64-lowercase-hex>'
python -m blackwell_lab.cloud.sealed_materialize prepare-import \
  --repo /absolute/canonical/checkout \
  --commit <40-lowercase-hex> \
  --development /absolute/external/new-empty-path/development \
  --holdout /absolute/external/new-empty-path/holdout
python -m blackwell_lab.cloud.sealed_materialize import-materialized \
  --repo /absolute/canonical/checkout \
  --commit <40-lowercase-hex> \
  --controller-digest sha256:<64-lowercase-hex> \
  --request-digest sha256:<64-lowercase-hex> \
  --development /absolute/external/new-empty-path/development \
  --holdout /absolute/external/new-empty-path/holdout \
  --output /absolute/external/new-custody-path \
  --approve 'I approve sealed qualification-set import using request sha256:<64-lowercase-hex>'
```

- Schedule: exactly one production call per stage,
  `generate_task_instances(stage_spec(stage)["template_ids"],
  stage_spec(stage)["tasks"], MEASURED_REPETITION_SEED)`, the call the
  qualification runner makes for the measured repetition. Its twenty-element
  round-robin result is the only execution schedule; templates are never
  generated separately and occurrence is never derived from source ids.
- Seed: `qualification.MEASURED_REPETITION_SEED` = `20260907`
  (`FROZEN_SEED + 1`), pinned as a schema constant.
- Output ids: the fixed values `sealed-development-0000` …
  `sealed-development-0019` and `sealed-holdout-0000` … `sealed-holdout-0019`;
  the suffix is the slot's zero-based position in the generator sequence, so
  task-id order equals materializer output order, custody index order,
  sealed runtime order, and the P1 catalog measured order. Historical
  source ids are not preserved as executable ids and influence nothing but
  the request's source aggregate.
- Frozen source gate (per stage, all required before a request exists):
  exactly twenty entries of shape `{"task_id": ..., "scenario": {...}}`
  with filename equal to `task_id` (`source-shape`); unique ids
  (`duplicate-id`); scenario ids drawn only from the stage's frozen template
  list (`scenario-set-mismatch`); every scenario document, repeated copies
  included, canonically deep-equal to `catalog()[scenario_id]`
  (`scenario-mismatch`); per-template multiset equal to the generator
  schedule, 4/4/3/3/3/3 for development and 5/5/5/5 for holdout
  (`distribution-mismatch`). Any other distribution is rejected; nothing is
  preserved or rebalanced.
- Payloads: encoded only by `encode_sealed_task` from the slot's instance
  surface, the catalog scenario, and the fixed id; decoded with
  `decode_sealed_task`; byte-identical on re-encode; and the ordered
  `(scenario_id, instance_seed, tracking_id, reported_minute)` tuples must
  equal the one-call generator result exactly.
- Output: `<output>/development/`, `<output>/holdout/` (twenty `0600`
  files each, filename = fixed task id) and a content-free
  `<output>/materialization.json`. The output path must not exist. Written
  bundles are re-read with the production bundle loader and compared with
  the schedule and the request before the record is written.
- Approval binds the request digest over commit, implementation digest,
  generator, seed, schedule rule, task-id rule, catalog digest, frozen
  template lists, source and predicted output aggregates, counts, set
  identities and frozen distribution summaries. The D-0023 import phrase
  does not authorize materialization and vice versa.
- Import: `prepare-import` and `import-materialized` load the materialized
  directories with the production bundle loader, order the tasks by their
  fixed ids, require exactly the fixed id set of each stage
  (`materialized-ids`), and hand them to the unchanged `prepare_import_request`
  and `import_authorized_set`. The predicted output set identity in the
  materialization request equals the set identity the importer records; no
  step depends on directory iteration or file creation order. The P2 adapter
  then loads the stage in task-id order, which is the generator order.

Decision record: D-0025 in the [decision log](decision-log.md).

## Rebase after P2 merges

This branch is file-disjoint from the P2 controller branch and was cut from
canonical commit `589d4f4bfe53367edddaefd18caf55899622510d`. Do not generate
sets on this pre-rebase commit, and do not treat this pull request as the
generation base.

After the P2 implementation merges:

1. Rebase this branch onto the exact canonical commit that contains that
   merge.
2. If the rebase is not clean, stop. Do not generate.
3. Recompute the commit-bound controller digest from that rebased commit
   and require the running sources to match it.
4. Freeze that rebased commit. Prepare the import request there. The
   approval phrase must bind that request digest.
5. Live import remains a separate owner-run local command.
