"""Apply platform schedules using Beijing time, independent of the Mac timezone."""
import argparse
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, time as wall_time, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import traceback

from agent_signin import PLATFORMS, _run_one, supported_platforms
from notify import send as send_notification, channel as notify_channel
from platform_paths import log_dir, state_dir

QODER_PLATFORMS = ("qoder-cn", "qoder-global")
DAILY_PLATFORMS = tuple(name for name in PLATFORMS if name not in QODER_PLATFORMS)
REWARD_TZ = timezone(timedelta(hours=8))
RETRY_SECONDS = 3600


def cycle_at(now):
    local = now.astimezone(REWARD_TZ)
    if wall_time(10) <= local.time() < wall_time(10, 5):
        return None
    return (local - timedelta(hours=10)).date().isoformat()


def _save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(state, handle)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def daily_cycle_at(now, hour, minute):
    local = now.astimezone(REWARD_TZ)
    return local.date().isoformat() if local.time() >= wall_time(hour, minute) else None


def run(group, now=None, path=None, runner=_run_one, hour=9, minute=30):
    now = now if now is not None else datetime.now(timezone.utc)
    is_qoder = group == "qoder"
    cycle = cycle_at(now) if is_qoder else daily_cycle_at(now, hour, minute)
    if cycle is None:
        return 0
    available = set(supported_platforms())
    platforms = tuple(platform for platform in (QODER_PLATFORMS if is_qoder else DAILY_PLATFORMS)
                      if platform in available)
    if not platforms:
        return 0
    path = path if path is not None else state_dir() / (
        "qoder-daily.json" if is_qoder else "daily-run.json"
    )
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (FileNotFoundError, ValueError):
        state = {}
    timestamp = int(now.timestamp())
    exit_code = 0
    for platform in platforms:
        last = state.get(platform, {})
        if isinstance(last, dict) and last.get("cycle") == cycle:
            attempted = last.get("attempted_at")
            if (not is_qoder or last.get("done")
                    or type(attempted) is int and timestamp - attempted < RETRY_SECONDS):
                continue
        code, result = runner(platform, "run")
        done = not code and result["result"] in {"CLAIMED", "ALREADY"}
        state[platform] = {"cycle": cycle, "done": done, "attempted_at": timestamp}
        _save(path, state)
        print(json.dumps({"cycle_utc8": cycle, **result}, ensure_ascii=False), flush=True)
        exit_code = max(exit_code, code)
    return exit_code


class _Collector:
    """写日志的同时收下 JSON 行，供推送汇总使用。"""

    def __init__(self, handle, sink):
        self._handle = handle
        self._sink = sink

    def write(self, text):
        self._handle.write(text)
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if isinstance(payload, dict) and "platform" in payload and "result" in payload:
                self._sink.append(payload)
        return len(text)

    def flush(self):
        self._handle.flush()


def summary_title(results):
    cycle = next((item.get("cycle_utc8") for item in results if item.get("cycle_utc8")), "")
    return "签到结果 {0}".format(cycle) if cycle else "签到结果"


STATUS_TEXT = {
    "CLAIMED": "已领取",
    "ALREADY": "今日已签",
    "DRY_RUN": "今日可领",
    "NO_AUTH": "未登录",
    "NOT_INSTALLED": "未安装",
    "BROWSER": "浏览器不可用",
    "VISITED": "已访问工作区",
    "ACTIVITY_REQUIRED": "需当天发起对话",
    "ERROR": "运行失败",
}


def summary_text(results, now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(REWARD_TZ)
    lines = ["北京时间 {0:%Y-%m-%d %H:%M}".format(now)]
    for item in results:
        flag = "待处理" if item.get("needs_attention") else "完成"
        report = str(item.get("report") or "").strip()
        status = str(item.get("result") or "")
        lines.append(
            "【{0}】{1}：{2}（{3}）".format(
                flag, item.get("platform"), STATUS_TEXT.get(status, status), report
            )
        )
    return chr(10).join(lines)


def _notify_once(logs, results):
    """每个周期只推一条汇总；推送失败只写日志，不影响签到退出码。"""
    if not results or not notify_channel():
        return
    try:
        ok, detail = send_notification(summary_title(results), summary_text(results))
    except Exception:
        with (logs / "notify.err").open("a", encoding="utf-8") as errors:
            traceback.print_exc(file=errors)
        return
    if not ok:
        with (logs / "notify.err").open("a", encoding="utf-8") as errors:
            errors.write("push failed: {0}".format(detail) + chr(10))


def main(argv=None):
    parser = argparse.ArgumentParser(description="按北京时间执行本机每日任务")
    parser.add_argument("group", choices=("daily", "qoder", "all"))
    parser.add_argument("--write-logs", action="store_true",
                        help="追加 JSONL 到当前用户的系统日志目录")
    parser.add_argument("--hour", type=int, choices=range(24), default=9)
    parser.add_argument("--minute", type=int, choices=range(60), default=30)
    args = parser.parse_args(argv)
    groups = ("qoder", "daily") if args.group == "all" else (args.group,)
    if not args.write_logs:
        return max(run(group, hour=args.hour, minute=args.minute) for group in groups)

    logs = log_dir()
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    codes = []
    results = []
    for group in groups:
        with (logs / f"{group}.jsonl").open("a", encoding="utf-8") as output, \
             (logs / f"{group}.err").open("a", encoding="utf-8") as errors, \
             redirect_stdout(_Collector(output, results)), redirect_stderr(errors):
            try:
                codes.append(run(group, hour=args.hour, minute=args.minute))
            except Exception:
                traceback.print_exc(file=errors)
                codes.append(2)
    _notify_once(logs, results)
    return max(codes)


if __name__ == "__main__":
    raise SystemExit(main())
