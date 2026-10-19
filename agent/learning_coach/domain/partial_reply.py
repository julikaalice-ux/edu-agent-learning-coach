"""Reading the reply text out of JSON that is still being written.

A model writes its reply as one JSON object. While it writes, only the text
of the first "message" string is of use to a learner; everything else stays
hidden. These functions read that text from an unfinished buffer. They never
decide whether the text may be shown.
"""

import json
import re

_MESSAGE = re.compile(r'"message"\s*:\s*"')
_HEX = frozenset("0123456789abcdefABCDEF")
_ESCAPES = frozenset('"\\/bfnrt')


def partial_message(raw: str):
    """The text of the first "message" string written so far, and the raw text before it.

    Returns (None, raw) until that string has started. The text stops before
    an escape that is not complete yet, before half of a surrogate pair, and
    before anything the final (strict) parse would reject, such as a raw
    line break inside the string.
    """
    match = _MESSAGE.search(raw)
    if match is None:
        return None, raw
    body, pos = raw[match.end():], 0
    while pos < len(body):
        char = body[pos]
        if char == '"' or char < " ":
            break
        if char != "\\":
            pos += 1
        elif pos + 1 < len(body) and body[pos + 1] in _ESCAPES:
            pos += 2
        elif body[pos + 1:pos + 2] == "u" and pos + 6 <= len(body) and set(body[pos + 2:pos + 6]) <= _HEX:
            pos += 6
        else:
            break  # not complete yet, or not valid JSON
    try:
        text = json.loads('"' + body[:pos] + '"')
    except ValueError:
        return "", raw[:match.start()]
    if text and "\ud800" <= text[-1] <= "\udbff":
        text = text[:-1]
    return text, raw[:match.start()]


def written_value(head: str, key: str) -> str | None:
    """The last complete string value written for key in head, or None."""
    found = re.findall(r'"' + re.escape(key) + r'"\s*:\s*"([^"\\]*)"', head)
    return found[-1] if found else None
