"""Forecast staging conventions for supported EVS CAM profiles."""

STAGING_TEMPLATES = {
    "rrfs": (
        "rrfs.{init_date}/{cycle}/"
        "rrfs.t{cycle}z.2dfld.3km.f{fhr:03d}.conus.grib2"
    ),
    "hrrr": (
        "hrrr.{init_date}/conus/"
        "hrrr.t{cycle}z.wrfsfcf{fhr:02d}.grib2"
    ),
    "rrfsmem": (
        "rrfsens.{init_date}/{cycle}/m{member:03d}/"
        "rrfs.t{cycle}z.m{member:03d}."
        "2dfld.3km.f{fhr:03d}.conus.grib2"
    ),
}


def get_staging_template(profile):
    try:
        return STAGING_TEMPLATES[profile]
    except KeyError:
        raise ValueError(
            f"Unsupported EVS profile: {profile}. "
            f"Available: {', '.join(STAGING_TEMPLATES)}"
        )


def validate_member(profile, member):
    if profile == "rrfsmem":
        if type(member) is not int or not 1 <= member <= 9:
            raise ValueError(
                "model.member must be an integer from 1 to 9 "
                "for the current EVS rrfsmem filename convention"
            )
    elif member is not None:
        raise ValueError(
            "model.member is only used with the rrfsmem profile"
        )
    return member

