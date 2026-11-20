
"""Discover EVS grid2obs variables and classify wrapper support."""

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path


# Only records which combinations we've scientifically tested.
# This does NOT limit what EVS can define.
BASELINE_TESTED = {
    ("rrfs", "metar"): {"TMP2m", "DPT2m"}
}

# Special processing that the current runner does not implement.
SPECIAL_ADAPTERS = {
    "PTYPE": "Requires PTYPE-specific derived-field processing",
    "WIND10m": "Wind-speed field handling needs verification",
}


def discover(evs_root, model="rrfs", obs_type="metar"):
    """Return EVS-defined variables and wrapper capability status."""

    source = (
        Path(evs_root)
        / "ush/cam/cam_stats_grid2obs_var_defs.py"
    )
    if not source.is_file():
        raise ValueError(f"EVS variable definitions missing: {source}")

    module_spec = importlib.util.spec_from_file_location(
        "_vr_cam_var_defs", source
    )
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)

    definitions = module.generate_stats_jobs_dict
    result = {}

    for name, by_obs in definitions.items():
        obs = by_obs.get(obs_type, {})
        definition = obs.get(model)

        if definition is None:
            continue

        reason = ""

        if (model, obs_type) != ("rrfs", "metar"):
            reason = "Wrapper currently supports RRFS/METAR only"
        elif (
            "ceiling_grib_field" in definition
            or "surface_grib_field" in definition
        ):
            reason = "Requires EVS PCPCombine ceiling AGL preprocessing"
        elif any(k.startswith("var2_") for k in definition):
            reason = "Requires second forecast/observation field"
        else:
            reason = SPECIAL_ADAPTERS.get(name, "")

        if reason:
            status = "needs-adapter"
        elif name in BASELINE_TESTED.get((model, obs_type), set()):
            status = "baseline-tested"
        else:
            status = "candidate"

        result[name] = {
            "status": status,
            "reason": reason,
            "definition": definition,
            "output_types": obs.get("output_types", {}),
        }

    return result


def check_field(source, entry):
    """Check simple EVS fields with wgrib2; defer complex selectors."""

    definition = entry["definition"]
    field = definition.get("var1_fcst_name", "")
    level = definition.get("var1_fcst_levels", "")
    options = definition.get("var1_fcst_options", "")

    # GRIB template-dependent fields require a more precise selector.
    if "GRIB2_ipdtmpl_" in options:
        return "not checked (specialized GRIB2 template selector)"

    # Generic conversions from EVS levels to wgrib2 inventory levels.
    if re.fullmatch(r"Z[1-9]\d*", level):
        vertical = f"{level[1:]} m above ground"
    elif re.fullmatch(r"P\d+", level):
        vertical = f"{level[1:]} mb"
    else:
        return (
            f"not checked (EVS level {level!r} "
            "needs specialized inventory selection)"
        )

    if not re.fullmatch(r"[A-Za-z0-9_]+", field):
        return f"not checked (unsupported field selector {field!r})"

    if not Path(source).is_file():
        return "not checked (forecast missing)"

    wgrib2 = shutil.which("wgrib2")
    if not wgrib2:
        return "not checked (wgrib2 unavailable)"

    pattern = f":{field}:{vertical}:"
    proc = subprocess.run(
        [wgrib2, source, "-match", pattern, "-s"],
        capture_output=True,
        text=True,
    )

    if proc.returncode != 0:
        return (
            f"FAILED: wgrib2 returned {proc.returncode}: "
            f"{proc.stderr.strip()[:200]}"
        )

    return "present" if proc.stdout.strip() else f"MISSING: {pattern}"

