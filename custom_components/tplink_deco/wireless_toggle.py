"""Select only the optional network flags understood by the wlan write API."""

GUEST_BANDS = ("band2_4", "band5_1", "band5_2", "band6", "band6_2")


def writable_wireless_flags(config, network: str) -> dict[str, bool]:
    """Read known, explicitly reported flags without retaining any other fields."""
    if not isinstance(config, dict):
        return {}
    if network == "guest":
        bands, section = GUEST_BANDS, "guest"
    elif network == "iot":
        bands, section = ("iot",), "host"
    else:
        return {}

    flags = {}
    for band in bands:
        block = config.get(band)
        settings = block.get(section) if isinstance(block, dict) else None
        if not isinstance(settings, dict) or "enable" not in settings:
            continue
        value = settings["enable"]
        if type(value) is bool:
            flags[band] = value
        elif type(value) is int and value in (0, 1):
            flags[band] = bool(value)
        elif isinstance(value, str):
            value = value.strip().lower()
            if value in ("1", "on", "true", "enable", "enabled"):
                flags[band] = True
            elif value in ("0", "off", "false", "disable", "disabled"):
                flags[band] = False
            else:
                return {}
        else:
            return {}
    return flags
