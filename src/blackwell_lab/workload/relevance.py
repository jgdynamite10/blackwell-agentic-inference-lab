"""Diagnosis-to-evidence relevance for ``workflow-controller-v2``.

The contract is a pure function of public tool-result metadata and the
selected diagnosis id already returned to the agent. It classifies that
text into generic diagnostic categories and identifier tokens. A retrieved
runbook's title and steps are not copied onto other published diagnosis
candidates. It does not import the evaluator, the scenario catalog, sealed
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
#: Ordinary prose shorter than this is not a significant word.
_MIN_PROSE_TOKEN = 5
#: Shortest agent-visible identifier segment that can be a technical token.
_MIN_TECHNICAL_TOKEN = 2
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


def _is_short_technical_token(token: str) -> bool:
    """True for a short acronym or technical segment of an identifier.

    The diagnosis id is agent-visible metadata. Its hyphen-separated
    segments of length 2-4 are technical tokens or acronyms (``mtu``,
    ``tls``, ``oom``, ``h2``), including segments that contain a digit.
    Ordinary stopwords and pure numbers are not technical tokens.
    """
    if len(token) < _MIN_TECHNICAL_TOKEN or len(token) >= _MIN_PROSE_TOKEN:
        return False
    return token.isalnum()


def _kept_token(token: str) -> bool:
    """Keep a significant word or a short technical identifier token."""
    if token.isdigit() or token in _TOKEN_STOPWORDS:
        return False
    if len(token) >= _MIN_PROSE_TOKEN:
        return True
    return _is_short_technical_token(token)


def diagnosis_tokens(diagnosis_id: str) -> frozenset[str]:
    """Significant tokens of a published diagnosis id.

    The id is agent-visible metadata. Significant words are kept, and so
    are short technical tokens and acronyms from that id. Ordinary
    stopwords and pure numbers are dropped so a diagnosis id cannot match
    a log merely by sharing a filler word.
    """
    folded = diagnosis_id.casefold()
    return frozenset(token for token in _TOKEN.findall(folded) if _kept_token(token))


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

    Support is a shared generic category, or a diagnosis-id token that
    appears in a returned log line. Short technical tokens and acronyms
    from the diagnosis id count. Ordinary stopwords do not. Either
    condition is enough. Neither condition reads an accepted answer or
    copies a runbook category onto another published candidate.
    """
    if diagnosis_categories & log_categories:
        return True
    return bool(diagnosis_id_tokens & log_tokens)
