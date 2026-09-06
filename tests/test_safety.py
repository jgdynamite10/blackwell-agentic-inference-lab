"""Automated data-safety scans over the synthetic workload.

These tests reject accidental real-looking account identifiers, private or
public IP addresses, credentials, provider endpoints, and provider metadata in
(a) the scenario catalog and (b) generated run output — enforcing the
workload-definition data-safety rules by machine, not by review alone.
"""

from __future__ import annotations

import ipaddress
import json
import re

import pytest
from fakes import FakeClock

from blackwell_lab.workload.runner import run_cell
from blackwell_lab.workload.scenarios import canonical_catalog_json, catalog

#: RFC 5737 (IPv4) and RFC 3849 (IPv6) documentation ranges — the only
#: networks synthetic fixtures may reference.
_ALLOWED_NETWORKS = [
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("2001:db8::/32"),
]

_IPV4_RE = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")

#: Credential-shaped and provider-metadata patterns that must never appear.
_FORBIDDEN_PATTERNS = {
    "aws access key id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "aws arn": re.compile(r"\barn:aws[a-z\-]*:", re.IGNORECASE),
    "aws 12-digit account id": re.compile(r"\b\d{12}\b"),
    "gcp api key": re.compile(r"\bAIza[0-9A-Za-z\-_]{16,}"),
    "google oauth token": re.compile(r"\bya29\.[0-9A-Za-z\-_]+"),
    "github token": re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[0-9A-Za-z]{20,}\b"),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "bearer token": re.compile(r"\bBearer\s+[0-9A-Za-z\-_\.=]{16,}", re.IGNORECASE),
    "email address": re.compile(r"\b[0-9A-Za-z._%+-]+@[0-9A-Za-z.-]+\.[A-Za-z]{2,}\b"),
    "aws endpoint": re.compile(r"amazonaws\.com", re.IGNORECASE),
    "gcp endpoint": re.compile(r"googleapis\.com|gcp\.internal", re.IGNORECASE),
    "akamai/linode endpoint": re.compile(r"linode\.com|akamai(?:\.com|\.net)", re.IGNORECASE),
    "cloud metadata service": re.compile(r"169\.254\.169\.254|metadata\.google\.internal"),
    "gcp project id flag": re.compile(r"\bprojects/[a-z][a-z0-9\-]{4,28}[a-z0-9]/", re.IGNORECASE),
}

#: Hostname-shaped strings must live under reserved documentation TLDs.
_HOSTNAME_RE = re.compile(r"\b([a-z0-9][a-z0-9\-]*(?:\.[a-z0-9][a-z0-9\-]*)+)\b")
_ALLOWED_HOST_SUFFIXES = (".example", ".invalid", ".test", ".localhost")
# Non-hostname dotted tokens that legitimately appear in fixture prose/output
# (semantic versions, decimals) are excluded by requiring at least one letter.
_HAS_LETTER = re.compile(r"[a-z]")


def _assert_text_is_safe(text: str, *, context: str) -> None:
    for description, pattern in _FORBIDDEN_PATTERNS.items():
        match = pattern.search(text)
        assert match is None, f"{context}: forbidden {description}: {match.group(0)!r}"

    for match in _IPV4_RE.finditer(text):
        try:
            address = ipaddress.ip_address(match.group(1))
        except ValueError:
            continue
        allowed = any(address in network for network in _ALLOWED_NETWORKS)
        assert allowed, f"{context}: IP {address} is outside the RFC 5737/3849 documentation ranges"


def _assert_hostnames_are_fictional(text: str, *, context: str) -> None:
    for match in _HOSTNAME_RE.finditer(text.casefold()):
        candidate = match.group(1)
        if not _HAS_LETTER.search(candidate):
            continue  # decimal numbers / versions, not hostnames
        labels = candidate.split(".")
        tld = labels[-1]
        if not _HAS_LETTER.search(tld):
            continue  # trailing label numeric -> an IP or version, handled above
        # Skip common English prose artifacts like "e.g" / file names checked
        # elsewhere; anything with 3+ labels or a known-real TLD must be
        # under a reserved documentation suffix.
        known_real_tlds = {"com", "net", "org", "io", "dev", "cloud", "ai"}
        looks_like_hostname = len(labels) >= 3 or tld in known_real_tlds
        if looks_like_hostname:
            assert candidate.endswith(_ALLOWED_HOST_SUFFIXES), (
                f"{context}: hostname-like token {candidate!r} is not under a "
                "reserved documentation TLD (.example/.invalid/.test)"
            )


class TestScenarioCatalogSafety:
    def test_catalog_contains_no_real_looking_identifiers(self):
        text = canonical_catalog_json()
        _assert_text_is_safe(text, context="scenario catalog")

    def test_catalog_hostnames_are_fictional(self):
        text = canonical_catalog_json()
        _assert_hostnames_are_fictional(text, context="scenario catalog")

    def test_fixture_log_hosts_use_example_tld(self):
        for scenario in catalog().values():
            for line in scenario.logs:
                assert line["host"].endswith(".example"), (scenario.scenario_id, line["host"])


@pytest.fixture(scope="module")
def generated_record():
    records = run_cell(
        profile_name="interactive",
        concurrency=1,
        repetitions=1,
        warmup_passes=0,
        tasks_per_repetition=4,
        scenario_ids=["elevated-latency-001", "dns-failures-001"],
        clock=FakeClock(),
    )
    return records[0]


class TestGeneratedOutputSafety:
    """Generated manifests and results must also scan clean, so nothing
    real-looking can leak through the runner either."""

    @staticmethod
    def _scan_text(record, document) -> str:
        # Run ids are locally generated UUIDs; mask them so an unlucky
        # all-digit UUID segment cannot false-positive the account-id pattern.
        return json.dumps(document).replace(record.run_id, "RUN-ID")

    def test_generated_manifest_is_safe(self, generated_record):
        text = self._scan_text(generated_record, generated_record.manifest)
        _assert_text_is_safe(text, context="generated manifest")

    def test_generated_result_is_safe(self, generated_record):
        text = self._scan_text(generated_record, generated_record.result)
        _assert_text_is_safe(text, context="generated result")

    def test_generated_raw_observations_are_safe(self, generated_record):
        """The raw task observations (turns, tool traces, terminal
        recommendations) must scan clean too."""
        text = self._scan_text(generated_record, generated_record.measured_observations)
        _assert_text_is_safe(text, context="generated raw observations")

    def test_safety_scan_actually_detects_violations(self):
        """Guard the guard: each forbidden pattern must trip the scanner."""
        # The fake access-key sample is concatenated at runtime so the literal
        # pattern never appears in the repository (it would trip gitleaks,
        # which is exactly the duplication of coverage we want to avoid).
        fake_akia = "AKIA" + "ABCDEFGHIJKLMNOP"
        bad_samples = [
            f"leaked {fake_akia} key",
            "arn:aws:iam::000000000000:role/x",
            "account 123456789012 affected",
            "host ip 10.1.2.3 unreachable",
            "endpoint s3.us-east-1.amazonaws.com",
            "curl https://storage.googleapis.com/x",
            "-----BEGIN RSA PRIVATE KEY-----",
            "contact ops@realcompany.com",
        ]
        for sample in bad_samples:
            with pytest.raises(AssertionError):
                _assert_text_is_safe(sample, context="self-test")
