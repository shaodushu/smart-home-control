# smart-home-control

## 1. 用途

通过 Home Assistant REST API，用自然语言控制智能家居设备（灯、空调、窗帘、场景、传感器查询与通知）。

## 2. 依赖

- Python 3.10+
- `httpx`（`pip install httpx`）
- Home Assistant 长期访问令牌（HA 后台 → 点用户名 → 安全 → 长期访问令牌 → 创建令牌）

## 3. 配置（从零到可用）

### 3.1 先看自己在哪一步

```bash
python scripts/ha_client.py context
```

返回 `phase`，据此决定下一步：

| phase | 含义 | 下一步 |
|---|---|---|
| `ready` | 全就绪 | 直接用 |
| `ha_running_uninitialized` | HA 在跑，没初始化 | 打开 http://localhost:8123 建管理员账号 |
| `ha_running_need_token` | HA 就绪，缺令牌 | 生成令牌后 setup |
| `token_invalid` | 令牌失效 | 重新生成令牌 |
| `no_ha` | 本地没 HA | 跑 `bootstrap` |
| `configured_remote` | 配置指向外部 HA | 不用装 |

### 3.2 本地没有 Home Assistant 时：装一个

```bash
python scripts/ha_client.py bootstrap --dry-run   # 只检查环境，不安装
python scripts/ha_client.py bootstrap             # 实际安装
```

`bootstrap` 自动完成：检查 Docker 与可用内存 → 拉取镜像（`ghcr.io` 不通时自动回退 Docker Hub）→ 创建并启动容器 → 等待就绪 → 输出下一步。

| 项 | 值 |
|---|---|
| 镜像 | `ghcr.io/home-assistant/home-assistant:stable` |
| 容器名 | `homeassistant` |
| 配置目录 | `~/.homeassistant`（迁移时拷这个目录即可） |
| 重启策略 | `unless-stopped`（随 Docker 自动启动） |
| 端口 | 8123（`--network host`） |
| 时区 | `Asia/Shanghai` |

**两个必须知道的限制**：

1. **这台机器休眠或关机时 Home Assistant 就停了。** Mac 版适合先跑起来验证、把设备接进来试；要长期常驻得迁到 NAS / 迷你主机 / 树莓派。
2. **首次启动后必须有人在浏览器完成初始化**（创建管理员账号）。这一步没有 API 可以代做。

常用运维命令：

```bash
docker logs -f homeassistant        # 看日志
docker stop homeassistant           # 停止
docker start homeassistant          # 启动
docker stats --no-stream homeassistant   # 看资源占用
```

### 3.3 自动完成初始化（可选，省掉手动建账号）

```bash
python scripts/ha_client.py onboard --generate-password
```

**不用提供任何参数**——用户名、密码、时区、位置全部自动：

| 项 | 来源 |
|---|---|
| 用户名 | 系统登录名（`--username` 可覆盖） |
| 密码 | 自动生成 20 位强密码，写入 `config.json` + 系统钥匙串 |
| 时区 | HA 容器 → 宿主机 `/etc/localtime` → 回退 UTC |
| 位置（家名） | 由时区换算的城市名（`--location` 可覆盖） |
| 界面语言 | `zh-Hans` |

想先看探测到什么：

```bash
python scripts/ha_client.py env
```

一步完成：创建管理员账号 → 换取访问令牌 → 完成 onboarding 剩余三步 → 设置位置/语言/时区 → 创建长期令牌 → 写入配置 → 验证令牌可用。

用的是 Home Assistant 前端自己在用的内部接口（`/api/onboarding/*`、`/auth/token`、WS `auth/long_lived_access_token`、WS `config/core/update`），**不在官方 API 文档里、无兼容性保证**，已在 HA 2026.9.2 上端到端实测通过。失败时会明确告知，可回退到手动初始化。

onboarding 共四步（源码 `components/onboarding/const.py` 的 `STEPS`），全部可自动完成：

| 步骤 | 能否自动 | 说明 |
|---|---|---|
| `user` | ✅ | 建管理员账号 |
| `analytics` | ✅ | 空 POST |
| `core_config` | ✅ | 空 POST；顺带创建 google_translate / met / radio_browser / shopping_list 集成 |
| `integration` | ✅ | **必须用 OAuth access token，且必须在创建长期令牌之前** |

> ⚠️ `integration` 那步如果用长期令牌会返回 403 `Credentials for user not available`——长期令牌没有 refresh_token 上下文。此时实例不会被标记为完成初始化（网页仍弹初始化向导）。脚本已按正确顺序调用，改代码时别调换。

**时区特别重要**：不设就是 UTC，会导致自动化定时和历史的日期时间**全错**（而且是开始真正用时才会发现的坑）。所以默认自动探测，只有探测失败才需要手动 `--time-zone`。

### 密码怎么处理

| 方式 | 说明 |
|---|---|
| `--generate-password` | **推荐**。自动生成 20 位强密码，用户全程不用参与 |
| `--password-env VAR` | 用户要自己定密码时用（避免 `--password`，明文参数会进进程列表） |
| `--use-stored-password` | 重新初始化时直接用已存的 |

生成的密码字符集为字母 + 数字 + `@%-_=+`，**不含任何在 shell / JSON / 表单里需要转义的字符**（无引号、反斜杠、`$`、反引号、`*`、`^`、空格，也不含 `!` `&` `#`）——粘到哪里都不会出岔子。20 位约 122 位熵。

密码存在两处：

- `config.json` 的 `HA_PASSWORD` 字段（**明文**，权限 600）
- 系统钥匙串（macOS，服务名 `smart-home-control`）

> ⚠️ `config.json` 里是明文密码。该文件权限 600、打包命令已排除它，但**不要分享、不要提交 git**。需要登录 HA 网页时取回密码：
> ```bash
> python scripts/ha_client.py auth --username <用户名> --show   # 或直接读 config.json
> ```

输出里默认**隐藏**明文密码，加 `--show-password` 才显示。`auth` 命令默认也只给长度和掩码。

### 3.4 接入真实设备（HA 装好只是空壳）

#### 安装集成

```bash
python scripts/ha_client.py install-integration --name xiaomi_home
```

自动完成：从容器挂载读出真实配置目录 → 从 GitHub release 下载 → 校验 `manifest.json` 并防目录穿越 → 解压到 `custom_components/` → 重启 HA → 等待就绪。可选 `--name xiaomi_miot`、`--container`、`--dry-run`、`--force`。

> ⚠️ **国内网络的下载回退**：GitHub 的 release 资产存在 `objects.githubusercontent.com`，这个域名在国内常被墙（实测超时），而 `api.github.com` 和 `codeload.github.com` 一般能通。所以下载失败时会**自动回退到官方 tarball**（`api.github.com/repos/.../tarball/<tag>`，走 codeload），内容是该 tag 的完整源码树——这类 HA 集成本来就是纯 Python 源码，与 release 资产等价。返回的 `steps` 里会标明用的是哪条路径。

| | `xiaomi_home`（默认） | `xiaomi_miot` |
|---|---|---|
| 维护 | 小米官方（`XiaoMi/ha_xiaomi_home`） | 社区（`al-one/hass-xiaomi-miot`） |
| 覆盖 | 依赖官方 spec 适配 | 更广 |
| 本地控制 | 需**小米中枢网关**（固件 3.3.0_0023+） | 支持本地 miot 协议的设备，需 token |
| 版本（实测时） | v0.4.7 | v1.1.5 |

已实测：装完 HA 日志出现 `We found a custom integration xiaomi_home`，`manifest.json` 的 `config_flow: true` 确认可在 UI 添加。

#### 用户必须自己做的一步

**小米账号授权无法代做**——那是 OAuth 跳转：

> 打开 http://localhost:8123 → 设置 → 设备与服务 → 添加集成 → 搜「xiaomi_home」→ 用小米账号登录授权

#### 读出设备清单

```bash
python scripts/ha_client.py devices
```

按域归组返回设备（`entity_id` / `friendly_name` / `state` / `area` / `controllable` / `capabilities`），已过滤 `zone`/`person`/`update`/`backup` 等 HA 内部实体，并标出 `sensitive_domains_present`。

### 3.5 手动初始化（或者 onboard 失败时的回退）

打开 http://localhost:8123 创建管理员账号 → 左下角点你的用户名 → 安全 → 长期访问令牌 → 创建令牌。

然后：

```bash
python scripts/ha_client.py setup --url http://localhost:8123 --token <令牌> --dry-run  # 先验证
python scripts/ha_client.py setup --url http://localhost:8123 --token <令牌>            # 正式写入
```

`setup` 会依次完成：验证地址可连通 → 验证令牌有效 → 读取 HA 版本 → 统计家中实体 → 写入 `config.json`（权限 600）→ 回读确认生效。

成功时返回的关键字段：

| 字段 | 含义 |
|---|---|
| `domains` | 你家实际有哪些类别的设备（如 `light: 12, switch: 5`） |
| `sensitive_domains_present` | 会被二次确认保护的类别（门锁、窗帘、安防等） |
| `config_verified` | 配置回读校验是否通过 |
| `config_file` | 写入的配置文件路径 |

也可以直接对助手说「配置智能家居」，由助手引导完成，不用自己敲命令。

### 3.6 配置文件与降级顺序

配置存于技能根目录的 `config.json`（即 `scripts/` 的上一级），**含访问令牌，权限 600，不要提交到 git 或分享**。它不随技能包分发，由 `setup` 在用户机器上生成。

读取优先级：**`config.json` → 环境变量 → 报错**。

环境变量仍然可用，适合临时测试或 CI：

| 变量 | 必填 | 说明 |
|---|---|---|
| `HA_URL` | 是 | 如 `https://ha.example.com`，末尾不要带 `/`（写了也会自动去掉） |
| `HA_TOKEN` | 是 | 长期访问令牌 |
| `HA_VERIFY_SSL` | 否 | 自签证书设为 `false`，默认 `true` |

```bash
export HA_URL=https://ha.example.com
export HA_TOKEN=eyJhbGciOi...
# export HA_VERIFY_SSL=false   # 仅自签证书时需要
```

缺失时脚本输出 JSON 错误并以退出码 1 结束，不崩溃，且会带上怎么配的提示。

> 注：本技能**不是**连接器（connector），WorkBuddy 的连接器图形化配置面板只对市场下载的连接器生效，用户导入的技能不会出现在那里，也不提供环境变量填写入口。

> ⚠️ **探测注意事项**：不要用未认证的请求反复打 `/api/`——Home Assistant 有防暴力破解机制，会封禁来源 IP。脚本内部已改为探测前端根路径（`/` 的 302 重定向）来判断状态，不触发封禁。

## 4. 安全模型

默认**放行普通设备**，只对危险类别设闸门，因此新加设备不用改配置。

| 层级 | 配置项 | 内容 | 行为 |
|---|---|---|---|
| 1 | `SENSITIVE_DOMAINS`（内置常量） | `lock` `cover` `alarm_control_panel` `garage_door` `vacuum` `water_heater` `siren` `valve` | 每次操作都要求二次确认 |
| 2 | `SENSITIVE_ENTITIES` | 额外点名的实体 | 要求二次确认 |
| 3 | `BLOCKED_ENTITIES` | 显式拉黑 | 直接拒绝 |
| 4 | `ALLOWED_ENTITIES` | 白名单 | **留空 = 不启用**；填了则只放行名单内实体（最严模式） |

后三项在 `config.json` 中按需填写，例如只允许客厅和卧室的灯：

```json
{
  "HA_URL": "http://homeassistant.local:8123",
  "HA_TOKEN": "...",
  "ALLOWED_ENTITIES": ["light.living_room", "light.bedroom"]
}
```

`cover` 涵盖窗帘。开合窗帘被视为不可逆操作（人可能在窗边），所以也在二次确认范围内——如果你觉得没必要，把它从 `SENSITIVE_DOMAINS` 里去掉即可（那是脚本内常量，需要改代码，与"新加设备不用改配置"的取向不同，是刻意的默认从严）。

entity_id 可在 HA 后台「开发者工具 → 状态」查看，也可以直接 `list` 列出来。

## 5. 自测

两套自测，都不需要真实设备、不碰真实 `config.json`、不装任何东西：

```bash
python scripts/selftest.py            # 控制与配置逻辑，39 项断言
python scripts/selftest_bootstrap.py  # 安装与状态判定逻辑，27 项断言
```

- **`selftest.py`**：起一个内存版 Home Assistant（HTTP 桩服务），覆盖未配置时的提示、错误令牌/不可达地址的报错、完整配置流程、`config.json` 权限 600、查询与控制、敏感域拦截（含"请求确实没发出去"的检查）、白名单/黑名单/自定义敏感实体、环境变量回退、配置文件损坏时的表现。
- **`selftest_bootstrap.py`**：用桩函数模拟 Docker 环境与 HA 各种状态，覆盖 Docker 不可用、内存不足、拉镜像失败、创建容器失败、`--dry-run` 不实际安装，以及 `context` 对 6 种 phase 的判定（ready / 未初始化 / 缺令牌 / 令牌失效 / 无 HA / 指向远程）。

手动试跑：

```bash
python -m py_compile scripts/ha_client.py                       # 语法检查
python scripts/ha_client.py context                             # 当前处于哪一步
python scripts/ha_client.py list --domain light --query 灯       # 列出实体
python scripts/ha_client.py state sensor.living_room_temperature
python scripts/ha_client.py history sensor.living_room_temperature --hours 6
python scripts/ha_client.py call light turn_on light.living_room --data '{"brightness_pct":60}'
python scripts/ha_client.py call climate set_temperature climate.bedroom --data '{"temperature":24,"hvac_mode":"cool"}'
python scripts/ha_client.py call cover set_cover_position cover.curtain --data '{"position":30}'
python scripts/ha_client.py scene scene.movie_mode
python scripts/ha_client.py notify notify.mobile_app_phone "测试消息"
```

## 6. 打包命令

```bash
cd ..   # 回到 smart-home-control 的父目录
zip -r smart-home-control.zip smart-home-control/ \
  -x "*.DS_Store" -x "__pycache__/*" -x "*.pyc" -x "*/config.json"
```

确认包内结构：

```bash
unzip -l smart-home-control.zip
```

应包含 `smart-home-control/SKILL.md`、`README.md`、`scripts/ha_client.py`、`scripts/selftest.py`、`scripts/selftest_bootstrap.py`、`references/ha_api_reference.md`；**不应**包含 `config.json`（含令牌，用户本地生成）。

## 7. 上架步骤

> 平台上架时填的「开发者信息」（头像、昵称、简介、联系方式）与其复现方法见 `publish/开发者信息.md`，成品头像为 `publish/avatar.jpg`。上架新技能时直接复用，不要重写。

### WorkBuddy

1. 本地安装：把 `smart-home-control/` 整个目录放到 `~/.workbuddy/skills/` 下。
2. 或发布：进入 SkillHub → 发布 skill → 上传 `smart-home-control.zip`。
3. 平台会进行三道审核：**内容合规**、**漏洞扫描**、**AI 模型安全**。审核通过后用户可在技能库中搜索安装。
4. 用户安装后对助手说「配置智能家居」并按引导提供地址与令牌即可，**不需要**在技能设置里填环境变量（该技能不是连接器，没有那个入口）。

### 豆包工作

1. 打开 PC 版侧边栏「技能·连接器·伙伴」→「我的技能」→「新建—上传技能」。
2. 上传 `smart-home-control.zip`。
3. 要求：zip 根目录含 `SKILL.md`，且 frontmatter 中必须有 `name` 和 `description` 两个字段（本技能已满足）。
4. 上传后同样通过「配置智能家居」引导完成，不需要在技能配置页填环境变量。

## 8. 小米手表支持现状（2026-09-17 调研）

本技能是纯 HA REST 调用，**跑不到手表上**，也不关心控制端是什么设备。「小米手表能不能用」实际取决于手表那一端有没有通路。以下是调研结论与核实程度。

### 8.1 用手表控制家居

| 手表系统 | 型号 | 结论 |
|---|---|---|
| Wear OS | Xiaomi Watch 2 / 2 Pro（海外版）、Watch 5 海外版 | ✅ 可装 HA 官方 Wear OS 应用，直接控制 `light/switch/cover/lock/fan/scene/script/button/input_*`，支持 Tiles 与 Assist 语音。需先用配对安卓手机的 HA App 登录一次，之后不依赖手机 |
| 澎湃OS（HyperOS 手表版） | 小米手表 S3 / S4 / S5 国行、Redmi Watch | ❌ 装不了 Wear OS APK，无官方米家手表 App、无快应用市场。官方通路是「穿戴手势控制」（响指/搓指触发米家联动）与小爱同学语音控米家，即「手表 → 米家 → HA」，绕过本技能 |

官方「穿戴手势控制」能力页（<https://hyperos.mi.com/continuity/abilities/ab0017>，2026-09-17 逐字核实）**只列 S4 / S4 eSIM**，要求手机升到 HyperOS 2+ 或手表处于蓝牙 Mesh 网关覆盖范围内；睡眠/勿扰/省电/AOD 状态不响应手势。S3 可用仅为社区说法，官方未写，勿当准。

自建手表应用可行但门槛高：dev.mi.com《手表应用发布操作指南》要求上传 APK、targetSdk≥26、32 位包体、软件著作权或电子版权认证 + APP 备案、审核 1~3 个工作日。

### 8.2 让技能把消息送到手表

链路是 `HA Companion 通知 → 手机系统通知 → 小米运动健康转发 → 手表`，不是 HA 直连手表。成败只取决于一件事：**小米运动健康 → 设备 → 应用通知列表里有没有 Home Assistant**。

- 动态列表（多数版本）：只列近期发过通知的 App——先发一条 HA 通知，再回列表里找。
- 固定白名单（部分国行固件/机型）：列表外的 App 永远转发不了，只能绕行（Server酱 / 企业微信应用消息 → 微信通知，或 Notify for Mi Band）。

HA 侧注意：`importance` 不要设 `min`/`low`；常驻通知不转发；通知按钮与可穿戴指令对小米手表无效（那是 Wear OS 专属）。技能侧无需改代码，`notify` 命令直接可用。

### 8.3 读手表健康数据进 HA

官方 `xiaomi_home` 集成与 HACS 的 `Xiaomi Miot Auto` **均不含手表手环**（穿戴设备归小米运动健康管理，不在米家设备列表里，所以既读不到也控不了）。可用替代：

| 方案 | 机制 | 限制 |
|---|---|---|
| `lzghzr/ha_miband` | 读 BLE 广播 | 纯本地、不依赖云账号；需蓝牙网关/ESP32 在近场；仅广播字段（心率、穿戴/睡眠状态），无历史明细。README 明列支持小米手表 4、5，2026-09-10 仍在更新 |
| `batareya16/MiFitnessHass` | 小米运动健康云账号拉取 | 步数/心率/睡眠/血氧/体重等；但 0 star，README 自称非官方、复用私有云 API，可能失效或违反 ToS |
| `Notify for Mi Band`（付费 App） | App 内置 HA 集成 | Standard REST 推数据到 HA，Enhanced 可反向下发到手表；付费门槛以 App 内实际标注为准 |

### 8.4 做不到的

表盘、设置、查找手表均无开放 API；两个官方集成都无法把手表变成 HA 实体。

> ⚠️ 8.1 的 Wear OS 通路与 8.2 的转发机制来自多方公开资料，**未在本机实测**。落地上架前请按下面两点复核：
> 1. 小米运动健康的应用通知列表里，本机能否看到 Home Assistant；
> 2. 手表具体型号与配对手机系统（「小米手表」同时指 Wear OS 与澎湃OS 两类，结论完全相反）。
>
> 结论有变时，请同步更新本节、SKILL.md 与记忆 `xiaomi-watch-as-ha-controller`。

## 许可证

MIT
