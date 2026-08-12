"""Payload integrity catalog for the public source delivery.

Operational payloads and their hashes are intentionally kept out of the
public repository.  A private release pipeline generates this catalog after
it has selected approved, non-public deployment files.
"""

PAYLOAD_CATALOG: dict[str, dict[str, object]] = {}
