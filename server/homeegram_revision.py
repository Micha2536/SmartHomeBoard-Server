"""Shared optimistic concurrency token for original native Homeegrams."""

import hashlib
import json


def homeegram_revision(homeegram):
    # Runtime notifications must not invalidate an editor. All configuration,
    # including IDs, creation time, activation and unknown future fields does.
    value = {key: item for key, item in homeegram.items()
             if key not in {"state", "play", "last_triggered"}}
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         allow_nan=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
