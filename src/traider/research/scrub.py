"""Make error text safe for alerts and the research table.

Masks what could identify the account or open a door: ARNs, 12-digit AWS account ids,
long base64 or hex runs that may be keys or tokens, and the query string of any link.
Then collapses whitespace and cuts the text to 300 characters.

The text may be model-written (a posture reason can echo injected news), so it is also
made plain first: control characters become spaces, and invisible or unpaired-surrogate
characters are dropped. Otherwise a zero-width character could split a key so the mask
misses it, or a lone surrogate could crash the alert or table write that follows.
"""

from __future__ import annotations

import re

LIMIT = 300

# Invisible format characters (zero-width, bidi controls, soft hyphen, BOM, tag characters)
# and unpaired surrogates: dropped, so they cannot split a key or break UTF-8 encoding.
_INVISIBLE_RANGES = (
    (0x00AD, 0x00AD),
    (0x0600, 0x0605),
    (0x061C, 0x061C),
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x180E, 0x180E),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x2064),
    (0x2066, 0x206F),
    (0xD800, 0xDFFF),
    (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFB),
    (0xE0001, 0xE0001),
    (0xE0020, 0xE007F),
)
_INVISIBLE = re.compile(
    "["
    + "".join(f"{re.escape(chr(lo))}-{re.escape(chr(hi))}" for lo, hi in _INVISIBLE_RANGES)
    + "]"
)
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")
_URL_QUERY = re.compile(r"(https?://[^\s?#]+)[?#]\S*")
_ARN = re.compile(r"arn:aws[a-zA-Z-]*:[^\s\"',;)]+")
_ACCOUNT = re.compile(r"(?<!\d)\d{12}(?!\d)")
# 20 or more key-like characters with at least one digit: keys, tokens, hashes. A slash
# ends a run, so paths such as /marketdata/v1/markets stay readable.
_TOKEN = re.compile(r"(?<![A-Za-z0-9+=_-])(?=[A-Za-z0-9+=_-]*\d)[A-Za-z0-9+=_-]{20,}")


def scrub(text: str, limit: int = LIMIT) -> str:
    text = _INVISIBLE.sub("", text)
    text = _CONTROL.sub(" ", text)
    text = _URL_QUERY.sub(r"\1?***", text)
    text = _ARN.sub("arn:***", text)
    text = _ACCOUNT.sub("***", text)
    text = _TOKEN.sub("***", text)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..." if limit > 3 else "..."[:limit]
