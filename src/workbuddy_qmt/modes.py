RUN_MODES = (
    "OBSERVE_ONLY",
    "SIM_SIGNAL",
    "MANUAL_LIVE",
    "LIMITED_AUTO",
)


# These names are accepted only while loading an older configuration or
# migrating an existing state database. They are never exposed as selectable
# modes and are never written by the current version.
LEGACY_MODE_MIGRATIONS = {
    "READ_ONLY": "OBSERVE_ONLY",
    "DRY_RUN": "OBSERVE_ONLY",
    "QMT_SIM_SIGNAL": "SIM_SIGNAL",
    "HALTED": "OBSERVE_ONLY",
}


def normalize_mode(value, allow_legacy=False):
    if value in RUN_MODES:
        return value
    if allow_legacy and value in LEGACY_MODE_MIGRATIONS:
        return LEGACY_MODE_MIGRATIONS[value]
    raise ValueError("invalid run mode")
