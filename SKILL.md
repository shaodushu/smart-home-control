---
name: smart-home-control
description: 通过 Home Assistant 控制智能家居设备。当用户要求打开/关闭灯、调节亮度、设置空调温度、开合窗帘、执行场景、查询温度湿度等传感器数据时使用。支持客厅、卧室、厨房、书房等区域。也用于「配置智能家居」这类首次配置请求。
description_zh: 通过 Home Assistant 控制智能家居设备。当用户要求打开/关闭灯、调节亮度、设置空调温度、开合窗帘、执行场景、查询温度湿度等传感器数据时使用。支持客厅、卧室、厨房、书房等区域。也用于「配置智能家居」这类首次配置请求。
description_en: Control smart home devices through Home Assistant. Use when the user asks to turn lights on/off, adjust brightness, set thermostat temperature, open or close curtains, run a scene, or query sensor data such as temperature and humidity. Also handles first-time setup such as "configure my smart home".
display_name: 智能家居控制
display_name_en: Smart Home Control
version: 1.1.0
license: MIT
allowed-tools: Read,Bash
---

# 智能家居控制技能

通过 Home Assistant REST API 控制智能家居。用户用自然语言下指令，你解析意图、调用 `scripts/ha_client.py`、用中文反馈结果。

## 前置条件

**依赖 httpx**。脚本靠它发 HTTP 请求；报 `缺少依赖 httpx` 就是当前解释器没装：

```bash
python3 -c "import httpx" || python3 -m pip install httpx
```

注意 `python` / `python3` 可能指向不同解释器（如 mise、托管运行时），哪个报错就装到哪个。

**配置文件位置**：按顺序找，第一个存在的生效。`context` 会回显实际生效的路径（`config.path`）：

1. `HA_CONFIG_FILE` 环境变量 —— **显式设置即排他生效**，即使指向的文件不存在也不回退
2. `~/.config/smart-home-control/config.json` —— **推荐位置**
3. 技能目录下的 `config.json`（与 `SKILL.md` 同级，不是 `scripts/` 里）—— 仅兼容旧版本

`context` 报 `config.exists: false` 说明三处都没找到。想复用别处已有的配置，用环境变量指过去，省得复制一份令牌出来：

```bash
export HA_CONFIG_FILE=/path/to/config.json
```

> ⚠️ **重新导入技能会删掉配置。** 技能包里**故意不含** `config.json`（它含访问令牌和管理员密码，不能分发），而导入会覆盖整个技能目录。所以：
>
> - **升级/重装前先备份**：`cp ~/.config/smart-home-control/config.json /tmp/ha-config-backup.json`
> - **导入后若 `context` 报未配置**，把备份放回去再 `chmod 600`，或重新跑 `onboard` / `setup`
> - 这也是为什么推荐放 `~/.config/smart-home-control/`——在技能目录外，导入冲不掉

## 首次配置（用户说"配置智能家居"时）

**第一步永远是先看状态**，不要上来就让用户给你令牌：

```bash
python scripts/ha_client.py context
```

按返回的 `phase` 决定做什么：

| phase | 含义 | 你要做的 |
|---|---|---|
| `ready` | 全就绪 | 告诉用户可以直接下指令了 |
| `ha_running_uninitialized` | HA 在跑，但没初始化 | 见"自动完成初始化" |
| `ha_running_need_token` | HA 就绪，缺令牌 | 进入"拿令牌" |
| `token_invalid` | 令牌失效 | 让用户重新生成令牌，再走 setup |
| `no_ha` | 本地没有 HA | 见"安装 Home Assistant" |
| `configured_remote` | 配置指向外部 HA | 不用装，直接验证连通性 |

### 自动完成初始化（不需要用户开浏览器）

**一条命令，什么都别问用户**：

```bash
python scripts/ha_client.py onboard --generate-password
```

用户名、密码、时区、位置**全部自动**：

| 项 | 怎么来的 |
|---|---|
| 用户名 | 系统登录名（如 `baymax`），`--username` 可覆盖 |
| 密码 | 自动生成 20 位强密码，写进 `config.json` 和系统钥匙串 |
| 时区 | 从 HA 容器读，读不到就查宿主机 `/etc/localtime`。**别问用户** |
| 位置（家名） | 由时区换算出城市名（如 `Asia/Shanghai` → 上海），`--location` 可覆盖 |
| 界面语言 | `zh-Hans` |

想看探测到什么、或需要覆盖时，先跑这个（同样不打扰用户）：

```bash
python scripts/ha_client.py env
```

`onboard` 一步完成八件事：创建管理员账号 → 换取访问令牌 → 完成 onboarding 的 analytics / core_config / integration 三步 → 设置位置/语言/时区 → 创建长期令牌 → 写入配置 → 验证令牌可用。

**时区这件事特别重要**：不设就是 UTC，会导致自动化定时和历史的日期时间**全错**，而且是后面真正开始用才会发现的坑。所以现在默认自动探测——只有在 `env` 报 `time_zone_source` 为"无法探测，回退 UTC"时，才需要问用户。

**密码处理**：

| 方式 | 什么时候用 |
|---|---|
| `--generate-password` | **默认推荐**。用户全程不用参与 |
| `--password-env HA_ADMIN_PW` | 用户坚持要自己定密码时用（避免 `--password`——明文参数会进进程列表） |
| `--use-stored-password` | 重新初始化时直接用已存的 |

**必须主动告诉用户的三件事**：

1. 密码是明文存在 `config.json` 里的（用户要求的做法）。该文件权限 600、打包命令已排除，但**不要分享这个文件**
2. 需要登录 HA 网页时取回密码：`auth --username <用户名> --show`
3. 别把密码贴到对话里——一旦进了对话记录就留在那里了

**实现上的一个关键点**（改代码时别踩）：`integration` 步骤必须用 **OAuth 换来的 access token**，且必须在创建长期令牌**之前**调用。长期令牌没有 refresh_token 上下文，用它会得到 403 `Credentials for user not available`，实例不会被标记为已初始化。

如果 `onboard` 失败，**不要重试**，改为引导用户手动完成（打开 http://localhost:8123 建账号），因为失败后实例状态可能已部分改变。

### 安装 Home Assistant（本地没有时）

**这一步可以替用户做完**，不需要用户碰命令行：

```bash
python scripts/ha_client.py bootstrap --dry-run   # 先检查环境
python scripts/ha_client.py bootstrap             # 实际安装
```

`bootstrap` 会自动：检查 Docker 与内存 → 拉取镜像（ghcr.io 不通时回退 Docker Hub）→ 创建容器（`--restart unless-stopped`，配置目录 `~/.homeassistant`）→ 等待就绪 → 告诉用户下一步。

**装之前必须告诉用户这两件事**：

1. 这台机器**休眠或关机时 HA 就停了**，适合先跑起来验证；要长期用得迁到常开设备（NAS / 迷你主机 / 树莓派）
2. 首次镜像拉取约 1-2 GB，国内网络可能较慢

装完后用户仍需**自己在浏览器完成初始化**（创建管理员账号）——这一步没有 API 可以代做。

### 拿令牌 + 写入配置

1. 告诉用户怎么拿（**不要让用户自己编辑配置文件**）：
   > 打开 http://localhost:8123 → 左下角点你的用户名 → 安全 → 长期访问令牌 → 创建令牌 → 复制（只显示一次）
2. 用户给出地址和令牌后，**先用 `--dry-run` 验证**，成功再正式写入：
   ```bash
   python scripts/ha_client.py setup --url http://localhost:8123 --token <令牌> --dry-run
   python scripts/ha_client.py setup --url http://localhost:8123 --token <令牌>
   ```
3. 读返回的 JSON：`domains` 是他家实际有哪些设备，`sensitive_domains_present` 是会被二次确认保护的类别。**用中文复述这两项**，让他确认这就是他家。
4. 失败时按 `error` / `hint` 处理：`令牌无效` → 让用户核对令牌；`无法建立连接` → 确认 HA 在跑；自签证书的 https 地址会自动降级重试。

`setup` 会自行校验，不需要预先配置任何东西。若需覆盖默认安全策略（见下），引导用户直接说需求，你来编辑 `config.json` 的对应字段，不要手改脚本。

## 接入真实设备（小米/米家等）

HA 装好只是个空壳，**里面一台设备都没有**。用户说"接我的小米设备"时走这条流程。

### 1. 安装集成

```bash
python scripts/ha_client.py install-integration --name xiaomi_home
```

自动完成：从容器挂载读出真实配置目录 → 从 GitHub release 下载 → 校验含 `manifest.json` 并防目录穿越 → 解压到 `custom_components/` → 重启 HA → 等待就绪。

可选：`--name xiaomi_miot`（社区集成，覆盖更广，支持本地 token 模式）、`--container <容器名>`、`--dry-run`、`--force`。

**下载会自动回退**：release 资产存在 `objects.githubusercontent.com`（国内常被墙），失败时自动改用官方 tarball（走 `codeload.github.com`，一般能通）。返回的 `steps` 里会标明实际用了哪条路径。**遇到下载失败先看是不是网络问题，不要以为代码坏了。**

**两个集成怎么选**：

| | `xiaomi_home`（官方） | `xiaomi_miot`（社区） |
|---|---|---|
| 维护 | 小米官方 | 社区，更新频繁 |
| 覆盖 | 依赖官方 spec 适配 | 更广 |
| 本地控制 | 需**小米中枢网关**（固件 3.3.0_0023+） | 支持本地 miot 协议的设备，需 token |
| 建议 | 默认选它，官方通路更稳 | 官方接不进某些设备时用它 |

### 2. 用户必须自己做的一步

装完后**必须让用户自己在浏览器完成小米账号授权**——那是 OAuth 跳转，无法代做：

> 打开 http://localhost:8123 → 设置 → 设备与服务 → 添加集成 → 搜「xiaomi_home」→ 用小米账号登录授权

**不要跳过这步、不要假装能做到。** 授权完成后用户会告诉你。

### 3. 读出设备清单

```bash
python scripts/ha_client.py devices
```

返回按域归组的设备，每台带 `entity_id` / `friendly_name` / `state` / `area` / `controllable` / `capabilities`，以及 `sensitive_domains_present`（会被二次确认保护的类别）。已过滤掉 `zone`/`person`/`update`/`backup` 等 HA 内部实体。

**把这份清单用中文复述给用户确认**——这是"你家在 HA 里长什么样"的唯一事实来源。设备为空说明集成还没授权成功。

## 执行步骤

### 1. 解析用户意图

提取四项：区域（客厅/卧室）、设备类型（灯/空调/窗帘）、动作（开/关/调节）、参数（亮度%/温度/位置）。

### 2. 确定 entity_id

用户未给出 entity_id 时，先搜索：

```bash
python scripts/ha_client.py list --domain light --area 客厅 --query 灯
```

从返回 JSON 的 `entities` 数组中按 `friendly_name` 匹配。

- 唯一匹配 → 继续
- 多个匹配 → 列出候选，让用户选择
- 无匹配 → 告知未找到，并列出可用实体

### 3. 执行控制

```bash
# 开灯并调亮度
python scripts/ha_client.py call light turn_on light.living_room --data '{"brightness_pct":60}'

# 关灯
python scripts/ha_client.py call light turn_off light.living_room

# 设置空调
python scripts/ha_client.py call climate set_temperature climate.bedroom --data '{"temperature":24,"hvac_mode":"cool"}'

# 开合窗帘（敏感域，需二次确认）
python scripts/ha_client.py call cover set_cover_position cover.curtain --data '{"position":30}'

# 执行场景
python scripts/ha_client.py scene scene.movie_mode
```

### 4. 查询状态

```bash
python scripts/ha_client.py state sensor.living_room_temperature
python scripts/ha_client.py history sensor.living_room_temperature --hours 24
```

### 5. 发送通知

```bash
python scripts/ha_client.py notify notify.mobile_app_phone "晚安模式已执行"
```

## 处理返回

所有命令输出 JSON。

- `"ok": true` → 用简洁中文向用户复述结果，例如"已完成：客厅主灯已调到 60%。"
- `"ok": false` → 读取 `error` 字段，如实告知用户失败原因。**绝不假装成功。**
- `"requires_confirmation": true` → 这是敏感设备，**必须先把将要执行的操作和影响说清楚，得到用户明确同意后再执行一次**。用户没明确同意就不要再调。

## 安全模型

默认**放行普通设备**（灯、开关、空调、传感器、场景），只对危险类别设闸门。这样新加设备不用改配置。

| 层级 | 内容 | 行为 |
|---|---|---|
| 敏感域 | `lock` `cover` `alarm_control_panel` `garage_door` `vacuum` `water_heater` `siren` `valve` | 每次操作都要用户二次确认 |
| 黑名单 | `config.json` 的 `BLOCKED_ENTITIES` | 直接拒绝 |
| 白名单 | `config.json` 的 `ALLOWED_ENTITIES` | **留空 = 不启用**；填了则只放行名单内实体（最严模式） |

`cover` 涵盖窗帘——开合窗帘不可逆（比如人可能在窗边），所以也在确认范围内。

其他约束：

1. 不猜测 `HA_URL` 和 `HA_TOKEN`，未配置时提示用户运行配置流程。
2. `config.json` 含令牌，**不要读取其内容、不要输出到对话里、不要提交到 git**。
3. 同一实体 1 秒内不重复调用。
4. 失败时说明具体原因，不静默重试超过 2 次。

## 常见问题

**用户说"设备控制不了 / 找不到设备"** → 先跑 `list` 看实体是否真在 HA 里，再确认 `entity_id` 拼写。HA 里的区域（area）需要用户在 HA 后台配置过，否则 `--area` 过滤不到，此时改用 `--query` 按名字搜。

**用户说"手表没收到通知"** → 小米手表的通知来自手机系统通知经「小米运动健康」转发，需该 App 的应用通知列表里存在 Home Assistant（动态列表下先发一条通知才会出现）。

**用户说"摄像头看不了画面 / 听不到声音"** → 先分清是哪一侧，别把它们混成一件事：

- **音视频流**：小米官方集成只给摄像机暴露**控制类**实体（开关/侦测/巡航/自检按钮），**不提供 `camera` 实体、不提供音频输入**。跑 `list --domain camera`——返回空就说明 HA 里根本没有视频实体，Lovelace 卡片和"播放声音"都无从谈起。这是集成对该型号的能力边界，**不是故障**，别去翻 HA 日志找报错（那里什么都没有）。
- **扬声器**：属于另一个服务（`siid=6`），`piid=1` 是 **volume**、`piid=2` 是 **mute**（bool，`true` = 静音）。注意**小米 spec 的中文翻译本身就错位了**——`piid=1` 被译成"静音"、`piid=2` 被译成"音量"，集成忠实继承，于是 HA 里 `number.*_volume_p_6_1` 的友好名显示"扬声器 静音"、`switch.*_mute_p_6_2` 显示"扬声器 音量"。**认 `entity_id` 尾部的 `_p_6_1` / `_p_6_2`，不要信友好名。**

## 参考文档

遇到 climate、cover、media_player 等复杂域的详细服务参数时，读取 `references/ha_api_reference.md`。

**排查"设备到底有哪些能力"时，直接读小米 miot spec**（比猜实体名可靠）。集成把 spec 缓存在 HA 配置目录：

```bash
ls ~/.homeassistant/.storage/xiaomi_home/miot_specs/
# urn:miot-spec-v2:device:camera:0000A01C:chuangmi-039c01:2_zh-Hans.dict
```

在线版带 `comment` 中文注释、最权威：`https://miot-spec.org/miot-spec-v2/instance?type=<urn>`

⚠️ **读缓存文件的两个坑**：它虽然是 JSON，但**可能含非法 UTF-8 字节，且末尾拖着二进制尾巴**，直接 `json.load()` 一定失败。正确姿势：

```python
d, _ = json.JSONDecoder().raw_decode(open(p,'rb').read().decode('utf-8', errors='replace'))
```
