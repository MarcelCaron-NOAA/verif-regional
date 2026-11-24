
"""Protect experiment outputs and record PBS submissions."""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


# Values that identify the verification task and its inputs.
IDENTITY_FIELDS = (
    "experiment",
    "init_date",
    "cycle",
    "fhr",
    "vdate",
    "vhour",
    "model",
    "forecast_id",
    "member",
    "domain",
    "obs_type",
    "variables",
    "source",
    "obs_file",
    "evs_root",
    "env_setup",
    "fix_root",
    "machine_config",
)


def _evs_revision(evs_root):
    try:
        return subprocess.check_output(
            ["git", "-C", str(evs_root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def ensure_safe_run(task):
    """Refuse collisions and unintended reuse of completed runs."""
    run = Path(task["run_dir"])

    if not run.exists():
        return

    if not run.is_dir():
        raise RuntimeError(f"Run path is not a directory: {run}")

    manifest_path = run / "manifest.json"

    if not manifest_path.is_file():
        if any(run.iterdir()):
            raise RuntimeError(
                f"Run directory already contains files without a manifest: "
                f"{run}\nUse a new experiment.name."
            )
        return

    try:
        previous = json.loads(manifest_path.read_text())
    except (ValueError, OSError) as exc:
        raise RuntimeError(
            f"Cannot read previous manifest: {manifest_path}"
        ) from exc

    differences = [
        key for key in IDENTITY_FIELDS
        if previous.get(key) != task.get(key)
    ]

    if differences:
        raise RuntimeError(
            f"Experiment directory collision: {run}\n"
            f"Changed task fields: {', '.join(differences)}\n"
            "Use a different experiment.name to avoid mixing results."
        )

    previous_sha = previous.get("evs_git_sha")
    current_sha = _evs_revision(task["evs_root"])

    if previous_sha and current_sha and previous_sha != current_sha:
        raise RuntimeError(
            f"EVS revision changed for existing run: {run}\n"
            f"Previous: {previous_sha}\nCurrent:  {current_sha}\n"
            "Use a new experiment.name."
        )

    if (run / "submission.json").exists():
        raise RuntimeError(
            f"Task was already submitted: {run}\n"
            "Use a new experiment.name. Automatic reruns "
            "are not implemented yet."
        )

    stat_dir = run / "work"
    if stat_dir.is_dir() and any(stat_dir.rglob("*.stat")):
        raise RuntimeError(
            f"Previous STAT outputs exist in {run}.\n"
            "Refusing to mix or reuse verification output."
        )

    log_dir = run / "logs"
    if log_dir.is_dir() and any(log_dir.glob("*.log")):
        raise RuntimeError(
            f"Previous PBS logs exist in {run}.\n"
            "Use a new experiment.name before resubmitting."
        )


def record_submission(task, job_id, job_name, log_file):
    """Save what PBS accepted. This is not a completion record."""
    path = Path(task["run_dir"]) / "submission.json"

    record = {
        "job_id": job_id,
        "job_name": job_name,
        "submitted_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "state_at_submission": "submitted",
        "experiment": task["experiment"],
        "forecast_id": task["forecast_id"],
        "verification_profile": task["model"],
        "init_date": task["init_date"],
        "cycle": task["cycle"],
        "forecast_hour": task["fhr"],
        "variables": task["variables"],
        "log_file": str(log_file),
    }

    # Exclusive creation prevents silently overwriting a prior record.
    with path.open("x") as output:
        json.dump(record, output, indent=2)
        output.write("\n")

    return path

