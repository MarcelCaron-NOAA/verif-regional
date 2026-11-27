#!/usr/bin/env python3
"""Minimal, intentionally narrow verif-regional experiment driver.

First supported case: RRFS CONUS 2-m temperature / METAR / one FHR.
Does not call EVS dev/ or jobs/. Requires PyYAML (`import yaml`).
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required for the YAML experiment configuration (install in the EVS Python environment).")


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from ush.evs_catalog import discover, check_field
from ush.model_profiles import get_product_key, get_staging_template, get_var_set_num, validate_member
from ush.task_planner import requested_hours, requested_cases
from ush.task_state import ensure_safe_run, record_submission

MODEL_TEMPLATE = (
    'rrfs.{init_date}/{cycle}/rrfs.t{cycle}z.2dfld.3km.f{fhr:03d}.conus.grib2'
)
REQUIRED_EVS_FILES = [
    'ush/cam/cam_stats_grid2obs_preprocess_prepbufr.py',
    'ush/cam/cam_stats_grid2obs_var_defs.py',
    'parm/metplus_config/stats/cam/grid2obs/PB2NC_obsMETAR.conf',
    'parm/metplus_config/stats/cam/grid2obs/PointStat_fcstCAM_obsMETAR.conf',
    'parm/metplus_config/stats/cam/grid2obs/PointStat_fcstCAM_obsMETAR_VAR2.conf',
    'parm/metplus_config/machine.conf',
]


def read_yaml(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(f"Configuration does not exist: {path}")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML mapping at {path}")
    return data



def grid2obs_requests(cfg):
    """Return ordered (observation type, variables) requests.

    Accept both:
      grid2obs:
        metar: [TMP2m]
        raob: [TMP, HGT]

    and the original:
      grid2obs:
        observations: [metar]
        variables: [TMP2m]
    """
    verification = cfg.get("verification")

    if (
        not isinstance(verification, dict)
        or set(verification) != {"grid2obs"}
    ):
        raise ValueError(
            "Currently only verification.grid2obs is supported"
        )

    grid = verification["grid2obs"]
    if not isinstance(grid, dict) or not grid:
        raise ValueError(
            "verification.grid2obs must be a nonempty mapping"
        )

    legacy = (
        "observations" in grid or "variables" in grid
    )

    if legacy:
        if set(grid) != {"observations", "variables"}:
            raise ValueError(
                "Do not mix the old observations/variables "
                "syntax with the new observation mapping"
            )

        obs_types = grid["observations"]
        if not isinstance(obs_types, list) or len(obs_types) != 1:
            raise ValueError(
                "Legacy grid2obs requires exactly one "
                "observation type"
            )

        requests = [(obs_types[0], grid["variables"])]
    else:
        requests = list(grid.items())

    for obs_type, variables in requests:
        if obs_type not in ("metar", "raob"):
            raise ValueError(
                f"Unsupported grid2obs observation type: {obs_type}"
            )

        if (
            not isinstance(variables, list)
            or not variables
            or any(
                not isinstance(v, str) or not v
                for v in variables
            )
            or len(variables) != len(set(variables))
        ):
            raise ValueError(
                f"grid2obs.{obs_type} requires a nonempty "
                "list of distinct variable names"
            )

    return requests


def expand(path: str, relative_to: Path | None = None) -> Path:
    resolved = Path(os.path.expanduser(os.path.expandvars(str(path))))
    if not resolved.is_absolute() and relative_to:
        resolved = relative_to / resolved
    return resolved.resolve(strict=False)


def time_case(expt: dict, obs_type="metar") -> tuple[dt.datetime, dt.datetime, int]:
    if expt.get("model") not in ("rrfs", "hrrr", "rrfsmem"):
        raise ValueError("Unsupported EVS model profile")

    if expt.get("domain") != "conus":
        raise ValueError("Only CONUS is currently supported")
    cycle = int(expt['cycle'])
    if not 0 <= cycle <= 23:
        raise ValueError('cycle must be between 0 and 23')
    fhr = int(expt['forecast_hour'])
    if not 0 <= fhr <= 84:
        raise ValueError('forecast_hour must be between 0 and 84')
    init = dt.datetime.strptime(str(expt['init_date']), '%Y%m%d').replace(hour=cycle)
    valid = init + dt.timedelta(hours=fhr)
    if obs_type == "raob":
        valid_hours = (0, 6, 12, 18)
    else:
        valid_hours = (0, 3, 6, 9, 12, 15, 18, 21)
    if valid.hour not in valid_hours:
        raise ValueError(
            f"{obs_type.upper()} verification requires valid hours "
            f"{', '.join(f'{h:02d}Z' for h in valid_hours)}"
        )
    return init, valid, fhr


def source_paths(config: dict, initial: dt.datetime, valid: dt.datetime, 
                 fhr: int, profile="rrfs", member=None, obs_type="metar",
                 var_set_num=0, template_override=None):
    inp = config['input']
    root = expand(inp['root'])
    fields = dict(init_date=initial.strftime('%Y%m%d'), cycle=initial.strftime('%H'),
                  fhr=fhr, valid_date=valid.strftime('%Y%m%d'),
                  valid_hour=valid.strftime('%H'), member=member)
    staging_template = get_staging_template(
        profile, obs_type, var_set_num
    )
    template = (
        template_override
        if template_override is not None
        else inp.get('template', staging_template)
    )
    try:
        rel_src = template.format(**fields)
        rel_evs = staging_template.format(**fields)
    except (ValueError, KeyError) as exc:
        raise ValueError(f'Invalid input template: {exc}') from exc
    source = expand(rel_src, root)
    if source == root:
        raise ValueError('Input template must name a forecast file')
    return source, Path(rel_evs)



def obs_relative_path(valid, obs_type):
    if obs_type == "metar":
        return (
            f"rrfs.{valid:%Y%m%d}/"
            f"rrfs.t{valid:%H}z.prepbufr.tm00"
        )
    if obs_type == "raob":
        return (
            f"gdas.{valid:%Y%m%d}/{valid:%H}/atmos/"
            f"gdas.t{valid:%H}z.prepbufr"
        )
    raise ValueError(f"Unsupported observations: {obs_type}")


def resolve_obs_root(
    value: str, 
    valid: dt.datetime,
    auto_root: str | None = None,
    obs_type="metar",
):
    rel = obs_relative_path(valid, obs_type)
    if value.strip().lower() != 'auto':
        base = expand(value)
        return base, base / rel, []
    env_ver = os.getenv('obsproc_ver', '')
    if not auto_root:
        raise ValueError(
            "observations.root is 'auto', but the machine profile "
            "does not define paths.obsproc_root"
        )
    base_root = expand(auto_root)
    candidates = []
    if env_ver:
        candidates.append(base_root / env_ver / rel)
    candidates.extend(Path(p) for p in glob.glob(str(base_root / 'v*' / rel)))
    # Resolve unique available paths without silently choosing an arbitrary version.
    found = sorted({p for p in candidates if p.is_file()})
    if len(found) == 1:
        return (
            found[0].parents[len(Path(rel).parts) - 1], 
            found[0], 
            [],
        )
    if len(found) > 1:
        return None, None, [f"Multiple obsproc versions found: {', '.join(map(str, found))}. Set observations.root explicitly."]
    return None, None, [f"Cannot auto-find obsproc file under {base_root}. Set observations.root explicitly."]


def spec(config_path: Path, evs_option: str | None = None, 
         fhr_override: int | None = None, init_date_override: str | None = None,
         cycle_override: int | None = None, obs_type_override=None):
    cfg = read_yaml(config_path)
    
    
    requests = grid2obs_requests(cfg)

    if obs_type_override is None:
        if len(requests) != 1:
            raise ValueError(
                "Configuration requests multiple observation "
                "types; select one with obs_type_override"
            )
        obs_type, variables = requests[0]
    else:
        request_map = dict(requests)

        if obs_type_override not in request_map:
            raise ValueError(
                f"Observation type not requested: "
                f"{obs_type_override}"
            )

        obs_type = obs_type_override
        variables = request_map[obs_type]

    # Preserve the existing internal interface.
    obs_types = [obs_type]

    observation_mapping = (
        "observations"
        not in cfg["verification"]["grid2obs"]
    )

    e = dict(cfg['experiment'])
    
    if 'model' in cfg and 'forecast' in cfg:
        raise ValueError(
            "Use model: or forecast:, not both. "
            "The preferred syntax is model:"
        )

    model_cfg = cfg.get('model', cfg.get('forecast', {}))

    if not isinstance(model_cfg, dict):
        raise ValueError("model must be a YAML mapping")

    profile = model_cfg.get(
        'verification_profile', e.get('model')
    )
    model_id = model_cfg.get('id', profile)

    if not isinstance(profile, str) or not profile:
        raise ValueError(
            "Set model.verification_profile (e.g. rrfs)"
        )

    if (
        'model' in e
        and 'verification_profile' in model_cfg
        and e['model'] != profile
    ):
        raise ValueError(
            "experiment.model conflicts with "
            "model.verification_profile. "
            "Remove experiment.model from the YAML."
        )

    if (
        not isinstance(model_id, str)
        or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', model_id)
    ):
        raise ValueError(
            "model.id must start with a letter and contain "
            "only letters, numbers, and underscores"
        )

    # Internal compatibility with existing EVS execution logic.
    e['model'] = profile
    forecast_id = model_id

    member = validate_member(profile, model_cfg.get("member"))

    
    var_sets = {
        variable: get_var_set_num(
            profile, obs_types[0], variable
        )
        for variable in variables
    }

    required_sets = sorted(set(var_sets.values()))
    var_set_num = required_sets[0]

    if init_date_override is not None:
        e['init_date'] = init_date_override
    if cycle_override is not None:
        e['cycle'] = cycle_override
    if fhr_override is not None:
        e['forecast_hour'] = fhr_override
    init, valid, fhr = time_case(e, obs_types[0])
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', e['name']):
        raise ValueError('experiment.name must contain only letters, numbers, hyphen, underscore')
    
    # Resolve each required EVS product independently.
    inp = cfg["input"]
    templates = inp.get("templates", {})

    if not isinstance(templates, dict):
        raise ValueError("input.templates must be a YAML mapping")

    unknown_products = set(templates) - {"pressure", "surface"}
    if unknown_products:
        raise ValueError(
            f"Unknown input template products: {sorted(unknown_products)}"
        )

    if "template" in inp and "templates" in inp:
        raise ValueError(
            "Use input.template or input.templates, not both"
        )

    if len(required_sets) > 1 and "template" in inp:
        raise ValueError(
            "Mixed-product verification cannot use one input.template. "
            "Use input.templates with pressure/surface entries, or "
            "remove input.template to use EVS filename conventions."
        )

    forecast_inputs = {}

    for set_num in required_sets:
        product = get_product_key(obs_types[0], set_num)
        override = templates.get(product)

        if override is not None and (
            not isinstance(override, str) or not override.strip()
        ):
            raise ValueError(
                f"input.templates.{product} must be a nonempty string"
            )

        product_source, product_relative = source_paths(
            cfg, init, valid, fhr,
            profile=profile,
            member=member,
            obs_type=obs_types[0],
            var_set_num=set_num,
            template_override=override,
        )

        forecast_inputs[str(set_num)] = {
            "product": product,
            "source": str(product_source),
            "evs_relative": str(product_relative),
        }

    # Preserve existing single-product fields for compatibility.
    primary = forecast_inputs[str(var_set_num)]
    source = primary["source"]
    evs_relative = primary["evs_relative"]

    machine_cfg = expand(
        cfg['machine']['profile'], config_path.parent
    )
    machine = read_yaml(machine_cfg)
    obs_root, obs_file, obs_warnings = resolve_obs_root(
        cfg['observations'].get('root', 'auto'), 
        valid,
        (machine.get('paths') or {}).get('obsproc_root'),
        obs_type=obs_types[0],
    )
    output_root = expand(cfg['output']['root'])
    
    case_name = (
        f"{e['name']}_{init:%Y%m%d%H}_f{fhr:03d}"
    )

    if observation_mapping:
        case_name += f"_{obs_type}"

    run_dir = output_root / case_name
    evs_root = expand(evs_option or os.getenv('VERIF_REGIONAL_EVS_ROOT') or str(REPO / 'evs'))
    required_evs_files = REQUIRED_EVS_FILES + [
        'parm/evs_config/cam/'
        f'config.evs.prod.stats.cam.atmos.grid2obs.{profile}'
    ]
    if "CEILING" in variables:
        required_evs_files.extend([
            "parm/metplus_config/stats/cam/grid2obs/"
            "PCPCombine_fcstCAM_CEILING.conf",
            "parm/metplus_config/stats/cam/grid2obs/"
            "PointStat_fcstCAM_obsMETAR_CEILING.conf",
        ])
    if "PTYPE" in variables:
        required_evs_files.extend([
            "parm/metplus_config/stats/cam/grid2obs/"
            "RegridDataPlane_fcstCAM_PTYPE.conf",
            "parm/metplus_config/stats/cam/grid2obs/"
            "PointStat_fcstCAM_obsMETAR_PTYPE.conf",
            "ush/cam/cam_stats_grid2obs_create_merged_ptype.py",
        ])
    if obs_types[0] == "raob":
        required_evs_files.extend([
            "parm/metplus_config/stats/cam/grid2obs/"
            "PB2NC_obsRAOB.conf",
            "parm/metplus_config/stats/cam/grid2obs/"
            "PointStat_fcstCAM_obsRAOB.conf",
            "parm/metplus_config/stats/cam/grid2obs/"
            "PointStat_fcstCAM_obsRAOB_VAR2.conf",
        ])

    missing_evs = [
        x for x in required_evs_files
        if not (evs_root / x).is_file()
    ]
    catalog = discover(evs_root, e['model'], obs_types[0])

    unknown = [v for v in variables if v not in catalog]
    if unknown:
        raise ValueError(
            f"Variables not defined by EVS for "
            f"{e['model']}/{obs_types[0]}: {unknown}"
        )

    blocked = [
        (v, catalog[v]['reason'])
        for v in variables
        if catalog[v]['status'] == 'needs-adapter'
    ]

    if blocked:
        details = "; ".join(
            f"{name}: {reason}" for name, reason in blocked
        )
        raise ValueError(
            f"Requested variables need additional wrapper support: "
            f"{details}"
        )
    experiment_env = cfg.get('environment') or {}
    machine_env = machine.get('environment') or {}
    exp_setup = experiment_env.get('setup_script') or ''
    exp_fix = experiment_env.get('fix_root') or ''
    env_setup = exp_setup or machine_env.get('setup_script') or ''
    fix_root = exp_fix or machine_env.get('fix_root') or ''
    env_setup_base = (
        config_path.parent if exp_setup else machine_cfg.parent
    )
    fix_root_base = (
        config_path.parent if exp_fix else machine_cfg.parent
    )
    return dict(
        experiment=e['name'], init_date=init.strftime('%Y%m%d'), cycle=init.strftime('%H'),
        fhr=fhr, vdate=valid.strftime('%Y%m%d'), vhour=valid.strftime('%H'),
        model=e['model'], forecast_id=forecast_id, member=member, 
        domain=e['domain'], obs_type=obs_types[0], variable=variables[0],
        variables=list(variables), variable_info={v: catalog[v] for v in variables},
        var_set_num=var_set_num, variable_sets=var_sets, forecast_inputs=forecast_inputs, 
        source=str(source), evs_relative=str(evs_relative),
        obs_root=str(obs_root) if obs_root else None,
        obs_file=str(obs_file) if obs_file else None,
        obs_relative=obs_relative_path(valid, obs_types[0]), 
        observation_mapping=observation_mapping,
        obs_warnings=obs_warnings, evs_root=str(evs_root), missing_evs=missing_evs,
        required_evs_files=required_evs_files, run_dir=str(run_dir),
        env_setup=str(expand(env_setup, env_setup_base)) if env_setup else '',
        fix_root=str(expand(fix_root, fix_root_base)) if fix_root else '',
        machine=machine, machine_config=str(machine_cfg),
    )


def issues(s: dict) -> list[str]:
    errors = []
    for product in s["forecast_inputs"].values():
        if not Path(product["source"]).is_file():
            errors.append(
                f"{product['product']} forecast missing: "
                f"{product['source']}"
            )
    if not Path(s['source']).is_file():
        errors.append(f"Forecast missing: {s['source']}")
    if not s['obs_file'] or not Path(s['obs_file']).is_file():
        errors.extend(s['obs_warnings'] or ['Obsproc file missing: '+str(s['obs_file'])])
    if s['missing_evs']:
        errors.append(f"EVS files missing under {s['evs_root']}: {', '.join(s['missing_evs'])}")
    
    env_setup = s['env_setup']
    if not env_setup:
        errors.append("No machine environment setup script configured")
    elif not Path(env_setup).is_file():
        errors.append(f"Environment setup script missing: {env_setup}")
    
    fix_root = s['fix_root']
    if not fix_root:
        errors.append("No EVS fix directory configured")
    elif not Path(fix_root).is_dir():
        errors.append(f"EVS fix directory missing: {fix_root}")
    elif not (Path(fix_root) / 'masks').is_dir():
        errors.append(f"EVS masks directory missing: {fix_root}/masks")
    return errors


def field_check(source: str, variable: str, s: dict):
    return check_field(source, s['variable_info'][variable])


def variable_source(s, variable):
    set_num = str(s["variable_sets"][variable])
    return s["forecast_inputs"][set_num]["source"]


def show_plan(s: dict, check_field: bool = False):
    print(f"Case:     {s['experiment']}")
    print(
        f"Model:    {s['forecast_id']} "
        f"(EVS profile: {s['model']})"
    )
    print(
        f"Verify:   {s['domain'].upper()} "
        f"{s['obs_type'].upper()} "
        f"{', '.join(s['variables'])}"
    )
    print(f"Init:     {s['init_date']} {s['cycle']}Z  F{s['fhr']:03d}")
    print(f"Valid:    {s['vdate']} {s['vhour']}Z")
    print(f"Obs:      {s['obs_file'] or '(unresolved)'}")
    for set_num in sorted(s["forecast_inputs"], key=int):
        product = s["forecast_inputs"][set_num]
        label = product["product"]

        print(f"Source ({label}):  {product['source']}")
        print(
            f"Staging ({label}): "
            f"{s['run_dir']}/input/{product['evs_relative']}"
        )
    print(f"EVS:      {s['evs_root']}")
    print(f"Machine:  {s['machine'].get('name', 'unknown')}")
    print(f"Env:      {s['env_setup']}")
    print(f"Fix:      {s['fix_root']}")
    print(f"Output:   {s['run_dir']}/work/grid2obs/METplus_output/")
    subset = "ADPUPA" if s["obs_type"] == "raob" else "ADPSFC"
    print(
        f"Steps:    stage symlinks > preprocess {subset} "
        "> EVS PB2NC > EVS PointStat"
    )
    problems = issues(s)

    for variable in s['variables']:
        info = s['variable_info'][variable]

        print(f"{variable}: {info['status']}")

        if check_field:
            result = field_check(variable_source(s, variable), variable, s)
            print(f"  GRIB2: {result}")

            if result.startswith(('MISSING:', 'FAILED:')):
                problems.append(
                    f"{variable} GRIB2 field check: {result}"
                )
    if problems:
        print('\nNeeds attention:')
        for problem in problems:
            print('  - '+problem)
    else:
        print('\nPreflight: Passed.  No blocking issues found.')
    return problems


def symlink_checked(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        if target.resolve() == source.resolve():
            return
        raise RuntimeError(f'Refusing to replace existing symlink: {target}')
    if target.exists():
        raise RuntimeError(f'Refusing to replace existing file: {target}')
    target.symlink_to(source)


def stage(s: dict):
    problems = issues(s)
    if problems:
        raise RuntimeError('Cannot stage until required inputs exist:\n  - '+'\n  - '.join(problems))
    
    # Check every requested forecast field.
    for variable in s['variables']:
        field_status = field_check(variable_source(s, variable), variable, s)

        if field_status.startswith(('MISSING:', 'FAILED:')):
            raise RuntimeError(
                f'Forecast field check failed: {field_status}'
            )

        if field_status.startswith('not checked'):
            print(
                    f'NOTE: {variable} GRIB2 inventory check was skipped: '
                f'{field_status}'
            )
        else:
            print(
                f'Forecast {variable} GRIB2 field: {field_status}'
            )
    run = Path(s['run_dir'])
    for folder in ['input', 'jobs', 'logs', 'work']:
        (run / folder).mkdir(parents=True, exist_ok=True)
    
    staged_by_set = {}

    for set_num, product in s["forecast_inputs"].items():
        staged = run / "input" / product["evs_relative"]

        symlink_checked(
            Path(product["source"]),
            staged,
        )
        staged_by_set[set_num] = str(staged)

    staged_fcst = Path(staged_by_set[str(s["var_set_num"])])

    staged_obs = run / "input" / "obsproc" / s["obs_relative"]
    symlink_checked(Path(s["obs_file"]), staged_obs)

    manifest = {
        k: v for k, v in s.items()
        if k not in ('machine', 'missing_evs', 'obs_warnings', 'variable_info')
    }
    manifest['forecast_staged'] = str(staged_fcst)
    manifest['obs_staged'] = str(staged_obs)
    manifest['forecast_stage_root'] = str(run / 'input')
    manifest['forecast_staged_by_set'] = staged_by_set
    manifest['obs_stage_root'] = str(run / 'input' / 'obsproc')
    manifest['evs_fingerprints'] = {
        x: hashlib.sha256((Path(s['evs_root']) / x).read_bytes()).hexdigest()
        for x in s['required_evs_files']
    }
    git_sha = None
    if (Path(s['evs_root']) / '.git').exists():
        try:
            git_sha = subprocess.check_output(
                ['git', '-C', s['evs_root'], 'rev-parse', 'HEAD'],
                text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            pass
    manifest['evs_git_sha'] = git_sha
    target = run / 'manifest.json'
    target.write_text(json.dumps(manifest, indent=2)+'\n')
    print(
        f"Staged {len(staged_by_set) + 1} symlinks "
        f"and wrote: {target}"
    )
    return target


def job_details(s: dict):
    
    if s.get("observation_mapping"):
        # Keep names short for PBS while distinguishing tasks.
        # Example: vr_m112000_f012 or vr_r112000_f012
        job_name = (
            f"vr_{s['obs_type'][0]}"
            f"{s['init_date'][4:]}{s['cycle']}"
            f"_f{s['fhr']:03d}"
        )
    else:
        # Preserve existing naming for legacy configurations.
        job_name = (
            f"vr_{s['init_date'][4:]}{s['cycle']}"
            f"_f{s['fhr']:03d}"
        )

    log_file = (
        Path(s['run_dir']) / 'logs' / f'{job_name}.log'
    )
    return job_name, log_file


def write_job(s: dict, manifest_path: Path):
    m = s['machine']
    if m.get('scheduler') != 'pbs':
        raise ValueError('Only PBS supported by this POC')
    account = str(m.get('account', ''))
    queue = str(m.get('queue', 'dev'))
    memory = str(m.get('memory', '100gb'))
    walltime = str(m.get('walltime', '01:00:00'))
    if not re.fullmatch(r'[A-Za-z0-9_-]+', account) or account.startswith('YOUR'):
        raise ValueError('Set a valid PBS account in parm/machines/wcoss2.yaml')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', queue):
        raise ValueError('Unexpected PBS queue')
    if not re.fullmatch(r'\d+(gb|mb)', memory, re.I):
        raise ValueError('memory must be e.g. 100gb')
    if not re.fullmatch(r'\d{2}:\d{2}:\d{2}', walltime):
        raise ValueError('walltime must be HH:MM:SS')
    run = Path(s['run_dir'])
    job_name, log_file = job_details(s)
    command = REPO / 'scripts' / 'stats' / 'run_grid2obs.sh'
    lines = [
        '#!/usr/bin/env bash',
        f'#PBS -N {job_name}',
        f'#PBS -A {account}',
        f'#PBS -q {queue}',
        f'#PBS -l select=1:ncpus=1:ompthreads=1:mem={memory}',
        f'#PBS -l walltime={walltime}',
        '#PBS -j oe',
        f'#PBS -o {log_file}',
        '',
        'set -euo pipefail',
        'export OMP_NUM_THREADS=1',
        f'export VR_EVS_ROOT={shlex.quote(s["evs_root"])}',
        f'export VR_ENV_SETUP={shlex.quote(s["env_setup"])}',
        f'export VR_FIX_ROOT={shlex.quote(s["fix_root"])}',
        f'export VR_MACHINE={shlex.quote(str(m["name"]))}',
        f'bash {shlex.quote(str(command))} {shlex.quote(str(manifest_path))}',
        '',
    ]
    job = run / 'jobs' / 'run.pbs'
    job.write_text('\n'.join(lines))
    job.chmod(0o750)
    print(f'Created PBS job: {job}')
    return job


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        'command',
        choices=['plan', 'stage', 'write-job', 'submit', 'list-variables']
    )
    parser.add_argument('config', type=Path, nargs='?')
    parser.add_argument('--model', default='rrfs')
    parser.add_argument('--obs', default='metar')
    parser.add_argument('--evs-root', help='Use existing EVS tree (e.g. extracted zip) instead of ./evs')
    parser.add_argument(
        '--check-fields', 
        action='store_true', 
        help='Check supported forecast GRIB2 fields using wgrib2')
    parser.add_argument(
        "--max-submit", 
        type=int,
        default=12,
        help="Maximum PBS jobs to submit in one invocation default: 12)",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == 'list-variables':
            evs_root = expand(
                args.evs_root
                or os.getenv('VERIF_REGIONAL_EVS_ROOT')
                or str(REPO / 'evs')
            )

            catalog = discover(
                evs_root, args.model, args.obs
            )

            print(f"EVS: {evs_root}")
            print(f"Model: {args.model}  Observations: {args.obs}")
            print()
            print(
                f"{'Variable':<20} {'Status':<18} Notes"
            )
            print("-" * 75)

            for name, info in catalog.items():
                print(
                    f"{name:<20} "
                    f"{info['status']:<18} "
                    f"{info['reason']}"
                )

            print(f"\n{len(catalog)} EVS-defined variables")
            return 0

        if args.config is None:
            parser.error(
                f"Configuration YAML required for {args.command}"
            )
        config_path = args.config.resolve()
        
        cfg = read_yaml(config_path)

        time_cases = requested_cases(cfg)
        requests = grid2obs_requests(cfg)

        cases = [
            (init_date, cycle, hour, obs_type)
            for init_date, cycle, hour in time_cases
            for obs_type, _ in requests
        ]

        if args.command == "submit":
            if args.max_submit < 1:
                raise ValueError("--max-submit must be positive")
            if len(cases) > args.max_submit:
                raise ValueError(
                    f"Experiment contains {len(cases)} tasks, exceeding "
                    f"the submission limit of {args.max_submit}. "
                    "Use --max-submit to authorize a larger batch."
                )
    
        failed = False
        processed = 0
        skipped = 0

        
        # Resolve and validate every task before starting submission.
        # A malformed RAOB request must not be discovered only
        # after the METAR jobs have already been submitted.
        prepared = []

        for init_date, cycle, hour, obs_type in cases:
            s = spec(
                config_path,
                args.evs_root,
                fhr_override=hour,
                init_date_override=init_date,
                cycle_override=cycle,
                obs_type_override=obs_type,
            )
            prepared.append(
                (init_date, cycle, hour, obs_type, s)
            )

        for init_date, cycle, hour, obs_type, s in prepared:

            print(f"\n{'=' * 50}")
            print(
                f"Init {init_date} {cycle:02d}Z / "
                f"F{hour:03d} / {obs_type.upper()}"
            )
            print(f"{'=' * 50}")

            if args.command == 'plan':
                problems = show_plan(
                    s, check_field=args.check_fields
                )
                failed = failed or bool(problems)
                continue

            # Missing forecast/obs inputs are common in retrospectives.
            # Skip these tasks without stopping the others.
            missing_input = (
                not Path(s['source']).is_file()
                or not s['obs_file']
                or not Path(s['obs_file']).is_file()
            )

            if len(cases) > 1 and missing_input:
                print(f"SKIPPED F{hour:03d}: missing input")
                print(f"  Forecast: {s['source']}")
                print(f"  Obs:      {s['obs_file']}")
                skipped += 1
                continue

            try:
                ensure_safe_run(s)
                manifest_path = stage(s)

                if args.command in ('write-job', 'submit'):
                    job = write_job(s, manifest_path)

                    if args.command == 'submit':
                        result = subprocess.run(
                            ['qsub', '-V', str(job)],
                            check=True,
                            capture_output=True,
                            text=True,
                        )

                        job_id = result.stdout.strip()
                        job_name, log_file = job_details(s)

                        print("\nPBS job submitted")
                        print(f"  Job ID:     {job_id}")
                        print(f"  Job name:   {job_name}")
                        print(f"  Job script: {job}")
                        print(f"  Log:        {log_file}")
                        try:
                            submission_file = record_submission(
                                s, job_id, job_name, log_file
                            )
                        except OSError as exc:
                            raise RuntimeError(
                                f"PBS job {job_id} WAS submitted, but its "
                                f" submission record could not be written: {exc}. "
                                f"Do not submit it again without checking PBS."
                            ) from exc
                        print(f"  Submission: {submission_file}")
                        print(
                            f"  Statistics: "
                            f"{Path(s['run_dir']) / 'work/grid2obs/METplus_output'}"
                        )

                processed += 1

            except (
                ValueError,
                KeyError,
                RuntimeError,
                OSError,
                subprocess.CalledProcessError,
            ) as exc:
                print(
                    f"ERROR F{hour:03d}: {exc}",
                    file=sys.stderr,
                )
                failed = True

        action_labels = {
            "stage": "staged",
            "write-job": "job_written",
            "submit": "submitted",
        }
        if args.command != 'plan':
            print(
                f"\nSummary: {processed} {action_labels[args.command]}, "
                f"{skipped} skipped, "
                f"submission/planning errors={'yes' if failed else 'no'}"
            )
            if processed == 0:
                return 1

        return 1 if failed else 0

    except (
        ValueError,
        KeyError,
        RuntimeError,
        OSError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())

