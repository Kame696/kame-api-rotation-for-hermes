"""One credential that holds several keys, seen as the several keys it is.

Hermes reads a provider env var and stores whatever it finds as one
credential:

    token = _get_env_prefer_dotenv(env_var)   # agent/credential_pool.py
    ...
    _upsert_entry(entries, provider, source, _env_payload(token=token, ...))

There is no split anywhere on that path. ``GOOGLE_API_KEY=k1,k2,k3`` becomes
a single pool entry whose key is the 119-character string ``k1,k2,k3``, and
every request sends that string whole. The provider rejects it, the pool has
exactly one credential to rotate to, and rotation has nothing to do. Measured
against the real host before this module existed: three keys in, one entry
out, ``commas in key: 2``.

That shape is not a mistake by the person who typed it. Agent Zero's key
pool, from which this plugin's rotation was ported, accepts exactly that
comma list, and so does this plugin's own ``/kame-keys add``. The list is the
obvious way to express "here are my keys" and it is the format the user
already has. So the fix belongs here, not in a instruction telling them to
retype anything.

**Nothing here writes.** The split is derived on load and recomputed on the
next one, which is what keeps it correct when the env var changes: there is
no second copy of the key list to fall out of date. The parent entry is left
exactly as the host built it, so the row on disk — and the ``.env`` line
behind it — remain the single source of truth.

The identity of a split key is derived from the key, not from its position,
so reordering the list does not renumber anything and the ledger's memory of
which key is spent survives an edit elsewhere in the list. The derivation is
a one-way hash: this module never stores, returns, or logs a key.
"""

from __future__ import annotations

import hashlib
from typing import List, Sequence, Tuple

from .keys import parse_keys

# Appended to the parent's source to name a split part. The host builds
# sources from a fixed vocabulary (``manual``, ``env:NAME``, file paths); the
# marker is spelled so that nothing the host produces can collide with it,
# and so that a human reading auth.json or a log line can tell at a glance
# that the row was derived rather than typed.
#
# Carried in ``source`` rather than in an attribute on purpose. The pool
# replaces credential objects wholesale on several paths — ``_replace_entry``
# swaps in a new instance built by the host — and an attribute set on the old
# object does not survive that. ``source`` is a declared field, so it is
# copied by every one of those paths. The marker is what tells the persist
# guard which rows must not reach disk, and a marker that can be lost by a
# routine host operation is a marker that eventually lets one through.
CHILD_SOURCE_MARK = "#kame-key-"


def is_child_source(source: object) -> bool:
    """Whether this source names a split part rather than a stored credential."""
    return CHILD_SOURCE_MARK in str(source or "")


def child_source(parent_source: object, index: int) -> str:
    """Name the ``index``-th part of a multi-key credential. 1-based."""
    return f"{str(parent_source or '')}{CHILD_SOURCE_MARK}{index}"


def child_label(parent_label: object, index: int, total: int) -> str:
    """A label that reads as one of several, because that is what it is.

    ``GOOGLE_API_KEY (2/7)`` rather than a new invented name: the person
    reading a status report is looking for the thing they typed, and the
    part number is the only new fact.
    """
    return f"{str(parent_label or '?')} ({index}/{total})"


def child_id(parent_id: object, key: str) -> str:
    """A stable id for a split key, derived from the key itself.

    Deriving from the position instead would mean that deleting the first key
    of a list renumbers every key after it, and the ledger — which remembers
    what is spent by id — would read every one of them as a different
    credential that has never been tried. Deriving from the key means the
    identity of a key is the key, which is what it actually is.

    SHA-256, truncated for readability. One-way: the id can be recomputed
    from the key and the key can never be recovered from the id, which is the
    property that lets this value appear in logs and on disk.
    """
    digest = hashlib.sha256(str(key or "").encode("utf-8")).hexdigest()
    return f"{str(parent_id or 'k')}-{digest[:8]}"


def split_value(raw: object) -> Tuple[List[str], int]:
    """Split a stored credential value into the keys it holds.

    Returns ``(keys, rejected_count)``. ``keys`` is empty when there is
    nothing to split — one key, or none — which is the overwhelmingly common
    case and must cost nothing and change nothing.

    ``rejected_count`` is a count and never the text: a value that splits
    into six keys and one fragment is worth a log line saying so, and that
    line must not be able to carry a key. Reuses ``parse_keys`` rather than
    splitting on a comma directly, so a value typed with semicolons, pipes,
    newlines or a stray byte-order mark is read the same way ``/kame-keys
    add`` already reads it — and so the separator rules have exactly one
    definition in this plugin.
    """
    text = str(raw or "").strip()
    if not text:
        return [], 0
    keys, rejected = parse_keys(text)
    if len(keys) < 2:
        # One key is not a list, and a value that parses to nothing is
        # somebody else's problem: an empty or malformed credential is
        # reported by the pool's own emptiness check, not invented here.
        return [], 0
    return keys, len(rejected)


def plan_children(
    *,
    parent_id: object,
    parent_source: object,
    parent_label: object,
    keys: Sequence[str],
) -> List[dict]:
    """Describe the rows a multi-key credential should become.

    Pure description: ids, labels, sources and the key each part carries.
    Building actual credential objects needs the host's class and belongs
    with the code that has it. Keeping the decision here is what lets every
    rule above be tested without Hermes installed.
    """
    total = len(keys)
    return [
        {
            "id": child_id(parent_id, key),
            "source": child_source(parent_source, index),
            "label": child_label(parent_label, index, total),
            "access_token": key,
        }
        for index, key in enumerate(keys, start=1)
    ]
