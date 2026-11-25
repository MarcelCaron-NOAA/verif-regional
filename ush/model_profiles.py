"""Forecast staging conventions for supported EVS CAM profiles."""

METAR_STAGING_TEMPLATES = {
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

RAOB_STAGING_TEMPLATES = {
    "rrfs": (
        "rrfs.{init_date}/{cycle}/"
        "rrfs.t{cycle}z.prslev.3km.f{fhr:03d}.conus.grib2"
    ),
    "hrrr": (
        "hrrr.{init_date}/conus/"
        "hrrr.t{cycle}z.wrfprsf{fhr:02d}.grib2"
    ),
    "rrfsmem": (
        "rrfsens.{init_date}/{cycle}/m{member:03d}/"
        "rrfs.t{cycle}z.m{member:03d}."
        "prslev.3km.f{fhr:03d}.conus.grib2"
    ),
}



RAOB_SURFACE_VARIABLES = frozenset({
    "SBCAPE",
    "MLCAPE",
    "HPBL",
})


def get_var_set_num(profile, obs_type, variable):
    """Select the upstream EVS CAM variable/file group."""
    if (
        obs_type == "raob"
        and profile in ("rrfs", "rrfsmem")
        and variable in RAOB_SURFACE_VARIABLES
    ):
        return 1

    return 0



def get_staging_template(
    profile, obs_type="metar", var_set_num=0
):
    if obs_type == "metar":
        templates = METAR_STAGING_TEMPLATES

    elif obs_type == "raob":
        if var_set_num == 0:
            templates = RAOB_STAGING_TEMPLATES
        elif (
            var_set_num == 1
            and profile in ("rrfs", "rrfsmem")
        ):
            # The RRFS/REFS RAOB surface-variable group
            # uses the same 2dfld product as METAR.
            templates = METAR_STAGING_TEMPLATES
        else:
            raise ValueError(
                "Unsupported RAOB forecast product group"
            )
    else:
        raise ValueError(
            f"Unsupported observation type: {obs_type}"
        )

    try:
        return templates[profile]
    except KeyError:
        raise ValueError(
            f"Unsupported EVS profile: {profile}"
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

