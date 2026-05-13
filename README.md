# 网络设备智能巡检告警系统

实时监控 rsyslog 按设备分目录存储的网络设备日志（锐捷/华为交换机、锐捷无线AC、深信服防火墙/SSL VPN、SmartX超融合平台），匹配告警后通过**钉钉 Webhook** 通知，并自动调用 **AI 进行根因分析和处理建议**。故障恢复后自动发送恢复通知并触发 **AI 复盘**，生成故障档案。

## 架构

```
┌──────────────────┐     ┌──────────────┐     ┌───────────────┐     ┌──────────────┐
│ 锐捷/华为交换机    │────▶│   rsyslog    │────▶│ switch-monitor│────▶│   钉钉通知    │
│ 锐捷无线AC        │     │ 按设备分目录  │     │   (本系统)     │     │  (Webhook)   │
│ 深信服AF防火墙     │     │  (IP目录名)  │     │               │     └──────────────┘
│ 深信服SSL VPN     │     └──────────────┘     │  告警匹配      │
│ SmartX超融合      │                          │  恢复检测      │     ┌──────────────┐
│ SmartX CloudTower│                          │  AI 分析       │────▶│  DeepSeek等  │
└──────────────────┘                          │  AI 复盘       │     │  大模型API    │
                                              │  故障归档      │     └──────────────┘
└──────────────────┘                          │  告警匹配      │
                                              │  恢复检测      │     ┌──────────────┐
                                              │  AI 分析       │────▶│  DeepSeek等  │
                                              │  AI 复盘       │     │  大模型API    │
                                              │  故障归档      │     └──────────────┘
                                              └───────────────┘           │
                                                                          ▼
                                                                   ┌──────────────┐
                                                                   │  故障档案     │
                                                                   │  (JSON归档)   │
                                                                   └──────────────┘
```

**Docker 部署架构**：

```
  192.168.40.198 (syslog)           192.168.40.17 (Docker 宿主机)       容器内
  ┌───────────────────┐              ┌──────────────────────┐           ┌─────────────────┐
  │ rsyslog           │    NFS       │ /mnt/syslog/         │  bind    │ /var/log/switches│
  │ /log/syslog/      │─────────────▶│  ├── core-sw01/      │─────────▶│  ├── core-sw01/  │
  │   ├── core-sw01/  │   自动挂载   │  ├── acc-sw02/       │  映射    │  ├── acc-sw02/   │
  │   ├── acc-sw02/   │              │  ├── AF-fw01/        │   :ro    │  ├── AF-fw01/    │
  │   ├── AF-fw01/    │              │  └── sslvpn01/       │          │  └── sslvpn01/   │
  │   └── sslvpn01/   │              └──────────────────────┘           │                 │
  └───────────────────┘                                                 │ switch-monitor  │
                                                                         │  告警匹配...    │
                                                                         └─────────────────┘
```

**数据流向**：网络设备 → rsyslog(198) → NFS → 宿主机挂载(17) → Docker bind mount → 容器

### ⚠️ 日志级别差异说明

| 设备类型 | 发送到 syslog 的日志级别 | 告警匹配策略 |
|:---|:---|:---|
| 锐捷/华为交换机 | 仅 level 4 (warning) 及以上 | 规则匹配所有收到的日志（本身已是告警级别） |
| 锐捷无线AC (WS6816) | 仅 level 4 (warning) 及以上 | 规则匹配所有收到的日志（本身已是告警级别） |
| 深信服 AF 防火墙 | 所有级别（debug→emerg） | **规则仅匹配告警级别事件**，普通信息/调试日志不会触发告警 |
| 深信服 SSL VPN | 所有级别（debug→emerg） | **规则仅匹配告警级别事件**，普通信息/调试日志不会触发告警 |
| SmartX HCI 节点 | 所有级别（debug→emerg） | **规则仅匹配告警级别事件**，普通信息/调试日志不会触发告警 |
| SmartX CloudTower | 所有级别（debug→emerg） | **规则仅匹配告警级别事件**，普通信息/调试日志不会触发告警 |

## 告警生命周期

```
  故障发生                     故障恢复
     │                           │
     ▼                           ▼
 ┌─────────┐  匹配告警正则   ┌──────────┐  匹配恢复正则
 │ syslog  │──────────────▶│ 告警触发  │──────────────▶  恢复触发
 │  日志行  │               └──────────┘
 └─────────┘                     │                           │
                                 ▼                           ▼
                          ┌──────────┐               ┌──────────┐
                          │钉钉告警通知│               │钉钉恢复通知│
                          │+ AI 初步分析│              │+ AI 复盘  │
                          └──────────┘               └──────────┘
                                 │                           │
                                 ▼                           ▼
                          ┌──────────┐               ┌──────────┐
                          │加入活跃告警│               │移出活跃告警│
                          │(不再重复通知)│              │(可再次告警)│
                          └──────────┘               │生成故障档案│
                                                      └──────────┘
```

**关键机制**：
- 故障发生 → 记入活跃告警，不再重复通知
- 故障恢复 → 移出活跃告警，发送恢复通知 + AI 复盘
- 恢复后同一故障再发生 → **会重新告警**（因为已从活跃列表移除）
- 活跃告警列表持久化，重启进程不丢失

## 目录结构

```
switch-monitor/
├── config.yaml                  # 主配置文件 ⬅️ 必须修改
├── monitor.py                   # 主监控脚本
├── notifier.py                  # 钉钉通知模块
├── analyzer.py                  # AI 告警分析模块
├── reviewer.py                  # AI 故障复盘模块
├── patterns/
│   ├── ruijie.yaml              # 锐捷交换机+无线AC日志匹配规则 (30条)
│   ├── huawei.yaml              # 华为交换机日志匹配规则 (23条)
│   ├── sangfor.yaml             # 深信服防火墙/SSL VPN匹配规则 (27条)
│   ├── smartx.yaml              # ⭐ SmartX超融合平台匹配规则 (23条)
│   └── custom.yaml              # ⭐ 自定义告警规则（所有设备生效）
├── Dockerfile                   # Docker 镜像构建
├── docker-compose.yml           # Docker Compose 部署配置
├── entrypoint.sh                # 容器入口脚本
├── requirements.txt             # Python 依赖
├── switch-monitor.service       # systemd 服务配置
└── README.md
```

---

## Docker 部署（推荐）

### 部署架构说明

| 角色 | IP | 关键路径 | 说明 |
|:---|:---|:---|:---|
| syslog 服务器 | 192.168.40.198 | `/log/syslog/` | rsyslog 接收网络设备日志，按设备分目录存储 |
| Docker 服务器 | 192.168.40.17 | `/mnt/syslog/` | 宿主机 NFS 挂载点，映射到容器 |

**挂载链路**：
```
192.168.40.198:/log/syslog  ──NFS──▶  192.168.40.17:/mnt/syslog  ──Docker bind──▶  容器:/var/log/switches
```

### 第一步：在 syslog 服务器 (192.168.40.198) 配置 NFS 导出

```bash
# 1. 安装 NFS 服务端
yum install -y nfs-utils    # CentOS/RHEL
# apt install -y nfs-kernel-server  # Ubuntu

# 2. 配置 NFS 导出（日志目录是 /log/syslog）
cat >> /etc/exports << 'EOF'
/log/syslog 192.168.40.17(rw,sync,no_subtree_check,no_root_squash)
EOF

# 3. 启动 NFS 并生效
systemctl enable --now nfs-server
exportfs -rav

# 4. 验证导出列表
showmount -e localhost
# 应输出: /log/syslog 192.168.40.17
```

### 第二步：在 Docker 服务器 (192.168.40.17) 挂载 NFS

```bash
# 1. 安装 NFS 客户端
yum install -y nfs-utils    # CentOS/RHEL
# apt install -y nfs-common  # Ubuntu

# 2. 创建挂载点
mkdir -p /mnt/syslog

# 3. 手动挂载测试
mount -t nfs 192.168.40.198:/log/syslog /mnt/syslog

# 4. 验证是否能看到设备目录
ls /mnt/syslog/
# 应输出类似: core-sw01/  acc-sw02/  AF-fw01/  sslvpn01/ ...

# 5. 确认无误后，配置开机自动挂载
cat >> /etc/fstab << 'EOF'
192.168.40.198:/log/syslog /mnt/syslog nfs defaults,_netdev 0 0
EOF

# 6. 卸载刚才的手动挂载，用 fstab 重新挂载验证
umount /mnt/syslog
mount -a
ls /mnt/syslog/
```

### 第三步：部署 Docker 容器

```bash
# 1. 上传项目文件到 Docker 服务器
cd /opt/switch-monitor

# 2. 修改配置文件
vim config.yaml
# ⬅️ 必须修改：dingtalk.webhook_url、ai.api_key
# ⬅️ syslog.base_dir 保持 "/var/log/switches" 不用改（这是容器内路径）
# ⬅️ 钉钉关键词建议改为"网络告警"或"告警"（兼容防火墙告警）

# 3. 构建镜像
docker compose build

# 4. 测试通知（验证钉钉配置，不需要 NFS 也能跑）
mkdir -p /mnt/syslog   # 确保挂载点存在，否则容器启动失败
docker compose run --rm switch-monitor test

# 5. 启动持续监控
docker compose up -d

# 6. 查看日志确认正常运行
docker compose logs -f switch-monitor
# 应看到: "加载 ruijie 模式 21 条", "加载 huawei 模式 22 条", "加载 sangfor 模式 26 条", "发现 N 个设备的日志文件"

# 7. 检查容器状态
docker compose ps
```

### 验证容器内能读到日志

```bash
# 进入容器检查
docker compose exec switch-monitor ls /var/log/switches/

# 应该能看到 192.168.40.198 上的设备目录
# core-sw01/  acc-sw02/  AF-fw01/  sslvpn01/ ...
```

### Docker 常用操作

```bash
# 重启（修改配置后）
docker compose restart switch-monitor

# 重新构建（修改代码后）
docker compose up -d --build

# 查看实时日志
docker compose logs -f switch-monitor

# 一次性全量扫描
docker compose run --rm switch-monitor scan

# 测试通知
docker compose run --rm switch-monitor test

# 停止
docker compose down

# 查看故障档案
docker compose exec switch-monitor ls /app/archive/
```

### Docker Compose 卷映射说明

| 容器路径 | 宿主机来源 | 说明 |
|:---|:---|:---|
| `/var/log/switches/` | `/mnt/syslog` (NFS 挂载自 192.168.40.198:/log/syslog) | syslog 日志目录（只读） |
| `/app/config.yaml` | `./config.yaml` | 主配置文件（修改后重启生效） |
| `/app/patterns/custom.yaml` | `./patterns/custom.yaml` | 自定义规则（修改后重启生效） |
| `/app/state/` | Docker Volume | 运行状态（文件位置、活跃告警） |
| `/app/archive/` | Docker Volume | 故障复盘档案 |
| `/app/logs/` | Docker Volume | 运行日志 |

---

## 非 Docker 部署（直接运行）

### 1. 安装依赖

```bash
pip3 install -r requirements.txt
```

### 2. 修改配置

编辑 `config.yaml`，**必须修改**以下项：

```yaml
# rsyslog 设备日志根目录
syslog:
  base_dir: "/var/log/switches"    # ⬅️ 改为你的实际路径

# 钉钉机器人
dingtalk:
  webhook_url: "https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN"  # ⬅️ 填入真实 token
  keyword: "网络告警"              # ⬅️ 建议改为"网络告警"（兼容防火墙告警），与钉钉机器人的安全设置一致
  secret: ""                        # ⬅️ 如开启加签，填入密钥

# AI 分析（推荐 DeepSeek，性价比高）
ai:
  enabled: true
  api_base: "https://api.deepseek.com/v1"
  api_key: "YOUR_API_KEY"           # ⬅️ 填入 API Key
  model: "deepseek-chat"
```

### 3. rsyslog 配置参考

确保 rsyslog 按设备分目录存储，在 `/etc/rsyslog.d/switches.conf` 中添加：

```bash
# 按网络设备主机名分目录存储（交换机、防火墙、VPN均适用）
$template SwitchLog,"/log/syslog/%HOSTNAME%/messages"
:fromhost-ip, startswith "10.0." ?SwitchLog
& stop
```

然后重启 rsyslog：
```bash
systemctl restart rsyslog
```

### 4. 测试通知

```bash
python3 monitor.py -m test
```

钉钉群应收到一条测试告警消息。

### 5. 启动持续监控

```bash
# 前台运行（调试用）
python3 monitor.py -m daemon

# 安装为 systemd 服务（推荐生产使用）
cp switch-monitor.service /etc/systemd/system/
cp -r . /opt/switch-monitor/
systemctl daemon-reload
systemctl enable --now switch-monitor
```

---

## 自定义告警规则

### 添加方式

自定义规则写入 `patterns/custom.yaml`，**对所有厂商设备生效**（锐捷、华为、深信服都会匹配）。

### 规则格式

```yaml
patterns:
  - name: "自定义告警名称"         # 必填，全局唯一，建议加"自定义-"前缀
    level: warning                 # 必填：critical / warning / info
    regex: "你的正则表达式"         # 必填，不区分大小写
    description: "告警描述"         # 必填
    affected_components:           # 选填，受影响组件
      - "组件A"
      - "组件B"
    quick_check: "show xxx"        # 选填，排查命令
    dedup_extract: "Interface (\\S+)"  # 选填，从日志中提取变量成分细化去重 key
    # 以下为恢复检测（选填）
    recovery_regex: "恢复正则"      # 匹配恢复日志的正则
    recovery_name: "恢复名称"       # 恢复通知标题
```

**dedup_extract 说明**：
- 可选字段，正则表达式，取第一个捕获组 `()` 的内容
- 用于从日志中提取变量成分（端口号、IP地址、磁盘号等）加入去重 key
- 避免不同端口/IP/磁盘的同类告警被误去重
- 例如 `"Interface ([^\\s,]+)"` 提取端口名，`"(\\d+\\.\\d+\\.\\d+\\.\\d+)"` 提取 IP 地址
- 不配置时，去重 key 为 `设备:告警名称`；配置后为 `设备:告警名称:提取值`

### 添加步骤

1. **编辑文件**：打开 `patterns/custom.yaml`
2. **添加规则**：在 `patterns:` 列表下方添加你的规则
3. **重启容器**：`docker compose restart switch-monitor`
4. **确认加载**：查看日志 `docker compose logs switch-monitor | grep "自定义"`

### 示例：添加 VPN 隧道告警

```yaml
patterns:
  - name: "自定义-VPN隧道断开"
    level: critical
    regex: "IPSEC-3-VPN_DOWN|IKE.*sa.*down|tunnel.*down|VPN.*disconnect"
    description: "VPN/IPSec隧道断开，远程站点连接中断"
    affected_components:
      - "VPN隧道"
      - "远程站点连通性"
    quick_check: "show crypto ipsec sa"
    recovery_regex: "IPSEC-6-VPN_UP|IKE.*sa.*up|tunnel.*up|VPN.*established"
    recovery_name: "VPN隧道恢复"
```

### 示例：添加认证服务器不可达告警

```yaml
patterns:
  - name: "自定义-认证服务器不可达"
    level: critical
    regex: "RADIUS-3-NORESPONSE|TACACS\\+.*timeout|AAA.*server.*unreachable|authentication.*timeout"
    description: "认证服务器(RADIUS/TACACS+)不可达，可能导致无法登录"
    affected_components:
      - "AAA认证"
      - "管理平面"
    quick_check: "show aaa server"
    recovery_regex: "RADIUS-6-RESPONSE|TACACS\\+.*alive|AAA.*server.*reachable"
    recovery_name: "认证服务器恢复"
```

### 正则编写注意事项

| 要点 | 说明 |
|:---|:---|
| 大小写 | 已内置 `re.IGNORECASE`，无需考虑大小写 |
| 多条件 | 用 `\|` 分隔（正则"或"语义） |
| 特殊字符 | `.` → `\\.`、`*` → `\\*`、`(` → `\\(`、`)` → `\\)`、`+` → `\\+` |
| name 唯一 | 不能和 ruijie.yaml / huawei.yaml / sangfor.yaml 中的重名 |
| 命名规范 | 建议加 "自定义-" 前缀，便于区分 |
| YAML 转义 | 正则中的 `\` 在 YAML 中需写成 `\\`，如 `\d+` → `\\d+` |

### 修改已有厂商规则

如果需要修改已有厂商的内置规则，直接编辑对应文件：
- `patterns/ruijie.yaml` — 锐捷交换机规则
- `patterns/huawei.yaml` — 华为交换机规则
- `patterns/sangfor.yaml` — 深信服防火墙/SSL VPN规则

修改后重启容器即可生效。

---

## 告警规则说明

### 告警去重机制

系统采用**时间窗口去重**策略：同一去重 key 在窗口期内不重复通知，窗口过后可再次通知。

**去重 key 构成**：
- 无 `dedup_extract`：`设备:告警名称`（如 `10.250.50.80:端口状态变更-Down`）
- 有 `dedup_extract`：`设备:告警名称:提取值`（如 `10.250.50.80:端口状态变更-Down:GigabitEthernet0/1`）

`dedup_extract` 是 pattern 规则中的可选字段，通过正则从日志中提取变量成分（如端口号、IP地址、磁盘号等），使不同端口/IP的同类告警各自独立去重，避免误去重。

**示例**：同一台交换机 Gi0/1 和 Gi0/2 同时 Down，由于 `dedup_extract: "Interface ([^\s,]+)"` 提取了端口号，两条告警的去重 key 分别为：
- `10.250.50.80:端口状态变更-Down:GigabitEthernet0/1`
- `10.250.50.80:端口状态变更-Down:GigabitEthernet0/2`

两者互不影响，都会正常发送通知。

**去重窗口按告警级别配置**：

| 级别 | 图标 | 钉钉通知 | AI 分析 | 去重窗口 |
|:---:|:---:|:---:|:---:|:---:|
| critical | 🔴 | ✅ | ✅ | 1800s (30分钟) |
| warning | 🟠 | ✅ | ✅ | 3600s (1小时) |
| info | 🟡 | ❌ | ❌ | 7200s (2小时) |

### 覆盖的告警类型

**锐捷交换机 + 无线AC** (ruijie.yaml，30条)：
- 🔴 端口Down、链路协议Down、STP-BPDU异常、环路检测、电源/风扇故障、VSU分裂、VSU链路断开、光模块故障、AP离线、CAPWAP隧道断开、射频故障
- 🟠 CPU/内存高、MAC漂移、FDB溢出、Err-Disable、温度、OSPF/BGP邻居断开、光模块信号异常、光模块温度过高、射频干扰、AC许可证不足、AP固件升级失败、无线用户认证失败
- 🟡 配置变更、登录成功/失败、ACL命中

**华为交换机** (huawei.yaml，22条)：
- 🔴 端口Down、链路协议Down、STP拓扑变更、环路/MAC漂移、电源/风扇故障、堆叠分裂、光模块故障
- 🟠 CPU/内存高、ARP攻击、Err-Down、温度、OSPF/BGP邻居断开、VRRP主备切换、光模块信号异常、光模块温度过高
- 🟡 配置变更、登录成功/失败、ACL命中

**深信服防火墙 AF** (sangfor.yaml，23条)：
- 🔴 入侵攻击检测、病毒检测、漏洞利用检测、僵尸网络/DNS隧道、暴力破解、HA主备切换、接口Down、电源/风扇故障
- 🟠 CPU/内存高、会话数超限、磁盘空间不足、温度过高、License即将到期、特征库过期、策略拒绝/阻断、DOS/DDOS攻击、异常流量/黑名单
- 🟡 配置变更、管理员登录

**深信服 SSL VPN** (sangfor.yaml，与AF共享，3条独有)：
- 🔴 VPN隧道断开、VPN服务异常
- 🟠 VPN认证失败、VPN并发用户超限、VPN资源异常
- 🟡 VPN用户登录成功

**自定义规则** (custom.yaml)：
- 由运维人员自行添加，对所有设备生效

**SmartX 超融合平台** (smartx.yaml，23条)：
- 🔴 硬盘I/O错误(不健康盘)、磁盘下线(故障盘隔离)、存储副本降级、数据恢复/重建、软件RAID故障、节点故障、虚拟机HA故障切换、内存ECC故障
- 🟠 硬盘亚健康(慢盘)、SMART自检不通过、SSD寿命不足、I/O阻塞超时、CPU高负载、内存高使用、存储空间不足、ZBS集群异常、网络链路异常、数据巡检异常(静默损坏)、HBA/RAID卡异常、CloudTower异常
- 🟡 虚拟机状态变更、集群配置变更、管理员操作

### 故障恢复检测

每种告警都可以配置对应的恢复正则（`recovery_regex`），当匹配到恢复日志时：

1. 从活跃告警列表中移除该告警
2. 发送钉钉恢复通知（含故障持续时长）
3. 触发 AI 复盘（仅 critical/warning 级别）
4. 故障恢复后，同一故障再发生会重新告警

已配置恢复检测的告警：

| 告警 | 锐捷恢复 | 华为恢复 | 深信服恢复 | SmartX恢复 |
|:---|:---|:---|:---|:---|
| 端口/接口Down | LINK-3-UPDOWN...state to up | IFNET/4/LINK_STATE...UP | 接口.*up/接口.*恢复 | - |
| 链路协议Down | LINEPROTO-5-UPDOWN...state to up | IFNET/5/LINK_PRO...UP | - | - |
| 电源异常 | POWER-6-PSOK | DEVM/6/POWERRECOVER | 电源.*恢复/电源.*正常 | - |
| 风扇故障 | FAN-6-FANOK | DEVM/6/FANRECOVER | 风扇.*恢复/风扇.*正常 | - |
| CPU/内存高 | CPU/MEM-6-NORMAL | DEVM/6/CPURECOVER/MEMRECOVER | CPU/内存.*恢复/正常 | CPU/内存.*恢复/normal |
| 温度告警 | TEMP-NORMAL | DEVM/6/TEMP_RECOVER | 温度.*正常/TEMP.*RECOVER | - |
| OSPF邻居断开 | OSPF-5-ADJCHG...Full | OSPF/5/NBR_UP | - | - |
| BGP邻居断开 | BGP-5-ADJCHANGE...Up | BGP/5/PEER_UP | - | - |
| VSU/堆叠分裂 | VSU-6-VSU_MERGE | HA/6/STACK_RECOVER | - | - |
| VSU链路断开 | VSU-6-VSL_UP | - | - | - |
| 光模块故障 | TRANSCEIVER-6-OK | DEVM/6/TRANSCEIVER_RECOVER | - | - |
| 光功率异常 | TRANSCEIVER-6-POWER_NORMAL | FSP/6/RX_PWR_NORMAL | - | - |
| HA主备切换 | - | - | HA.*恢复/HA.*稳定 | - |
| VPN隧道断开 | - | - | VPN.*隧道.*建立/VPN.*恢复 | - |
| VPN服务异常 | - | - | VPN.*服务.*恢复/启动 | - |
| VPN并发超限 | - | - | VPN.*并发.*恢复 | - |
| License到期 | - | - | License.*续期/更新 | 许可证.*充足 |
| 特征库过期 | - | - | 特征库.*更新.*成功 | - |
| AP离线 | AP.*online/AP.*在线 | - | - | - |
| CAPWAP隧道断开 | CAPWAP.*up/CAPWAP.*建立 | - | - | - |
| 硬盘I/O错误 | - | - | - | disk.*recovered/I/O.*clear |
| 磁盘下线 | - | - | - | disk.*mounted/盘.*挂载 |
| 存储副本降级 | - | - | - | replica.*recover/副本.*恢复 |
| 软件RAID故障 | - | - | - | md.*recover/RAID.*恢复 |
| 节点故障 | - | - | - | node.*online/节点.*恢复 |
| 虚拟机HA切换 | - | - | - | vm.*running/VM.*started |
| ZBS集群异常 | - | - | - | zbs.*normal/ZBS.*恢复 |

## 设备厂商识别

系统支持两种方式识别设备厂商（优先级从高到低）：

### 1. IP 精确匹配（推荐 — 目录名是 IP 时使用）

当 rsyslog 的设备目录名使用 IP 地址时，在 `config.yaml` 中配置 `match_ips`：

```yaml
devices:
  - vendor: ruijie
    match_keywords: ["ruijie", "rj", "RG-", "S5"]
    match_ips:
      # 无线AC
      - "10.250.50.20"               # 锐捷无线AC (WS6816)
      # VSU堆叠 - 楼层汇聚交换机
      - "10.250.50.50"               # 楼层汇聚 VSU堆叠组1
      - "10.250.50.60"               # 楼层汇聚 VSU堆叠组2
      - "10.250.50.70"               # 楼层汇聚 VSU堆叠组3
      - "10.250.50.80"               # 楼层汇聚 VSU堆叠组4
      - "10.250.50.90"               # 楼层汇聚 VSU堆叠组5
      - "10.250.50.100"              # 楼层汇聚 VSU堆叠组6
      # VSU堆叠 - 服务器交换机
      - "10.250.50.151"              # 服务器交换机 VSU堆叠
      - "10.250.50.153"              # 服务器交换机 VSU堆叠
      - "10.250.50.155"              # 服务器交换机 VSU堆叠
      # 单台接入交换机（38台，完整列表见 config.yaml）
      - "10.250.50.51"               # 接入交换机
    pattern_file: "ruijie.yaml"

  - vendor: huawei
    match_keywords: ["huawei", "hw", "CE", "S9", "S7", "S5700", "S67"]
    match_ips:
      - "10.250.50.152"              # 华为堆叠交换机
      - "10.250.50.154"              # 华为堆叠交换机
    pattern_file: "huawei.yaml"

  - vendor: sangfor
    match_keywords: ["sangfor", "AF", "af-", "ssl-vpn", "vpn-", "SSLVPN", "sxf"]
    match_ips:
      - "10.250.55.21"               # AF 防火墙节点1
      - "10.250.55.22"               # AF 防火墙节点2
      - "10.250.50.42"               # SSL VPN 节点1
      - "192.168.50.43"              # SSL VPN 节点2
      - "192.168.50.41"              # SSL VPN 集群虚拟IP
    pattern_file: "sangfor.yaml"

  - vendor: smartx
    match_keywords: ["smartx", "smtx", "SMTX", "cloudtower", "zbs"]
    match_ips:
      - "192.168.30.111"             # HCI 节点1
      - "192.168.30.112"             # HCI 节点2
      - "192.168.30.113"             # HCI 节点3
      - "192.168.30.200"             # CloudTower
    pattern_file: "smartx.yaml"
```

**工作原理**：系统发现目录 `10.250.50.20/` → 在 `match_ips` 中查到该 IP 属于 `ruijie` → 使用 `ruijie.yaml` 规则匹配。

### 设备清单总览

| 厂商 | 设备类型 | 数量 | IP 网段 | 说明 |
|:---|:---|:---:|:---|:---|
| **锐捷** | 无线AC (WS6816) | 1 | 10.250.50.20 | AC_RGOS 11.9(2)B2P11 |
| **锐捷** | 楼层汇聚交换机(VSU堆叠) | 6 | 10.250.50.50/60/70/80/90/100 | 每组VSU堆叠 |
| **锐捷** | 服务器交换机(VSU堆叠) | 3 | 10.250.50.151/153/155 | 服务器区VSU堆叠 |
| **锐捷** | 单台接入交换机 | 38 | 10.250.50.51-57/61-67/71-74/76-78/81-82/84-86/91-96/101-106 | 楼层接入 |
| **华为** | 堆叠交换机 | 2 | 10.250.50.152/154 | 华为堆叠 |
| **深信服** | AF 防火墙(集群) | 2+1VIP | 10.250.55.21/22, 192.168.50.41(VIP) | AF 8.0.95 |
| **深信服** | SSL VPN(集群) | 2+1VIP | 10.250.50.42/192.168.50.43, 192.168.50.41(VIP) | SSL M7.6.8 R2 |
| **SmartX** | HCI 节点 | 3 | 192.168.30.111/112/113 | SMTX OS 6.2.0 |
| **SmartX** | CloudTower | 1 | 192.168.30.200 | 管理平台 |
| | **合计** | **59** | | |

### 锐捷接入交换机完整IP列表

```
10.250.50.51  10.250.50.52  10.250.50.53  10.250.50.54  10.250.50.55  10.250.50.56  10.250.50.57
10.250.50.61  10.250.50.62  10.250.50.63  10.250.50.64  10.250.50.65  10.250.50.66  10.250.50.67
10.250.50.71  10.250.50.72  10.250.50.73  10.250.50.74                10.250.50.76  10.250.50.77  10.250.50.78
10.250.50.81  10.250.50.82                10.250.50.84  10.250.50.85  10.250.50.86
10.250.50.91  10.250.50.92  10.250.50.93  10.250.50.94  10.250.50.95  10.250.50.96
10.250.50.101 10.250.50.102 10.250.50.103 10.250.50.104 10.250.50.105 10.250.50.106
```
> 注：.75、.83 等编号不在列表中，说明该位置无设备

### 2. 关键字模糊匹配（目录名含厂商关键字时使用）

当 rsyslog 的设备目录名使用有意义的名称时，系统通过目录名中的关键字自动识别厂商：

```yaml
devices:
  - vendor: ruijie
    match_keywords: ["ruijie", "rj", "RG-", "S5"]          # 目录名含这些关键字 -> 锐捷交换机
  - vendor: huawei
    match_keywords: ["huawei", "hw", "CE", "S9", "S7"]      # 目录名含这些关键字 -> 华为交换机
  - vendor: sangfor
    match_keywords: ["sangfor", "AF", "af-", "ssl-vpn",     # 目录名含这些关键字 -> 深信服设备
                     "vpn-", "SSLVPN", "sxf"]
```

**设备目录命名建议**（在 rsyslog 中配置主机名时参考）：

| 设备类型 | 建议目录名 | 匹配到的厂商 |
|:---|:---|:---|
| 锐捷核心/汇聚交换机 | `rj-core-sw01` 或 `10.250.50.50` | ruijie |
| 锐捷接入交换机 | `rj-acc-sw02` 或 `10.250.50.51` | ruijie |
| 华为堆叠交换机 | `hw-stack-01` 或 `10.250.50.152` | huawei |
| 深信服防火墙 | `AF-fw01` 或 `sangfor-fw01` | sangfor |
| 深信服SSL VPN | `ssl-vpn01` 或 `SSLVPN-01` | sangfor |

> 本环境使用 IP 目录名，所有设备已配置 `match_ips`，无需依赖关键字匹配。

### 3. 未匹配到任何厂商

如果目录名既不在 `match_ips` 中，也不包含任何关键字，系统会对该设备的日志**同时应用所有厂商的匹配规则 + 自定义规则**。

### IP 目录名的 rsyslog 配置参考

当目录名是 IP 地址时，rsyslog 通常使用 `%fromhost-ip%` 模板：

```bash
# /etc/rsyslog.d/network-devices.conf
# 按发送者IP分目录（目录名就是IP地址）
$template IpLog,"/log/syslog/%fromhost-ip%/messages"
:fromhost-ip, startswith "10.250.50." ?IpLog
:fromhost-ip, startswith "10.250.55." ?IpLog
:fromhost-ip, startswith "192.168.30." ?IpLog
:fromhost-ip, startswith "192.168.50." ?IpLog
& stop
```

> **建议**：如果使用 IP 目录名，务必在 `config.yaml` 的 `match_ips` 中配置所有设备的 IP，否则系统无法识别厂商，会对每条日志应用所有厂商规则，增加误匹配风险。
>
> **注意**：关键字匹配是子串包含（如 `ce` 会匹配到含 `ce` 的任何目录名），IP 目录名下建议全部走 `match_ips` 精确匹配，避免关键字误匹配。本环境已将全部 59 台设备 IP 配置到 `match_ips`。

## 深信服设备日志适配说明

### 日志级别差异处理

深信服 AF 防火墙（版本 8.0.95）和 SSL VPN（版本 SSL M7.6.8 R2）默认向 syslog 发送**所有级别**的日志，包括：
- 正常的访问记录、会话日志
- 管理员操作记录
- 安全检测通过的正常流量
- 调试/诊断信息

本系统的 `sangfor.yaml` 规则已经做了**级别过滤**——只有符合告警特征的关键词（入侵检测、病毒、HA切换、接口Down、VPN隧道断开等）才会触发告警。大量普通信息日志不会触发任何告警通知。

### 如果日志量过大

防火墙和 SSL VPN 的日志量远大于交换机，如果出现以下情况，可以优化：

1. **在深信服设备侧配置日志过滤**：
   - AF：`系统 → 系统配置 → 日志设置 → 日志功能开启` 中取消勾选不需要的日志类型
   - SSL VPN：`系统设置 → 日志配置` 中设置最小日志级别为 `warning` 或 `error`
   - 这样可以减少发送到 syslog 服务器的日志量，降低网络和存储开销

2. **调整 poll_interval**：如果日志量特别大，可以在 `config.yaml` 中将轮询间隔从 5 秒调整为 3 秒，确保及时处理

3. **调整 max_lines_per_read**：如果单次读取经常超限（500行），可以适当增大此值

## AI 复盘

故障恢复后，系统自动调用 AI 对故障进行复盘，生成结构化的故障档案：

**复盘内容**：
1. 故障概要
2. 根因分析（直接原因 + 深层原因）
3. 时间线（发现→处理→恢复）
4. 影响面评估（受影响设备/端口/业务/用户/持续时间）
5. 处理过程
6. 改进建议（可执行、可验证）

**档案存储**：容器内 `/app/archive/`，持久化到 Docker Volume，JSON 格式

**复盘推送**：完成后自动通过钉钉推送复盘报告卡片

## 故障档案查询

```bash
# 进入容器
docker compose exec switch-monitor bash

# 查看所有档案
ls /app/archive/

# 查看某个档案
cat /app/archive/core-sw01_光模块故障_2026-05-09_14-32-15.json
```

或通过 Python：

```python
from reviewer import FaultReviewer

reviewer = FaultReviewer(api_base="...", api_key="...", model="deepseek-chat")

# 查询所有故障档案
archives = reviewer.list_archives()

# 按设备过滤
archives = reviewer.list_archives(device="core-sw01")

# 读取完整档案
detail = reviewer.get_archive("core-sw01_端口状态变更-Down_2026-05-09_14-32-15.json")
```

## 运行模式

| 模式 | 命令 | 说明 |
|:---:|:---|:---|
| daemon | `docker compose up -d` 或 `python3 monitor.py -m daemon` | 持续监控，实时告警（推荐生产使用） |
| scan | `docker compose run --rm switch-monitor scan` | 一次性全量扫描已有日志 |
| test | `docker compose run --rm switch-monitor test` | 发送测试通知，验证钉钉配置 |

## 钉钉消息示例

### 告警通知（交换机）

```
🔴 紧急 网络告警

设备: core-sw01-ruijie
告警: 光模块故障
描述: 光模块故障或不兼容，端口将不可用
时间: 2026-05-09 14:32:15

> %TRANSCEIVER-3-TRANSCEIVER_FAIL: Transceiver on Gi0/1 failed

排查命令: show interface transceiver | include Fail

---
🤖 AI 分析

**根因**: Gi0/1 光模块硬件故障，可能原因：
1. 光模块达到使用寿命
2. 光模块与设备不兼容
3. 光模块金手指氧化

**处理步骤**:
1. `show interface transceiver` 查看光模块状态
2. 清洁光模块金手指后重新插入
3. 如仍报错，更换同型号光模块

**紧急程度**: 🔴 紧急 - 端口已断开业务
```

### 告警通知（防火墙）

```
🔴 紧急 网络告警

设备: AF-fw01
告警: 入侵攻击检测
描述: 检测到入侵攻击行为，可能存在安全威胁
时间: 2026-05-11 10:32:15

> [攻击日志] 检测到SQL注入攻击 源IP=10.0.1.100 目的IP=192.168.1.10

排查命令: AF控制台 → 攻击日志 → 查看攻击详情

---
🤖 AI 分析

**根因**: 外部IP 10.0.1.100 对Web服务器发起SQL注入攻击
1. Web应用未做输入参数过滤
2. 防火墙IPS规则已拦截该攻击

**处理步骤**:
1. AF控制台确认攻击已拦截
2. 检查Web服务器是否已存在注入漏洞
3. 加固Web应用输入参数校验

**紧急程度**: 🟠 较高 - 攻击已拦截，但需排查是否已有渗透
```

### 告警通知（SSL VPN）

```
🔴 紧急 网络告警

设备: sslvpn01
告警: VPN隧道断开
描述: SSL VPN隧道断开，远程用户无法访问内网资源
时间: 2026-05-11 09:15:30

> [VPN隧道日志] VPN隧道断开 隧道ID=tun-001 用户=user01

排查命令: SSL VPN控制台 → VPN隧道状态

---
🤖 AI 分析

**根因**: VPN隧道异常断开，可能原因：
1. 客户端网络不稳定
2. VPN服务器资源不足
3. 证书或认证过期

**处理步骤**:
1. 检查VPN服务器运行状态和资源使用率
2. 确认客户端网络是否正常
3. 检查VPN证书有效期

**紧急程度**: 🔴 紧急 - 远程用户无法办公
```

### 恢复通知

```
✅ 故障恢复通知

设备: core-sw01-ruijie
故障类型: 光模块故障
恢复事件: 光模块恢复正常
故障发生: 2026-05-09 14:32:15
故障恢复: 2026-05-09 15:18:42
持续时长: 0小时46分钟

> %TRANSCEIVER-6-TRANSCEIVER_OK: Transceiver on Gi0/1 recovered

---
🤖 AI 复盘进行中，稍后推送...
```

### AI 复盘报告

```
🤖 AI 故障复盘报告

设备: core-sw01-ruijie
故障类型: 光模块故障
持续时长: 0小时46分钟

---
📋 故障概要: 核心交换机Gi0/1光模块硬件故障导致端口中断46分钟

🔍 根因分析:
- 直接原因: 光模块SFP-10G-SR内部电路异常
- 深层原因: 该批次光模块已运行3年，进入故障高发期

💥 影响面评估:
- 受影响设备: core-sw01
- 受影响端口: Gi0/1（上联核心交换机）
- 受影响业务: 3楼办公区约120用户网络中断

🔧 改进建议:
- 对同批次光模块进行预防性更换
- 建立光模块使用年限台账
- 增加光功率监控告警阈值
```
