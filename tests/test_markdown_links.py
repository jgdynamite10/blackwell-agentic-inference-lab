"""Internal Markdown relative-link validation.

External https links are cited official sources and are not fetched here.
Relative targets must resolve inside the repository.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
SKIP_DIR_NAMES = {".git", ".venv", ".terraform", "dist", "build", "__pycache__"}


def _markdown_files() -> list[Path]:
    files = []
    for path in ROOT.rglob("*.md"):
        if SKIP_DIR_NAMES.intersection(path.parts):
            continue
        files.append(path)
    return files


def test_relative_markdown_links_resolve():
    missing: list[str] = []
    for markdown in _markdown_files():
        text = markdown.read_text(encoding="utf-8")
        for match in LINK_RE.finditer(text):
            raw = match.group(1).strip()
            target = raw.split()[0].split("#", 1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:", "ftp://")):
                continue
            resolved = (markdown.parent / target).resolve()
            try:
                resolved.relative_to(ROOT.resolve())
            except ValueError:
                missing.append(f"{markdown.relative_to(ROOT)} -> {target} (escapes repository)")
                continue
            if not resolved.exists():
                missing.append(f"{markdown.relative_to(ROOT)} -> {target}")
    assert missing == [], "broken relative Markdown links:\n" + "\n".join(missing)
