"""
钉钉 Webhook 通知模块
支持 text 和 markdown 消息格式，支持加签验证
"""

import hashlib
import hmac
import base64
import time
import urllib.parse
import json
import logging
import urllib.request
import urllib.error

logger = logging.getLogger("switch-monitor")


class DingTalkNotifier:
    """钉钉机器人通知器"""

    def __init__(self, webhook_url: str, keyword: str = "",
                 secret: str = "", at_mobiles: list = None,
                 at_all: bool = False):
        """
        初始化钉钉通知器

        Args:
            webhook_url: 钉钉机器人 Webhook URL
            keyword: 消息中必须包含的关键词
            secret: 加签密钥（可选）
            at_mobiles: @ 指定人的手机号列表
            at_all: 是否 @ 所有人
        """
        self.webhook_url = webhook_url
        self.keyword = keyword
        self.secret = secret
        self.at_mobiles = at_mobiles or []
        self.at_all = at_all

    def _sign_url(self) -> str:
        """生成加签后的 Webhook URL"""
        if not self.secret:
            return self.webhook_url

        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{self.secret}"
        hmac_code = hmac.new(
            self.secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))

        separator = "&" if "?" in self.webhook_url else "?"
        return f"{self.webhook_url}{separator}timestamp={timestamp}&sign={sign}"

    def _send_request(self, payload: dict) -> dict:
        """发送 HTTP 请求到钉钉 API"""
        url = self._sign_url()
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"}
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if result.get("errcode") != 0:
                    logger.error("钉钉通知发送失败: %s", result)
                else:
                    logger.info("钉钉通知发送成功")
                return result
        except urllib.error.URLError as e:
            logger.error("钉钉通知请求失败: %s", e)
            return {"errcode": -1, "errmsg": str(e)}
        except Exception as e:
            logger.error("钉钉通知异常: %s", e)
            return {"errcode": -1, "errmsg": str(e)}

    def send_text(self, content: str) -> dict:
        """发送 text 类型消息"""
        # 确保消息包含关键词
        if self.keyword and self.keyword not in content:
            content = f"[{self.keyword}] {content}"

        payload = {
            "msgtype": "text",
            "text": {"content": content},
            "at": {
                "atMobiles": self.at_mobiles,
                "isAtAll": self.at_all
            }
        }
        return self._send_request(payload)

    def send_markdown(self, title: str, text: str) -> dict:
        """发送 markdown 类型消息（适合告警详情）"""
        # markdown 消息的 title 不支持关键词检查，在正文中确保包含
        if self.keyword and self.keyword not in text:
            text = f"**[{self.keyword}]**\n\n{text}"

        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": text
            },
            "at": {
                "atMobiles": self.at_mobiles,
                "isAtAll": self.at_all
            }
        }
        return self._send_request(payload)

    def send_alert(self, alert: dict, ai_result: str = "") -> dict:
        """
        发送网络设备告警通知

        Args:
            alert: 告警信息字典，包含 device, pattern_name, level, description, raw_log 等
            ai_result: AI 分析结果（可选）
        """
        level_icons = {
            "critical": "🔴 紧急",
            "warning": "🟠 警告",
            "info": "🟡 关注"
        }
        level_text = level_icons.get(alert.get("level", "info"), "🟡 关注")
        device = alert.get("device", "未知设备")
        pattern_name = alert.get("pattern_name", "未知告警")
        description = alert.get("description", "")
        raw_log = alert.get("raw_log", "")
        timestamp = alert.get("timestamp", time.strftime("%Y-%m-%d %H:%M:%S"))
        quick_check = alert.get("quick_check", "")

        # 构建 markdown 消息
        title = f"【{level_text}】{device} - {pattern_name}"
        md_lines = [
            f"### {level_text} 网络告警",
            f"",
            f"**设备**: {device}",
            f"**告警**: {pattern_name}",
            f"**描述**: {description}",
            f"**时间**: {timestamp}",
            f"",
            f"> {raw_log[:200]}",
        ]

        if quick_check:
            md_lines.append(f"")
            md_lines.append(f"**排查命令**: `{quick_check}`")

        if ai_result:
            md_lines.append(f"")
            md_lines.append(f"---")
            md_lines.append(f"#### 🤖 AI 分析")
            md_lines.append(f"")
            md_lines.append(ai_result)

        md_text = "\n".join(md_lines)
        return self.send_markdown(title, md_text)

    def send_recovery(self, device: str, recovery_name: str, pattern_name: str, 
                      level: str, raw_log: str) -> dict:
        """
        发送告警恢复通知

        Args:
            device: 设备名称/IP
            recovery_name: 恢复告警名称
            pattern_name: 原始告警名称
            level: 原始告警级别
            raw_log: 原始日志内容
        """
        level_icons = {
            "critical": "🔴",
            "warning": "🟠",
            "info": "🟡"
        }
        level_icon = level_icons.get(level, "🟡")

        title = f"【✅ 恢复】{device} - {recovery_name}"
        md_lines = [
            f"### ✅ 告警恢复通知",
            f"",
            f"**设备**: {device}",
            f"**恢复项**: {recovery_name}",
            f"**原始告警**: {level_icon} {pattern_name}",
            f"**恢复时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"",
            f"> {raw_log[:200]}",
        ]

        md_text = "\n".join(md_lines)
        return self.send_markdown(title, md_text)

    def send_summary(self, stats: dict) -> dict:
        """
        发送巡检汇总通知

        Args:
            stats: 汇总统计，包含 total, critical, warning, info, devices 等
        """
        title = f"网络设备巡检汇总 - {time.strftime('%Y-%m-%d')}"
        md_lines = [
            "### 网络设备巡检汇总",
            "",
            f"**巡检时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"| 级别 | 数量 |",
            f"|:---:|:---:|",
            f"| 🔴 紧急 | {stats.get('critical', 0)} |",
            f"| 🟠 警告 | {stats.get('warning', 0)} |",
            f"| 🟡 关注 | {stats.get('info', 0)} |",
            f"| **合计** | **{stats.get('total', 0)}** |",
        ]

        devices = stats.get("devices", {})
        if devices:
            md_lines.append("")
            md_lines.append("**各设备告警数**:")
            for dev, count in sorted(devices.items(), key=lambda x: -x[1]):
                md_lines.append(f"- {dev}: {count} 条")

        md_text = "\n".join(md_lines)
        return self.send_markdown(title, md_text)
