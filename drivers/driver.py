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
MODEL_TEMPLATE = (
    'rrfs.{init_date}/{cycle}/rrfs.t{cycle}z.2dfld.3km.f{fhr:03d}.conus.grib2'
)
REQUIRED_EVS_FILES = [
    'ush/cam/cam_stats_grid2obs_preprocess_prepbufr.py',
    'ush/cam/cam_stats_grid2obs_var_defs.py',
    'parm/evs_config/cam/config.evs.prod.stats.cam.atmos.grid2obs.rrfs',
    'parm/metplus_config/stats/cam/grid2obs/PB2NC_obsMETAR.conf',
    'parm/metplus_config/stats/cam/grid2obs/PointStat_fcstCAM_obsMETAR.conf',
    'parm/metplus_config/machine.conf',
]


def read_yaml(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(f"Configuration does not exist: {path}")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML mapping at {path}")
    return data


def expand(path: str, relative_to: Path | None = None) -> Path:
    resolved = Path(os.path.expanduser(os.path.expandvars(str(path))))
    if not resolved.is_absolute() and relative_to:
        resolved = relative_to / resolved
    return resolved.resolve(strict=False)


def requested_hours(cfg: dict) -> list[int]:
    expt = cfg['experiment']

    if 'forecast_hours' not in expt:
        return [int(expt['forecast_hour'])]

    if 'forecast_hour' in expt:
        raise ValueError(
            "Use either forecast_hour or forecast_hours, not both"
        )

    hours = expt['forecast_hours']

    if not isinstance(hours, list) or not hours:
        raise ValueError("forecast_hours must be a nonempty list")

    if any(type(h) is not int or not 0 <= h <= 84 for h in hours):
        raise ValueError("forecast_hours must contain integers from 0 to 84")

    if len(hours) != len(set(hours)):
        raise ValueError("Duplicate forecast hours are not allowed")

    # Temporary submission limit until concurrency controls are added.
    if len(hours) > 12:
        raise ValueError("Prototype currently supports at most 12 forecast hours")

    return sorted(hours)


def time_case(expt: dict) -> tuple[dt.datetime, dt.datetime, int]:
    if expt.get('model') != 'rrfs' or expt.get('domain') != 'conus':
        raise ValueError("POC only supports model: rrfs, domain: conus")
    cycle = int(expt['cycle'])
    if not 0 <= cycle <= 23:
        raise ValueError('cycle must be between 0 and 23')
    fhr = int(expt['forecast_hour'])
    if not 0 <= fhr <= 84:
        raise ValueError('forecast_hour must be between 0 and 84')
    init = dt.datetime.strptime(str(expt['init_date']), '%Y%m%d').replace(hour=cycle)
    valid = init + dt.timedelta(hours=fhr)
    if valid.hour not in (0, 3, 6, 9, 12, 15, 18, 21):
        raise ValueError('This POC only supports the existing RRFS METAR 3-hourly valid times')
    return init, valid, fhr


def source_paths(config: dict, initial: dt.datetime, valid: dt.datetime, fhr: int):
    inp = config['input']
    root = expand(inp['root'])
    fields = dict(init_date=initial.strftime('%Y%m%d'), cycle=initial.strftime('%H'),
                  fhr=fhr, valid_date=valid.strftime('%Y%m%d'),
                  valid_hour=valid.strftime('%H'))
    template = inp.get('template', MODEL_TEMPLATE)
    try:
        rel_src = template.format(**fields)
        rel_evs = MODEL_TEMPLATE.format(**fields)
    except (ValueError, KeyError) as exc:
        raise ValueError(f'Invalid input template: {exc}') from exc
    source = expand(rel_src, root)
    if source == root:
        raise ValueError('Input template must name a forecast file')
    return source, Path(rel_evs)


def resolve_obs_root(
    value: str, 
    valid: dt.datetime,
    auto_root: str | None = None,
):
    rel = f'rrfs.{valid:%Y%m%d}/rrfs.t{valid:%H}z.prepbufr.tm00'
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
        return found[0].parents[1], found[0], []
    if len(found) > 1:
        return None, None, [f"Multiple obsproc versions found: {', '.join(map(str, found))}. Set observations.root explicitly."]
    return None, None, [f"Cannot auto-find obsproc file under {base_root}. Set observations.root explicitly."]


def spec(config_path: Path, evs_option: str | None = None, 
         fhr_override: int | None = None):
    cfg = read_yaml(config_path)
    
    verification = cfg.get('verification') or {}
    if set(verification) != {'grid2obs'}:
        raise ValueError(
            "Currently only verification.grid2obs is supported"
        )
    grid2obs = verification['grid2obs']
    obs_types = grid2obs.get('observations', [])
    variables = grid2obs.get('variables', [])
    if obs_types != ['metar'] or variables not in (
            ['TMP2m'], ['DPT2m']
            ):
        raise ValueError(
            "Currently supported: RRFS CONUS grid2obs, "
            "METAR, with one variable (TMP2m or DPT2m)"
        )
    e = dict(cfg['experiment'])
    if fhr_override is not None:
        e['forecast_hour'] = fhr_override
    init, valid, fhr = time_case(e)
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', e['name']):
        raise ValueError('experiment.name must contain only letters, numbers, hyphen, underscore')
    source, evs_relative = source_paths(cfg, init, valid, fhr)
    machine_cfg = expand(
        cfg['machine']['profile'], config_path.parent
    )
    machine = read_yaml(machine_cfg)
    obs_root, obs_file, obs_warnings = resolve_obs_root(
        cfg['observations'].get('root', 'auto'), 
        valid,
        (machine.get('paths') or {}).get('obsproc_root'),
    )
    output_root = expand(cfg['output']['root'])
    case_name = f"{e['name']}_{init:%Y%m%d%H}_f{fhr:03d}"
    run_dir = output_root / case_name
    evs_root = expand(evs_option or os.getenv('VERIF_REGIONAL_EVS_ROOT') or str(REPO / 'evs'))
    missing_evs = [x for x in REQUIRED_EVS_FILES if not (evs_root / x).is_file()]
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
        model=e['model'], domain=e['domain'], obs_type=obs_types[0], variable=variables[0],
        source=str(source), evs_relative=str(evs_relative),
        obs_root=str(obs_root) if obs_root else None,
        obs_file=str(obs_file) if obs_file else None,
        obs_relative=f'rrfs.{valid:%Y%m%d}/rrfs.t{valid:%H}z.prepbufr.tm00',
        obs_warnings=obs_warnings, evs_root=str(evs_root), missing_evs=missing_evs,
        run_dir=str(run_dir),
        env_setup=str(expand(env_setup, env_setup_base)) if env_setup else '',
        fix_root=str(expand(fix_root, fix_root_base)) if fix_root else '',
        machine=machine, machine_config=str(machine_cfg),
    )


def issues(s: dict) -> list[str]:
    errors = []
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

def field_check(source: str, variable: str):
    patterns = {
        'TMP2m': ':TMP:2 m above ground:',
        'DPT2m': ':DPT:2 m above ground:',
    }

    pattern = patterns[variable]
    wgrib = shutil.which('wgrib2')

    if not wgrib or not Path(source).is_file():
        return 'not checked (wgrib2 unavailable or forecast missing)'

    proc = subprocess.run(
        [wgrib, source, '-match', pattern, '-s'],
        capture_output=True,
        text=True,
    )

    if proc.returncode != 0:
        return (
            f'FAILED: wgrib2 returned {proc.returncode}: '
            + proc.stderr.strip()[:250]
        )

    return 'present' if proc.stdout.strip() else f'MISSING: {variable}'


def show_plan(s: dict, check_field: bool = False):
    print(
        f"Case:    {s['experiment']}  "
        f"{s['model'].upper()} {s['domain'].upper()} "
        f"{s['obs_type'].upper()} {s['variable']}"
    )
    print(f"Init:    {s['init_date']} {s['cycle']}Z  F{s['fhr']:03d}")
    print(f"Valid:   {s['vdate']} {s['vhour']}Z")
    print(f"Source:  {s['source']}")
    print(f"Obs:     {s['obs_file'] or '(unresolved)'}")
    print(f"Staging: {s['run_dir']}/input/{s['evs_relative']}")
    print(f"EVS:     {s['evs_root']}")
    print(f"Machine: {s['machine'].get('name', 'unknown')}")
    print(f"Env:     {s['env_setup']}")
    print(f"Fix:     {s['fix_root']}")
    print(f"Output:  {s['run_dir']}/work/grid2obs/METplus_output/")
    print('Steps:   stage symlinks > preprocess ADPSFC > EVS PB2NC > EVS PointStat')
    if check_field:
        print(
            f"{s['variable']}:   " 
            + field_check(s['source'], s['variable'])
        )
    problems = issues(s)
    if problems:
        print('\nNeeds attention:')
        for problem in problems:
            print('  - '+problem)
    else:
        print('\nPreflight checks passed.  Runtime environment will be initialized by PBS.')
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
    
    # Perform a GRIB field-level preflight when wgrib2 is available.
    field_status = field_check(s['source'], s['variable'])
    if field_status.startswith(('MISSING:', 'FAILED:')):
        raise RuntimeError(
            f'Forecast field check failed: {field_status}'
        )
    if field_status.startswith('not checked'):
        print(
            f'WARNING: {s["variable"]} GRIB2 field not inspected: '
            f'{field_status}'
        )
    else:
        print(
            f'Forecast {s["variable"]} GRIB2 field: {field_status}'
        )
    run = Path(s['run_dir'])
    for folder in ['input', 'jobs', 'logs', 'work']:
        (run / folder).mkdir(parents=True, exist_ok=True)
    staged_fcst = run / 'input' / s['evs_relative']
    staged_obs = run / 'input' / 'obsproc' / s['obs_relative']
    symlink_checked(Path(s['source']), staged_fcst)
    symlink_checked(Path(s['obs_file']), staged_obs)
    manifest = {
        k: v for k, v in s.items()
        if k not in ('machine', 'missing_evs', 'obs_warnings')
    }
    manifest['forecast_staged'] = str(staged_fcst)
    manifest['obs_staged'] = str(staged_obs)
    manifest['forecast_stage_root'] = str(run / 'input')
    manifest['obs_stage_root'] = str(run / 'input' / 'obsproc')
    manifest['evs_fingerprints'] = {
        x: hashlib.sha256((Path(s['evs_root']) / x).read_bytes()).hexdigest()
        for x in REQUIRED_EVS_FILES
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
    print(f'Staged two symlinks and wrote: {target}')
    return target


def job_details(s: dict):
    job_name = (
        f"vr_{s['init_date'][4:]}{s['cycle']}_f{s['fhr']:03d}"
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
    command = REPO / 'scripts' / 'stats' / 'run_rrfs_metar_tmp2m.sh'
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
    parser.add_argument('command', choices=['plan', 'stage', 'write-job', 'submit'])
    parser.add_argument('config', type=Path)
    parser.add_argument('--evs-root', help='Use existing EVS tree (e.g. extracted zip) instead of ./evs')
    parser.add_argument('--check-fields', action='store_true', help='Check TMP2m GRIB2 field using wgrib2, if available')
    args = parser.parse_args(argv)
    try:
        config_path = args.config.resolve()
        cfg = read_yaml(config_path)
        hours = requested_hours(cfg)

        failed = False
        processed = 0
        skipped = 0

        for hour in hours:
            s = spec(
                config_path,
                args.evs_root,
                fhr_override=hour,
            )

            print(f"\n{'=' * 50}")
            print(f"Forecast F{hour:03d}")
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

            if len(hours) > 1 and missing_input:
                print(f"SKIPPED F{hour:03d}: missing input")
                print(f"  Forecast: {s['source']}")
                print(f"  Obs:      {s['obs_file']}")
                skipped += 1
                continue

            try:
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

                        job_name, log_file = job_details(s)

                        print("\nPBS job submitted")
                        print(f"  Job ID:     {result.stdout.strip()}")
                        print(f"  Job name:   {job_name}")
                        print(f"  Job script: {job}")
                        print(f"  Log:        {log_file}")
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

        if args.command != 'plan':
            print(
                f"\nSummary: {processed} processed, "
                f"{skipped} skipped, "
                f"errors={'yes' if failed else 'no'}"
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

