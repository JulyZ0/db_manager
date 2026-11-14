"""告警通知：webhook 推送。

为什么只做 webhook 而不做邮件/短信
----------------------------------
内网部署里最常见的是企业微信、钉钉、飞书的群机器人——一个 POST 就够，
不需要 SMTP 服务器、不需要短信通道。邮件需要额外的账号配置与失败
排查路径，收益不比一个群机器人更高，因此先做最有用的那一条。

只推送两类事件
--------------
**新触发**与**恢复**。已在告警中的重复命中不推送——否则一个持续越界的
指标会每分钟推一条，运维第一件事就是把这个告警关掉。

失败不影响主流程
----------------
推送失败只记日志，不抛异常：告警评估本身是主业务，通知是附属动作。
反过来说，通知失败不能导致事件状态错乱。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

__all__ = ["notify_event", "build_text"]


def _webhook_url() -> str:
    return (getattr(settings, "MONITOR_ALERT_WEBHOOK_URL", "") or "").strip()


def build_text(event, kind: str) -> str:
    """拼一条人能直接读懂的告警文本。

    包含"哪个实例、什么指标、阈值多少、当前多少"四要素——缺任何一项，
    收到消息的人都得再去系统里翻一遍才知道发生了什么。
    """
    from apps.monitor.models import AlertRule

    operator_label = dict(AlertRule.Operator.choices).get(event.operator, event.operator)
    severity = (
        "严重" if event.severity == AlertRule.Severity.CRITICAL else "警告"
    )
    head = "【告警触发】" if kind == "fired" else "【告警恢复】"
    return (
        f"{head}[{severity}] {event.rule_name}\n"
        f"实例：{event.instance_label}\n"
        f"指标：{event.metric} {operator_label} {event.threshold:g}\n"
        f"当前值：{event.latest_value:g}\n"
        f"时间：{event.last_seen_at:%Y-%m-%d %H:%M:%S}"
    )


def _payload(text: str) -> dict:
    """同时给出企业微信/钉钉（msgtype）与飞书（msg_type）的字段。

    三者结构相似但键名不同，同时带上两边都能识别的字段，避免为每种
    机器人各写一个适配器——一份配置打通三家。
    """
    return {
        "msgtype": "text",
        "msg_type": "text",
        "text": {"content": text},
        "content": {"text": text},
    }


def notify_event(event, kind: str) -> bool:
    """推送一条告警。返回是否成功（未配置 webhook 视为成功且不发送）。"""
    url = _webhook_url()
    if not url:
        return True

    text = build_text(event, kind)
    body = json.dumps(_payload(text)).encode("utf-8")
    timeout = int(getattr(settings, "MONITOR_ALERT_WEBHOOK_TIMEOUT", 5))

    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # 不抛：通知失败不该让评估任务整体失败并触发 Celery 重试，
        # 那会造成同一批事件被重复评估。
        logger.warning("告警通知发送失败 kind=%s err=%s", kind, type(exc).__name__)
        return False

    if status >= 400:
        logger.warning("告警通知返回非 2xx kind=%s status=%s", kind, status)
        return False
    return True
