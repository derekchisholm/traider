"""Make error text safe for alerts and the research table.

Masks what could identify the account or open a door: ARNs, 12-digit AWS account ids,
AWS access key ids and secret keys, header and field values (``authorization: ...``,
``api_key=...``), the password in a link, the query string of any link, and long base64 or
hex runs that may be tokens. Then collapses whitespace and cuts the text to 300 characters.

The text may be model-written (a posture reason can echo injected news), so it is made
plain first: the input is capped so no pattern sees more than ``limit * 20`` characters,
control characters become spaces, and invisible characters (format characters, combining
marks, fillers, unpaired surrogates) are dropped. Otherwise a zero-width character could
split a key so the mask misses it, or a lone surrogate could crash the alert or table write
that follows. Every pattern is bounded or possessive, so the cost is linear in the input.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from typing import Final

LIMIT = 300
_CAP_FACTOR = 20
_MIN_CAP = 4096

# Dropped before masking: Unicode categories Cf (format: zero-width, bidi, tags), Mn and Me
# (combining marks, such as U+034F), Cs (unpaired surrogates), plus blank-looking fillers.
_INVISIBLE_CATEGORIES: Final = frozenset({"Cf", "Mn", "Me", "Cs"})
_FILLERS: Final = frozenset({0x115F, 0x1160, 0x2800, 0x3164, 0xFFA0})


@functools.cache
def _invisible() -> re.Pattern[str]:
    ranges: list[tuple[int, int]] = []
    # Planes 4-13 are unassigned and 15-16 are private use: nothing to hide there.
    for cp in [*range(0x40000), *range(0xE0000, 0xE1000)]:
        if cp in _FILLERS or unicodedata.category(chr(cp)) in _INVISIBLE_CATEGORIES:
            if ranges and ranges[-1][1] == cp - 1:
                ranges[-1] = (ranges[-1][0], cp)
            else:
                ranges.append((cp, cp))
    return re.compile(
        "[" + "".join(f"{re.escape(chr(lo))}-{re.escape(chr(hi))}" for lo, hi in ranges) + "]"
    )


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# scheme://user:password@host -> keep the user, mask the password.
_USERINFO = re.compile(r"(\b[a-z][a-z0-9+.-]{0,20}://[^\s/:@]{1,256}:)[^\s/@]{1,256}@", re.I)
# The query or fragment of a link. The path part is possessive so a miss costs one pass.
_URL_QUERY = re.compile(r"(https?://[^\s?#]{1,2048}+)[?#]\S*+", re.I)
_ARN = re.compile(r"arn:aws[a-zA-Z-]{0,20}:[^\s\"',;)]+")
# Header or field values: keep the name, mask the value (and a Bearer or Basic scheme word).
_FIELD = re.compile(
    r"(\b(?:[A-Za-z0-9_-]{0,40}[_-])?"
    r"(?:x-amz-security-token|aws_secret_access_key|aws_session_token|authorization"
    r"|x-finnhub-token|api[_-]?key|token|secret|password)\b[\"']?\s{0,8}[:=]\s{0,8}[\"']?)"
    r"(?:(?:bearer|basic|token)\s{1,8})?\S+",
    re.I,
)
_ACCESS_KEY_ID = re.compile(
    r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA|ABIA|ACCA)[A-Z0-9]{16}\b"
)
_ACCOUNT = re.compile(r"(?<!\d)\d{12}(?!\d)")
# An AWS secret access key: exactly 40 characters of [A-Za-z0-9/+=] between non-key
# characters. A run that is only lowercase letters and slashes is a path, not a key.
_SECRET_KEY = re.compile(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])")
_PATHLIKE = re.compile(r"[a-z/]+")
# A long base64-like run (session tokens, signatures): 32 or more characters with both
# cases, and a digit or a slash or plus. All-lowercase paths such as /marketdata/v1/markets
# stay readable.
_LONG_RUN = re.compile(r"(?<![A-Za-z0-9/+=_-])[A-Za-z0-9/+=_-]{32,}(?![A-Za-z0-9/+=_-])")
# 20 or more key-like characters with at least one digit: keys, tokens, hashes. A slash
# ends a run, so paths such as /marketdata/v1/markets stay readable.
_TOKEN = re.compile(r"(?<![A-Za-z0-9+=_-])(?=[A-Za-z0-9+=_-]*\d)[A-Za-z0-9+=_-]{20,}")


def _mask_secret_key(match: re.Match[str]) -> str:
    return match.group() if _PATHLIKE.fullmatch(match.group()) else "***"


def _mask_long_run(match: re.Match[str]) -> str:
    run = match.group()
    mixed = run != run.lower() and run != run.upper()
    return "***" if mixed and re.search(r"[0-9/+]", run) else run


def scrub(text: str, limit: int = LIMIT) -> str:
    text = text[: max(limit * _CAP_FACTOR, _MIN_CAP)]
    text = _invisible().sub("", text)
    text = _CONTROL.sub(" ", text)
    text = _USERINFO.sub(r"\1***@", text)
    text = _URL_QUERY.sub(r"\1?***", text)
    text = _ARN.sub("arn:***", text)
    text = _FIELD.sub(r"\1***", text)
    text = _ACCESS_KEY_ID.sub("***", text)
    text = _ACCOUNT.sub("***", text)
    text = _SECRET_KEY.sub(_mask_secret_key, text)
    text = _LONG_RUN.sub(_mask_long_run, text)
    text = _TOKEN.sub("***", text)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..." if limit > 3 else "..."[:limit]
