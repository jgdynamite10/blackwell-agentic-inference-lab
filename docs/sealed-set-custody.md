# Sealed qualification-set custody

Credential-free tooling that can later import two externally supplied opaque
bundles and record a content-free custody manifest. This track does not
generate qualification tasks, read task catalogs, contact a provider, or
run inference.

SHA-256 digests provide integrity, not confidentiality. The manifest is not
encryption and is not a cryptographic seal. Key management is outside this
track.

## Invocation

The controller is a module. It is not registered as a console script.

```bash
python -m blackwell_lab.sealed_sets.controller validate-synthetic
python -m blackwell_lab.sealed_sets.controller approval-phrase
python -m blackwell_lab.sealed_sets.controller import-bundles \
  --repo /absolute/canonical/checkout \
  --commit <40-lowercase-hex> \
  --controller-digest sha256:<64-lowercase-hex> \
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

`approval-phrase` prints the unbound phrase template. Formatting or printing
the phrase does not approve generation and does not import a set.

`import-bundles` is the later owner-authorized import. It accepts two
external opaque directories that already exist. It does not create tasks.

`verify` and `receipt` recompute digests and return a content-free receipt.
Verification is read-only. The stored receipt keeps `operation` equal to
`import`. The returned receipt uses `verify` or `receipt`.

## Approval phrase

The exact phrase, with the frozen commit and the controller digest
substituted, is:

```text
I approve sealed qualification-set generation at canonical commit {commit} using controller digest {controller_digest}
```

`{commit}` is forty lowercase hexadecimal characters. `{controller_digest}`
is `sha256:` plus sixty-four lowercase hexadecimal characters, the digest
defined below. The import path requires this exact string. This document
does not supply that approval, and this repository does not run generation.

The controller-source digest is SHA-256 over the package's `*.py` files in
filename order. Each file contributes `filename`, a NUL, the raw SHA-256 of
the file bytes, and a NUL. The published form prefixes `sha256:`.

## Custody invariants

- The checkout named by `--repo` must be clean and its `HEAD` must equal
  the supplied commit.
- The running controller-source digest must equal the supplied digest, and
  both are stored in the manifest.
- The output root must be an absolute path outside every Git repository,
  including the supplied checkout and this repository. Relative paths,
  repository-internal paths, and any symlink component are rejected.
- The destination must be absent, or an empty directory with mode `0700`.
  A nonempty destination is rejected and is not overwritten.
- New directories are mode `0700`. New files are mode `0600`. Any other
  mode on the tree or on an input bundle is rejected.
- Each stage contains exactly twenty opaque tasks. Identifiers match
  `^[a-z0-9][a-z0-9-]{7,63}$`, are unique within and across stages, and
  are not written to the public manifest.
- Content digests are unique within and across stages. Development bytes
  must not reappear in holdout.
- Each file has a SHA-256 digest. Each stage has one aggregate digest over
  the sorted `identifier`, tab, content digest lines. The custody manifest
  digest is SHA-256 of the canonical JSON document with
  `custody_manifest_digest` removed.
- The public manifest schema forbids additional properties. It has no
  fields for task bodies, accepted answers, scenario names, private
  filenames, or task identifiers.
- If any invariant fails before the manifest is finalized, the attempt is
  scrubbed. A failed import does not leave a schema-valid finalized
  manifest. A later verification failure rejects the set and does not
  rewrite it. A second import into an already finalized directory is
  rejected and leaves the existing manifest unchanged.
- Hashes bind bytes to the frozen commit and controller digest. They do
  not conceal those bytes.

The public manifest is `manifest.json`. A private index and content-addressed
blobs live under `private/` so the aggregate can be recomputed. That index
is mode `0600` and is never printed. The content-free receipt is
`receipt.json`.

## Later sequence

This sequence is documentation only. It is not executed by this change.

Real development and holdout sets may be created only after all of the
following are true:

1. The P2 implementation is merged to canonical `main`.
2. Post-merge CI on that commit passes.
3. The code is frozen at one exact canonical commit.
4. The owner provides the digest-bearing generation approval for that
   commit and for the controller-source digest of that frozen tree.

The later local operation then imports exactly twenty development tasks and
exactly twenty holdout tasks into an external directory. It returns only
the two counts, the two aggregate digests, the controller commit, the
controller digest, the custody-manifest digest, and pass/fail status.

## Rebase after P2 merges

This branch is file-disjoint from the P2 controller branch and was cut from
canonical commit `589d4f4bfe53367edddaefd18caf55899622510d`. Do not generate
sets on this pre-rebase commit, and do not treat this pull request as the
generation base.

After the P2 implementation merges:

1. Rebase this branch onto the exact canonical commit that contains that
   merge.
2. If the rebase is not clean, stop. Do not generate.
3. Recompute the controller-source digest from the rebased tree.
4. Freeze that rebased commit. The generation approval must name that
   commit and the recomputed digest.
5. Live import remains a separate owner-run local command.
