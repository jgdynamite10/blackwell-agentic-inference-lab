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
version 1.1.0. The decision record is
[D-0023](decision-log.md).

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

The public manifest is `manifest.json`. A private index and
content-addressed blobs live under `private/` so the aggregate and the
set identity can be recomputed. That index is mode `0600` and is never
printed. The content-free receipt is `receipt.json`.

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
never printed, never persisted, and never committed. The runner opens
only the selected stage's blobs, so a development run never reads holdout
bodies. Each blob must satisfy the versioned payload contract in
[sealed-task-payload.schema.json](../schemas/sealed-task-payload.schema.json).
The binding, loading, provenance, and fail-closed rules are recorded as
D-0024 in the [decision log](decision-log.md).

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
