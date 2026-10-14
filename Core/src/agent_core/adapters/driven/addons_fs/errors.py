"""Errors of the addon system. Every one names the addon and, when it can, the field."""

from __future__ import annotations

__all__ = [
    "AddonError",
    "AddonInstallError",
    "AddonRegistrationError",
    "AddonResolutionError",
    "AddonSettingsError",
    "ManifestError",
]


class AddonError(Exception):
    """An addon cannot be loaded. Raised at startup, never mid-turn.

    `addon` is the addon's name (or the directory it was found in when the name itself is wrong),
    `field` the dotted path of the offending key, `reason` the sentence that says what to change.
    A value is never put in the message: a setting may be a secret.
    """

    def __init__(self, addon: str | None, field: str | None, reason: str) -> None:
        self.addon = addon
        self.field = field
        self.reason = reason
        subject = f"addon {addon!r}" if addon else "addon"
        where = f", {self._kind} {field!r}" if field else ""
        super().__init__(f"{subject}{where}: {reason}")

    _kind = "field"


class ManifestError(AddonError):
    """`addon.yaml` is missing, is not YAML, or does not follow the schema."""


class AddonResolutionError(AddonError):
    """The enabled set cannot be resolved: unknown addon, missing dependency, cycle, bad Core."""

    _kind = "dependency"


class AddonSettingsError(AddonError):
    """A setting does not match the schema the addon declares, or names a key it does not have."""

    _kind = "setting"


class AddonRegistrationError(AddonError):
    """An addon contributes something it did not declare, or something that collides.

    Raised while the enabled addons are registered into the composition root (A4): a toolset,
    an audit entry, a policy rule, a fragment, a profile or a channel that the manifest's
    `provides` does not declare, that is not what the addon's files say, or whose name another
    addon or Core already owns.
    """

    _kind = "extension point"


class AddonInstallError(AddonError):
    """An addon's install state cannot be brought up to date (A7, D80).

    Raised at startup, after the schema is in place: a versioned migration fails, a hook fails or
    cannot be found, or the recorded version is newer than the manifest's (a downgrade). The
    message names the addon and the file or hook, and what was left done, because a half-installed
    addon is the one state an operator has to reason about.
    """

    _kind = "install step"
