"""Exception types for the role management system.

Split into two families:

  RoleManagerError  — the authoritative operation itself failed. The Google
                      Sheet was NOT modified (or was rolled back).
  SyncError         — an external platform (Discord / Roblox) failed. The sheet
                      remains authoritative and untouched; the job is retried.
"""


# ── Authoritative-side errors ────────────────────────────────────────────────

class RoleManagerError(Exception):
    """Base class for errors in the authoritative path."""


class NotConfiguredError(RoleManagerError):
    """A required configuration value has not been supplied yet."""


class RecordNotFoundError(RoleManagerError):
    """No PERSONNEL record exists for the requested user."""


class RecordAlreadyExistsError(RoleManagerError):
    """A PERSONNEL record already exists for this Discord/Roblox identity."""


class ValidationError(RoleManagerError):
    """User-supplied input was malformed or referenced an unknown value."""


class PermissionDeniedError(RoleManagerError):
    """The acting user is not authorized to perform this action."""


class SheetStructureError(RoleManagerError):
    """The PERSONNEL sheet does not match the expected structure."""


class ConcurrencyError(RoleManagerError):
    """The record changed underneath us; the caller should re-read and retry."""


# ── Synchronization-side errors ──────────────────────────────────────────────

class SyncError(Exception):
    """Base class for external synchronization failures. Never fatal to the sheet."""

    #: Whether a retry could plausibly succeed.
    retryable = True


class TransientSyncError(SyncError):
    """Rate limit, timeout, 5xx — retry with backoff."""

    retryable = True


class PermanentSyncError(SyncError):
    """Missing role, missing guild, revoked credentials — retrying will not help."""

    retryable = False


class TargetNotPresentError(SyncError):
    """The user is not a member of the target guild / group.

    Not an error condition per se: nothing to synchronize. Not retryable, but
    recorded so it can be surfaced rather than silently swallowed.
    """

    retryable = False
