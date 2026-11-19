"""库库AI (kukuai.cn) 网页端每日签到。

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
PROFILE_DIR = Path(os.environ.get("KUKU_PROFILE_DIR")
                   or (HOME / ".agent-auto-signin" / "kuku-profile"))
PROFILE_DIR.mkdir(parents=True, exist_ok=True)

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
    """清理 SingletonLock 文件。"""
    import subprocess
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        for sub in ("", "Default/"):
            for exe_path in ("msedge.exe", "chrome.exe", "chromium.exe"):
                try:
                    subprocess.run(
                        ["wmic", "process", "where",
                         f"name='{exe_path}' and commandline like '%kuku-profile%'",
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


BASE_URL = "https://kuku.baidu.com"
CHECKIN_URL = BASE_URL + "/"

SUCCESS_TEXT = {"签到成功", "领取成功", "已领取", "获得积分", "今日已签"}
ALREADY_TEXT = {"已签到", "今日已签到", "今日已签", "明日再来", "已领取过", "已完成", "连签", "已签"}


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
    """两段式引导登录。"""
    import webbrowser
    import shutil

    if step == "open":
        print(f"[库库AI] 打开新标签页: {BASE_URL}")
        webbrowser.open_new_tab(BASE_URL)
        return True

    if step == "confirm":
        import subprocess
        print("[库库AI] 检查浏览器是否已关闭...")
        user_exe = _find_system_browser()
        user_exe_name = os.path.basename(user_exe) if user_exe else "msedge.exe"

        # 先强制 kill 所有 Edge/Chrome 进程（包括开机自启的后台进程）
        # 不 kill 掉的话 Cookie 文件会被锁，只能复制到部分 Cookie
        print(f"[库库AI] 清理 {user_exe_name} 进程以解锁 Cookie 文件...")
        subprocess.run(
            ["taskkill", "/F", "/IM", user_exe_name, "/T"],
            capture_output=True, timeout=10,
        )
        import time
        time.sleep(1)  # 等进程完全退出释放文件锁

        print("[库库AI] ✅ Cookie 文件已解锁，开始复制...")
        _kill_stale_browser_exes()

        src = _find_user_browser_profile()
        if not src:
            print("[库库AI] ❌ 找不到 Edge/Chrome profile")
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
                    print(f"[库库AI] 复制 {filename} ✅")
                except Exception as exc:
                    print(f"[库库AI] ⚠️ 复制 {filename} 失败: {exc}")

        for dirname in ("Local Storage", "Session Storage"):
            src_dir = src / dirname
            dst_dir = PROFILE_DIR / dirname
            if src_dir.is_dir():
                try:
                    if dst_dir.exists():
                        shutil.rmtree(str(dst_dir), ignore_errors=True)
                    shutil.copytree(str(src_dir), str(dst_dir))
                    copied = True
                    print(f"[库库AI] 复制 {dirname} ✅")
                except Exception as exc:
                    print(f"[库库AI] ⚠️ 复制 {dirname} 失败: {exc}")

        if not copied:
            print("[库库AI] ❌ 没复制到任何 Cookie")
            return False

        # 验证
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
                    "退出登录", "我的积分", "个人中心", "我的主页",
                ])

                if has_logged_in_flag and not has_login_hint:
                    print("[库库AI] ✅ 登录态验证通过！")
                    return True
                elif not has_login_hint:
                    print("[库库AI] ⚠️ 未检测到登录标志但也无未登录提示")
                    return True
                else:
                    print("[库库AI] ❌ 还是未登录")
                    return False
            except Exception as exc:
                print(f"[库库AI] ⚠️ 验证异常: {exc}")
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
        ok = login_interactive(step="confirm")
        return (0 if ok else 2,
                {"result": "OK" if ok else "NO_AUTH",
                 "report": "库库AI登录完成" if ok else "库库AI登录未完成",
                 "needs_attention": not ok})

    if not _has_cookie():
        return _result("NO_AUTH", "库库AI还没有登录态，请先引导登录", 2)

    # status 模式静默后台跑，run 模式才显示浏览器
    is_status = (action == "status")

    with sync_playwright() as pw:
        try:
            browser = _new_context(pw, headless=is_status)
        except Error:
            return _result("BROWSER", "无法启动浏览器", 2)

        try:
            page = browser.new_page()
            resp = page.goto(CHECKIN_URL, wait_until="domcontentloaded", timeout=25000)
            if resp is None or not resp.ok:
                return _result("NETWORK", "无法打开库库AI首页", 1)

            page.wait_for_timeout(3000)
            body_text = page.locator("body").inner_text(timeout=5000)

            # 登录态检查
            if any(k in body_text for k in ["扫码登录", "手机号登录"]):
                return _result("NO_AUTH", "库库AI登录态失效，请重新引导登录", 2)

            # 首页文本里直接有"已签到"类文字 → 今天已签
            for text in ALREADY_TEXT:
                if text in body_text:
                    return _result("ALREADY", "库库AI今天已签到")

            # ======== status/run 都往下走：点左侧签到按钮，进 iframe 检测 is-signed ========
            # （status 模式不再 early return，改为点按钮看弹窗状态）

            # ============ run 模式：两步签到 ============
            # 第一步：点左侧 sidebar 的「签到」按钮，展开签到面板
            left_signin_clicked = False
            for btn in page.get_by_text("签到", exact=True).all():
                try:
                    box = btn.bounding_box()
                    if box and box["y"] > 300:
                        btn.click(timeout=3000, force=True)
                        left_signin_clicked = True
                        page.wait_for_timeout(2500)
                        print(f"[库库AI] ✅ 左侧签到按钮已点击 (y={box['y']:.0f})")
                        break
                except Exception:
                    continue

            if not left_signin_clicked:
                try:
                    all_signin = page.locator("button").filter(has_text="签到").all()
                    for btn in all_signin:
                        try:
                            if btn.is_visible() and btn.is_enabled():
                                btn.click(timeout=3000, force=True)
                                left_signin_clicked = True
                                page.wait_for_timeout(2500)
                                print("[库库AI] ✅ 兜底找到左侧签到按钮")
                                break
                        except Exception:
                            continue
                except Exception:
                    pass

            if not left_signin_clicked:
                return _result("UNKNOWN", "库库AI没找到左侧签到入口", 1)

            # 等待 iframe 加载（签到面板的内容在 iframe 里）
            print("[库库AI] 等待签到 iframe 加载...")
            sign_frame = None
            try:
                page.wait_for_selector("iframe.signin-center-iframe", timeout=12000)
                # 进入 iframe
                for frame in page.frames:
                    if "kukuPcSignAct" in (frame.url or "") or "signin" in (frame.url or "").lower():
                        sign_frame = frame
                        print(f"  Found iframe: {frame.url}")
                        break
                if not sign_frame:
                    sign_frame = page.frame(name="signin-center-iframe") or page.frames[1]
                    print(f"  Using fallback frame: {sign_frame.url if sign_frame else 'None'}")
            except Exception as exc:
                return _result("BROWSER", f"签到弹窗加载失败: {exc}", 1)

            # 在 iframe 里找 .pc-sign-btn
            if not sign_frame:
                return _result("UNKNOWN", "找不到签到 iframe", 1)

            sign_btn = None
            try:
                sign_frame.wait_for_selector(".pc-sign-btn", timeout=10000)
                sign_btn = sign_frame.locator(".pc-sign-btn").first
            except Exception as exc:
                return _result("UNKNOWN", f"iframe 里没找到 .pc-sign-btn: {exc}", 1)

            btn_classes = sign_btn.get_attribute("class") or ""
            btn_text = sign_btn.inner_text(timeout=2000)
            print(f"  .pc-sign-btn text='{btn_text}' classes='{btn_classes}'")

            if "is-signed" in btn_classes:
                return _result("ALREADY", "库库AI今天已签到")

            # status 模式：检测到未签到就返回，不真正点击
            if is_status:
                return _result("DRY_RUN", "库库AI今日可签到")

            # 点击签到
            print("[库库AI] 第二步：点击「立即签到」...")
            try:
                sign_btn.click(timeout=3000, force=True)
                page.wait_for_timeout(3000)
            except Exception as exc:
                return _result("ERROR", f"点击签到失败: {exc}", 1)

            # 验证：按钮变 is-signed → 成功
            try:
                sign_frame.wait_for_selector(".pc-sign-btn.is-signed", timeout=8000)
                return _result("CLAIMED", "库库AI签到成功")
            except Exception:
                pass

            # 兜底
            new_text = page.locator("body").inner_text(timeout=5000)
            for t in SUCCESS_TEXT:
                if t in new_text:
                    return _result("CLAIMED", "库库AI签到成功")
            for t in ALREADY_TEXT:
                if t in new_text:
                    return _result("ALREADY", "库库AI今天已签到")

            return _result("CLAIMED", "库库AI签到已触发")

        except Error as exc:
            return _result("BROWSER", f"库库AI页面操作失败: {type(exc).__name__}", 1)
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
