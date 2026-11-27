
"""Expand experiment dates, cycles, and forecast hours into tasks."""

from __future__ import annotations

import datetime as dt


def _parse_date(value):
    try:
        return dt.datetime.strptime(
            str(value), "%Y%m%d"
        ).date()
    except ValueError as exc:
        raise ValueError(
            f"Invalid initialization date {value!r}; expected YYYYMMDD"
        ) from exc


def requested_hours(cfg):
    expt = cfg["experiment"]

    if "forecast_hours" in expt:
        if "forecast_hour" in expt:
            raise ValueError(
                "Use forecast_hour or forecast_hours, not both"
            )
        hours = expt["forecast_hours"]
    else:
        hours = [int(expt["forecast_hour"])]

    if (
        not isinstance(hours, list)
        or not hours
        or any(type(h) is not int or not 0 <= h <= 84 for h in hours)
        or len(hours) != len(set(hours))
    ):
        raise ValueError(
            "Forecast hours must be a nonempty list of distinct "
            "integers between 0 and 84"
        )

    return sorted(hours)


def requested_cases(cfg):
    """Return (init_date, cycle, forecast_hour) tuples."""
    expt = cfg["experiment"]

    if "init_dates" in expt:
        if "init_date" in expt:
            raise ValueError(
                "Use init_date or init_dates, not both"
            )

        selection = expt["init_dates"]

        if isinstance(selection, dict):
            if set(selection) != {"start", "end"}:
                raise ValueError(
                    "init_dates range needs start and end"
                )

            start = _parse_date(selection["start"])
            end = _parse_date(selection["end"])

            if end < start:
                raise ValueError(
                    "init_dates.end precedes start"
                )

            dates = [
                start + dt.timedelta(days=day)
                for day in range((end - start).days + 1)
            ]

        elif isinstance(selection, list) and selection:
            dates = [_parse_date(d) for d in selection]
        else:
            raise ValueError(
                "init_dates must be a date list or start/end mapping"
            )
    else:
        dates = [_parse_date(expt["init_date"])]

    if len(dates) != len(set(dates)):
        raise ValueError("Duplicate initialization dates")

    if "cycles" in expt:
        if "cycle" in expt:
            raise ValueError(
                "Use cycle or cycles, not both"
            )
        cycles = expt["cycles"]
    else:
        cycles = [int(expt["cycle"])]

    if (
        not isinstance(cycles, list)
        or not cycles
        or any(type(c) is not int or not 0 <= c <= 23 for c in cycles)
        or len(cycles) != len(set(cycles))
    ):
        raise ValueError(
            "Cycles must be distinct integers from 0 to 23"
        )

    hours = requested_hours(cfg)

    return [
        (date.strftime("%Y%m%d"), cycle, hour)
        for date in sorted(dates)
        for cycle in sorted(cycles)
        for hour in hours
    ]

