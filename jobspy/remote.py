"""Classifies a job posting as remote, hybrid, on-site or unclear from its text."""

from __future__ import annotations

import re

REMOTE = "remote"
HYBRID = "hybrid"
ONSITE = "onsite"
UNCLEAR = "unclear"

# Explicit statements that the job is NOT remote.
_ONSITE_RE = re.compile(
    r"\b(?:no|not|non|without)[\s-]+(?:a\s+)?(?:fully[\s-]+)?remote\b"
    r"|\bremote\s+(?:work|working)\s+(?:is|are)\s+(?:not|unavailable)"
    r"|\bremote\s+(?:work|working)\s+(?:is\s+)?not\s+(?:available|possible|offered)"
    r"|\bon[\s-]?site\s+(?:only|position|role|job)\b"
    r"|\b(?:100%|fully|must\s+work)\s+(?:on[\s-]?site|in[\s-]the[\s-]office)\b"
    r"|\bin[\s-]office\s+(?:only|role|position)\b"
    r"|\b(?:must|required\s+to)\s+(?:be\s+)?(?:work\s+)?(?:on[\s-]?site|in\s+the\s+office)\b",
    re.I,
)

_HYBRID_RE = re.compile(
    r"\bhybrid\b"
    r"|\b\d\s*(?:-\s*\d\s*)?days?\s+(?:a|per|each|every|/)\s*week\s+(?:in|at|from)\s+(?:the\s+|our\s+)?(?:office|site)"
    r"|\b(?:in|at)\s+(?:the\s+|our\s+)?office\s+\d\s*(?:-\s*\d\s*)?days?\s+(?:a|per|each|every)\s+week",
    re.I,
)

# Phrases that say the job is remote, wherever they appear in the text.
_STRONG_REMOTE_RE = re.compile(
    r"\b(?:fully|100%|completely|entirely|totally)[\s-]+(?:remote|distributed)\b"
    r"|\bremote[\s-]+first\b"
    r"|\bwork(?:ing)?\s+(?:from\s+anywhere|from\s+home|remotely)\b"
    r"|\b(?:this|the)\s+(?:is\s+a\s+)?(?:role|position|job)\s+is\s+(?:fully\s+)?remote\b"
    r"|\bremote\s+(?:position|role|job|opportunity|work)\b"
    r"|\bwfh\b"
    r"|\bfully\s+distributed\b",
    re.I,
)

# "Remote" in the title/location is a stronger signal than in a long description.
_HEADLINE_REMOTE_RE = re.compile(r"\bremote(?:ly)?\b|\bwork\s+from\s+home\b|\bwfh\b|\banywhere\b", re.I)


def classify_remote(
    title: str | None,
    location: str | None,
    description: str | None,
    work_from_home_type: str | None = None,
) -> str:
    """Returns "remote", "hybrid", "onsite" or "unclear".

    "onsite" and "hybrid" are only returned on an explicit statement, so a job
    with no description is "unclear" rather than wrongly "onsite".
    """
    title = title or ""
    location = location or ""
    description = description or ""
    wfh = (work_from_home_type or "").lower()
    headline = f"{title} {location}"
    text = f"{headline} {description}"

    explicit_remote = bool(_STRONG_REMOTE_RE.search(text)) or "remote" in wfh
    headline_remote = bool(_HEADLINE_REMOTE_RE.search(headline))

    if "hybrid" in wfh or _HYBRID_RE.search(text):
        # "Fully remote ... no hybrid" style text is rare; a hybrid mention wins
        # unless the posting also says fully/100% remote.
        if re.search(r"\b(?:fully|100%|completely)[\s-]+remote\b|\bremote[\s-]+first\b", text, re.I):
            return REMOTE
        return HYBRID
    if _ONSITE_RE.search(text):
        return ONSITE
    if explicit_remote or headline_remote:
        return REMOTE
    return UNCLEAR
