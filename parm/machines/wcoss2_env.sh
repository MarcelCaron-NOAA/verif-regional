#!/usr/bin/env bash
# WCOSS2 environment for verif-regional.
# Sourced inside the PBS job.

: "${VR_EVS_ROOT:?Driver must define VR_EVS_ROOT}"

if ! type module >/dev/null 2>&1; then
    echo "ERROR: module command unavailable inside PBS." >&2
    return 1
fi

# Use the versions specified by upstream EVS.
source "$VR_EVS_ROOT/versions/run.ver"

# Initialize the standard EVS environment.
module reset
module load "prod_envir/${prod_envir_ver}"

# Load the appropriate CAM dependencies.
vr_step="${VR_EVS_STEP:-stats}"

case "$vr_step" in
    prep|stats|plots) ;;
    *)
        echo "ERROR: Unsupported EVS step: $vr_step" >&2
        return 1
        ;;
esac

source "$VR_EVS_ROOT/dev/modulefiles/cam/cam_${vr_step}.sh"

# The upstream module script enables Bash tracing.
set +x

# Normalize the environment variables required by our runner.
export METPLUS_PATH="${METPLUS_PATH:-${METPLUS_ROOT:-${metplus_ROOT:-}}}"
export MET_ROOT="${MET_ROOT:-${met_ROOT:-}}"
export bufr_ROOT="${bufr_ROOT:-${BUFR_ROOT:-}}"

echo "Loaded WCOSS2 CAM $vr_step environment."
