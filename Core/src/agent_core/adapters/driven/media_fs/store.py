"""Driven adapter: MediaStore over the filesystem.

Phase:   F7
Tasks:   docs/TASKS.md#t-f7-04
Implements: ports/media_store.py

LAYOUT
    <media_root>/<sha256[:2]>/<sha256>          bytes, named by CONTENT
    metadata in Postgres                        media_id, kind, mime, size, sha256, filename

    Content addressing gives deduplication for free and makes the original filename pure
    metadata - which is what keeps an attacker-controlled name away from the filesystem.

VALIDATION HAPPENS IN IngestMedia, BEFORE bytes arrive here
    Size, magic-byte sniffing, and the profile's accepted kinds. Validating after storing
    means a hostile upload already consumed the disk.

signed_url() MUST REFUSE when the profile's delivery mode is BYTES
    Defence in depth. The decision belongs to MediaPolicy; this is the last place to catch
    a caller that ignored it, and the consequence of missing it is handing evidence with
    personal data to a third party.

LATER: swap for S3-compatible storage. The port does not change - that is the point.
"""
