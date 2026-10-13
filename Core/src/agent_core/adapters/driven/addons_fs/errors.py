"""Errors of the addon system. Every one names the addon and, when it can, the field."""

from __future__ import annotations

__all__ = [
    "AddonError",
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
