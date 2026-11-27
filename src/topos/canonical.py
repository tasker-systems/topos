"""The canonical JSON form topos hashes, so a document read later can be shown to be the one frozen.

The form is the document serialized with sorted keys, separators `,` and `:`, UTF-8, and no ASCII
escaping. Every `sha256` a topos document records is over this form.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_bytes(document: Any) -> bytes:
    """Serialize `document` in the canonical form."""
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256(document: Any) -> str:
    """The lowercase hex SHA-256 of `document`'s canonical form."""
    return hashlib.sha256(canonical_bytes(document)).hexdigest()
