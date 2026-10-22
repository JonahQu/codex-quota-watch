#!/usr/bin/python3
"""Read-only Codex quota watcher. Never starts an inference turn or consumes resets."""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time
from forecast import update_forecast

ROOT = Path(os.environ.get('QUOTA_WATCH_ROOT', str(Path(__file__).resolve().parent / 'runtime'))).resolve()
STATE = ROOT / 'state.json'
CLI = os.environ.get('CODEX_CLI', 'codex')
INTERVAL = 30


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_json(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def read_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def event(name, **fields):
    with (ROOT / 'logs/events.jsonl').open('a') as output:
        output.write(json.dumps(dict(at=now(), event=name, **fields), ensure_ascii=False) + '\n')


def record_snapshot(snapshot):
    """Retain every successful reading without exporting account identifiers."""
    sample = {key: snapshot.get(key) for key in (
        'checkedAt', 'limitId', 'usedPercent', 'remainingPercent',
        'windowDurationMins', 'resetsAt', 'resetCreditCount',
    )}
    sample.update(source='native_monitor', timestampKind='quota_read_time')
    with (ROOT / 'quota-history.jsonl').open('a') as output:
        fcntl.flock(output, fcntl.LOCK_EX)
        output.write(json.dumps(sample, ensure_ascii=False) + '\n')
        output.flush()
        os.fsync(output.fileno())


class ReadOnlyClient:
    def __init__(self):
        self.process = None
        self.serial = 0
        self.selector = selectors.DefaultSelector()
        self.buffer = b''

    def start(self):
        self.process = subprocess.Popen(
            [CLI, 'app-server', '--listen', 'stdio://'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
        )
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.request('initialize', {'clientInfo': {
            'name': 'codex_reset_watch', 'title': 'Codex Reset Watch', 'version': '1.0',
        }})
        self.send({'method': 'initialized', 'params': {}})

    def send(self, message):
        self.process.stdin.write((json.dumps(message) + '\n').encode())
        self.process.stdin.flush()

    def request(self, method, params=None):
        # Deliberate allowlist: quota reading cannot redeem credits or start work.
        if method not in ('initialize', 'account/read', 'account/rateLimits/read'):
            raise ValueError('Unsupported read-only method')
        self.serial += 1
        message = {'id': self.serial, 'method': method}
        if params is not None:
            message['params'] = params
        self.send(message)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                if not line.strip():
                    continue
                response = json.loads(line)
                if response.get('id') == self.serial:
                    if 'error' in response:
                        raise RuntimeError('Read-only RPC error: ' + str(response['error'].get('code')))
                    return response['result']
            if not self.selector.select(1):
                continue
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError('Read-only app-server disconnected')
            self.buffer += chunk
        raise TimeoutError('Read-only quota request timed out')

    def snapshot(self):
        account = (self.request('account/read', {}) or {}).get('account') or {}
        identity = account.get('id') or account.get('email')
        if not identity:
            raise RuntimeError('ChatGPT account identity unavailable')
        data = self.request('account/rateLimits/read')
        bucket = (data.get('rateLimitsByLimitId') or {}).get('codex')
        if bucket is None:
            bucket = data.get('rateLimits') or {}
        primary = bucket.get('primary') or {}
        used, duration, reset = (primary.get(k) for k in ('usedPercent', 'windowDurationMins', 'resetsAt'))
        if not isinstance(used, (int, float)) or not 0 <= used <= 100:
            raise RuntimeError('Valid quota percentage unavailable')
        if not isinstance(duration, int) or duration < 1440 or not isinstance(reset, int):
            raise RuntimeError('Global long-window quota unavailable')
        return {
            'checkedAt': now(), 'accountFingerprint': hashlib.sha256(identity.lower().encode()).hexdigest(),
            'limitId': bucket.get('limitId') or 'codex',
            'usedPercent': used, 'remainingPercent': max(0, 100 - used),
            'windowDurationMins': duration, 'resetsAt': reset,
            'resetCreditCount': (data.get('rateLimitResetCredits') or {}).get('availableCount'),
        }

    def close(self):
        self.selector.close()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


def reset_reason(before, after, timestamp=None):
    if not before or any(before.get(k) != after.get(k) for k in ('accountFingerprint', 'limitId', 'windowDurationMins')):
        return None
    if after['usedPercent'] < before['usedPercent']:
        return 'remaining_percentage_increased'
    stamp = time.time() if timestamp is None else timestamp
    if before['resetsAt'] <= stamp and after['resetsAt'] > before['resetsAt']:
        return 'scheduled_window_rolled_over'
    return None


def start_alarm():
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve().parent / 'notify.py')], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    ).pid


def next_threshold(remaining, alerts):
    if remaining <= 5 and '5' not in alerts:
        return 5
    if remaining <= 10 and '10' not in alerts and '5' not in alerts:
        return 10
    return None


def check_thresholds(state, snapshot):
    alerts = state.setdefault('thresholdAlerts', {})
    level = next_threshold(snapshot['remainingPercent'], alerts)
    if level is None:
        return
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve().parent / 'notify.py'), '--kind', 'low' + str(level),
         '--remaining', str(snapshot['remainingPercent'])], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    alerts[str(level)] = {
        'triggeredAt': now(), 'checkedAt': snapshot['checkedAt'],
        'remainingPercent': snapshot['remainingPercent'], 'alarmPid': process.pid,
    }
    if level == 5 and '10' not in alerts:
        alerts['10'] = {
            'triggeredAt': alerts['5']['triggeredAt'],
            'remainingPercent': snapshot['remainingPercent'], 'supersededBy': 5,
        }
    write_json(STATE, state)
    event('low_quota_threshold', threshold=level,
          remainingPercent=snapshot['remainingPercent'], alarmPid=process.pid)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    (ROOT / 'logs').mkdir(exist_ok=True)
    os.umask(0o077)
    lock = (ROOT / 'monitor.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    state = read_state()
    if (ROOT / 'completion.json').exists() or state.get('status') in ('reset_confirmed', 'completed'):
        return
    client = None
    while True:
        if (ROOT / 'completion.json').exists():
            if client is not None:
                client.close()
            return
        started = time.monotonic()
        try:
            if client is None:
                client = ReadOnlyClient()
                client.start()
            current = client.snapshot()
            expected = state.get('accountFingerprint')
            if expected and current['accountFingerprint'] != expected:
                raise RuntimeError('Account changed; preserving original baseline')
            record_snapshot(current)
            previous = state.get('lastSuccessfulSnapshot')
            reason = reset_reason(previous, current)
            if reason:
                time.sleep(3)
                confirmation = client.snapshot()
                if confirmation['accountFingerprint'] != current['accountFingerprint']:
                    raise RuntimeError('Account changed during reset confirmation')
                record_snapshot(confirmation)
                if reset_reason(previous, confirmation):
                    state.update(status='reset_confirmed', resetDetectedAt=now(),
                                 resetReason=reason, beforeReset=previous,
                                 confirmedSnapshots=[current, confirmation],
                                 lastSuccessfulSnapshot=confirmation, lastSuccessAt=now(),
                                 lastAttemptAt=now(), consecutiveErrors=0, lastError=None)
                    write_json(STATE, state)
                    state['alarmPid'] = start_alarm()
                    write_json(STATE, state)
                    event('reset_confirmed', reason=reason,
                          beforeUsed=previous['usedPercent'], afterUsed=confirmation['usedPercent'])
                    client.close()
                    return
                current = confirmation
                event('candidate_not_confirmed')
            check_thresholds(state, current)
            try:
                update_forecast(current)
            except Exception as error:
                # Forecasting must never prevent quota alarms or reset handling.
                event('forecast_failed', error=type(error).__name__)
            if not state.get('baseline'):
                state['baseline'] = current
                state['accountFingerprint'] = current['accountFingerprint']
                state['armedAt'] = now()
                event('armed', usedPercent=current['usedPercent'])
            if state.get('consecutiveErrors', 0):
                event('read_recovered')
            state.update(status='watching', lastSuccessfulSnapshot=current,
                         lastSuccessAt=now(), lastAttemptAt=now(),
                         consecutiveErrors=0, lastError=None, monitorPid=os.getpid(),
                         pollIntervalSeconds=INTERVAL)
            write_json(STATE, state)
        except Exception as error:
            if client is not None:
                client.close()
                client = None
            count = state.get('consecutiveErrors', 0) + 1
            state.update(status='degraded', lastAttemptAt=now(),
                         consecutiveErrors=count, lastError=type(error).__name__ + ': ' + str(error))
            write_json(STATE, state)
            if count in (1, 3):
                event('read_failed', consecutiveErrors=count, error=state['lastError'])
        if args.once:
            if client is not None:
                client.close()
            return
        time.sleep(max(1, INTERVAL - (time.monotonic() - started)))


if __name__ == '__main__':
    main()
