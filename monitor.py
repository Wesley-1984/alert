#!/usr/bin/env python3
"""
网络设备智能巡检告警系统 - 主监控脚本
功能：实时监控 rsyslog 按设备分目录存储的网络设备日志，
     支持锐捷/华为交换机、锐捷无线AC、深信服防火墙(AF)和SSL VPN、SmartX超融合平台，
     匹配告警模式后通过钉钉通知 + AI 分析

告警去重策略：
     同一设备 + 同一告警类型，在 notify_window（默认1小时）内不重复通知。
     窗口过后如果日志中仍在产生同类告警，会重新通知。
     按告警级别可配置不同的窗口时间。
"""

import os
import sys
import re
import json
import time
import yaml
import signal
import logging
import argparse
from pathlib import Path
from datetime import datetime

# 添加项目目录到 path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from notifier import DingTalkNotifier
from analyzer import AIAnalyzer

logger = logging.getLogger("switch-monitor")


class SwitchMonitor:
    """交换机日志监控引擎"""

    def __init__(self, config_path: str):
        """
        初始化监控引擎

        Args:
            config_path: 配置文件路径
        """
        self.config = self._load_config(config_path)
        self.running = False
        self.file_positions = {}  # 记录每个日志文件的读取位置
        self.patterns = {}        # 按厂商分组的编译后正则
        self.device_vendor_map = {}  # 设备 -> 厂商映射（关键字匹配）
        self.ip_vendor_map = {}      # IP -> 厂商映射（IP 精确匹配，优先级更高）

        # alert_history: 去重用，记录 { "device:pattern_name": 最后一次通知时间戳 }
        self.alert_history = {}
        
        # active_alerts: 跟踪当前活跃的告警，用于恢复检测
        # 格式: { "device:pattern_name[:dedup_extra]": { "timestamp": ..., "pattern": ... } }
        self.active_alerts = {}

        self._init_logging()
        self._init_patterns()
        self._init_notifier()
        self._init_analyzer()
        self._load_state()

    def _load_config(self, config_path: str) -> dict:
        """加载 YAML 配置文件"""
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _init_logging(self):
        """初始化日志系统"""
        runtime = self.config.get("runtime", {})
        log_file = runtime.get("log_file", "/var/log/switch-monitor.log")
        log_level = runtime.get("log_level", "INFO")

        # 确保日志目录存在
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

        logging.basicConfig(
            level=getattr(logging, log_level, logging.INFO),
            format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
            handlers=[
                logging.FileHandler(log_file, encoding="utf-8"),
                logging.StreamHandler()
            ]
        )

    def _init_patterns(self):
        """初始化日志匹配模式"""
        patterns_dir = os.path.join(SCRIPT_DIR, "patterns")
        devices = self.config.get("devices", [])

        for device_conf in devices:
            vendor = device_conf["vendor"]
            pattern_file = device_conf["pattern_file"]
            match_keywords = device_conf.get("match_keywords", [])
            match_ips = device_conf.get("match_ips", [])

            # 记录设备厂商识别规则 — 关键字匹配
            for kw in match_keywords:
                self.device_vendor_map[kw.lower()] = vendor

            # 记录设备厂商识别规则 — IP 精确匹配（优先级更高）
            for ip in match_ips:
                self.ip_vendor_map[ip.strip()] = vendor

            # 加载并编译正则
            pattern_path = os.path.join(patterns_dir, pattern_file)
            if not os.path.exists(pattern_path):
                logger.warning("模式文件不存在: %s", pattern_path)
                continue

            with open(pattern_path, "r", encoding="utf-8") as f:
                pattern_conf = yaml.safe_load(f)

            compiled = []
            for p in pattern_conf.get("patterns", []):
                try:
                    entry = self._compile_pattern(p)
                    compiled.append(entry)
                except re.error as e:
                    logger.error("正则编译失败 [%s]: %s - %s", vendor, p["name"], e)

            self.patterns[vendor] = compiled
            ip_count = len(match_ips)
            logger.info("加载 %s 模式 %d 条, IP映射 %d 个",
                        vendor, len(compiled), ip_count)

        # === 加载自定义规则（应用到所有厂商） ===
        self._load_custom_patterns(patterns_dir)

    def _compile_pattern(self, p: dict) -> dict:
        """编译单条匹配规则"""
        entry = {
            "name": p["name"],
            "level": p["level"],
            "regex": re.compile(p["regex"], re.IGNORECASE),
            "description": p.get("description", ""),
            "affected_components": p.get("affected_components", []),
            "quick_check": p.get("quick_check", ""),
            "custom": p.get("custom", False),
            "recovery_name": p.get("recovery_name", ""),
        }
        # 编译 dedup_extract（可选）：用于从日志中提取变量成分细化去重 key
        dedup_extract = p.get("dedup_extract", "")
        if dedup_extract:
            try:
                entry["dedup_extract"] = re.compile(dedup_extract, re.IGNORECASE)
            except re.error as e:
                logger.warning("dedup_extract 编译失败 [%s]: %s", p["name"], e)
                entry["dedup_extract"] = None
        else:
            entry["dedup_extract"] = None
        # 编译 recovery_regex（可选）：用于检测告警恢复
        recovery_regex = p.get("recovery_regex", "")
        if recovery_regex:
            try:
                entry["recovery_regex"] = re.compile(recovery_regex, re.IGNORECASE)
            except re.error as e:
                logger.warning("recovery_regex 编译失败 [%s]: %s", p["name"], e)
                entry["recovery_regex"] = None
        else:
            entry["recovery_regex"] = None
        return entry

    def _load_custom_patterns(self, patterns_dir: str):
        """
        加载自定义规则 (patterns/custom.yaml)
        自定义规则会追加到所有厂商的模式列表中，对全部设备生效
        """
        custom_path = os.path.join(patterns_dir, "custom.yaml")
        if not os.path.exists(custom_path):
            logger.info("未发现自定义规则文件 custom.yaml，跳过")
            return

        try:
            with open(custom_path, "r", encoding="utf-8") as f:
                custom_conf = yaml.safe_load(f)
        except (yaml.YAMLError, IOError) as e:
            logger.error("自定义规则文件加载失败: %s", e)
            return

        custom_patterns = custom_conf.get("patterns", [])
        if not custom_patterns:
            logger.info("自定义规则文件为空，无规则加载")
            return

        compiled_custom = []
        for p in custom_patterns:
            # 跳过被注释掉的空规则（YAML 解析后为 None）
            if p is None:
                continue
            try:
                p["custom"] = True  # 标记为自定义规则
                entry = self._compile_pattern(p)
                compiled_custom.append(entry)
            except re.error as e:
                logger.error("自定义正则编译失败: %s - %s", p.get("name", "?"), e)

        # 追加到所有厂商
        for vendor in self.patterns:
            self.patterns[vendor].extend(compiled_custom)

        # 如果没有任何厂商配置，也要为 unknown 厂商添加
        if not self.patterns and compiled_custom:
            self.patterns["custom"] = compiled_custom

        logger.info("加载自定义规则 %d 条 (应用到所有厂商)", len(compiled_custom))

    def _init_notifier(self):
        """初始化钉钉通知器"""
        dt_conf = self.config.get("dingtalk", {})
        self.notifier = DingTalkNotifier(
            webhook_url=dt_conf.get("webhook_url", ""),
            keyword=dt_conf.get("keyword", ""),
            secret=dt_conf.get("secret", ""),
            at_mobiles=dt_conf.get("at_mobiles", []),
            at_all=dt_conf.get("at_all", False)
        )

    def _init_analyzer(self):
        """初始化 AI 分析器"""
        ai_conf = self.config.get("ai", {})
        if ai_conf.get("enabled", False):
            self.analyzer = AIAnalyzer(
                api_base=ai_conf.get("api_base", ""),
                api_key=ai_conf.get("api_key", ""),
                model=ai_conf.get("model", "deepseek-chat"),
                system_prompt=ai_conf.get("system_prompt", ""),
                timeout=ai_conf.get("timeout", 30)
            )
            self.ai_enabled = True
            logger.info("AI 分析已启用 (模型: %s)", ai_conf.get("model"))
        else:
            self.analyzer = None
            self.ai_enabled = False
            logger.info("AI 分析未启用")

    def _load_state(self):
        """加载运行时状态（文件读取位置 + 告警去重历史）"""
        runtime = self.config.get("runtime", {})
        state_file = runtime.get("state_file", "/tmp/switch-monitor-state.json")

        if os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                self.file_positions = state.get("file_positions", {})
                # 兼容旧状态文件：不再加载 active_alerts
                logger.info("加载状态文件: %s (跟踪 %d 个文件)",
                           state_file, len(self.file_positions))
            except (json.JSONDecodeError, IOError) as e:
                logger.warning("状态文件加载失败，将从文件末尾开始: %s", e)

    def _save_state(self):
        """保存运行时状态"""
        runtime = self.config.get("runtime", {})
        state_file = runtime.get("state_file", "/tmp/switch-monitor-state.json")

        state = {
            "file_positions": self.file_positions
        }
        try:
            # 确保目录存在
            state_dir = os.path.dirname(state_file)
            if state_dir:
                os.makedirs(state_dir, exist_ok=True)
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
        except IOError as e:
            logger.error("状态文件保存失败: %s", e)

    def _identify_vendor(self, device_name: str) -> str:
        """
        根据设备目录名识别厂商

        优先级：
        1. IP 精确匹配 — 目录名就是 IP 地址时，直接从 ip_vendor_map 查找
        2. 关键字模糊匹配 — 目录名包含关键字时，从 device_vendor_map 查找
        3. 未匹配到 — 返回 "unknown"，会对该设备应用所有厂商规则
        """
        # 优先 IP 精确匹配（目录名就是 IP 地址，如 "192.168.40.30"）
        if device_name in self.ip_vendor_map:
            return self.ip_vendor_map[device_name]

        # 关键字模糊匹配（目录名含厂商关键字，如 "rj-core-sw01"）
        name_lower = device_name.lower()
        for keyword, vendor in self.device_vendor_map.items():
            if keyword.lower() in name_lower:
                return vendor

        return "unknown"

    def _discover_log_files(self) -> list:
        """
        发现所有需要监控的日志文件

        日志目录结构：{base_dir}/{IP}/{IP}_YYYY-MM-DD.log
        只监控当天的日志文件，避免扫描历史文件。
        """
        syslog_conf = self.config.get("syslog", {})
        base_dir = syslog_conf.get("base_dir", "/var/log/switches")

        log_files = []

        if not os.path.isdir(base_dir):
            logger.warning("syslog 目录不存在: %s", base_dir)
            return log_files

        # 只监控当天的日志文件
        today_str = datetime.now().strftime("%Y-%m-%d")

        for device_dir in sorted(os.listdir(base_dir)):
            device_path = os.path.join(base_dir, device_dir)
            if not os.path.isdir(device_path):
                continue

            vendor = self._identify_vendor(device_dir)

            # 构造当天日志文件名：{IP}_YYYY-MM-DD.log
            today_log = os.path.join(device_path, f"{device_dir}_{today_str}.log")

            if os.path.exists(today_log):
                log_files.append({
                    "device": device_dir,
                    "vendor": vendor,
                    "log_path": today_log
                })
            else:
                logger.debug("当天日志文件不存在: %s", today_log)

        logger.info("发现 %d 个设备的当天日志文件 (日期: %s)",
                    len(log_files), today_str)
        for lf in log_files:
            match_method = "IP" if lf["device"] in self.ip_vendor_map else \
                           "关键字" if lf["vendor"] != "unknown" else "未匹配"
            logger.info("  设备: %-20s 厂商: %-10s 匹配方式: %s  文件: %s",
                        lf["device"], lf["vendor"], match_method,
                        os.path.basename(lf["log_path"]))
        return log_files

    def _read_new_lines(self, log_path: str) -> list:
        """
        读取日志文件的新增行

        file_positions 语义：
        - key 不存在（None）：首次遇到该文件，守护模式下跳到末尾、scan 模式下从头读
        - 值为 0：从文件开头读（scan 模式使用）
        - 值为 N：从第 N 字节读（增量读取）
        """
        max_lines = self.config.get("syslog", {}).get("max_lines_per_read", 500)
        last_pos = self.file_positions.get(log_path)  # None = 首次遇到

        try:
            file_size = os.path.getsize(log_path)
        except OSError:
            return []

        # 文件被截断（如日志轮转重建），从头读
        if last_pos is not None and file_size < last_pos:
            last_pos = 0

        # 首次遇到该文件：跳到末尾，后续只读新增内容
        if last_pos is None:
            self.file_positions[log_path] = file_size
            return []

        new_lines = []
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(last_pos)
                for i, line in enumerate(f):
                    if i >= max_lines:
                        logger.warning("单次读取行数超限 (%d)，暂停: %s",
                                      max_lines, log_path)
                        break
                    new_lines.append(line.rstrip("\n\r"))
                self.file_positions[log_path] = f.tell()
        except IOError as e:
            logger.error("读取日志文件失败 %s: %s", log_path, e)
            return []

        return new_lines

    def _match_alert(self, line: str, vendor: str) -> dict:
        """对一行日志进行告警匹配"""
        vendors_to_check = [vendor] if vendor != "unknown" else list(self.patterns.keys())

        for v in vendors_to_check:
            for pattern in self.patterns.get(v, []):
                if pattern["regex"].search(line):
                    # 提取去重变量成分（如端口号、IP地址等）
                    dedup_extra = ""
                    if pattern.get("dedup_extract"):
                        m = pattern["dedup_extract"].search(line)
                        if m:
                            # 优先取第一个捕获组，否则取整个匹配
                            dedup_extra = m.group(1) if m.lastindex else m.group(0)

                    return {
                        "pattern_name": pattern["name"],
                        "level": pattern["level"],
                        "description": pattern["description"],
                        "affected_components": pattern["affected_components"],
                        "quick_check": pattern["quick_check"],
                        "vendor": v,
                        "dedup_extra": dedup_extra,
                        "recovery_name": pattern.get("recovery_name", ""),
                        "recovery_regex": pattern.get("recovery_regex", None),
                    }
        return None

    def _match_recovery(self, line: str, vendor: str) -> dict:
        """
        对一行日志进行恢复匹配
        检查是否匹配某个告警的恢复条件
        """
        vendors_to_check = [vendor] if vendor != "unknown" else list(self.patterns.keys())

        for v in vendors_to_check:
            for pattern in self.patterns.get(v, []):
                recovery_regex = pattern.get("recovery_regex")
                if recovery_regex and recovery_regex.search(line):
                    # 提取去重变量成分（用于匹配对应的活跃告警）
                    dedup_extra = ""
                    if pattern.get("dedup_extract"):
                        m = pattern["dedup_extract"].search(line)
                        if m:
                            dedup_extra = m.group(1) if m.lastindex else m.group(0)

                    return {
                        "pattern_name": pattern["name"],
                        "recovery_name": pattern.get("recovery_name", f"{pattern['name']}-恢复"),
                        "level": pattern["level"],
                        "vendor": v,
                        "dedup_extra": dedup_extra,
                    }
        return None

    def _should_notify(self, alert_key: str, level: str) -> bool:
        """
        告警去重检查

        逻辑：同一去重 key（设备+告警类型+可选变量成分）在 notify_window 内不重复通知。
        窗口过后如果仍在产生同类告警，会重新通知。

        去重 key 构成：
        - 无 dedup_extract: "设备:告警名称"（如 10.250.50.80:端口状态变更-Down）
        - 有 dedup_extract: "设备:告警名称:提取值"（如 10.250.50.80:端口状态变更-Down:GigabitEthernet0/1）

        notify_window 按告警级别配置：
        - critical: 1800s (30分钟)
        - warning:  3600s (1小时)
        - info:     7200s (2小时)
        """
        alert_levels = self.config.get("alert_levels", {})
        level_conf = alert_levels.get(level, {})
        # 优先使用 notify_window，其次 dedup_window（兼容旧配置），最后默认1小时
        window = level_conf.get("notify_window",
                 level_conf.get("dedup_window",
                 self.config.get("dingtalk", {}).get("notify_window", 3600)))

        now = time.time()
        last_time = self.alert_history.get(alert_key, 0)

        if now - last_time < window:
            logger.debug("告警去重窗口内跳过: %s (窗口 %ds, 已过 %ds)",
                        alert_key, window, int(now - last_time))
            return False

        self.alert_history[alert_key] = now
        return True

    def _process_alert(self, alert: dict):
        """处理一条告警：去重 -> 通知 -> AI 分析"""
        device = alert["device"]
        pattern_name = alert["pattern_name"]
        level = alert["level"]
        alert_key = f"{device}:{pattern_name}"
        # 如果有 dedup_extra（如端口号、IP地址），加入去重 key 以细化粒度
        dedup_extra = alert.get("dedup_extra", "")
        if dedup_extra:
            alert_key = f"{device}:{pattern_name}:{dedup_extra}"

        # 告警去重（时间窗口）
        if not self._should_notify(alert_key, level):
            # 即使不通知，也要更新活跃告警记录
            self.active_alerts[alert_key] = {
                "timestamp": time.time(),
                "pattern_name": pattern_name,
                "level": level,
            }
            return

        # 将告警标记为活跃状态
        self.active_alerts[alert_key] = {
            "timestamp": time.time(),
            "pattern_name": pattern_name,
            "level": level,
        }

        # 检查该级别是否需要钉钉通知和 AI 分析
        alert_levels = self.config.get("alert_levels", {})
        level_conf = alert_levels.get(level, {})
        should_notify = level_conf.get("dingtalk", True)
        should_analyze = level_conf.get("ai_analysis", True) and self.ai_enabled

        # AI 分析
        ai_result = ""
        if should_analyze:
            logger.info("AI 分析中: %s - %s", device, pattern_name)
            try:
                ai_result = self.analyzer.analyze(alert)
            except Exception as e:
                logger.error("AI 分析异常: %s", e)
                ai_result = f"⚠️ AI 分析失败: {str(e)}"

        # 钉钉通知
        if should_notify:
            logger.info("发送钉钉告警通知: %s - %s [%s]", device, pattern_name, level)
            try:
                self.notifier.send_alert(alert, ai_result)
            except Exception as e:
                logger.error("钉钉通知发送失败: %s", e)

        # 输出到控制台
        level_icons = {"critical": "🔴", "warning": "🟠", "info": "🟡"}
        icon = level_icons.get(level, "🟡")
        print(f"{icon} [{level.upper()}] {device} | {pattern_name} | {alert['raw_log'][:100]}")
        if ai_result:
            print(f"  🤖 AI: {ai_result[:100]}...")

    def _process_recovery(self, recovery: dict, raw_log: str):
        """
        处理恢复告警通知
        """
        device = recovery["device"]
        pattern_name = recovery["pattern_name"]
        recovery_name = recovery["recovery_name"]
        level = recovery["level"]
        alert_key = f"{device}:{pattern_name}"
        dedup_extra = recovery.get("dedup_extra", "")
        if dedup_extra:
            alert_key = f"{device}:{pattern_name}:{dedup_extra}"

        # 检查是否有对应的活跃告警
        if alert_key not in self.active_alerts:
            logger.debug("无对应的活跃告警，跳过恢复通知: %s", alert_key)
            return

        # 移除活跃告警记录
        del self.active_alerts[alert_key]

        # 发送恢复通知
        logger.info("发送恢复告警通知: %s - %s", device, recovery_name)
        try:
            self.notifier.send_recovery(device, recovery_name, pattern_name, level, raw_log)
        except Exception as e:
            logger.error("恢复通知发送失败: %s", e)

        # 输出到控制台
        print(f"✅ [RECOVERY] {device} | {recovery_name} | {raw_log[:100]}")

    def run_once(self):
        """执行一次巡检（扫描所有日志文件的新增内容）"""
        log_files = self._discover_log_files()
        alert_count = {"critical": 0, "warning": 0, "info": 0, "total": 0}
        device_alerts = {}

        for lf in log_files:
            device = lf["device"]
            vendor = lf["vendor"]
            log_path = lf["log_path"]

            new_lines = self._read_new_lines(log_path)
            if not new_lines:
                continue

            logger.debug("读取 %s 新增 %d 行", device, len(new_lines))

            for line in new_lines:
                if not line.strip():
                    continue

                # 先检查是否匹配恢复模式
                recovery_match = self._match_recovery(line, vendor)
                if recovery_match:
                    recovery = {
                        "device": device,
                        "vendor": recovery_match["vendor"],
                        "pattern_name": recovery_match["pattern_name"],
                        "recovery_name": recovery_match["recovery_name"],
                        "level": recovery_match["level"],
                        "dedup_extra": recovery_match["dedup_extra"],
                    }
                    self._process_recovery(recovery, line)
                    continue

                # 检查是否匹配告警模式
                match = self._match_alert(line, vendor)
                if match:
                    alert = {
                        "device": device,
                        "vendor": match["vendor"],
                        "pattern_name": match["pattern_name"],
                        "level": match["level"],
                        "description": match["description"],
                        "affected_components": match["affected_components"],
                        "quick_check": match["quick_check"],
                        "dedup_extra": match["dedup_extra"],
                        "raw_log": line,
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }

                    self._process_alert(alert)

                    level = match["level"]
                    alert_count[level] = alert_count.get(level, 0) + 1
                    alert_count["total"] = alert_count.get("total", 0) + 1
                    device_alerts[device] = device_alerts.get(device, 0) + 1

        # 保存状态
        self._save_state()

        return alert_count, device_alerts

    def run_daemon(self):
        """以守护进程模式持续运行"""
        poll_interval = self.config.get("syslog", {}).get("poll_interval", 5)
        self.running = True

        logger.info("交换机监控启动 (轮询间隔: %ds)", poll_interval)

        def stop_handler(signum, frame):
            logger.info("收到停止信号，正在退出...")
            self.running = False
            self._save_state()

        signal.signal(signal.SIGINT, stop_handler)
        signal.signal(signal.SIGTERM, stop_handler)

        # 首次启动时，从文件末尾开始
        log_files = self._discover_log_files()
        for lf in log_files:
            log_path = lf["log_path"]
            if log_path not in self.file_positions:
                try:
                    self.file_positions[log_path] = os.path.getsize(log_path)
                except OSError:
                    self.file_positions[log_path] = 0
        self._save_state()

        logger.info("初始状态已记录，开始实时监控...")

        # 获取去重窗口配置，用于启动通知
        notify_window_default = self.config.get("dingtalk", {}).get("notify_window", 3600)
        critical_window = self.config.get("alert_levels", {}).get("critical", {}).get("notify_window", 1800)

        self.notifier.send_text(
            f"✅ 网络设备智能巡检系统已启动\n"
            f"监控目录: {self.config.get('syslog', {}).get('base_dir', 'N/A')}\n"
            f"设备类型: 交换机(锐捷/华为) + 无线AC(锐捷) + 防火墙(AF) + SSL VPN + 超融合(SmartX)\n"
            f"IP映射设备: {len(self.ip_vendor_map)} 台\n"
            f"AI 分析: {'已启用' if self.ai_enabled else '未启用'}\n"
            f"轮询间隔: {poll_interval}s\n"
            f"告警去重: critical {critical_window}s / 其他 {notify_window_default}s"
        )

        while self.running:
            try:
                self.run_once()
            except Exception as e:
                logger.error("巡检循环异常: %s", e)

            time.sleep(poll_interval)

        logger.info("监控已停止")

    def run_scan(self):
        """执行一次性全量扫描（从文件开头读取所有内容）"""
        logger.info("开始全量日志扫描...")

        log_files = self._discover_log_files()
        # scan 模式：从文件开头（位置 0）开始读取
        for lf in log_files:
            self.file_positions[lf["log_path"]] = 0

        alert_count, device_alerts = self.run_once()

        if alert_count.get("total", 0) > 0:
            self.notifier.send_summary({
                **alert_count,
                "devices": device_alerts
            })

        # 扫描完成后，将位置更新到文件末尾，避免 daemon 模式重复读取
        for lf in log_files:
            try:
                self.file_positions[lf["log_path"]] = os.path.getsize(lf["log_path"])
            except OSError:
                pass
        self._save_state()

        logger.info("全量扫描完成: %s", json.dumps(alert_count, ensure_ascii=False))
        return alert_count, device_alerts


def main():
    parser = argparse.ArgumentParser(
        description="网络设备智能巡检告警系统"
    )
    parser.add_argument(
        "-c", "--config",
        default=os.path.join(SCRIPT_DIR, "config.yaml"),
        help="配置文件路径 (默认: config.yaml)"
    )
    parser.add_argument(
        "-m", "--mode",
        choices=["daemon", "scan", "test"],
        default="daemon",
        help="运行模式: daemon(持续监控), scan(一次性全量扫描), test(测试通知)"
    )
    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"❌ 配置文件不存在: {args.config}")
        print(f"   请复制 config.yaml 并修改其中的配置项")
        sys.exit(1)

    monitor = SwitchMonitor(args.config)

    if args.mode == "test":
        print("📤 发送测试通知...")
        test_alert = {
            "device": "test-core-sw01",
            "vendor": "ruijie",
            "pattern_name": "端口状态变更-Down",
            "level": "critical",
            "description": "测试告警 - 端口物理链路断开",
            "raw_log": "%LINK-3-UPDOWN: Interface GigabitEthernet0/1, changed state to down",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "affected_components": ["物理端口", "光模块"],
            "quick_check": "show interface status"
        }
        result = monitor.notifier.send_alert(test_alert, "🧪 这是一条测试消息，AI 分析功能正常。")
        if result.get("errcode") == 0:
            print("✅ 钉钉通知发送成功！")
        else:
            print(f"❌ 钉钉通知发送失败: {result}")

    elif args.mode == "scan":
        monitor.run_scan()

    else:
        monitor.run_daemon()


if __name__ == "__main__":
    main()
