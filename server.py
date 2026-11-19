"""agent-auto-signin HTTP API 层。

把 CLI 能力包装成 REST API，供前端 index.html 调用。
启动: python server.py  (默认 http://127.0.0.1:8765)
"""
import json
import os
import sys
import signal
import threading
import subprocess
from pathlib import Path
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder=None)

# status 接口缓存（签到状态一天内不变，没必要每次都跑 9 个 Playwright）
_status_cache = {"date": None, "platforms": None}

# 多线程串行化锁（Playwright + redirect_stdout 在多线程下不是线程安全的）
_invoke_lock = threading.Lock()

# daily.jsonl 写入锁（避免多个签到同时写文件冲突）
_log_lock = threading.Lock()

# Windows 下允许前端跨域（前端是独立打开的 HTML 文件）
# Flask 3.x 内置了简单 CORS
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp

app.after_request(_cors)

# 导入项目模块
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from agent_signin import (  # noqa: E402
    PLATFORMS,
    supported_platforms,
    _run_one,
)
from platform_paths import state_dir, log_dir  # noqa: E402

# 前端 AGENTS id → 后端平台 id 的映射
FRONTEND_TO_BACKEND = {
    "workbuddy":       "workbuddy",
    "doubao":          "doubao",
    "trae":            "trae-cn",
    "dazi":            "dumate",
    "qianwen-office":  "qwenwork",
    "qoder-cn":        "qoder-cn",
    "miaoda":          "miaoda",
    "kuku":            "kuku",
    # 以下后端尚未适配
    "qianwen":         None,  # 千问网页版
    "yuanbao":         None,  # 元宝
}

# 反向映射
BACKEND_TO_FRONTEND = {v: k for k, v in FRONTEND_TO_BACKEND.items() if v}

# 平台中文名
PLATFORM_NAMES = {
    "workbuddy": "WorkBuddy", "doubao": "豆包", "trae-cn": "Trae",
    "dumate": "百度搭子", "qwenwork": "千问办公", "qoder-cn": "Qoder 国内",
    "qoder-global": "Qoder 国际", "workbuddy-global": "WorkBuddy 国际",
}


def _today_str():
    """今天的 YYYY-MM-DD（北京时间）。"""
    from datetime import timezone
    bj = timezone(timedelta(hours=8))
    return datetime.now(bj).strftime("%Y-%m-%d")


# ---------- 接口 ----------

@app.route("/api/platforms")
def api_platforms():
    """返回支持的平台列表 + 前后端 id 映射。"""
    win_platforms = supported_platforms("win32")
    out = []
    for fe_id, be_id in FRONTEND_TO_BACKEND.items():
        out.append({
            "frontend_id": fe_id,
            "backend_id": be_id,
            "name": be_id and PLATFORM_NAMES.get(be_id, be_id) or fe_id,
            "supported": be_id is not None and be_id in win_platforms,
        })
    return jsonify({"platforms": out, "backend_list": list(win_platforms)})


@app.route("/api/status")
def api_status():
    """返回今日所有平台状态（带缓存，一天内只跑一次 Playwright）。

    ?force=1 强制刷新（跑了签到或引导登录后自动走这个）。
    """
    global _status_cache

    today = _today_str()
    force = request.args.get("force") == "1"

    # 命中缓存：日期没变 + 没强制刷新
    if not force and _status_cache["date"] == today and _status_cache["platforms"]:
        return jsonify({"date": today, "platforms": _status_cache["platforms"], "cached": True})

    # 缓存未命中 → 多线程并发跑
    win_platforms = supported_platforms("win32")

    def _check(platform):
        # 加锁：Playwright + redirect_stdout 多线程下不是线程安全的
        with _invoke_lock:
            try:
                code, result = _run_one(platform, "status")
            except Exception as exc:
                result = {"platform": platform, "result": "ERROR",
                          "report": f"异常 {type(exc).__name__}"}
                code = 1
        fe_id = BACKEND_TO_FRONTEND.get(platform)

        # 官方状态同步回 daily.jsonl：检测到已签到但日志没有时补写
        _maybe_sync_status_to_log(platform, result, today)

        return {
            "frontend_id": fe_id,
            "backend_id": platform,
            "name": PLATFORM_NAMES.get(platform, platform),
            "status": result.get("result"),
            "report": result.get("report", ""),
            "code": code,
        }

    targets = [p for p in PLATFORMS if p in win_platforms]
    results = []
    global _global_executor
    _global_executor = None
    try:
        _global_executor = ThreadPoolExecutor(max_workers=min(len(targets), 8))
        future_map = {_global_executor.submit(_check, p): p for p in targets}
        for future in as_completed(future_map):
            results.append(future.result())
    except Exception:
        pass  # 单个平台失败不影响其他
    finally:
        if _global_executor is not None:
            _global_executor.shutdown(wait=True)
            _global_executor = None

    order = {p: i for i, p in enumerate(PLATFORMS)}
    results.sort(key=lambda r: order.get(r["backend_id"], 999))

    # 写入缓存
    _status_cache = {"date": today, "platforms": results}
    return jsonify({"date": today, "platforms": results, "cached": False})


def _invalidate_status_cache():
    """签到/引导登录成功后调用，让下一次 status 请求强制刷新。"""
    global _status_cache
    _status_cache = {"date": None, "platforms": None}


def _append_daily_log(platform, result_dict):
    """把签到结果追写到 daily.jsonl（与 scheduled_run.py 格式一致）。

    手动在前端签到 /api/run 时也要写日志，不然 history 看不到。
    """
    bj = timezone(timedelta(hours=8))
    cycle = datetime.now(bj).strftime("%Y-%m-%d")
    line = json.dumps({
        "cycle_utc8": cycle,
        "platform": platform,
        "result": result_dict.get("result", ""),
        "report": result_dict.get("report", ""),
        "needs_attention": result_dict.get("needs_attention", False),
    }, ensure_ascii=False)

    try:
        from platform_paths import log_dir as _log_dir
        daily_log = _log_dir() / "daily.jsonl"
        daily_log.parent.mkdir(parents=True, exist_ok=True)
        with _log_lock:
            with daily_log.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass  # 日志写失败不影响签到主流程


def _maybe_sync_status_to_log(platform, result_dict, cycle):
    """status 检测到已签到但 daily.jsonl 没有这条记录时，自动补写。

    这让 history 反映真实状态（官方 API 返回），而不是只记录"我跑过"的动作。
    不管你在哪签到（本地 Flask、手机 APP、官方网页），下次 status 都会同步进来。
    """
    status = result_dict.get("result", "")
    if status not in ("CLAIMED", "ALREADY", "VISITED"):
        return  # 未签到 / 失败的不用写

    try:
        from platform_paths import log_dir as _log_dir
        daily_log = _log_dir() / "daily.jsonl"
        if not daily_log.is_file():
            # 日志文件不存在，直接写
            _append_daily_log(platform, result_dict)
            return

        # 扫描今天有没有这个平台的记录，避免重复
        with _log_lock:
            with daily_log.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if (obj.get("cycle_utc8") == cycle
                            and obj.get("platform") == platform):
                        return  # 已有，不重复

        # 补写（去重确认过了，安全）
        _append_daily_log(platform, result_dict)
    except Exception:
        pass  # 同步失败不影响 status 返回值


@app.route("/api/run", methods=["GET", "POST"])
def api_run():
    """触发签到。GET 参数或 POST body: ?platform=xxx 或 {"platform": "all"}。"""
    data = request.get_json(silent=True) or {}
    platform = (request.args.get("platform") or data.get("platform") or "all").strip()

    win_platforms = supported_platforms("win32")

    if platform in ("", "all"):
        targets = list(win_platforms)
    elif platform in win_platforms:
        targets = [platform]
    else:
        mapped = FRONTEND_TO_BACKEND.get(platform)
        if mapped and mapped in win_platforms:
            targets = [mapped]
        else:
            return jsonify({"error": f"未知平台: {platform}"}), 400

    results = []
    for p in targets:
        try:
            code, result = _run_one(p, "run")
        except Exception as exc:
            result = {"platform": p, "result": "ERROR",
                      "report": f"异常 {type(exc).__name__}"}
            code = 1
        fe_id = BACKEND_TO_FRONTEND.get(p)
        results.append({
            "frontend_id": fe_id,
            "backend_id": p,
            "name": PLATFORM_NAMES.get(p, p),
            "status": result.get("result"),
            "report": result.get("report", ""),
            "code": code,
        })
        # 写 daily.jsonl，让 history API 能看到手动签到的记录
        _append_daily_log(p, result)

    # 签到完清缓存，下次 status 自动刷新
    _invalidate_status_cache()
    return jsonify({"date": _today_str(), "platforms": results})


@app.route("/api/login", methods=["GET", "POST"])
def api_login():
    """两段式引导登录（秒哒/库库AI）。

    step=open     → webbrowser.open_new_tab() 打开系统浏览器新标签页
    step=confirm  → 用户自行关闭浏览器后，复制 Cookie + 验证

    请求: ?platform=miaoda&step=open 或 ?platform=miaoda&step=confirm
    """
    data = request.get_json(silent=True) or {}
    platform = (request.args.get("platform") or data.get("platform") or "").strip()
    step = (request.args.get("step") or data.get("step") or "open").strip()

    if not platform:
        return jsonify({"error": "缺少 platform 参数"}), 400

    web_login_platforms = {
        "miaoda": ("miaoda_signin", "秒哒", "miaoda.cn"),
        "kuku":   ("kuku_signin", "库库AI", "kukuai.cn"),
    }

    if platform not in web_login_platforms:
        return jsonify({
            "error": f"平台 {platform} 不支持引导登录，"
                     f"支持: {list(web_login_platforms.keys())}"
        }), 400

    module_name, display_name, site_url = web_login_platforms[platform]

    try:
        mod = __import__(module_name)

        if step == "open":
            ok = mod.login_interactive(step="open")
            return jsonify({
                "result": "OK",
                "report": f"已在系统浏览器打开 {display_name} 登录页",
                "next_step": "在浏览器中登录 → 关闭所有 Edge/Chrome 窗口 → 点「我已完成登录」",
                "site_url": site_url,
            })

        if step == "confirm":
            ok = mod.login_interactive(step="confirm")
            if ok:
                _invalidate_status_cache()  # 登录成功清缓存
                try:
                    code, result = mod.run("run")
                    return jsonify({
                        "result": "OK",
                        "report": f"{display_name} 登录成功",
                        "after_login_result": result,
                        "code": code,
                    })
                except Exception as exc:
                    return jsonify({
                        "result": "OK",
                        "report": f"{display_name} 登录成功",
                        "code": 0,
                    })
            else:
                return jsonify({
                    "result": "FAILED",
                    "report": "登录态验证失败，请确认已关闭所有浏览器窗口",
                    "code": 2,
                }), 400

        return jsonify({"error": f"未知 step: {step}"}), 400

    except ImportError as exc:
        return jsonify({"error": f"模块导入失败: {exc}"}), 500
    except Exception as exc:
        return jsonify({"error": f"登录异常: {exc}"}), 500


def _read_log_history(days):
    """从 scheduled_run 日志 JSONL 里解析历史签到记录。

    返回: { "YYYY-MM-DD": ["doubao", "trae-cn", ...], ... }
    """
    log = log_dir()
    daily_log = log / "daily.jsonl"
    history = {}
    try:
        with open(daily_log, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                cycle = obj.get("cycle_utc8")
                platform = obj.get("platform")
                result = obj.get("result")
                if (not cycle or not platform
                        or result not in ("CLAIMED", "ALREADY")):
                    continue
                fe_id = BACKEND_TO_FRONTEND.get(platform, platform)
                # 只收集有前端映射的平台
                if fe_id and fe_id in FRONTEND_TO_BACKEND:
                    history.setdefault(cycle, [])
                    if fe_id not in history[cycle]:
                        history[cycle].append(fe_id)
    except FileNotFoundError:
        pass
    return history


@app.route("/api/history")
def api_history():
    """返回最近 N 天的历史签到记录。"""
    try:
        days = int(request.args.get("days", 60))
    except ValueError:
        days = 60
    days = max(1, min(days, 365))

    history = _read_log_history(days)

    # 构造前端需要的格式: { days: { "2026-11-18": ["workbuddy", ...], ... } }
    out = {}
    from datetime import timezone
    bj = timezone(timedelta(hours=8))
    today = datetime.now(bj).date()
    for i in range(days):
        d = (today - timedelta(days=i)).isoformat()
        out[d] = history.get(d, [])
    return jsonify({"days": out, "backend_log_found": bool(history)})


@app.route("/api/history-month")
def api_history_month():
    """返回指定月份的签到历史 + 月度统计。
    参数: month=2026-11 (默认本月)
    """
    from datetime import timezone
    bj = timezone(timedelta(hours=8))
    try:
        month = request.args.get("month", "")
        if month:
            year, mon = month.split("-")[:2]
            year, mon = int(year), int(mon)
        else:
            now = datetime.now(bj)
            year, mon = now.year, now.month
    except (ValueError, AttributeError):
        now = datetime.now(bj)
        year, mon = now.year, now.month

    month_prefix = f"{year:04d}-{mon:02d}"

    # 读 daily.jsonl，过滤本月
    daily_log = log_dir() / "daily.jsonl"
    by_day = {}        # {"2026-11-19": [ {platform, result, report}, ... ]}
    platform_ok = {}   # {"workbuddy": 3}
    platform_bad = {}  # {"kuku": 3}
    all_results = []   # 统计用

    try:
        with open(daily_log, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                cycle = obj.get("cycle_utc8", "")
                if not cycle.startswith(month_prefix):
                    continue
                be_id = obj.get("platform", "")
                result = obj.get("result", "")
                report = obj.get("report", "")
                fe_id = BACKEND_TO_FRONTEND.get(be_id, be_id)

                by_day.setdefault(cycle, []).append({
                    "platform": fe_id,
                    "backend_id": be_id,
                    "result": result,
                    "report": report[:120],
                })

                ok_res = ("CLAIMED", "ALREADY", "VISITED")
                if result in ok_res:
                    platform_ok[fe_id] = platform_ok.get(fe_id, 0) + 1
                else:
                    platform_bad[fe_id] = platform_bad.get(fe_id, 0) + 1
                all_results.append(result)
    except FileNotFoundError:
        pass

    # 按日期排序
    sorted_days = {d: sorted(entries, key=lambda x: x["platform"])
                   for d, entries in sorted(by_day.items())}

    # 月度汇总
    total_entries = sum(len(v) for v in sorted_days.values())
    ok_count = sum(platform_ok.values())
    bad_count = sum(platform_bad.values())
    unique_days = len(sorted_days)

    return jsonify({
        "month": month_prefix,
        "days_in_month": unique_days,
        "total_records": total_entries,
        "ok_count": ok_count,
        "bad_count": bad_count,
        "success_rate": round(ok_count / total_entries * 100, 1) if total_entries else 0,
        "by_day": sorted_days,
        "platforms": {
            fe_id: {"ok": platform_ok.get(fe_id, 0), "bad": platform_bad.get(fe_id, 0)}
            for fe_id in sorted(set(list(platform_ok.keys()) + list(platform_bad.keys())))
        },
    })


@app.route("/api/sync-history", methods=["POST", "GET"])
def api_sync_history():
    """强制扫描全平台今日签到状态，同步补写到 daily.jsonl。

    场景：用户在官方 APP / 网页签了到，但 Flask status 缓存还没刷新，
    手动点一下"同步"就能把官方真实状态同步进历史记录。
    """
    _invalidate_status_cache()

    bj = timezone(timedelta(hours=8))
    cycle = datetime.now(bj).strftime("%Y-%m-%d")
    win_platforms = supported_platforms("win32")

    synced = []
    skipped = []
    errors = []

    for platform in win_platforms:
        with _invoke_lock:
            try:
                code, result = _run_one(platform, "status")
            except Exception as exc:
                errors.append({"platform": platform, "error": str(exc)})
                continue

        status = result.get("result", "")
        fe_id = BACKEND_TO_FRONTEND.get(platform)

        if status in ("CLAIMED", "ALREADY", "VISITED"):
            # 检查 daily.jsonl 今天有没有，没有才补写
            from platform_paths import log_dir as _log_dir
            daily_log = _log_dir() / "daily.jsonl"
            already = False
            if daily_log.is_file():
                with _log_lock:
                    with daily_log.open("r", encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                obj = json.loads(line)
                            except ValueError:
                                continue
                            if (obj.get("cycle_utc8") == cycle
                                    and obj.get("platform") == platform):
                                already = True
                                break

            if already:
                skipped.append({"platform": fe_id or platform, "reason": "已有记录"})
            else:
                _append_daily_log(platform, result)
                synced.append({"platform": fe_id or platform,
                               "status": status,
                               "report": result.get("report", "")})
        else:
            skipped.append({"platform": fe_id or platform,
                            "reason": f"status={status}"})

    return jsonify({"date": cycle, "synced": synced,
                    "skipped": skipped, "errors": errors,
                    "synced_count": len(synced)})


# ---------- 前端静态文件 ----------

# 前端 index.html 位置（独立于本项目，可通过环境变量覆盖）
WEB_ROOT = Path(os.environ.get("WEB_ROOT")
                or (Path.home() / "Desktop" / "独立开发" / "一键签到"))

@app.route("/")
def serve_index():
    return send_from_directory(str(WEB_ROOT), "index.html")

@app.route("/<path:path>")
def serve_static(path):
    target = WEB_ROOT / path
    if target.is_file():
        return send_from_directory(str(WEB_ROOT), path)
    return jsonify({"error": "not found"}), 404


# 全局 executor 引用 + 优雅退出（解决 Ctrl+C 时 ThreadPoolExecutor 残留报错）
_global_executor = None


def _shutdown_executor():
    global _global_executor
    if _global_executor:
        try:
            _global_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        _global_executor = None


def _register_signal_handlers():
    def _handler(sig, frame):
        _shutdown_executor()
        print("\n👋 server.py 已退出。下次要重启：")
        print("   cd C:\\Users\\qinzh\\agent-auto-signin")
        print("   .\\.venv\\Scripts\\python.exe server.py")
        sys.exit(0)
    try:
        signal.signal(signal.SIGINT, _handler)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, _handler)
    except ValueError:
        pass


if __name__ == "__main__":
    _register_signal_handlers()
    port = int(os.environ.get("PORT", "8765"))
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"\n🔗 agent-auto-signin API 启动: http://{host}:{port}")
    print(f"   前端页面: http://{host}:{port}/")
    print(f"   今日状态: http://{host}:{port}/api/status")
    print(f"   执行签到: http://{host}:{port}/api/run?platform=all")
    print(f"   历史记录: http://{host}:{port}/api/history?days=60\n")
    try:
        app.run(host=host, port=port, debug=False)
    except KeyboardInterrupt:
        print("\n👋 server.py 已退出。下次要重启：")
        print("   cd C:\\Users\\qinzh\\agent-auto-signin")
        print("   .\\.venv\\Scripts\\python.exe server.py")
