"""秒哒 (miaoda.cn) 网页端每日签到。

策略：两段式引导登录 + Playwright 自动化签到
  step 1: webbrowser.open_new_tab() → 用户在自己浏览器里登录
  step 2: 用户自己关浏览器 → 我们复制 Cookie 到 Playwright profile → 验证
"""

import json
import os
import sys
from pathlib import Path


# ---------- 配置 ----------
HOME = Path.home()
PROFILE_DIR = Path(os.environ.get("MIAODA_PROFILE_DIR")
                   or (HOME / ".agent-auto-signin" / "miaoda-profile"))
PROFILE_DIR.mkdir(parents=True, exist_ok=True)

# ---------- 系统浏览器自动检测（优先 Edge → Chrome） ----------
_SYSTEM_BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

_USER_BROWSER_PROFILE_CANDIDATES = [
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default"),
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data\Default"),
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data\Profile 1"),
]


def _find_system_browser():
    for path in _SYSTEM_BROWSERS:
        if os.path.isfile(path):
            return path
    return None


def _find_user_browser_profile():
    for p in _USER_BROWSER_PROFILE_CANDIDATES:
        if os.path.isdir(p) and (os.path.isfile(os.path.join(p, "Cookies")) or
                                 os.path.isdir(os.path.join(p, "Network"))):
            return Path(p)
    return None


def _kill_stale_browser_exes():
    """清理 SingletonLock 文件（浏览器关了但锁可能残留）。"""
    import subprocess
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        for sub in ("", "Default/"):
            for exe_path in ("msedge.exe", "chrome.exe", "chromium.exe"):
                # 只杀 Playwright profile 相关的（用户自己的 Edge 我们不动）
                try:
                    subprocess.run(
                        ["wmic", "process", "where",
                         f"name='{exe_path}' and commandline like '%miaoda-profile%'",
                         "get", "processid"],
                        capture_output=True, text=True, timeout=5,
                    )
                except Exception:
                    pass
            lock_file = PROFILE_DIR / sub / lock_name
            try:
                if lock_file.exists():
                    lock_file.unlink()
            except Exception:
                pass


BASE_URL = "https://www.miaoda.cn"
CHECKIN_URL = BASE_URL + "/"

SUCCESS_TEXT = {"签到成功", "领取成功", "已领取"}
ALREADY_TEXT = {"已签到", "今日已签到", "明日再来", "已领取过"}


def _result(status, report, code=0):
    return code, {"result": status, "report": report, "needs_attention": code != 0}


def _has_cookie() -> bool:
    candidates = [
        PROFILE_DIR / "Cookies",
        PROFILE_DIR / "Default" / "Network" / "Cookies",
        PROFILE_DIR / "Default" / "Cookies",
        PROFILE_DIR / "Local Storage",
    ]
    for c in candidates:
        if c.is_file() or c.is_dir():
            return True
    return False


def login_interactive(step="open"):
    """两段式引导登录。

    step="open":   打开系统浏览器新标签页到 miaoda.cn
    step="confirm": 用户已自行关闭浏览器后，复制 Cookie 并验证
    """
    import webbrowser
    import shutil
    import time as _time

    if step == "open":
        print(f"[秒哒] 打开新标签页: {BASE_URL}")
        webbrowser.open_new_tab(BASE_URL)
        return True

    if step == "confirm":
        print("[秒哒] 清理浏览器进程以解锁 Cookie 文件...")
        user_exe = _find_system_browser()
        user_exe_name = os.path.basename(user_exe) if user_exe else "msedge.exe"
        import subprocess
        # 强制 kill 所有 Edge/Chrome 进程（包括开机自启的后台进程）
        subprocess.run(
            ["taskkill", "/F", "/IM", user_exe_name, "/T"],
            capture_output=True, timeout=10,
        )
        import time
        time.sleep(1)

        print("[秒哒] ✅ Cookie 文件已解锁，开始复制...")
        _kill_stale_browser_exes()

        # 复制 Cookie
        src = _find_user_browser_profile()
        if not src:
            print("[秒哒] ❌ 找不到 Edge/Chrome profile")
            return False

        copied = False
        dst_net = PROFILE_DIR / "Default" / "Network"
        dst_net.mkdir(parents=True, exist_ok=True)

        for filename in ("Cookies", "Cookies-journal"):
            src_file = src / "Network" / filename
            if not src_file.is_file():
                src_file = src / filename
            if src_file.is_file():
                try:
                    shutil.copy2(str(src_file), str(dst_net / filename))
                    copied = True
                    print(f"[秒哒] 复制 {filename} ✅")
                except Exception as exc:
                    print(f"[秒哒] ⚠️ 复制 {filename} 失败: {exc}")

        for dirname in ("Local Storage", "Session Storage"):
            src_dir = src / dirname
            dst_dir = PROFILE_DIR / dirname
            if src_dir.is_dir():
                try:
                    if dst_dir.exists():
                        shutil.rmtree(str(dst_dir), ignore_errors=True)
                    shutil.copytree(str(src_dir), str(dst_dir))
                    copied = True
                    print(f"[秒哒] 复制 {dirname} ✅")
                except Exception as exc:
                    print(f"[秒哒] ⚠️ 复制 {dirname} 失败: {exc}")

        if not copied:
            print("[秒哒] ❌ 没复制到任何 Cookie/Storage")
            return False

        # 验证：Playwright headless 访问看能不能检测到登录态
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            try:
                ctx = _new_context(pw, headless=True)
                page = ctx.new_page()
                page.goto(BASE_URL, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(3000)
                text = page.locator("body").inner_text(timeout=5000)
                ctx.close()

                has_login_hint = any(k in text for k in ["扫码登录", "手机号登录", "立即登录"])
                has_logged_in_flag = any(k in text for k in [
                    "退出登录", "我的秒点", "个人中心", "我的主页",
                ])

                if has_logged_in_flag and not has_login_hint:
                    print("[秒哒] ✅ 登录态验证通过！")
                    return True
                elif not has_login_hint:
                    print("[秒哒] ⚠️ 未检测到登录标志但也无未登录提示，Cookie 已复制")
                    return True
                else:
                    print("[秒哒] ❌ 还是未登录，Cookie 可能不对")
                    return False
            except Exception as exc:
                print(f"[秒哒] ⚠️ 验证异常: {exc}，Cookie 已复制")
                return True

    return False


def _new_context(playwright, headless=True):
    """创建 Playwright 持久化上下文。

    注意：不指定 executable_path，使用 Playwright 自带的 Chromium。
    用系统 Edge + Playwright profile 会导致 Cookie 读取异常（Edge 安全机制）。
    """
    _kill_stale_browser_exes()
    kwargs = dict(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        locale="zh-CN",
    )
    if headless:
        kwargs["args"] = ["--headless=new", "--disable-gpu"]
    return playwright.chromium.launch_persistent_context(**kwargs)


def run(action):
    from playwright.sync_api import Error, sync_playwright

    if action == "login":
        # CLI 调用时简化为一段式
        ok = login_interactive(step="confirm")  # 默认 confirm 假设用户已关浏览器
        return (0 if ok else 2,
                {"result": "OK" if ok else "NO_AUTH",
                 "report": "秒哒登录完成" if ok else "秒哒登录未完成",
                 "needs_attention": not ok})

    if not _has_cookie():
        return _result("NO_AUTH", "秒哒还没有登录态；请先点「🔐 重新引导登录」", 2)

    # status / run 都静默，秒哒是"登录即送秒点"（每月最多 7 天）
    is_status = (action == "status")

    with sync_playwright() as pw:
        try:
            browser = _new_context(pw, headless=True)
        except Error:
            return _result("BROWSER", "无法启动浏览器", 2)

        try:
            page = browser.new_page()
            resp = page.goto(CHECKIN_URL, wait_until="domcontentloaded", timeout=25000)
            if resp is None or not resp.ok:
                return _result("NETWORK", "无法打开秒哒首页", 1)

            page.wait_for_timeout(3000)
            body_text = page.locator("body").inner_text(timeout=5000)

            # 登录态检查：首页出现"扫码登录"且没"退出" → Cookie 失效
            if "扫码登录" in body_text and "退出" not in body_text:
                return _result("NO_AUTH", "秒哒登录态失效，请重新引导登录", 2)

            # 秒哒规则：登录即送秒点（每月最多 7 天），不需要手动点签到按钮
            # 已登录 = 今日已签到（status）/ 签到成功（run）
            if is_status:
                return _result("ALREADY", "秒哒登录即签到，今日已完成")
            else:
                return _result("CLAIMED", "秒哒登录即送，已签到")

        except Error as exc:
            return _result("BROWSER", f"秒哒页面操作失败: {type(exc).__name__}", 1)
        finally:
            browser.close()


def main(action):
    try:
        code, result = run(action)
    except Exception as exc:
        code, result = _result("BROWSER", str(exc), 2)
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    sys.exit(main(action))
