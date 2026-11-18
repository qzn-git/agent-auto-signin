"""把每日签到结果推送到微信。

凭据只从环境变量读取，不写入仓库、状态文件或日志。
按下列顺序取第一个已配置的通道：

    SERVERCHAN_SENDKEY              Server酱，推送到微信「方糖服务」号，免版 5 条/日
    WXPUSHER_APP_TOKEN + WXPUSHER_UID   WxPusher，推送到微信「WxPusher」服务号
    WECOM_WEBHOOK                   企业微信群机器人，消息在企业微信（可开微信插件同步到微信）
    PUSHPLUS_TOKEN                  PushPlus，需账号已实名
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 20

ENV_KEYS = {
    "serverchan": ("SERVERCHAN_SENDKEY",),
    "wxpusher": ("WXPUSHER_APP_TOKEN", "WXPUSHER_UID"),
    "wecom": ("WECOM_WEBHOOK",),
    "pushplus": ("PUSHPLUS_TOKEN",),
}


def _post_json(url, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_form(url, fields):
    body = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _env(name):
    return os.environ.get(name, "").strip()


def _as_markdown(content):
    """Server酱的 desp 按 Markdown 渲染，单换行会被并成一段，这里改成列表项。"""
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines:
        return content
    body = [lines[0]]
    body.extend("- " + line for line in lines[1:])
    return chr(10).join(body)


def _send_serverchan(title, content):
    key = _env("SERVERCHAN_SENDKEY")
    if not key:
        return None
    body = _post_form(
        "https://sctapi.ftqq.com/{0}.send".format(key),
        {"text": title, "desp": _as_markdown(content)},
    )
    body = body if isinstance(body, dict) else {}
    return body.get("code") == 0, "code={0} {1}".format(
        body.get("code"), body.get("message") or ""
    ).strip()


def _send_wxpusher(title, content):
    if not _env("WXPUSHER_APP_TOKEN") or not _env("WXPUSHER_UID"):
        return None
    payload = {
        "appToken": _env("WXPUSHER_APP_TOKEN"),
        "content": content,
        "summary": title[:20],
        "contentType": 1,
        "uids": [_env("WXPUSHER_UID")],
    }
    body = _post_json("https://wxpusher.zjiecode.com/api/send/message", payload)
    body = body if isinstance(body, dict) else {}
    return body.get("code") == 1000, "code={0} {1}".format(
        body.get("code"), body.get("msg") or ""
    ).strip()


def _send_wecom(title, content):
    url = _env("WECOM_WEBHOOK")
    if not url:
        return None
    payload = {"msgtype": "text", "text": {"content": title + chr(10) + content}}
    body = _post_json(url, payload)
    body = body if isinstance(body, dict) else {}
    return body.get("errcode") == 0, "errcode={0} {1}".format(
        body.get("errcode"), body.get("errmsg") or ""
    ).strip()


def _send_pushplus(title, content):
    value = _env("PUSHPLUS_TOKEN")
    if not value:
        return None
    payload = {
        "token": value,
        "title": title,
        "content": content,
        "template": "txt",
        "channel": "wechat",
    }
    body = _post_json("https://www.pushplus.plus/send", payload)
    body = body if isinstance(body, dict) else {}
    return body.get("code") == 200, "code={0} {1}".format(
        body.get("code"), body.get("msg") or ""
    ).strip()


CHANNELS = (
    ("serverchan", _send_serverchan),
    ("wxpusher", _send_wxpusher),
    ("wecom", _send_wecom),
    ("pushplus", _send_pushplus),
)


def configured(name):
    return all(_env(key) for key in ENV_KEYS[name])


def channel():
    """返回当前生效的通道名，未配置任何通道时返回空串。"""
    for name, _ in CHANNELS:
        if configured(name):
            return name
    return ""


def send(title, content):
    """按优先级发送一条消息；返回 (是否成功, 不含凭据的说明)。"""
    for name, sender in CHANNELS:
        if not configured(name):
            continue
        try:
            outcome = sender(title, content)
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as error:
            return False, "{0}: 请求失败 {1}".format(name, type(error).__name__)
        if outcome is None:
            continue
        ok, detail = outcome
        return ok, "{0}: {1}".format(name, detail)
    return False, "未配置任何推送通道"
