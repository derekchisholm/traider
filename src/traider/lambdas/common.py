"""Shared pieces for the Lambda handlers: HTTP responses and a plain HTML page.

Standard library only. These functions are deployed as source with nothing bundled.
"""

from __future__ import annotations

import html
from typing import Any

_HEADERS = {
    "cache-control": "no-store",
    # The callback address carries a one-time code; never pass it on as a referrer.
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
}

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{title}</title>
<style>
body {{ font: 16px/1.5 system-ui, sans-serif; max-width: 34rem; margin: 3rem auto; padding: 0 1rem;
       color: #1a1a1a; background: #fafafa; }}
h1 {{ font-size: 1.3rem; }}
a.button, button {{ display: inline-block; padding: .6rem 1rem; border: 0; border-radius: .4rem;
       background: #0b5cad; color: #fff; font: inherit; text-decoration: none; cursor: pointer; }}
input[type=text] {{ width: 100%; padding: .5rem; font: inherit; box-sizing: border-box; }}
ol {{ padding-left: 1.2rem; }}
li {{ margin-bottom: 1rem; }}
</style>
</head>
<body>
<h1>{title}</h1>
{body}
</body>
</html>
"""


def escape(text: object) -> str:
    return html.escape(str(text), quote=True)


def page(status: int, title: str, body_html: str) -> dict[str, Any]:
    """An HTML response. ``body_html`` must already be escaped where it carries input."""
    return {
        "statusCode": status,
        "headers": {**_HEADERS, "content-type": "text/html; charset=utf-8"},
        "body": _PAGE.format(title=escape(title), body=body_html),
    }


def message(status: int, title: str, text: str) -> dict[str, Any]:
    """An HTML response with one escaped paragraph."""
    return page(status, title, f"<p>{escape(text)}</p>")


def redirect(location: str) -> dict[str, Any]:
    return {"statusCode": 302, "headers": {**_HEADERS, "location": location}, "body": ""}


def not_found() -> dict[str, Any]:
    return message(404, "Not found", "There is nothing here.")
