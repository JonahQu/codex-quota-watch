#!/usr/bin/python3
"""Persistent local alarm, acknowledged by a native Mac dialog button."""
import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
import threading
import time

ROOT = Path(os.environ.get('QUOTA_WATCH_ROOT', str(Path(__file__).resolve().parent / 'runtime'))).resolve()


def alert_content(kind, remaining=None, test=False):
    if test:
        return ('【测试】Codex 额度提醒',
                '这是声音与弹窗测试，没有触发真实额度提醒，也不会暂停任务。测试会自动关闭。',
                '关闭测试')
    if kind in ('low10', 'low5'):
        percent = format(remaining if remaining is not None else (5 if kind == 'low5' else 10), 'g')
        if kind == 'low10':
            return ('Codex 额度提醒：剩余 ' + percent + '%',
                    '账户共享额度已降至 ' + percent + '%。请留意后续消耗。', '知道了')
        return ('Codex 额度紧急提醒：剩余 ' + percent + '%',
                '账户共享额度已降至 ' + percent + '%。请及时检查正在运行的任务。'
                '\n\n点击“停止提醒”关闭持续声音。', '停止提醒')
    return ('Codex 额度已恢复 / 重置',
            '已连续两次读取确认额度恢复。请按需检查正在运行的任务。'
            '\n\n点击“停止提醒”仅关闭声音。', '停止提醒')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--test', action='store_true')
    parser.add_argument('--timeout', type=int, default=0)
    parser.add_argument('--kind', choices=('reset', 'low10', 'low5'), default='reset')
    parser.add_argument('--remaining', type=float)
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    (ROOT / 'logs').mkdir(exist_ok=True)
    os.umask(0o077)
    stem = '' if args.kind == 'reset' else args.kind + '-'
    lock = (ROOT / ('test-' + stem + 'alarm.lock' if args.test else stem + 'alarm.lock')).open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    acknowledgement = ROOT / (stem + 'alarm-ack.json')
    if not args.test and acknowledgement.exists():
        return
    stopped = threading.Event()
    plays = []

    def sound():
        while not stopped.is_set():
            try:
                sound_name = 'Sosumi' if args.kind == 'low5' else 'Glass'
                result = subprocess.run(['/usr/bin/afplay', '/System/Library/Sounds/' + sound_name + '.aiff'],
                                        timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                plays.append(result.returncode)
            except (OSError, subprocess.TimeoutExpired):
                plays.append(-1)
            if args.kind == 'low10':
                return
            stopped.wait(2)

    title, message, button = alert_content(args.kind, args.remaining, args.test)
    script = 'activate\ndisplay alert ' + json.dumps(title, ensure_ascii=False)
    script += ' message ' + json.dumps(message, ensure_ascii=False)
    script += ' as informational' if args.kind == 'low10' else ' as critical'
    script += ' buttons {' + json.dumps(button, ensure_ascii=False) + '} default button 1'
    timeout = args.timeout or (20 if args.kind == 'low10' else 0)
    if timeout:
        script += ' giving up after ' + str(timeout)
    thread = threading.Thread(target=sound, daemon=True)
    thread.start()
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    result = None
    try:
        result = subprocess.run(['/usr/bin/osascript', '-e', script], capture_output=True, text=True)
    finally:
        stopped.set()
        thread.join(timeout=6)
    details = {
        'startedAt': started, 'endedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'test': args.test, 'kind': args.kind, 'remainingPercent': args.remaining,
        'dialogExitCode': result.returncode if result else None,
        'dialogResponse': result.stdout.strip() if result else '',
        'soundExitCodes': plays, 'pid': os.getpid(),
    }
    output = ROOT / (stem + 'alarm-test-result.json' if args.test else
                     (stem + 'alarm-ack.json' if result and result.returncode == 0 else stem + 'alarm-error.json'))
    output.write_text(json.dumps(details, ensure_ascii=False, indent=2) + '\n')
    os.chmod(output, 0o600)


if __name__ == '__main__':
    main()
