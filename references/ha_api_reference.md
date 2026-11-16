# Home Assistant 常用域服务速查表

配合 `scripts/ha_client.py` 使用。所有示例假设已运行 `setup` 完成配置（`HA_URL` / `HA_TOKEN`
写在技能目录的 `config.json`，或通过同名环境变量提供）。

安全模型：默认放行普通设备，只对敏感域（`lock` `cover` `alarm_control_panel` `garage_door`
`vacuum` `water_heater` `siren` `valve`）要求二次确认。仅当 `config.json` 显式配置了
`ALLOWED_ENTITIES` 时，才启用白名单模式。

通用说明：
- `call` 子命令会把 `entity_id` 与 `--data` 合并成一个 JSON 对象 POST 给 `/api/services/{domain}/{service}`。
- `--data` 必须是 JSON **对象**字符串，注意 shell 中的引号转义。
- 服务调用成功后返回 `{"ok": true, "domain": ..., "service": ..., "data": {...}}`。

---

## light（灯）

| service | 必需参数 | 常用可选参数 |
|---|---|---|
| `turn_on` | — | `brightness_pct`(0-100)、`brightness`(0-255)、`color_temp_kelvin`、`rgb_color`([r,g,b])、`transition`、`flash` |
| `turn_off` | — | `transition`、`flash` |
| `toggle` | — | `transition` |

示例 JSON：
```json
{
  "entity_id": "light.living_room",
  "brightness_pct": 60,
  "color_temp_kelvin": 3000,
  "rgb_color": [255, 180, 120]
}
```

调用示例：
```bash
python scripts/ha_client.py call light turn_on light.living_room --data '{"brightness_pct":60,"color_temp_kelvin":3000}'
python scripts/ha_client.py call light turn_off light.living_room
python scripts/ha_client.py call light toggle light.bedroom
```

---

## switch（开关）

| service | 必需参数 | 常用可选参数 |
|---|---|---|
| `turn_on` | — | — |
| `turn_off` | — | — |
| `toggle` | — | — |

示例 JSON：
```json
{ "entity_id": "switch.kitchen" }
```

调用示例：
```bash
python scripts/ha_client.py call switch turn_on switch.kitchen
python scripts/ha_client.py call switch turn_off switch.kitchen
```

---

## climate（空调/温控）

| service | 必需参数 | 常用可选参数 |
|---|---|---|
| `set_temperature` | `temperature`（或 `target_temp_high` + `target_temp_low`） | `hvac_mode`、`target_temp_high`、`target_temp_low` |
| `set_hvac_mode` | `hvac_mode`（`off`/`heat`/`cool`/`auto`/`dry`/`fan_only`） | — |
| `set_fan_mode` | `fan_mode`（如 `auto`/`low`/`medium`/`high`） | — |

示例 JSON：
```json
{
  "entity_id": "climate.bedroom",
  "temperature": 24,
  "hvac_mode": "cool"
}
```

调用示例：
```bash
python scripts/ha_client.py call climate set_temperature climate.bedroom --data '{"temperature":24,"hvac_mode":"cool"}'
python scripts/ha_client.py call climate set_hvac_mode climate.bedroom --data '{"hvac_mode":"auto"}'
python scripts/ha_client.py call climate set_fan_mode climate.bedroom --data '{"fan_mode":"low"}'
```

---

## cover（窗帘/车库门/卷帘）

| service | 必需参数 | 常用可选参数 |
|---|---|---|
| `open_cover` | — | — |
| `close_cover` | — | — |
| `stop_cover` | — | — |
| `set_cover_position` | `position`（0=完全关闭，100=完全打开） | — |

示例 JSON：
```json
{ "entity_id": "cover.curtain", "position": 30 }
```

调用示例：
```bash
python scripts/ha_client.py call cover set_cover_position cover.curtain --data '{"position":30}'
python scripts/ha_client.py call cover open_cover cover.curtain
python scripts/ha_client.py call cover close_cover cover.curtain
python scripts/ha_client.py call cover stop_cover cover.curtain
```

---

## media_player（电视/音箱）

| service | 必需参数 | 常用可选参数 |
|---|---|---|
| `media_play_pause` | — | — |
| `media_next_track` | — | — |
| `volume_set` | `volume_level`（0-1） | — |
| `turn_on` | — | — |
| `turn_off` | — | — |

示例 JSON：
```json
{ "entity_id": "media_player.tv", "volume_level": 0.35 }
```

调用示例：
```bash
python scripts/ha_client.py call media_player turn_on media_player.tv
python scripts/ha_client.py call media_player volume_set media_player.tv --data '{"volume_level":0.35}'
python scripts/ha_client.py call media_player media_play_pause media_player.tv
python scripts/ha_client.py call media_player media_next_track media_player.tv
```

---

## scene（场景）

| service | 必需参数 | 常用可选参数 |
|---|---|---|
| `turn_on` | `entity_id` | — |

示例 JSON：
```json
{ "entity_id": "scene.movie_mode" }
```

调用示例：
```bash
python scripts/ha_client.py scene scene.movie_mode
# 等价写法
python scripts/ha_client.py call scene turn_on scene.movie_mode
```

---

## script（脚本/自动化）

| service | 必需参数 | 常用可选参数 |
|---|---|---|
| `turn_on` | `entity_id` | 脚本自定义变量 |

示例 JSON：
```json
{ "entity_id": "script.morning_routine" }
```

调用示例：
```bash
python scripts/ha_client.py call script turn_on script.morning_routine
```

---

## notify（通知）

服务名为 `notify.<service>`，其中 `<service>` 是去掉 `notify.` 前缀后的部分。
`data` 中常用字段：`message`（正文，必需）、`title`（标题，可选）。

示例 JSON：
```json
{ "message": "晚安模式已执行", "title": "智能家居" }
```

调用示例：
```bash
python scripts/ha_client.py notify notify.mobile_app_phone "晚安模式已执行"
# 需要标题时走通用 call（entity_id 为通知服务本身）
python scripts/ha_client.py call notify mobile_app_phone notify.mobile_app_phone --data '{"message":"洗衣机已完成","title":"家电"}'
```

> 注意：仅当 `config.json` 配置了 `ALLOWED_ENTITIES`（白名单模式）时，`notify.mobile_app_phone`
> 需要出现在名单中，否则会被拒绝。默认（未启用白名单）可直接发送。

---

## sensor（传感器）

只读域，**没有可调用的服务**。只能查询状态与历史。

调用示例：
```bash
python scripts/ha_client.py state sensor.living_room_temperature
python scripts/ha_client.py history sensor.living_room_temperature --hours 24
```

返回示例：
```json
{
  "ok": true,
  "entity_id": "sensor.living_room_temperature",
  "hours": 24,
  "points": [
    { "state": "24.5", "last_changed": "2026-09-17T08:00:00.123456+08:00" }
  ]
}
```
