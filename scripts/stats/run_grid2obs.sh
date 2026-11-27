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
  'VR_FORECAST_ID': 'forecast_id',
  'VR_MEMBER': 'member',
  'VR_DOMAIN': 'domain',
  'VR_EVS_ROOT':'evs_root',
  'VR_RUN_DIR':'run_dir',
  'VR_INIT_DATE': 'init_date',
  'VR_CYCLE': 'cycle',
  'VR_VDATE':'vdate',
  'VR_VHOUR':'vhour',
  'VR_FHR':'fhr',
  'VR_FCST_STAGE':'forecast_stage_root',
  'VR_OBS_STAGE':'obs_stage_root',
  'VR_FORECAST':'forecast_staged',
  'VR_OBS':'obs_staged',
  'VR_OBS_TYPE':'obs_type',
  'VR_VARIABLE':'variable',
  'VR_VAR_SET_NUM':'var_set_num',
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

sets = info["variable_sets"]
exports += (
    "export VR_VARIABLE_SETS="
    + shlex.quote(
        " ".join(str(sets[v]) for v in variables)
    )
    + "\n"
)

staged_by_set = info["forecast_staged_by_set"]

for set_num in ("0", "1"):
    path = staged_by_set.get(set_num, "")
    exports += (
        f"export VR_FORECAST_SET_{set_num}="
        f"{shlex.quote(str(path))}\n"
    )


Path(sys.argv[2]).write_text(exports)
PY
# shellcheck disable=SC1090
source "$meta_env"

case "$VR_MODEL" in rrfs|hrrr|rrfsmem) ;;
    *)
        echo "Unsupported EVS profile: $VR_MODEL" >&2
        exit 2
        ;;
esac

# Only the CONUS / METAR implementation is validated so far.
if [[ "$VR_DOMAIN" != "conus" ||
      "$VR_OBS_TYPE" != "metar" &&
      "$VR_OBS_TYPE" != "raob" ]]; then
    echo "Unsupported grid2obs combination: $VR_DOMAIN / $VR_OBS_TYPE" >&2
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
export vhr="$VR_VHOUR" NEST="$VR_DOMAIN" VERIF_TYPE="$VR_OBS_TYPE" 
export var_set_num="$VR_VAR_SET_NUM"
export COMINrrfs="$VR_FCST_STAGE" 
export COMINhrrr="$VR_FCST_STAGE"
export COMINobsproc="$VR_OBS_STAGE"
if [[ "$VR_MODEL" == "rrfsmem" ]]; then
    export mem="$VR_MEMBER"
fi
export COMOUT="$VR_RUN_DIR/comout"
export COMOUTsmall="$COMOUT/atmos.$VDATE/rrfs/grid2obs"
export MET_PLUS_CONF="$PARMevs/metplus_config/stats/cam/grid2obs"
export PYTHONPATH="$USHevs/cam:${PYTHONPATH:-}"
mkdir -p "$DATA" "$COMOUTsmall"
cd "$DATA"

# Source upstream configuration to obtain the exact operational RRFS input
# template, masks, grid, observation type and METplus settings.
config="$PARMevs/evs_config/cam/config.evs.prod.stats.cam.atmos.grid2obs.${VR_MODEL}"
[[ -f "$config" ]] || {
    echo "Missing EVS profile configuration: $config" >&2
    exit 5
}
echo "EVS variable set: $var_set_num"
source "$config"
# Verify that EVS's live template resolves to exactly our staged file.

resolve_evs_forecast() {
    python3 -c 'import cam_util as c; import os; print(c.get_cam_fcst_file_path(
        VERIF_CASE="grid2obs", job_type="generate",
        COMINfcst=os.environ["COMINfcst"],
        MODEL_INPUT_TEMPLATE=os.environ["MODEL_INPUT_TEMPLATE"],
        VDATE=os.environ["VDATE"], VHOUR=os.environ["VHOUR"],
        FHR=os.environ["FHR"]))'
}

resolved_fcst="$(resolve_evs_forecast)"

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


# EVS observation preprocessing for METAR or RAOB.
obs_dir="$DATA/grid2obs/data/$VERIF_TYPE/prepbufr"
met_obs_dir="$MET_PLUS_OUT/$VERIF_TYPE/$NEST"

mkdir -p "$DATA/grid2obs/data/${VR_MODEL}/tmp" "$obs_dir"
mkdir -p "$met_obs_dir/pb2nc" \
         "$met_obs_dir/out/confs" \
         "$met_obs_dir/out/logs" \
         "$MET_PLUS_OUT/$VERIF_TYPE/point_stat" \
         "$MET_PLUS_OUT/$VERIF_TYPE/out/confs" \
         "$MET_PLUS_OUT/$VERIF_TYPE/out/logs"

if [[ "$VERIF_TYPE" == "raob" ]]; then
    subset="ADPUPA"
    prep_name="gdas.t${VHOUR}z.prepbufr"
    obs_nc_name="prepbufr.gfs.${VDATE}${VHOUR}.nc"
    pb2nc_conf="PB2NC_obsRAOB.conf"
else
    subset="ADPSFC"
    prep_name="rrfs.t${VHOUR}z.prepbufr.tm00"
    obs_nc_name="prepbufr.rrfs.${VDATE}${VHOUR}.nc"
    pb2nc_conf="PB2NC_obsMETAR.conf"
fi

echo "Step 1: EVS $subset PrepBUFR preprocessing"
python3 "$USHevs/cam/cam_stats_grid2obs_preprocess_prepbufr.py"

prepbufr="$obs_dir/$prep_name"
[[ -s "$prepbufr" ]] || {
    echo "ERROR: Preprocessed $subset file missing: $prepbufr" >&2
    exit 6
}

echo "Step 2: EVS PB2NC for $VERIF_TYPE"
run_metplus.py -c "$PARMevs/metplus_config/machine.conf" \
    -c "$MET_PLUS_CONF/$pb2nc_conf"

obs_nc="$met_obs_dir/pb2nc/$obs_nc_name"
[[ -s "$obs_nc" ]] || {
    echo "ERROR: PB2NC output missing: $obs_nc" >&2
    exit 7
}

# Observation preprocessing used the EVS profile.
# PointStat now uses the developer's forecast identity.
export MODELNAME="$VR_FORECAST_ID"

echo "Forecast: $MODELNAME (EVS profile: $VR_MODEL)"

# One PB2NC output is shared by all requested variables.
read -r -a requested_variables <<< "$VR_VARIABLES"
read -r -a requested_sets <<< "$VR_VARIABLE_SETS"

if (( ${#requested_variables[@]} != ${#requested_sets[@]} )); then
    echo "ERROR: Variable/product mapping is inconsistent" >&2
    exit 8
fi

if (( ${#requested_variables[@]} == 0 )); then
    echo "No grid2obs variables requested" >&2
    exit 8
fi

stat_root="$MET_PLUS_OUT/$VERIF_TYPE/point_stat"
shopt -s nullglob globstar


for idx in "${!requested_variables[@]}"; do
    variable="${requested_variables[$idx]}"
    set_num="${requested_sets[$idx]}"

    export VAR_NAME="$variable"
    export var_set_num="$set_num"

    case "$set_num" in
        0) export VR_FORECAST="$VR_FORECAST_SET_0" ;;
        1) export VR_FORECAST="$VR_FORECAST_SET_1" ;;
        *)
            echo "ERROR: Unsupported EVS variable set: $set_num" >&2
            exit 8
            ;;
    esac

    [[ -s "$VR_FORECAST" ]] || {
        echo "ERROR: Missing forecast for $VAR_NAME: $VR_FORECAST" >&2
        exit 8
    }

    # Read the correct forecast template from EVS, without
    # re-sourcing its entire configuration into the main shell.
    MODEL_INPUT_TEMPLATE="$(
        export var_set_num="$set_num"
        source "$config" >/dev/null
        printf '%s' "$MODEL_INPUT_TEMPLATE"
    )"
    export MODEL_INPUT_TEMPLATE

    resolved_fcst="$(resolve_evs_forecast)"

    if [[ "$resolved_fcst" != "$VR_FORECAST" ]]; then
        echo "ERROR: EVS forecast template mismatch for $VAR_NAME" >&2
        echo "  EVS:    $resolved_fcst" >&2
        echo "  Staged: $VR_FORECAST" >&2
        exit 8
    fi

    echo "Forecast product: $VR_FORECAST (EVS set $set_num)"

    export job_name="vr_${MODELNAME}_${VAR_NAME}"

    echo "=================================================="
    echo "Running EVS PointStat: $MODELNAME $VAR_NAME / $VERIF_TYPE"
    echo "=================================================="

    # Load variable definitions directly from upstream EVS.
    var_env="$VR_RUN_DIR/variables_${VAR_NAME}.env"

    python3 - "$USHevs/cam" "$var_env" \
        "$VAR_NAME" "$VERIF_TYPE" "$VR_MODEL" <<'PY'
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
entries["CEILING_GRIB_FIELD"] = spec.get("ceiling_grib_field", "")
entries["SURFACE_GRIB_FIELD"] = spec.get("surface_grib_field", "")

# Populate the second field when EVS defines one.
# Export empty values otherwise, avoiding stale settings
# from previously processed variables.
for side in ("fcst", "obs"):
    for attr in ("name", "levels", "thresholds", "options"):
        entries[
            f"{side.upper()}_VAR2_{attr.upper()}"
        ] = spec.get(f"var2_{side}_{attr}", "")

# Always reset output flags between variables.
# Otherwise values from a previous variable could persist.
for key in ('CTC', 'SL1L2', 'VL1L2', 'CNT', 'VCNT', 'MCTC'):
    entries[f'OUTPUT_FLAG_{key}'] = (
        v['output_types'].get(key, 'NONE')
    )

for key in (
    "FCST_VAR1_LEVELS",
    "OBS_VAR1_LEVELS",
    "FCST_VAR2_LEVELS",
    "OBS_VAR2_LEVELS",
    'FCST_VAR1_OPTIONS',
    'OBS_VAR1_OPTIONS',
    'FCST_VAR2_OPTIONS',
    'OBS_VAR2_OPTIONS',
):
    entries[key] = entries[key].replace(r'\"', '"')

Path(sys.argv[2]).write_text(''.join(
    f'export {key}={shlex.quote(str(value))}\n'
    for key, value in entries.items()
))
PY

    # shellcheck disable=SC1090
    source "$var_env"

    if [[ "$VAR_NAME" == "PTYPE" ]]; then
        echo "Creating merged PTYPE forecast using EVS"

        export job_type=generate
        export njob=0

        saved_met_plus_out="$MET_PLUS_OUT"

        # This layout is expected by EVS's merged-ptype script.
        export MET_PLUS_OUT="$DATA/grid2obs/METplus_output/workdirs/generate/$job_name"

        run_metplus.py -c "$PARMevs/metplus_config/machine.conf" \
            -c "$MET_PLUS_CONF/RegridDataPlane_fcstCAM_PTYPE.conf"

        regrid_file="$MET_PLUS_OUT/$VERIF_TYPE/regrid_data_plane/${MODELNAME}.${VDATE}/regrid_data_plane_${MODELNAME}_t${VHOUR}z_${VERIF_TYPE}_${NEST}_${job_name}_fhr${FHR}.nc"

        [[ -s "$regrid_file" ]] || {
            echo "ERROR: PTYPE regridded input missing: $regrid_file" >&2
            exit 8
        }

        merged_dir="$DATA/grid2obs/data/$MODELNAME/merged_ptype"
        mkdir -p "$merged_dir"

        python3 "$USHevs/cam/cam_stats_grid2obs_create_merged_ptype.py"

        # Restore normal METplus output paths for PointStat.
        export MET_PLUS_OUT="$saved_met_plus_out"

        printf -v fhr3 '%03d' "$VR_FHR"
        init_ymdh="${VR_INIT_DATE}${VR_CYCLE}"

        merged_file="$merged_dir/merged_ptype_${VERIF_TYPE}_${NEST}_${job_name}_init${init_ymdh}_fhr${fhr3}.nc"

        [[ -s "$merged_file" ]] || {
            echo "ERROR: PTYPE merged file missing: $merged_file" >&2
            exit 8
        }

        echo "Merged PTYPE output: $merged_file"
    fi

    if [[ "$VAR_NAME" == "CEILING" ]]; then
        echo "Deriving forecast CEILING_AGL using EVS PCPCombine"

        printf -v fhr3 '%03d' "$VR_FHR"

        export CEILING_AGL_DIR="$DATA/grid2obs/data/$MODELNAME/tmp"
        export CEILING_AGL_FILENAME="ceiling_agl.${NEST}.t${VHOUR}z.f${fhr3}.nc"
        export CEILING_FCST_FILE="$VR_FORECAST"

        mkdir -p "$CEILING_AGL_DIR"

        run_metplus.py -c "$PARMevs/metplus_config/machine.conf" \
            -c "$MET_PLUS_CONF/PCPCombine_fcstCAM_CEILING.conf"

        derived="$CEILING_AGL_DIR/$CEILING_AGL_FILENAME"

        [[ -s "$derived" ]] || {
            echo "ERROR: CEILING_AGL output missing or empty: $derived" >&2
            exit 8
        }

        echo "Derived CEILING_AGL: $derived"
    fi

    point_conf="PointStat_fcstCAM_obsMETAR.conf"

    
    if [[ "$VERIF_TYPE" == "raob" ]]; then
        if [[ -n "${FCST_VAR2_NAME:-}" ]]; then
            point_conf="PointStat_fcstCAM_obsRAOB_VAR2.conf"
        else
            point_conf="PointStat_fcstCAM_obsRAOB.conf"
        fi

        run_metplus.py -c "$PARMevs/metplus_config/machine.conf" \
            -c "$MET_PLUS_CONF/$point_conf"

    elif [[ "$VAR_NAME" == "CEILING" ]]; then
        run_metplus.py -c "$PARMevs/metplus_config/machine.conf" \
            -c "$MET_PLUS_CONF/$point_conf" \
            -c "$MET_PLUS_CONF/PointStat_fcstCAM_obsMETAR_CEILING.conf"
    elif [[ "$VAR_NAME" == "PTYPE" ]]; then
        run_metplus.py -c "$PARMevs/metplus_config/machine.conf" \
            -c "$MET_PLUS_CONF/PointStat_fcstCAM_obsMETAR_PTYPE.conf"
    elif [[ -n "${FCST_VAR2_NAME:-}" ]]; then
        run_metplus.py -c "$PARMevs/metplus_config/machine.conf" \
            -c "$MET_PLUS_CONF/PointStat_fcstCAM_obsMETAR_VAR2.conf"
    else
        run_metplus.py -c "$PARMevs/metplus_config/machine.conf" \
            -c "$MET_PLUS_CONF/$point_conf"
    fi

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

