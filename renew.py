#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MWS (cloud.m-ws.cc) 自动续期脚本
原理：MWS 的 Bot/Site 有 7 天倒计时，到期自动停止。
     点一次 Renew 按钮 = POST /api/bots/{id}/renew，把倒计时重置回 7 天。
     本脚本每周一、三、五跑一次，把所有 Bot/Site 全部续期，永不停止。
登录态：__Host-mrtcloud_token（JWT，约 26 天有效，过期需重新登录抓取）
通知：走 notify-gateway（notify.py 上报结构化结果，网关统一发邮件 + Telegram）。
     仓库只需配 NOTIFY_URL / NOTIFY_TOKEN，不内置 SMTP / TG。
依赖：requests（pip install requests）
"""
import os
import sys
import json
import urllib.error
from datetime import datetime
import requests
from notify import notify
API = "https://cloud.m-ws.cc/api"
# Cloudflare 对 Python 默认 UA 返回 403 (error 1010)，需伪装成浏览器
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
def http(path, method="GET", token=None):
    """请求 API，返回 (status_code, body)。网络异常返回 (0, 错误信息)。"""
    headers = {"User-Agent": UA}
    if token:
        headers["Cookie"] = "__Host-mrtcloud_token=" + token
    if method != "GET":
        headers["Content-Type"] = "application/json"
    try:
        resp = requests.request(method, API + path, headers=headers, timeout=30)
        return resp.status_code, resp.text
    except requests.RequestException as e:
        return 0, str(e)
def now_str():
    """本地时间（workflow 里设 TZ=Asia/Shanghai 则显示北京时间）。"""
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
def collect(token):
    """拉取所有 Bot / Site，返回 [(kind, id, name, remaining_hours), ...]。"""
    items = []
    for kind, path, key in [("Bot", "/bots", "bots"), ("Site", "/sites", "sites")]:
        status, body = http(path, token=token)
        if status != 200:
            print("[!] 获取 {} 失败: HTTP {} {}".format(path, status, body))
            items.append((kind, None, "<列表获取失败 HTTP {}>".format(status), None))
            continue
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = []
        lst = data if isinstance(data, list) else data.get(key, [])
        for obj in lst or []:
            oid = obj.get("id")
            name = obj.get("name") or obj.get("username") or "id:{}".format(oid)
            timer = obj.get("timer") or {}
            rem = timer.get("remaining_hours")
            items.append((kind, oid, name, rem))
    return items
def renew_one(token, kind, oid):
    """续期单个对象，返回 (ok, status, body)。"""
    path = "/bots/{}/renew".format(oid) if kind == "Bot" else "/sites/{}/renew".format(oid)
    status, body = http(path, method="POST", token=token)
    return (status == 200), status, body
def _short_error(status, body):
    """把续期失败的原始返回压成一句人话，不把整段 JSON 原样发通知。

    captcha_required 是唯一需要翻译的：平台要求在面板里人工确认，
    自动续期过不了。其他错误只保留简短原因，截断到 60 字以内。
    """
    text = (body or "").strip()
    try:
        data = json.loads(text)
        code = data.get("code") if isinstance(data, dict) else None
        detail = data.get("detail") if isinstance(data, dict) else None
    except (json.JSONDecodeError, AttributeError):
        code, detail = None, None
    if code == "captcha_required":
        return "需在面板手动确认（验证码），自动续期被拦"
    if detail:
        return " ".join(str(detail).split())[:60]
    if text:
        return "HTTP {} {}".format(status, " ".join(text.split())[:60])
    return "HTTP {}".format(status)


def _report(level, title, content, details):
    """上报 notify-gateway；失败只打日志，不阻断续期主流程。"""
    try:
        notify(title, content, level=level, details=details)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print("::warning::通知上报失败 HTTP {}: {}".format(e.code, body))
    except Exception as e:
        print("::warning::通知上报失败: {}".format(e))
def main():
    token = os.environ.get("MWS_TOKEN", "").strip()
    if not token:
        print("[✗] 环境变量 MWS_TOKEN 未设置")
        sys.exit(1)
    # 0) 验证 token 有效性
    status, body = http("/auth/me", token=token)
    if status == 401:
        title = "MWS token 已失效 ({})".format(now_str())
        content = ("请重新登录 cloud.m-ws.cc，F12 抓取 __Host-mrtcloud_token，"
                   "更新到 GitHub Secret MWS_TOKEN")
        print(title)
        print(content)
        _report("failed", title, content, None)
        sys.exit(1)
    if status != 200:
        print("[✗] 验证 token 异常: HTTP {} {}".format(status, body))
        sys.exit(1)
    try:
        who = json.loads(body).get("username")
    except (json.JSONDecodeError, AttributeError):
        who = "?"
    print("[✓] 登录有效: {}".format(who))
    # 1) 拉取并续期
    items = collect(token)
    if not items:
        title = "MWS 续期报告 ({})".format(now_str())
        print(title)
        _report("success", title, "账号下没有 Bot / Site，跳过。", None)
        return
    failed = 0
    detail_items = []
    failure_reasons = []
    for kind, oid, name, rem in items:
        if oid is None:
            detail_items.append({"id": "unknown", "name": name, "status": "failed",
                                 "error": "列表获取失败"})
            failure_reasons.append("列表获取失败")
            failed += 1
            continue
        ok, status, body = renew_one(token, kind, oid)
        rem_txt = "，续期前剩 {}h".format(int(rem)) if rem is not None else ""
        if ok:
            msg = rem_txt.strip().lstrip("，") if rem is not None else ""
            detail_items.append({"id": str(oid), "name": name, "status": "success",
                                 "message": msg})
            print("[✓] {} {} (id:{}) 续期成功{}".format(kind, name, oid, rem_txt))
        else:
            short = _short_error(status, body)
            detail_items.append({"id": str(oid), "name": name, "status": "failed",
                                 "error": short})
            failure_reasons.append(short)
            failed += 1
            print("[✗] {} {} (id:{}) 续期失败：{}".format(kind, name, oid, short))
    # 2) 汇总 + 通知：通知正文只写汇总和下一步，不再逐项重复；
    # 逐项明细由网关按 details 渲染一次，避免同一份失败出现两遍、
    # 原始 JSON 也只留在 Actions 日志里，不进通知。
    total = len(items)
    success = total - failed
    level = "success" if failed == 0 else "partial"
    title = "MWS 续期报告 ({})".format(now_str())
    if failed == 0:
        content = "总计 {} 项，全部续期成功。".format(total)
    else:
        reasons = "；".join(dict.fromkeys(failure_reasons))
        content = ("总计 {} 项，成功 {} 项，失败 {} 项。\n"
                   "失败原因：{}。".format(total, success, failed, reasons))
        if any("验证码" in r for r in failure_reasons):
            content += "\n下一步：打开 https://cloud.m-ws.cc/ 手动续期。"
    print("\n" + title + "\n" + content)
    _report(
        level,
        title,
        content,
        {"total": total, "success": success, "failed": failed, "details": detail_items},
    )
    if failed:
        sys.exit(1)
if __name__ == "__main__":
    main()
