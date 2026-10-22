#!/usr/bin/python3
"""Estimate quota exhaustion from observed consumption; no network or inference."""
import datetime
import json
import os
from pathlib import Path
import statistics

ROOT = Path(os.environ.get('QUOTA_WATCH_ROOT', str(Path(__file__).resolve().parent / 'runtime'))).resolve()
UTC = datetime.timezone.utc


def timestamp(value):
    return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def iso(value):
    return datetime.datetime.fromtimestamp(value, UTC).isoformat()


def build_forecast(records, current):
    end = timestamp(current['checkedAt'])
    signature = tuple(current.get(k) for k in ('limitId', 'windowDurationMins', 'resetsAt'))
    # Use equally spaced bins so busy chats do not receive extra statistical weight.
    bins = {}
    for record in list(records) + [current]:
        if tuple(record.get(k) for k in ('limitId', 'windowDurationMins', 'resetsAt')) != signature:
            continue
        try:
            at, used = timestamp(record['checkedAt']), float(record['usedPercent'])
        except (ValueError, KeyError, TypeError):
            continue
        if end - 31 * 60 <= at <= end and 0 <= used <= 100:
            bins.setdefault(int(at // 30), []).append((at, used))
    points = sorted((max(t for t, _ in values), statistics.median(u for _, u in values))
                    for values in bins.values())
    # A substantial refill starts a new consumption segment, even within an epoch.
    start = 0
    for i in range(1, len(points)):
        if points[i][1] < points[i - 1][1] - 2:
            start = i
    points = points[start:]
    windows = {}
    for minutes in (5, 15, 30):
        selected = [(t, u) for t, u in points if t >= end - minutes * 60]
        if len(selected) < 4 or (selected[-1][0] - selected[0][0]) < minutes * 60 * 0.7:
            continue
        # Time-based least squares on 30-second bins smooths integer rounding.
        times = [(t - end) / 60 for t, _ in selected]
        used = [u for _, u in selected]
        mean_t, mean_u = statistics.mean(times), statistics.mean(used)
        denominator = sum((t - mean_t) ** 2 for t in times)
        slope = sum((t - mean_t) * (u - mean_u) for t, u in zip(times, used)) / denominator
        windows[str(minutes)] = {
            'requestedWindowMinutes': minutes, 'sampleBins': len(selected),
            'observedSpanMinutes': round((selected[-1][0] - selected[0][0]) / 60, 2),
            'fromAt': iso(selected[0][0]), 'toAt': iso(selected[-1][0]),
            'fromUsedPercent': selected[0][1], 'toUsedPercent': selected[-1][1],
            'ratePercentagePointsPerMinute': round(max(0.0, slope), 5),
        }
    remaining = current['remainingPercent']
    preferred = '15' if '15' in windows else ('5' if '5' in windows else None)
    chosen = windows.get(preferred, {})
    rate = chosen.get('ratePercentagePointsPerMinute', 0)
    result = {
        'status': 'estimating' if preferred and rate > 0.01 else 'insufficient_consumption',
        'asOf': current['checkedAt'], 'timezone': 'UTC',
        'remainingPercent': remaining, 'usedPercent': current['usedPercent'],
        'method': 'linear_trend_on_30_second_bins', 'primaryWindowMinutes': int(preferred) if preferred else None,
        'ratePercentagePointsPerMinute': rate if preferred else None, 'windows': windows,
        'minutesUntilEmpty': None, 'estimatedEmptyAt': None,
        'scenarioEarliestEmptyAt': None, 'scenarioLatestEmptyAt': None,
        'scenarioRangeMeaning': 'Different recent consumption windows; not a confidence interval',
        'conditionalOn': 'Current consumption continuing without a refill or task changes',
    }
    if preferred and rate > 0.01:
        duration = max(0, remaining) / rate
        result.update(minutesUntilEmpty=round(duration, 1), estimatedEmptyAt=iso(end + duration * 60))
        rates = [windows[k]['ratePercentagePointsPerMinute'] for k in ('5', '15')
                 if k in windows and windows[k]['ratePercentagePointsPerMinute'] > 0.01]
        result['scenarioEarliestEmptyAt'] = iso(end + max(0, remaining) / max(rates) * 60)
        result['scenarioLatestEmptyAt'] = iso(end + max(0, remaining) / min(rates) * 60)
        result['projected10PercentAt'] = iso(end + max(0, remaining - 10) / rate * 60)
        result['projected5PercentAt'] = iso(end + max(0, remaining - 5) / rate * 60)
    if remaining <= 0:
        result.update(status='exhausted', minutesUntilEmpty=0, estimatedEmptyAt=current['checkedAt'])
    return result


def update_forecast(current):
    records = []
    history = ROOT / 'quota-history.jsonl'
    if history.exists():
        for line in history.read_text().splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    result = build_forecast(records, current)
    destination = ROOT / 'forecast.json'
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    os.chmod(temporary, 0o600)
    temporary.replace(destination)
    with (ROOT / 'forecast-history.jsonl').open('a') as stream:
        stream.write(json.dumps({k: result.get(k) for k in (
            'asOf', 'status', 'remainingPercent', 'primaryWindowMinutes',
            'ratePercentagePointsPerMinute', 'minutesUntilEmpty', 'estimatedEmptyAt',
            'scenarioEarliestEmptyAt', 'scenarioLatestEmptyAt',
        )}) + '\n')
    return result


if __name__ == '__main__':
    current = json.loads((ROOT / 'state.json').read_text())['lastSuccessfulSnapshot']
    print(json.dumps(update_forecast(current), ensure_ascii=False))
