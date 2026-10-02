"""Credential-free sealed-set custody controller.

Invoke as ``python -m blackwell_lab.sealed_sets.controller``.

SHA-256 digests provide integrity, not confidentiality. This controller
does not encrypt, does not cryptographically seal, and does not generate
qualification tasks. Real development and holdout sets are a later local
operation and are not executed here.

Later sequence, not executed by this module:

1. The P2 implementation is merged.
2. Post-merge CI passes.
3. The code is frozen at one exact canonical commit.
4. The owner prepares a content-free import request and supplies the
   approval phrase bound to that request digest.
5. A local run then imports exactly those 20 development and 20 holdout
   opaque tasks into an external directory and returns only counts,
   aggregate digests, the controller commit, the controller digest, the
   custody manifest digest, and pass/fail status.

The approval phrase does not provide global anti-replay. A second custody
location is outside this tool. SHA-256 digests are integrity checks, not
encryption and not a cryptographic seal.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from blackwell_lab.sealed_sets.custody import (
    CustodyReceipt,
    import_authorized_set,
    load_bundle_directory,
    prepare_import_request,
    report_receipt,
    verify_custody,
)
from blackwell_lab.sealed_sets.model import (
    APPROVAL_TEMPLATE,
    INTEGRITY_STATEMENT,
    CustodyError,
    running_controller_digest,
    set_identity_for,
    sha256_digest,
    synthetic_placeholders,
    validate_bundles,
)


def _emit(receipt: CustodyReceipt) -> None:
    print(json.dumps(receipt.public_dict(), sort_keys=True, separators=(",", ":")))


def _validate_synthetic() -> CustodyReceipt:
    """In-memory placeholder check. Does not write a custody set."""
    development, holdout = synthetic_placeholders()
    validation = validate_bundles(development, holdout)
    controller_digest = running_controller_digest()
    identity = set_identity_for(development, holdout)
    preview = {
        "operation": "validate-synthetic",
        "controller_digest": controller_digest,
        "set_identity": identity,
        "development_aggregate_digest": validation.development.aggregate_digest,
        "holdout_aggregate_digest": validation.holdout.aggregate_digest,
        "file_digests": list(validation.file_digests),
    }
    preview_bytes = (json.dumps(preview, sort_keys=True, separators=(",", ":")) + "\n").encode()
    preview_digest = sha256_digest(preview_bytes)
    return CustodyReceipt(
        status="pass",
        operation="validate-synthetic",
        development_task_count=validation.development.task_count,
        holdout_task_count=validation.holdout.task_count,
        development_aggregate_digest=validation.development.aggregate_digest,
        holdout_aggregate_digest=validation.holdout.aggregate_digest,
        controller_commit="",
        controller_digest=controller_digest,
        custody_manifest_digest=preview_digest,
        set_identity=identity,
        import_request_digest=preview_digest,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m blackwell_lab.sealed_sets.controller",
        description=(
            "Credential-free sealed qualification-set custody. "
            + INTEGRITY_STATEMENT
            + " Key management is outside this tool."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "validate-synthetic",
        help="Validate built-in artificial placeholder bundles in memory.",
    )
    sub.add_parser(
        "approval-phrase",
        help="Print the approval phrase format without approving import.",
    )
    preparer = sub.add_parser(
        "prepare",
        help="Print a content-free import request. Does not write or approve.",
    )
    importer = sub.add_parser(
        "import-bundles",
        help="Import two external opaque bundles after the exact approval phrase.",
    )
    verifier = sub.add_parser(
        "verify",
        help="Content-free verification of a finalized external custody set.",
    )
    reporter = sub.add_parser(
        "receipt",
        help="Content-free receipt for a finalized external custody set.",
    )
    for target in (preparer, importer, verifier, reporter):
        target.add_argument("--repo", required=True)
        target.add_argument("--commit", required=True)
    for target in (preparer, importer):
        target.add_argument("--development", required=True)
        target.add_argument("--holdout", required=True)
    for target in (importer, verifier, reporter):
        target.add_argument("--output", required=True)
    importer.add_argument("--controller-digest", required=True)
    importer.add_argument("--request-digest", required=True)
    importer.add_argument("--approve", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "validate-synthetic":
            _emit(_validate_synthetic())
            return 0
        if args.command == "approval-phrase":
            print(APPROVAL_TEMPLATE)
            return 0
        repo = Path(args.repo)
        if args.command == "prepare":
            development = load_bundle_directory(args.development, repo=repo)
            holdout = load_bundle_directory(args.holdout, repo=repo)
            request = prepare_import_request(
                repo=repo,
                expected_commit=args.commit,
                development=development,
                holdout=holdout,
            )
            print(json.dumps(request, sort_keys=True, separators=(",", ":")))
            return 0
        if args.command == "import-bundles":
            development = load_bundle_directory(args.development, repo=repo)
            holdout = load_bundle_directory(args.holdout, repo=repo)
            receipt = import_authorized_set(
                repo=repo,
                output_root=args.output,
                expected_commit=args.commit,
                expected_controller_digest=args.controller_digest,
                expected_request_digest=args.request_digest,
                approval=args.approve,
                development=development,
                holdout=holdout,
            )
            _emit(receipt)
            return 0
        if args.command == "verify":
            _emit(verify_custody(args.output, repo=repo, expected_commit=args.commit))
            return 0
        if args.command == "receipt":
            _emit(report_receipt(args.output, repo=repo, expected_commit=args.commit))
            return 0
        print("BLOCKED: usage", file=sys.stderr)
        return 2
    except CustodyError as exc:
        print(f"BLOCKED: {exc.reason}", file=sys.stderr)
        return 1
    except Exception:
        print("BLOCKED: internal", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
