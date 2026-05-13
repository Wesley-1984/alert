"""
AI 根因分析模块
调用 OpenAI 兼容 API 对交换机告警进行智能分析
支持 DeepSeek、通义千问、智谱等国内大模型
"""

import json
import logging
import urllib.request
import urllib.error

logger = logging.getLogger("switch-monitor")


class AIAnalyzer:
    """AI 告警分析器"""

    def __init__(self, api_base: str, api_key: str, model: str,
                 system_prompt: str, timeout: int = 30):
        """
        初始化 AI 分析器

        Args:
            api_base: OpenAI 兼容 API 基础 URL（如 https://api.deepseek.com/v1）
            api_key: API 密钥
            model: 模型名称
            system_prompt: 系统提示词
            timeout: 请求超时（秒）
        """
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.system_prompt = system_prompt
        self.timeout = timeout

    def analyze(self, alert: dict) -> str:
        """
        对告警进行 AI 分析

        Args:
            alert: 告警信息字典

        Returns:
            AI 分析结果（Markdown 格式），失败时返回空字符串
        """
        # 构建用户提示词
        user_prompt = self._build_prompt(alert)

        # 调用 API
        try:
            result = self._call_api(user_prompt)
            return result
        except Exception as e:
            logger.error("AI 分析失败: %s", e)
            return f"⚠️ AI 分析暂时不可用: {str(e)}"

    def _build_prompt(self, alert: dict) -> str:
        """构建发送给 AI 的用户提示词"""
        lines = [
            "请分析以下交换机告警：",
            "",
            f"**设备**: {alert.get('device', '未知')}",
            f"**厂商**: {alert.get('vendor', '未知')}",
            f"**告警类型**: {alert.get('pattern_name', '未知')}",
            f"**告警级别**: {alert.get('level', '未知')}",
            f"**告警描述**: {alert.get('description', '无')}",
            f"**时间**: {alert.get('timestamp', '未知')}",
            "",
            "**原始日志**:",
            f"```",
            f"{alert.get('raw_log', '无')}",
            f"```",
        ]

        affected = alert.get("affected_components", [])
        if affected:
            lines.append(f"**可能受影响组件**: {', '.join(affected)}")

        quick_check = alert.get("quick_check", "")
        if quick_check:
            lines.append(f"**初步排查命令**: `{quick_check}`")

        lines.append("")
        lines.append("请给出根因分析、处理步骤和紧急程度评估。")

        return "\n".join(lines)

    def _call_api(self, user_prompt: str) -> str:
        """调用 OpenAI 兼容 API"""
        url = f"{self.api_base}/chat/completions"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 500
        }

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}"
            }
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                content = result["choices"][0]["message"]["content"]
                logger.info("AI 分析完成，token 用量: %s",
                           result.get("usage", {}))
                return content.strip()
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            logger.error("AI API HTTP 错误 %d: %s", e.code, error_body)
            raise
        except urllib.error.URLError as e:
            logger.error("AI API 网络错误: %s", e)
            raise
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            logger.error("AI API 响应解析失败: %s", e)
            raise

    def batch_analyze(self, alerts: list) -> list:
        """
        批量分析告警

        Args:
            alerts: 告警列表

        Returns:
            带 AI 分析结果的告警列表
        """
        results = []
        for alert in alerts:
            ai_result = self.analyze(alert)
            alert["ai_analysis"] = ai_result
            results.append(alert)
        return results
