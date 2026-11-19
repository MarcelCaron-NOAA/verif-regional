#!/usr/bin/env bash
# Single-case proof-of-concept runner. Owns orchestration; all scientific
# processing configurations/variable definitions come from the EVS source.
set -eo pipefail

if [[ $# -ne 1 || ! -f "$1" ]]; then
    echo 'Usage: run_rrfs_metar_tmp2m.sh /absolute/path/manifest.json' >&2
    exit 2
fi
manifest="$1"
if [[ -n "${VR_ENV_SETUP:-}" ]]; then
    [[ -f "$VR_ENV_SETUP" ]] || { echo "Missing site setup script: $VR_ENV_SETUP" >&2; exit 2; }
    # shellcheck disable=SC1090
    source "$VR_ENV_SETUP"
fi
set -u

run_dir="$(dirname "$manifest")"
meta_env="$run_dir/manifest.env"
python3 - "$manifest" "$meta_env" <<'PY'
import json, shlex, sys
from pathlib import Path
info=json.loads(Path(sys.argv[1]).read_text())
keys={
  'VR_MODEL': 'model',
  'VR_DOMAIN': 'domain',
  'VR_EVS_ROOT':'evs_root',
  'VR_RUN_DIR':'run_dir',
  'VR_VDATE':'vdate',
  'VR_VHOUR':'vhour',
  'VR_FHR':'fhr',
  'VR_FCST_STAGE':'forecast_stage_root',
  'VR_OBS_STAGE':'obs_stage_root',
  'VR_FORECAST':'forecast_staged',
  'VR_OBS':'obs_staged',
  'VR_OBS_TYPE':'obs_type',
  'VR_VARIABLE':'variable',
}
exports = ''.join(
    f'export {name}={shlex.quote(str(info[value]))}\n'
    for name, value in keys.items()
)

variables = info.get('variables') or [info['variable']]
exports += (
    f'export VR_VARIABLES='
    f'{shlex.quote(" ".join(variables))}\n'
)

Path(sys.argv[2]).write_text(exports)
PY
# shellcheck disable=SC1090
source "$meta_env"

# Only the RRFS CONUS / METAR implementation is validated so far.
if [[ "$VR_MODEL" != "rrfs" ||
      "$VR_DOMAIN" != "conus" ||
      "$VR_OBS_TYPE" != "metar" ]]; then
    echo "Unsupported grid2obs combination: $VR_MODEL / $VR_DOMAIN / $VR_OBS_TYPE" >&2
    exit 2
fi

[[ -s "$VR_FORECAST" ]] || { echo "Missing forecast: $VR_FORECAST" >&2; exit 3; }
[[ -s "$VR_OBS" ]] || { echo "Missing obsproc input: $VR_OBS" >&2; exit 3; }
export HOMEevs="$VR_EVS_ROOT"
export USHevs="$HOMEevs/ush" PARMevs="$HOMEevs/parm"
: "${VR_FIX_ROOT:?ERROR: Set environment.fix_root in the machine YAML}"
export FIXevs="$VR_FIX_ROOT"
export METPLUS_PATH="${METPLUS_PATH:-${METPLUS_ROOT:-}}"
export MET_ROOT="${MET_ROOT:-}"
export bufr_ROOT="${bufr_ROOT:-}"
for envname in METPLUS_PATH MET_ROOT bufr_ROOT; do
    if [[ -z "${!envname}" || ! -d "${!envname}" ]]; then
        echo "Missing required EVS/MET environment variable $envname. Set environment.setup_script in YAML or provide the loaded EVS environment." >&2
        exit 4
    fi
done
command -v run_metplus.py >/dev/null || { echo 'run_metplus.py not on PATH' >&2; exit 4; }
[[ -x "$bufr_ROOT/bin/split_by_subset" ]] || { echo "Missing $bufr_ROOT/bin/split_by_subset" >&2; exit 4; }

export machine="${VR_MACHINE:?Machine name missing from profile}"
export NET=evs STEP=stats COMPONENT=cam RUN=atmos
export VERIF_CASE=grid2obs MODELNAME="$VR_MODEL"
export DATA="$VR_RUN_DIR/work"
export VDATE="$VR_VDATE" VHOUR="$VR_VHOUR" FHR="$(printf '%02d' "$VR_FHR")"
export vhr="$VR_VHOUR" NEST="$VR_DOMAIN" VERIF_TYPE="$VR_OBS_TYPE" var_set_num=0
export COMINrrfs="$VR_FCST_STAGE" COMINobsproc="$VR_OBS_STAGE"
export COMOUT="$VR_RUN_DIR/comout"
export COMOUTsmall="$COMOUT/atmos.$VDATE/rrfs/grid2obs"
export MET_PLUS_CONF="$PARMevs/metplus_config/stats/cam/grid2obs"
export PYTHONPATH="$USHevs/cam:${PYTHONPATH:-}"
mkdir -p "$DATA" "$COMOUTsmall"
cd "$DATA"

# Source upstream configuration to obtain the exact operational RRFS input
# template, masks, grid, observation type and METplus settings.
config="$PARMevs/evs_config/cam/config.evs.prod.stats.cam.atmos.grid2obs.rrfs"
# shellcheck disable=SC1090
source "$config"
[[ "$MODEL_INPUT_TEMPLATE" == *'2dfld.3km.'* ]] || {
    echo 'EVS input template changed; review verif-regional staging adapter.' >&2; exit 5;
}
# Verify that EVS's live template resolves to exactly our staged file.
resolved_fcst="$(python3 -c 'import cam_util as c; import os; print(c.get_cam_fcst_file_path(
    VERIF_CASE="grid2obs", job_type="generate", COMINfcst=os.environ["COMINfcst"],
    MODEL_INPUT_TEMPLATE=os.environ["MODEL_INPUT_TEMPLATE"],
    VDATE=os.environ["VDATE"], VHOUR=os.environ["VHOUR"], FHR=os.environ["FHR"]))')"
if [[ "$resolved_fcst" != "$VR_FORECAST" ]]; then
    echo 'EVS forecast template does not match staged input:' >&2
    echo "  EVS resolves: $resolved_fcst" >&2
    echo "  Our staging:  $VR_FORECAST" >&2
    exit 5
fi
# The PointStat config references this grid mask from the upstream config.
IFS=',' read -ra mask_paths <<< "$MASK_POLY_LIST"
for raw_mask in "${mask_paths[@]}"; do
    mask="$(echo "$raw_mask" | xargs)"
    [[ -s "$mask" ]] || { echo "Missing EVS mask: $mask (set environment.fix_root)" >&2; exit 5; }
done

# Single forecast, single valid time: no operational loops.
export FHR_GROUP_LIST=SHORT
export FHR_START="$VR_FHR" FHR_END="$VR_FHR" FHR_INCR=1
export FHR_END_SHORT="$VR_FHR" FHR_INCR_SHORT=1
export SKIP_IF_OUTPUT_EXISTS=no
export VAR_NAME="$VR_VARIABLE" 
export job_name="vr_${MODELNAME}_${VAR_NAME}"
export MET_PLUS_OUT="$DATA/$VERIF_CASE/METplus_output"
export MET_CONFIG_OVERRIDES=''
export OMP_NUM_THREADS=1

# CAM's existing observation preprocessing: rrfs prepbufr -> ADPSFC subset.
mkdir -p "$DATA/grid2obs/data/rrfs/tmp" "$DATA/grid2obs/data/metar/prepbufr"
mkdir -p "$MET_PLUS_OUT/metar/conus/pb2nc" \
         "$MET_PLUS_OUT/metar/conus/out/confs" \
         "$MET_PLUS_OUT/metar/conus/out/logs" \
         "$MET_PLUS_OUT/metar/point_stat" \
         "$MET_PLUS_OUT/metar/out/confs" \
         "$MET_PLUS_OUT/metar/out/logs"

echo "Step 1: upstream CAM ADPSFC subset preprocessing"
python3 "$USHevs/cam/cam_stats_grid2obs_preprocess_prepbufr.py"
prepbufr="$DATA/grid2obs/data/metar/prepbufr/rrfs.t${VHOUR}z.prepbufr.tm00"
[[ -s "$prepbufr" ]] || { echo "Preprocessed ADPSFC file missing: $prepbufr" >&2; exit 6; }

echo "Step 2: upstream METplus PB2NC configuration"
run_metplus.py -c "$PARMevs/metplus_config/machine.conf" \
    -c "$MET_PLUS_CONF/PB2NC_obsMETAR.conf"
obs_nc="$MET_PLUS_OUT/metar/conus/pb2nc/prepbufr.rrfs.${VDATE}${VHOUR}.nc"
[[ -s "$obs_nc" ]] || { echo "PB2NC output missing: $obs_nc" >&2; exit 7; }


# One PB2NC output is shared by all requested variables.
read -r -a requested_variables <<< "$VR_VARIABLES"

if (( ${#requested_variables[@]} == 0 )); then
    echo "No grid2obs variables requested" >&2
    exit 8
fi

stat_root="$MET_PLUS_OUT/metar/point_stat"
shopt -s nullglob globstar

for variable in "${requested_variables[@]}"; do
    export VAR_NAME="$variable"
    export job_name="vr_${MODELNAME}_${VAR_NAME}"

    echo "=================================================="
    echo "Running EVS PointStat: $MODELNAME $VAR_NAME / $VERIF_TYPE"
    echo "=================================================="

    # Load variable definitions directly from upstream EVS.
    var_env="$VR_RUN_DIR/variables_${VAR_NAME}.env"

    python3 - "$USHevs/cam" "$var_env" \
        "$VAR_NAME" "$VERIF_TYPE" "$MODELNAME" <<'PY'
import shlex
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from cam_stats_grid2obs_var_defs import generate_stats_jobs_dict

variable, obs_type, model = sys.argv[3:6]

try:
    v = generate_stats_jobs_dict[variable][obs_type]
    spec = v[model]
except KeyError as exc:
    raise SystemExit(
        f"Unsupported EVS grid2obs selection: "
        f"{variable}/{obs_type}/{model}: {exc}"
    )

entries = {
    'FCST_VAR1_NAME': spec['var1_fcst_name'],
    'FCST_VAR1_LEVELS': spec['var1_fcst_levels'],
    'FCST_VAR1_THRESHOLDS': spec['var1_fcst_thresholds'],
    'FCST_VAR1_OPTIONS': spec['var1_fcst_options'],
    'OBS_VAR1_NAME': spec['var1_obs_name'],
    'OBS_VAR1_LEVELS': spec['var1_obs_levels'],
    'OBS_VAR1_THRESHOLDS': spec['var1_obs_thresholds'],
    'OBS_VAR1_OPTIONS': spec['var1_obs_options'],
}

# Always reset output flags between variables.
# Otherwise values from a previous variable could persist.
for key in ('CTC', 'SL1L2', 'VL1L2', 'CNT', 'VCNT', 'MCTC'):
    entries[f'OUTPUT_FLAG_{key}'] = (
        v['output_types'].get(key, 'NONE')
    )

Path(sys.argv[2]).write_text(''.join(
    f'export {key}={shlex.quote(str(value))}\n'
    for key, value in entries.items()
))
PY

    # shellcheck disable=SC1090
    source "$var_env"

    run_metplus.py -c "$PARMevs/metplus_config/machine.conf" \
        -c "$MET_PLUS_CONF/PointStat_fcstCAM_obsMETAR.conf"

    # Check for the requested variable and forecast hour,
    # not just any existing STAT output.
    stat_files=(
        "$stat_root"/**/*_"$VAR_NAME"_OBS_f"$FHR"_*.stat
    )

    if (( ${#stat_files[@]} == 0 )); then
        echo "ERROR: No PointStat output found for $VAR_NAME F$FHR" >&2
        exit 8
    fi

    for stat_file in "${stat_files[@]}"; do
        [[ -s "$stat_file" ]] || {
            echo "ERROR: Empty STAT file: $stat_file" >&2
            exit 8
        }
    done

    printf 'STAT output available for %s:\n' "$VAR_NAME"
    printf '  %s\n' "${stat_files[@]}"
done

echo "SUCCESS: grid2obs workflow completed for ${requested_variables[*]}"

