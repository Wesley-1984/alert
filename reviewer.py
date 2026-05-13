"""
AI 故障复盘模块
故障恢复后自动触发 AI 复盘，生成故障档案并持久化存储
档案包含：故障原因、处理过程、影响面、改进建议
"""

import json
import os
import logging
from datetime import datetime

logger = logging.getLogger("switch-monitor")


class FaultReviewer:
    """故障复盘引擎"""

    def __init__(self, api_base: str, api_key: str, model: str,
                 timeout: int = 60, archive_dir: str = "/var/log/switch-monitor/archive"):
        """
        初始化复盘引擎

        Args:
            api_base: OpenAI 兼容 API 基础 URL
            api_key: API 密钥
            model: 模型名称
            timeout: 请求超时（秒）
            archive_dir: 故障档案存储目录
        """
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.archive_dir = archive_dir
        os.makedirs(archive_dir, exist_ok=True)

        self.system_prompt = """你是一位资深的网络基础设施运维专家，擅长交换机故障复盘和根因分析。

你需要对故障事件进行复盘，输出结构化的故障档案。包含以下维度：

1. **故障概要**：一句话概括故障
2. **根因分析**：故障的根本原因（直接原因 + 深层原因）
3. **时间线**：故障发现→处理→恢复的关键时间节点
4. **影响面评估**：
   - 受影响的设备和端口
   - 受影响的业务/用户范围
   - 持续时间
5. **处理过程**：运维人员做了什么（基于已知信息推断）
6. **改进建议**：防止同类故障再次发生的具体措施

回复要求：
- 使用 JSON 格式输出
- 字段名用英文，值用中文
- 客观严谨，区分"确认事实"和"合理推测"
- 改进建议要可执行、可验证"""

    def review(self, alert: dict, recovery_log: str = "",
              ai_initial_analysis: str = "") -> dict:
        """
        对已恢复的故障进行复盘

        Args:
            alert: 故障告警信息
            recovery_log: 恢复时的原始日志
            ai_initial_analysis: 故障发生时的 AI 初步分析

        Returns:
            故障档案字典
        """
        user_prompt = self._build_prompt(alert, recovery_log, ai_initial_analysis)

        try:
            result_text = self._call_api(user_prompt)
            # 尝试解析 JSON
            archive = self._parse_result(result_text)
        except Exception as e:
            logger.error("AI 复盘失败: %s", e)
            archive = {
                "error": f"AI复盘失败: {str(e)}",
                "raw_alert": alert
            }

        # 补充元数据
        archive["meta"] = {
            "device": alert.get("device", ""),
            "vendor": alert.get("vendor", ""),
            "pattern_name": alert.get("pattern_name", ""),
            "level": alert.get("level", ""),
            "fault_time": alert.get("timestamp", ""),
            "recovery_time": alert.get("recovery_timestamp", ""),
            "duration_seconds": alert.get("duration_seconds"),
            "review_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        # 持久化存储
        self._save_archive(archive)

        return archive

    def _build_prompt(self, alert: dict, recovery_log: str,
                      ai_initial_analysis: str) -> str:
        """构建复盘提示词"""
        lines = [
            "请对以下已恢复的交换机故障进行复盘：",
            "",
            f"**设备**: {alert.get('device', '未知')}",
            f"**厂商**: {alert.get('vendor', '未知')}",
            f"**告警类型**: {alert.get('pattern_name', '未知')}",
            f"**告警级别**: {alert.get('level', '未知')}",
            f"**故障描述**: {alert.get('description', '无')}",
            f"**故障发生时间**: {alert.get('timestamp', '未知')}",
            f"**故障恢复时间**: {alert.get('recovery_timestamp', '未知')}",
        ]

        duration = alert.get("duration_seconds")
        if duration is not None:
            hours = duration // 3600
            minutes = (duration % 3600) // 60
            lines.append(f"**故障持续时长**: {hours}小时{minutes}分钟")

        lines.append("")
        lines.append("**故障发生时原始日志**:")
        lines.append("```")
        lines.append(alert.get("raw_log", "无"))
        lines.append("```")

        if recovery_log:
            lines.append("")
            lines.append("**故障恢复时原始日志**:")
            lines.append("```")
            lines.append(recovery_log)
            lines.append("```")

        if ai_initial_analysis:
            lines.append("")
            lines.append("**故障发生时的 AI 初步分析**:")
            lines.append(ai_initial_analysis)

        affected = alert.get("affected_components", [])
        if affected:
            lines.append(f"**受影响组件**: {', '.join(affected)}")

        quick_check = alert.get("quick_check", "")
        if quick_check:
            lines.append(f"**排查命令**: `{quick_check}`")

        lines.append("")
        lines.append("请输出 JSON 格式的故障复盘档案。")

        return "\n".join(lines)

    def _call_api(self, user_prompt: str) -> str:
        """调用 OpenAI 兼容 API"""
        import urllib.request
        import urllib.error

        url = f"{self.api_base}/chat/completions"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.2,
            "max_tokens": 1000
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

        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result["choices"][0]["message"]["content"].strip()

    def _parse_result(self, text: str) -> dict:
        """解析 AI 返回的 JSON"""
        # 尝试提取 JSON 块
        json_text = text
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            json_text = text[start:end].strip()
        elif "```" in text:
            start = text.index("```") + 3
            end = text.index("```", start)
            json_text = text[start:end].strip()

        try:
            return json.loads(json_text)
        except json.JSONDecodeError:
            logger.warning("AI 复盘结果非标准 JSON，包装为文本")
            return {"review_text": text}

    def _save_archive(self, archive: dict):
        """持久化故障档案"""
        meta = archive.get("meta", {})
        device = meta.get("device", "unknown")
        fault_time = meta.get("fault_time", "").replace(":", "-").replace(" ", "_")
        pattern = meta.get("pattern_name", "unknown").replace(" ", "-")

        filename = f"{device}_{pattern}_{fault_time}.json"
        filepath = os.path.join(self.archive_dir, filename)

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(archive, f, indent=2, ensure_ascii=False)
            logger.info("故障档案已保存: %s", filepath)
        except IOError as e:
            logger.error("故障档案保存失败: %s", e)

    def list_archives(self, device: str = None, limit: int = 20) -> list:
        """
        查询故障档案列表

        Args:
            device: 按设备名过滤（可选）
            limit: 返回数量限制

        Returns:
            故障档案列表（按时间倒序）
        """
        archives = []
        if not os.path.exists(self.archive_dir):
            return archives

        files = sorted(
            [f for f in os.listdir(self.archive_dir) if f.endswith(".json")],
            reverse=True
        )

        for fname in files[:limit * 2]:  # 多读一些用于过滤
            filepath = os.path.join(self.archive_dir, fname)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    archive = json.load(f)
                meta = archive.get("meta", {})
                if device and device not in meta.get("device", ""):
                    continue
                archives.append({
                    "file": fname,
                    "device": meta.get("device", ""),
                    "pattern_name": meta.get("pattern_name", ""),
                    "level": meta.get("level", ""),
                    "fault_time": meta.get("fault_time", ""),
                    "recovery_time": meta.get("recovery_time", ""),
                    "duration_seconds": meta.get("duration_seconds"),
                })
                if len(archives) >= limit:
                    break
            except (json.JSONDecodeError, IOError):
                continue

        return archives

    def get_archive(self, filename: str) -> dict:
        """读取指定故障档案"""
        filepath = os.path.join(self.archive_dir, filename)
        if not os.path.exists(filepath):
            return {"error": f"档案不存在: {filename}"}
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
