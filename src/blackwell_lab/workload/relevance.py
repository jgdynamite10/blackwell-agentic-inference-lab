"""Diagnosis-to-evidence relevance for ``workflow-controller-v2``.

The contract is a pure function of public tool-call metadata, tool-result
metadata, the selected diagnosis id, and runbook title/step text already
returned to the agent. It classifies that text into generic diagnostic
categories. It does not import the evaluator, the scenario catalog, sealed
payloads, holdout material, or accepted answers, and it never reads them.
"""

from __future__ import annotations

import re

#: Generic diagnostic classes. Markers are case-insensitive substrings of
#: public diagnostic vocabulary. They are class names, not evaluator
#: predicate ids, service names, or accepted answers.
CATEGORY_MARKERS: dict[str, tuple[str, ...]] = {
    "servfail": ("servfail",),
    "upstream-health": ("upstream",),
    "cache": ("cache",),
    "readiness": ("readiness",),
    "dns": ("dns",),
    "gpu": ("gpu",),
    "capacity": ("autoscaler", "capacity"),
}

_TOKEN = re.compile(r"[a-z0-9]+")
_MIN_DIAGNOSIS_TOKEN = 5
#: Ordinary words that must not, by themselves, make a log relevant.
_TOKEN_STOPWORDS = frozenset(
    {
        "about",
        "after",
        "before",
        "change",
        "changes",
        "check",
        "config",
        "could",
        "error",
        "errors",
        "failed",
        "failure",
        "false",
        "found",
        "from",
        "message",
        "missing",
        "other",
        "query",
        "recent",
        "release",
        "should",
        "status",
        "their",
        "there",
        "these",
        "this",
        "using",
        "which",
        "would",
    }
)


def categories_in_text(text: str) -> frozenset[str]:
    """Generic categories named by ``text`` (case-insensitive markers)."""
    folded = text.casefold()
    return frozenset(
        category
        for category, markers in CATEGORY_MARKERS.items()
        if any(marker in folded for marker in markers)
    )


def _kept_token(token: str) -> bool:
    return (
        len(token) >= _MIN_DIAGNOSIS_TOKEN and not token.isdigit() and token not in _TOKEN_STOPWORDS
    )


def diagnosis_tokens(diagnosis_id: str) -> frozenset[str]:
    """Significant tokens of a published diagnosis id.

    Short tokens and ordinary stopwords are dropped so a diagnosis id cannot
    match a log merely by sharing a filler word.
    """
    folded = diagnosis_id.casefold()
    return frozenset(token for token in _TOKEN.findall(folded) if _kept_token(token))


def runbook_categories(runbook: dict) -> frozenset[str]:
    """Categories declared by a retrieved runbook's title and steps.

    Remediation ids are not inspected. They are not diagnostic class metadata.
    """
    parts: list[str] = []
    title = runbook.get("title")
    if isinstance(title, str):
        parts.append(title)
    steps = runbook.get("steps")
    if isinstance(steps, list):
        parts.extend(step for step in steps if isinstance(step, str))
    return categories_in_text("\n".join(parts))


def equivalent_query_key(query: object) -> str | None:
    """Identity of a search query for repeated-zero-match detection.

    Case and surrounding whitespace do not make two queries different. A
    non-string query has no key and cannot be a repeat.
    """
    if not isinstance(query, str):
        return None
    return " ".join(query.casefold().split())


def _usable_direct_log(payload: dict) -> bool:
    """True only for a successful direct log result.

    A zero-match search and a ``found: false`` lookup stay unusable even
    when the query text names a diagnostic category.
    """
    if payload.get("found") is False:
        return False
    total = payload.get("total_matches")
    lines = payload.get("lines")
    if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
        return False
    if not isinstance(lines, list) or not lines:
        return False
    return all(isinstance(line, dict) and isinstance(line.get("message"), str) for line in lines)


def classify_usable_log(payload: dict) -> frozenset[str]:
    """Categories of one successful direct-log observation.

    Tool-call metadata is the echoed query. Result metadata is each returned
    line's message. Unusable payloads classify as no category.
    """
    if not _usable_direct_log(payload):
        return frozenset()
    parts: list[str] = []
    query = payload.get("query")
    if isinstance(query, str):
        parts.append(query)
    lines = payload.get("lines")
    if isinstance(lines, list):
        for line in lines:
            if isinstance(line, dict) and isinstance(line.get("message"), str):
                parts.append(line["message"])
    return categories_in_text("\n".join(parts))


def log_message_tokens(payload: dict) -> frozenset[str]:
    """Tokens from returned log lines only.

    The echoed query is not a line. Injecting a diagnosis token into the
    query cannot create a token overlap.
    """
    if not _usable_direct_log(payload):
        return frozenset()
    lines = payload.get("lines")
    if not isinstance(lines, list):
        return frozenset()
    messages = [
        line["message"]
        for line in lines
        if isinstance(line, dict) and isinstance(line.get("message"), str)
    ]
    folded = "\n".join(messages).casefold()
    return frozenset(token for token in _TOKEN.findall(folded) if _kept_token(token))


def evidence_relevant(
    *,
    diagnosis_categories: frozenset[str],
    diagnosis_id_tokens: frozenset[str],
    log_categories: frozenset[str],
    log_tokens: frozenset[str],
) -> bool:
    """True when one usable log supports the selected diagnosis.

    Support is a shared generic category, or a significant diagnosis-id token
    that appears in a returned log line. Either condition is enough. Neither
    condition reads an accepted answer.
    """
    if diagnosis_categories & log_categories:
        return True
    return bool(diagnosis_id_tokens & log_tokens)
