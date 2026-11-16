#!/usr/bin/env python3
"""Home Assistant REST API 命令行客户端。

所有输出均为 JSON（含错误），便于 Agent 直接解析。
配置（Home Assistant 地址 / 令牌 / 安全策略）查找顺序：
  1. HA_CONFIG_FILE 环境变量（显式设置即排他生效）
  2. ~/.config/smart-home-control/config.json（推荐位置）
  3. 技能目录下的 config.json（兼容旧版本）
用 `python ha_client.py setup` 可一次性完成验证与写入。
用法见 `python ha_client.py --help`。
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

if sys.version_info < (3, 10):
    # 必须放在最前：本文件用了 3.10+ 的类型注解语法，
    # 低版本解释器会在解析阶段就崩溃，报错信息与真实原因无关。
    print(json.dumps({
        "ok": False,
        "error": "Python 版本过低：当前 {}，需要 3.10 或更高".format(
            ".".join(map(str, sys.version_info[:3]))),
        "python_path": sys.executable,
        "why": "脚本用了 3.10+ 的类型注解语法（如 dict | None），低版本无法解析",
        "hint": "换一个解释器运行，例如 python3.12 scripts/ha_client.py；"
                "或确认 PATH 里的 python3 指向新版本（which -a python3 可看全部候选）",
    }, ensure_ascii=False, indent=2))
    sys.exit(1)

try:
    import httpx
except ImportError:  # 依赖缺失时也输出 JSON，保持接口一致
    print(json.dumps({
        "ok": False,
        "error": "缺少依赖 httpx（当前解释器：{}）".format(sys.executable),
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "hint": "装到当前解释器：{} -m pip install httpx".format(sys.executable),
    }, ensure_ascii=False, indent=2))
    sys.exit(1)

TIMEOUT = 15.0
HISTORY_TIMEOUT = 20.0

# 技能根目录（scripts/ 的上一级）
SKILL_ROOT = Path(__file__).resolve().parent.parent

# 配置查找位置，按优先级：
#   1. HA_CONFIG_FILE 环境变量（显式指定，最高优先级）
#   2. ~/.config/smart-home-control/config.json（**推荐位置**）
#      放在这里就不会被"重新导入技能"覆盖掉——技能包不含 config.json，
#      而导入会清空技能目录，配置放在技能目录里会丢。
#   3. 技能目录下的 config.json（向后兼容旧版本）
CANONICAL_CONFIG = Path.home() / ".config" / "smart-home-control" / "config.json"
SKILL_CONFIG = SKILL_ROOT / "config.json"


def _config_candidates() -> list[Path]:
    """按优先级返回候选配置路径。

    注意语义：**显式设置 HA_CONFIG_FILE 时它就排他生效**——不会再回退到别的位置。
    否则会出现"用户说配置在 A，脚本发现 A 不存在就悄悄用了 B"这种静默穿透，
    用户改了一份、生效的是另一份，极难排查。
    """
    env = os.environ.get("HA_CONFIG_FILE", "").strip()
    if env:
        return [Path(env)]
    return [CANONICAL_CONFIG, SKILL_CONFIG]


def _config_file() -> Path:
    """**读取**用：选定唯一一份配置。

    规则：
      - 显式设置了 HA_CONFIG_FILE —— 就用它，**即使不存在也不回退**
        （否则用户说"配置在 A"、脚本发现 A 不在就悄悄用 B，改 A 却生效 B，极难排查）
      - 否则取第一个存在的候选；都不存在则返回推荐位置
    """
    candidates = _config_candidates()
    if len(candidates) == 1:
        # 显式指定：不做存在性回退
        return candidates[0]
    for path in candidates:
        if path.exists():
            return path
    return CANONICAL_CONFIG


def _config_file_for_write() -> Path:
    """**写入**用：显式指定了 HA_CONFIG_FILE 就写那里（即使还不存在），
    否则沿用当前生效的那份。

    读写语义不同是刻意的：读的时候不存在的路径要跳过（回退到已有配置），
    但写的时候用户既然点名了这个路径，就该写到那里——这正是 setup/onboard
    首次落盘的场景。
    """
    env = os.environ.get("HA_CONFIG_FILE", "").strip()
    if env:
        return Path(env)
    return _config_file()


def _load_config_file() -> dict:
    """读取选定那份配置。

    返回空字典的情况有三种，调用方可通过 _config_diagnosis() 区分：
    文件不存在 / 内容不是合法 JSON / 内容不是对象。
    """
    path = _config_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _config_diagnosis() -> dict:
    """说明配置为什么不可用——供错误信息用，别让用户猜。"""
    path = _config_file()
    base = {"selected": str(path), "candidates": [str(p) for p in _config_candidates()]}
    if not path.exists():
        return dict(base, state="missing", path=str(path), detail="配置文件不存在")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return dict(base, state="unreadable", path=str(path),
                    detail="无法读取：{}".format(exc))
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return dict(base, state="corrupt", path=str(path),
                    detail="内容不是合法 JSON：{}".format(exc))
    if not isinstance(data, dict):
        return dict(base, state="corrupt", path=str(path),
                    detail="内容不是 JSON 对象（是 {}）".format(type(data).__name__))
    return dict(base, state="ok", path=str(path))

# ---------------------------------------------------------------------------
# 安全策略（可在 config.json 中覆盖）
#
# 设计取向：默认放行，只拦真正危险的东西。新加设备不用改配置。
#   1. SENSITIVE_DOMAINS：整个域都算敏感，操作前必须二次确认
#   2. BLOCKED_ENTITIES：显式拉黑，直接拒绝
#   3. ALLOWED_ENTITIES：可选白名单；非空时只放行名单内实体（最严模式）
# ---------------------------------------------------------------------------
SENSITIVE_DOMAINS = (
    "lock", "alarm_control_panel", "cover", "garage_door",
    "vacuum", "water_heater", "siren", "valve",
)
SENSITIVE_ENTITIES: set[str] = set()   # 额外点名的敏感实体
BLOCKED_ENTITIES: set[str] = set()     # 显式拉黑
ALLOWED_ENTITIES: set[str] = set()     # 为空 = 不启用白名单

# 配置载入后填充
_CONFIG: dict = {}
_SOURCES: dict[str, str] = {}


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def _resolve(cfg: dict, key: str) -> tuple[str, str]:
    """按 config.json → 环境变量 的优先级取值，返回 (值, 来源)。"""
    value = cfg.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip(), "config.json"
    env = os.environ.get(key, "").strip()
    if env:
        return env, "环境变量"
    return "", ""


def load_config() -> tuple[dict, bool]:
    """载入配置；HA_URL / HA_TOKEN 缺失时输出可操作的 JSON 错误并退出。"""
    global _CONFIG, SENSITIVE_ENTITIES, BLOCKED_ENTITIES, ALLOWED_ENTITIES

    cfg = _load_config_file()
    _CONFIG = cfg

    url, url_src = _resolve(cfg, "HA_URL")
    token, token_src = _resolve(cfg, "HA_TOKEN")
    missing = [name for name, val in (("HA_URL", url), ("HA_TOKEN", token)) if not val]
    if missing:
        diag = _config_diagnosis()
        hint = ("直接对助手说「配置智能家居」并给出 Home Assistant 地址和长期访问令牌，"
                "助手会运行 setup 完成验证与写入。")
        if diag["state"] == "corrupt":
            hint = ("配置文件已损坏（{}），请修复或删除后重新配置：{}".format(
                diag["detail"], diag["path"]))
        elif diag["state"] == "unreadable":
            hint = "配置文件无法读取（{}）：{}".format(diag["detail"], diag["path"])
        emit({
            "ok": False,
            "error": "尚未配置 {}".format("、".join(missing)),
            "missing": missing,
            "config_file": diag["path"],
            "config_state": diag["state"],
            "config_detail": diag.get("detail"),
            "config_candidates": diag.get("candidates"),
            "hint": hint,
        }, 1)

    # 安全策略：config.json 覆盖默认值
    def _entity_set(key: str, default: set[str]) -> set[str]:
        raw = cfg.get(key)
        if isinstance(raw, list):
            return {str(item) for item in raw}
        return set(default)

    SENSITIVE_ENTITIES = _entity_set("SENSITIVE_ENTITIES", set())
    BLOCKED_ENTITIES = _entity_set("BLOCKED_ENTITIES", set())
    ALLOWED_ENTITIES = _entity_set("ALLOWED_ENTITIES", set())

    raw_verify = cfg.get("HA_VERIFY_SSL")
    if raw_verify is None:
        raw_verify = os.environ.get("HA_VERIFY_SSL", "true")
        verify_src = "环境变量"
    else:
        verify_src = "config.json"
    if isinstance(raw_verify, bool):
        verify = raw_verify
    else:
        verify = str(raw_verify).strip().lower() not in ("0", "false", "no", "off")

    _SOURCES.clear()
    _SOURCES.update({
        "HA_URL": url_src,
        "HA_TOKEN": token_src,
        "HA_VERIFY_SSL": verify_src,
    })

    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    }
    _CONFIG["_url"] = url.rstrip("/")
    _CONFIG["_verify"] = verify
    return headers, verify


def emit(payload: dict, exit_code: int = 0) -> None:
    """输出 JSON 并退出。"""
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.exit(exit_code)


def check_entity(entity_id: str) -> dict | None:
    """安全闸门。返回 None 表示放行，否则返回要输出给用户的 JSON。"""
    if entity_id in SENSITIVE_ENTITIES:
        return {
            "ok": False,
            "requires_confirmation": True,
            "entity_id": entity_id,
            "error": "{} 被配置为敏感设备，请先向用户说明操作内容并获得明确同意后再执行一次"
                     .format(entity_id),
        }

    domain = entity_id.split(".")[0]
    if domain in SENSITIVE_DOMAINS:
        return {
            "ok": False,
            "requires_confirmation": True,
            "entity_id": entity_id,
            "domain": domain,
            "error": "{} 属于敏感域 {}（门锁/窗帘/车库/安防等），操作不可逆，"
                     "请先说明将要执行的操作并获得明确同意后再执行一次"
                     .format(entity_id, domain),
        }

    if entity_id in BLOCKED_ENTITIES:
        return {
            "ok": False,
            "entity_id": entity_id,
            "error": "{} 在禁止操作名单（BLOCKED_ENTITIES）中，已拒绝执行".format(entity_id),
        }

    if ALLOWED_ENTITIES and entity_id not in ALLOWED_ENTITIES:
        return {
            "ok": False,
            "entity_id": entity_id,
            "error": "{} 不在白名单（ALLOWED_ENTITIES）内，已拒绝执行".format(entity_id),
            "hint": "该技能仅在 config.json 显式配置 ALLOWED_ENTITIES 时启用白名单；"
                    "清空该项即可放行全部设备",
            "allowed": sorted(ALLOWED_ENTITIES),
        }
    return None


def request(method: str, path: str, headers: dict, verify: bool,
            timeout: float = TIMEOUT, **kwargs) -> dict:
    """发起请求并返回 HA 的响应体（列表或字典）。出错时直接输出 JSON 并退出。"""
    url = "{}{}".format(_CONFIG["_url"], path)
    try:
        with httpx.Client(verify=verify, timeout=timeout) as client:
            resp = client.request(method, url, headers=headers, **kwargs)
    except httpx.HTTPError as exc:
        emit({
            "ok": False,
            "error": "无法连接 Home Assistant: {}".format(exc),
        }, 1)

    if resp.status_code >= 400:
        body = resp.text.strip()
        status = resp.status_code
        if status == 401:
            error = "令牌无效或过期，请检查 HA_TOKEN"
        elif status == 403:
            error = "权限不足，该令牌可能没有调用此服务的权限"
        elif status == 404:
            error = "实体或接口不存在，请确认 entity_id 是否正确"
        elif status == 400:
            error = "参数错误: {}".format(body)
        elif status >= 500:
            error = "Home Assistant 内部错误"
        else:
            error = "Home Assistant 返回 {}: {}".format(status, body)
        emit({"ok": False, "error": error, "status_code": status}, 1)

    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        emit({
            "ok": False,
            "error": "Home Assistant 返回非 JSON 内容: {}".format(resp.text[:200]),
        }, 1)


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
def cmd_list(args, headers, verify) -> None:
    states = request("GET", "/api/states", headers, verify)
    if not isinstance(states, list):
        emit({"ok": False, "error": "Home Assistant 返回的实体列表格式异常"}, 1)

    domain = (args.domain or "").lower()
    area = (args.area or "").strip()
    query = (args.query or "").strip().lower()

    entities = []
    for item in states:
        if not isinstance(item, dict):
            continue
        entity_id = item.get("entity_id", "")
        attrs = item.get("attributes") or {}
        friendly = attrs.get("friendly_name") or entity_id
        item_area = attrs.get("area_id") or attrs.get("area") or ""

        if domain and entity_id.split(".")[0] != domain:
            continue
        if area and area not in str(item_area) and area not in friendly \
                and area not in entity_id:
            continue
        if query and query not in entity_id.lower() and query not in friendly.lower():
            continue

        entities.append({
            "entity_id": entity_id,
            "state": item.get("state"),
            "friendly_name": friendly,
            "area": item_area or None,
        })

    entities.sort(key=lambda e: e["entity_id"])
    emit({
        "ok": True,
        "count": len(entities),
        "entities": entities,
        "whitelist_enabled": bool(ALLOWED_ENTITIES),
    })


def cmd_state(args, headers, verify) -> None:
    blocked = check_entity(args.entity_id)
    if blocked:
        emit(blocked)
    data = request("GET", "/api/states/{}".format(args.entity_id), headers, verify)
    if not isinstance(data, dict):
        emit({"ok": False, "error": "Home Assistant 返回的状态格式异常"}, 1)
    attrs = data.get("attributes") or {}
    emit({
        "ok": True,
        "entity_id": data.get("entity_id", args.entity_id),
        "state": data.get("state"),
        "friendly_name": attrs.get("friendly_name"),
        "attributes": attrs,
        "last_changed": data.get("last_changed"),
    })


def cmd_call(args, headers, verify) -> None:
    blocked = check_entity(args.entity_id)
    if blocked:
        emit(blocked)

    try:
        data = json.loads(args.data) if args.data else {}
    except ValueError as exc:
        emit({"ok": False, "error": "--data 不是合法 JSON: {}".format(exc)})
    if not isinstance(data, dict):
        emit({"ok": False, "error": "--data 必须是 JSON 对象，如 '{\"brightness_pct\":60}'"})

    payload = {"entity_id": args.entity_id}
    payload.update(data)
    request(
        "POST",
        "/api/services/{}/{}".format(args.domain, args.service),
        headers, verify, json=payload,
    )
    emit({"ok": True, "domain": args.domain, "service": args.service, "data": payload})


def cmd_scene(args, headers, verify) -> None:
    blocked = check_entity(args.entity_id)
    if blocked:
        emit(blocked)
    payload = {"entity_id": args.entity_id}
    request("POST", "/api/services/scene/turn_on", headers, verify, json=payload)
    emit({"ok": True, "domain": "scene", "service": "turn_on", "data": payload})


def cmd_history(args, headers, verify) -> None:
    blocked = check_entity(args.entity_id)
    if blocked:
        emit(blocked)

    now = datetime.now().astimezone()
    start = (now - timedelta(hours=args.hours)).isoformat()
    end = now.isoformat()
    raw = request(
        "GET",
        "/api/history/period/{}".format(start),
        headers, verify, timeout=HISTORY_TIMEOUT,
        params={
            "filter_entity_id": args.entity_id,
            "end_time": end,
            "minimal_response": "1",
        },
    )

    series = []
    if isinstance(raw, list):
        for bucket in raw:
            if isinstance(bucket, list) and bucket:
                first = bucket[0]
                if not isinstance(first, dict) or first.get("entity_id") in (
                    None, args.entity_id,
                ):
                    series.extend(item for item in bucket if isinstance(item, dict))
    if not series:
        emit({
            "ok": True,
            "entity_id": args.entity_id,
            "hours": args.hours,
            "points": [],
        })

    points = [
        {"state": item.get("state"), "last_changed": item.get("last_changed")}
        for item in series
    ]
    emit({
        "ok": True,
        "entity_id": args.entity_id,
        "hours": args.hours,
        "points": points,
    })


def cmd_notify(args, headers, verify) -> None:
    target = args.target
    blocked = check_entity(target)
    if blocked:
        emit(blocked)
    if not target.startswith("notify."):
        emit({
            "ok": False,
            "error": "通知目标必须是 notify.<service> 形式，如 notify.mobile_app_phone",
        })
    service = target[len("notify."):]
    payload = {"message": args.message}
    request(
        "POST",
        "/api/services/notify/{}".format(service),
        headers, verify, json=payload,
    )
    emit({"ok": True, "domain": "notify", "service": service, "data": payload})


# ---------------------------------------------------------------------------
# setup：一次性完成验证 + 写入配置
# ---------------------------------------------------------------------------
def _write_config_file(url: str, token: str, verify_ssl: bool,
                       extra: dict | None = None, username: str = "",
                       password: str = "") -> None:
    """原子写入 config.json，并收紧权限到 600。

    若给了 username/password，会一并写入——**这意味着密码以明文存在这个文件里**。
    调用方应当已就此明确告知用户。文件权限 600 + 打包时排除，是这套做法的前提。
    """
    payload = {
        "_comment": "本文件由 ha_client.py setup/onboard 生成。含访问令牌"
                    + ("和管理员密码" if password else "")
                    + "，勿提交到 git、勿分享、勿打包分发。",
        "HA_URL": url,
        "HA_TOKEN": token,
        "HA_VERIFY_SSL": verify_ssl,
    }
    if username:
        payload["HA_USERNAME"] = username
    if password:
        payload["HA_PASSWORD"] = password
    payload.update(extra or {})

    target = _config_file_for_write()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), prefix=".config-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _probe(url: str, token: str, verify: bool, path: str = "/api/") -> tuple[bool, str, dict]:
    """探测 HA 连通性与令牌有效性。返回 (是否成功, 失败原因, 响应体)。"""
    try:
        with httpx.Client(verify=verify, timeout=TIMEOUT) as client:
            resp = client.get(
                "{}{}".format(url, path),
                headers={"Authorization": "Bearer " + token},
            )
    except httpx.ConnectError as exc:
        return False, "无法建立连接：{}".format(exc) + (
            "（如果地址是 https 且使用自签证书，加 --verify-ssl false 再试）"
            if url.startswith("https") else ""
        ), {}
    except httpx.HTTPError as exc:
        return False, "连接失败：{}".format(exc), {}

    if resp.status_code == 401:
        return False, "令牌无效：Home Assistant 返回 401，请确认长期访问令牌是否正确、是否已被删除", {}
    if resp.status_code >= 400:
        return False, "Home Assistant 返回 HTTP {}".format(resp.status_code), {}

    try:
        return True, "", resp.json()
    except ValueError:
        return False, "地址可达但返回的不是 Home Assistant API（返回内容非 JSON），请确认 HA_URL 是否正确", {}


def cmd_setup(args, headers=None, verify=None) -> None:
    """验证 Home Assistant 连接并写入 config.json。不依赖既有配置。"""
    url = (args.url or "").strip() or os.environ.get("HA_URL", "").strip()
    token = (args.token or "").strip() or os.environ.get("HA_TOKEN", "").strip()

    if not url or not token:
        emit({
            "ok": False,
            "error": "setup 需要 Home Assistant 地址和长期访问令牌",
            "missing": [name for name, val in (("--url", url), ("--token", token)) if not val],
            "how_to_get_token": "打开 Home Assistant → 左下角点你的用户名 → 安全 → "
                                "长期访问令牌 → 创建令牌 → 复制（只显示一次）",
            "usage": "python scripts/ha_client.py setup --url http://homeassistant.local:8123 "
                     "--token <令牌>",
        }, 1)

    if not url.startswith(("http://", "https://")):
        emit({"ok": False, "error": "地址必须以 http:// 或 https:// 开头，当前为：{}".format(url)}, 1)
    url = url.rstrip("/")

    verify_ssl = args.verify_ssl.strip().lower() != "false"
    if url.startswith("http://"):
        verify_ssl = True  # 明文 http 下证书校验无意义，避免写入误导性配置

    # 1) 探测连通性与令牌
    ok, reason, body = _probe(url, token, verify_ssl)
    if not ok and verify_ssl and url.startswith("https"):
        # 自签证书的常见情况：自动降级重试一次，但明确告知
        ok_retry, _reason_retry, body_retry = _probe(url, token, False)
        if ok_retry:
            ok, body, verify_ssl = True, body_retry, False
            reason = "（已自动关闭证书校验：检测到自签证书，本次自动以 --verify-ssl false 重试成功）"
    if not ok:
        emit({
            "ok": False,
            "stage": "连接验证",
            "error": reason,
            "url": url,
            "hint": "确认设备与 Home Assistant 在同一网络，且 HA_URL 不带结尾斜杠",
        }, 1)

    version = body.get("version") if isinstance(body, dict) else None
    location = body.get("location_name") if isinstance(body, dict) else None

    # 2) 拉取实体清单，给出「你家现在有什么」的概览
    try:
        with httpx.Client(verify=verify_ssl, timeout=TIMEOUT) as client:
            states_resp = client.get(
                "{}/api/states".format(url),
                headers={"Authorization": "Bearer " + token},
            )
        states = states_resp.json() if states_resp.status_code < 400 else []
    except (httpx.HTTPError, ValueError):
        states = []

    domains: dict[str, int] = {}
    sensitive_found: dict[str, int] = {}
    for item in states if isinstance(states, list) else []:
        if not isinstance(item, dict):
            continue
        entity_id = str(item.get("entity_id", ""))
        domain = entity_id.split(".")[0] if "." in entity_id else ""
        if not domain:
            continue
        domains[domain] = domains.get(domain, 0) + 1
        if domain in SENSITIVE_DOMAINS:
            sensitive_found[domain] = sensitive_found.get(domain, 0) + 1

    # 3) 写入配置
    if args.dry_run:
        emit({
            "ok": True,
            "dry_run": True,
            "would_write": str(_config_file_for_write()),
            "url": url,
            "verify_ssl": verify_ssl,
            "ha_version": version,
            "entity_count": len(states) if isinstance(states, list) else 0,
            "note": "已跳过写入（--dry-run）",
        })

    try:
        _write_config_file(url, token, verify_ssl)
    except OSError as exc:
        emit({"ok": False, "stage": "写入配置", "error": "无法写入 {}: {}".format(_config_file_for_write(), exc)}, 1)

    # 4) 读回刚写入的配置，确认确实落盘生效（不靠假设）
    reloaded = _load_config_file()
    verified = reloaded.get("HA_URL") == url and bool(reloaded.get("HA_TOKEN"))

    emit({
        "ok": True,
        "configured": True,
        "config_file": str(_config_file_for_write()),
        "url": url,
        "verify_ssl": verify_ssl,
        "ha_version": version,
        "location_name": location,
        "entity_count": len(states) if isinstance(states, list) else 0,
        "domains": dict(sorted(domains.items(), key=lambda kv: -kv[1])),
        "sensitive_domains_present": sensitive_found,
        "config_verified": verified,
        "note": reason.lstrip("（").rstrip("）") if reason else None,
        "next": "现在可以直接说「把客厅的灯打开」了。查看设备清单："
                "python scripts/ha_client.py list --domain light",
    })


# ---------------------------------------------------------------------------
# bootstrap：安装并启动 Home Assistant（Docker 方式）
# ---------------------------------------------------------------------------
CONTAINER_NAME = "homeassistant"
DEFAULT_IMAGE = "ghcr.io/home-assistant/home-assistant:stable"
FALLBACK_IMAGE = "homeassistant/home-assistant:stable"
HA_CONFIG_DIR = Path.home() / ".homeassistant"
HA_BASE_URL = "http://127.0.0.1:8123"


def _run_cmd(cmd: list[str], timeout: float = 120.0,
             input_text: str | None = None) -> tuple[int, str, str]:
    """跑外部命令，返回 (退出码, stdout, stderr)。命令不存在时退出码 -1。

    input_text 非空时通过 stdin 传给命令——用于需要把内容原样写入的场景
    （避免制表符、引号等被 shell 解析或转义）。
    """
    import subprocess
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              input=input_text)
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return -1, "", "命令不存在: {}".format(cmd[0])
    except subprocess.TimeoutExpired:
        return -2, "", "命令超时（{}秒）: {}".format(timeout, " ".join(cmd))


def _docker_available() -> tuple[bool, str]:
    """检查 docker CLI 与 daemon 是否可用。"""
    code, out, err = _run_cmd(["docker", "version", "--format", "{{.Server.Version}}"], timeout=15)
    if code == -1:
        return False, "未安装 docker 命令"
    if code != 0:
        return False, "Docker 未运行或不可用：{}".format((err or out).strip()[:200])
    return True, out.strip()


def _port_in_use(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _container_state(name: str) -> tuple[str, str]:
    """返回容器状态 ('running'/'exited'/'created'/''不存在, 状态原文)。"""
    code, out, _ = _run_cmd(
        ["docker", "inspect", "-f", "{{.State.Status}}", name], timeout=15)
    if code != 0:
        return "", ""
    return out.strip(), out.strip()


def _ha_probe(timeout: float = 3.0) -> tuple[bool, str]:
    """探测 http://127.0.0.1:8123 上的 HA 是否就绪。

    返回 (是否就绪, 状态描述)。判定依据：
      - 401：服务已就绪，但尚未认证（未初始化或未带令牌）——**这是就绪信号，不是失败**
      - 200 + "API running."：就绪且允许匿名探测（已初始化）
      - 302：前端已起来
    注意：未认证的 /api/ 重复请求会触发 HA 的防暴力破解封禁，所以只在根路径探测，
    且调用方应拉长间隔。
    """
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            resp = client.get("http://127.0.0.1:8123/")
    except httpx.HTTPError:
        return False, "还没起来"
    if resp.status_code in (200, 302, 401):
        return True, "frontend ready (HTTP {})".format(resp.status_code)
    return False, "HTTP {}".format(resp.status_code)


def _probe_state(token: str = "", timeout: float = 5.0,
                 base_url: str = "") -> tuple[bool, str, str]:
    """探测 HA 状态。返回 (服务是否在跑, 状态, 版本/说明)。

    状态取值：
      "uninitialized" —— 服务已起，但还没完成初始化（无管理员账号）
      "ready"         —— 已初始化且令牌有效
      "no_token"      —— 已初始化，但没给令牌或令牌无效
      "down"          —— 连不上
    用已认证的 /api/config 判定，避免误触防暴力破解封禁。
    """
    base = (base_url or HA_BASE_URL).rstrip("/")

    # 先看前端：未初始化时根路径会 302 到 /onboarding.html，这个请求不需要认证，
    # 且不会产生 401（避免触发 HA 的防暴力破解封禁）
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            root = client.get(base + "/")
    except httpx.HTTPError:
        return False, "down", "无法连接 {}".format(base)

    if "onboarding" in str(root.headers.get("location", "")):
        return True, "uninitialized", "等待在网页里创建管理员账号"

    if not token:
        # 已初始化但我们手上没有令牌——不主动请求 /api/config，免得白挨一次 401
        return True, "no_token", "尚未提供长期访问令牌"

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(base + "/api/config",
                              headers={"Authorization": "Bearer " + token})
    except httpx.HTTPError:
        return False, "down", "无法连接 {}".format(base)

    if resp.status_code == 200:
        try:
            body = resp.json()
        except ValueError:
            return True, "ready", ""
        version = body.get("version", "") if isinstance(body, dict) else ""
        return True, "ready", version

    if resp.status_code == 401:
        return True, "no_token", "令牌无效或已过期"

    return True, "unknown", "HTTP {}".format(resp.status_code)


def _wait_for_ha(deadline_s: float = 300.0) -> tuple[bool, str]:
    """等待 HA 服务就绪，返回 (是否就绪, 最后状态)。

    只探测前端根路径（不带令牌），间隔 5 秒——避免触发防暴力破解封禁。
    首次启动要 1-3 分钟（初始化数据库）。
    """
    import time
    start = time.time()
    last = ""
    while time.time() - start < deadline_s:
        try:
            with httpx.Client(timeout=3.0, follow_redirects=False) as client:
                resp = client.get("http://127.0.0.1:8123/")
            if resp.status_code in (200, 302):
                return True, "HTTP {}".format(resp.status_code)
            last = "HTTP {}".format(resp.status_code)
        except httpx.HTTPError:
            last = "还没起来"
        time.sleep(5)
    return False, last


def cmd_bootstrap(args, headers=None, verify=None) -> None:
    """安装并启动 Home Assistant（Docker），装完即可在浏览器完成初始化。"""
    steps: list[dict] = []

    def note(step: str, ok: bool, detail: str = "") -> None:
        steps.append({"step": step, "ok": ok, "detail": detail})

    # 0) 已经能连上？那就不用装了
    running, state, info = _probe_state()
    if running and state == "ready" and not args.force:
        emit({
            "ok": True,
            "already_running": True,
            "version": info,
            "web_ui": "http://localhost:8123",
            "message": "检测到 Home Assistant 已在运行（版本 {}），无需重复安装".format(info),
            "next": "如果还没拿长期访问令牌：打开 http://localhost:8123 → 点左下角用户名 → "
                    "安全 → 长期访问令牌 → 创建令牌。拿到后运行 setup 完成配置",
        })
    if running and state == "uninitialized" and not args.force:
        emit({
            "ok": True,
            "already_running": True,
            "initialized": False,
            "web_ui": "http://localhost:8123",
            "message": "Home Assistant 已在运行，但还没初始化",
            "next": [
                "1. 打开 http://localhost:8123 创建管理员账号（这就是初始化）",
                "2. 按需添加设备集成",
                "3. 生成长期访问令牌：左下角点用户名 → 安全 → 长期访问令牌 → 创建令牌",
                "4. 拿到令牌后运行 setup 完成配置",
            ],
        })
    if running and args.force:
        note("检测到已有实例", True, "状态 {}，按 --force 继续".format(state))

    # 1) 环境检查
    docker_ok, docker_info = _docker_available()
    if not docker_ok:
        emit({
            "ok": False,
            "stage": "环境检查",
            "error": docker_info,
            "hint": "需要先安装 Docker Desktop：https://www.docker.com/products/docker-desktop/ "
                    "（也可以用 Homebrew：brew install --cask docker）",
            "steps": steps,
        }, 1)
    note("docker", True, "Docker " + docker_info)

    code, out, _ = _run_cmd(["docker", "info", "--format", "{{.MemTotal}}"], timeout=15)
    try:
        mem_limit_gb = int(out.strip()) / 1073741824
    except (ValueError, TypeError):
        mem_limit_gb = 0
    if mem_limit_gb and mem_limit_gb < 2.0:
        emit({
            "ok": False,
            "stage": "环境检查",
            "error": "Docker 可用内存只有 {:.1f} GB，Home Assistant 至少需要 2 GB".format(mem_limit_gb),
            "hint": "Docker Desktop → Settings → Resources → Memory 调大",
            "steps": steps,
        }, 1)
    if mem_limit_gb:
        note("内存", True, "{:.1f} GB 可用".format(mem_limit_gb))

    if _port_in_use(8123):
        emit({
            "ok": False,
            "stage": "环境检查",
            "error": "端口 8123 已被占用",
            "hint": "先找出占用它的程序：lsof -nP -iTCP:8123 -sTCP:LISTEN",
            "steps": steps,
        }, 1)
    note("端口 8123", True, "空闲")

    # 2) 处理已有容器
    state, _ = _container_state(CONTAINER_NAME)
    if state == "running" and not args.force:
        emit({
            "ok": False,
            "stage": "容器状态",
            "error": "容器 {} 正在运行，但 8123 上没有响应".format(CONTAINER_NAME),
            "hint": "可能还在启动中，等 1-2 分钟再试；查看日志：docker logs {}".format(CONTAINER_NAME),
            "steps": steps,
        }, 1)
    if state and state != "running":
        note("已有容器", True, "状态 {}，将启动它".format(state))
        code, out, err = _run_cmd(["docker", "start", CONTAINER_NAME], timeout=120)
        if code != 0:
            emit({"ok": False, "stage": "启动容器",
                  "error": (err or out).strip()[:300], "steps": steps}, 1)
    elif not state:
        # 3) 创建容器
        if args.dry_run:
            emit({
                "ok": True,
                "dry_run": True,
                "would_run": ["docker", "run", "-d", "--name", CONTAINER_NAME,
                              "--restart", "unless-stopped", "--network", "host",
                              "-v", "{}:/config".format(HA_CONFIG_DIR),
                              "-v", "/etc/localtime:/etc/localtime:ro",
                              "-e", "TZ=Asia/Shanghai",
                              args.image or DEFAULT_IMAGE],
                "config_dir": str(HA_CONFIG_DIR),
                "steps": steps,
            })

        HA_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        note("配置目录", True, str(HA_CONFIG_DIR))

        image = args.image or DEFAULT_IMAGE
        images_tried = [image]
        note("拉取镜像", True, image + "（首次约 1-2 GB，可能需要几分钟）")
        code, out, err = _run_cmd(["docker", "pull", image], timeout=1800)
        if code != 0 and image == DEFAULT_IMAGE:
            # ghcr.io 在国内可能不通，回退 Docker Hub
            image = FALLBACK_IMAGE
            images_tried.append(image)
            code, out, err = _run_cmd(["docker", "pull", image], timeout=1800)
        if code != 0:
            emit({
                "ok": False,
                "stage": "拉取镜像",
                "error": (err or out).strip()[:400],
                "images_tried": images_tried,
                "hint": "国内网络拉 ghcr.io 常失败。可配置镜像加速，或改用回退源重试",
                "steps": steps,
            }, 1)

        run_cmd = [
            "docker", "run", "-d",
            "--name", CONTAINER_NAME,
            "--restart", "unless-stopped",
            "--network", "host",
            "-v", "{}:/config".format(HA_CONFIG_DIR),
            "-v", "/etc/localtime:/etc/localtime:ro",
            "-e", "TZ=Asia/Shanghai",
            image,
        ]
        code, out, err = _run_cmd(run_cmd, timeout=180)
        if code != 0:
            emit({"ok": False, "stage": "创建容器",
                  "error": (err or out).strip()[:400], "steps": steps}, 1)
        note("创建容器", True, out.strip()[:12])

    # 4) 等待就绪
    note("等待启动", True, "首次启动要 1-3 分钟（要初始化数据库）")
    ready, last = _wait_for_ha(deadline_s=args.wait)
    if not ready:
        emit({
            "ok": False,
            "stage": "等待启动",
            "error": "等待 {} 秒后服务仍未监听（最后状态：{}）".format(int(args.wait), last),
            "hint": "查看日志：docker logs --tail 50 {}".format(CONTAINER_NAME),
            "steps": steps,
        }, 1)

    # 再确认一次：是"起来了待初始化"还是"已就绪"
    _running, state, version = _probe_state()
    note("服务就绪", True, "状态 {}".format(state))

    emit({
        "ok": True,
        "installed": True,
        "state": state,
        "version": version,
        "initialized": state == "ready",
        "container": CONTAINER_NAME,
        "config_dir": str(HA_CONFIG_DIR),
        "web_ui": "http://localhost:8123",
        "steps": steps,
        "next": [
            "1. 打开 http://localhost:8123 完成初始化（创建你的管理员账号）",
            "2. 在 HA 里添加设备集成（比如小米米家：设置 → 设备与服务 → 添加集成 → Xiaomi Home）",
            "3. 生成长期访问令牌：左下角点你的用户名 → 安全 → 长期访问令牌 → 创建令牌",
            "4. 把令牌给我，我运行 setup 完成最后配置",
        ],
        "notes": [
            "Home Assistant 会随 Docker 自动启动（--restart unless-stopped）",
            "这台机器休眠或关机时 HA 停止工作，长期使用建议迁到常开设备",
            "停止：docker stop homeassistant ｜ 查看日志：docker logs -f homeassistant",
            "配置全部在 {}，迁移时拷这个目录即可".format(HA_CONFIG_DIR),
        ],
    })


def cmd_context(args, headers=None, verify=None) -> None:
    """报告当前处于配置的哪一步，供 Agent 决定下一步做什么。"""
    running, state, info = _probe_state()

    cfg = _load_config_file()
    url, url_src = _resolve(cfg, "HA_URL")
    token, token_src = _resolve(cfg, "HA_TOKEN")
    configured = bool(url and token)

    # 配置指向的是本地实例吗？（决定 bootstrap 是否管得着）
    local_url = url.rstrip("/") in ("http://localhost:8123", "http://127.0.0.1:8123")

    # 若已配置，再验一次令牌是否真能用
    token_works = None
    if configured and local_url:
        _r, s, _v = _probe_state(token, base_url=url)
        token_works = s == "ready"

    # 注意：这里的 state 来自**不带令牌**的探测。对已初始化的实例它只会是 "no_token"
    # （没令牌就不去请求 /api/config，避免挨 401）。所以判断"还缺令牌"必须看
    # state == "no_token"，而不是 state == "ready"——后者在这种调用下永远不成立。
    if running and token_works:
        phase, next_step = "ready", "已就绪，可以直接下指令控制设备"
    elif running and state == "uninitialized":
        phase = "ha_running_uninitialized"
        next_step = ("Home Assistant 已运行但未初始化。运行 onboard 可自动完成"
                     "（会自动探测用户名/密码/时区）；也可以让用户打开 "
                     "http://localhost:8123 手动创建账号")
    elif running and state == "no_token" and not configured:
        phase = "ha_running_need_token"
        next_step = ("Home Assistant 已初始化，但本机还没有可用令牌。"
                     "打开 http://localhost:8123 → 左下角点用户名 → 安全 → 长期访问令牌 → "
                     "创建令牌，然后运行 setup；若是本地实例也可以直接运行 onboard")
    elif running and configured and token_works is False:
        phase = "token_invalid"
        next_step = "Home Assistant 在跑，但配置里的令牌无效，需要重新生成令牌并运行 setup"
    elif not running and configured and not local_url:
        phase = "configured_remote"
        next_step = "配置指向远程/其他设备的 Home Assistant；本地没有实例属正常情况"
    elif not running:
        phase = "no_ha"
        next_step = ("本地没有 Home Assistant。可以运行 bootstrap 用 Docker 装一个，"
                     "或把 setup 的地址指向你已有的 HA 实例")
    else:
        phase, next_step = "unknown", "状态不明确，运行 bootstrap 查看详情"

    emit({
        "ok": True,
        "phase": phase,
        "ha_local": {
            "running": running,
            "state": state,
            "detail": info,
        },
        "config": {
            "exists": any(p.exists() for p in _config_candidates()),
            "path": str(_config_file()),
            "configured": configured,
            "url": url or None,
            "url_source": url_src or None,
            "token_source": token_src or None,
            "points_to_local": local_url,
            "token_verified": token_works,
        },
        "next_step": next_step,
        "commands": {
            "bootstrap": "python scripts/ha_client.py bootstrap",
            "setup": "python scripts/ha_client.py setup --url <地址> --token <令牌>",
            "context": "python scripts/ha_client.py context",
        },
    })


# ---------------------------------------------------------------------------
# 自动初始化：创建管理员账号并换取长期令牌
#
# 用的是 Home Assistant 前端自己在用的内部接口，**不在官方 API 文档里、无兼容性保证**。
# 已在 HA 2026.9.2 上逐字核对源码并实测：
#   POST /api/onboarding/users  —— requires_auth=False，仅在尚未 onboarding 时可用，
#                                  已初始化会返回 403；body 需要 name/username/password/
#                                  client_id/language；返回 {"auth_code": ...}
#   POST /auth/token            —— form 编码，grant_type=authorization_code + client_id + code
#   WS   auth/long_lived_access_token —— 需要 client_name 与 lifespan（单位：天）
# 任一步失败都必须回退到让用户在网页手动完成。
# ---------------------------------------------------------------------------
ONBOARD_CLIENT_ID = "http://localhost:8123/"
ONBOARD_LANGUAGE = "zh-Hans"
TOKEN_LIFESPAN_DAYS = 3650  # 10 年；HA 前端默认即 10 年


def _onboard_admin(username: str, password: str, display_name: str | None = None,
                   base_url: str = "") -> tuple[bool, str, str]:
    """创建管理员账号，返回 (是否成功, auth_code 或错误, 说明)。"""
    base = (base_url or HA_BASE_URL).rstrip("/")
    payload = {
        "name": display_name or username,
        "username": username,
        "password": password,
        "client_id": ONBOARD_CLIENT_ID,
        "language": ONBOARD_LANGUAGE,
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post("{}/api/onboarding/users".format(base), json=payload)
    except httpx.HTTPError as exc:
        return False, "", "无法连接 Home Assistant: {}".format(exc)

    if resp.status_code == 403:
        return False, "", "该实例已经初始化过了（用户已存在），跳过创建账号"
    if resp.status_code == 400:
        try:
            detail = resp.json().get("message", resp.text[:200])
        except ValueError:
            detail = resp.text[:200]
        return False, "", "创建账号被拒（参数问题）：{}".format(detail)
    if resp.status_code != 200:
        return False, "", "创建账号失败：HTTP {} {}".format(resp.status_code, resp.text[:200])

    try:
        auth_code = resp.json().get("auth_code", "")
    except ValueError:
        return False, "", "创建账号的响应不是 JSON"
    if not auth_code:
        return False, "", "创建账号成功但没拿到 auth_code"
    return True, auth_code, "管理员账号已创建"


def _exchange_auth_code(auth_code: str, client_id: str = ONBOARD_CLIENT_ID,
                        base_url: str = "") -> tuple[bool, str, str]:
    """用 auth_code 换取 access_token（form 编码，不是 JSON）。"""
    base = (base_url or HA_BASE_URL).rstrip("/")
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                "{}/auth/token".format(base),
                data={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "code": auth_code,
                },
            )
    except httpx.HTTPError as exc:
        return False, "", "换取令牌时无法连接: {}".format(exc)

    if resp.status_code != 200:
        return False, "", "换取令牌失败：HTTP {} {}".format(resp.status_code, resp.text[:200])
    try:
        body = resp.json()
    except ValueError:
        return False, "", "令牌响应不是 JSON"
    token = body.get("access_token", "")
    if not token:
        return False, "", "令牌响应里没有 access_token"
    return True, token, "已换取访问令牌"


def _ws_session(access_token: str, commands: list[dict],
                base_url: str = "") -> tuple[bool, list[dict], str]:
    """在一次 WebSocket 连接里依次发送多条命令。

    返回 (是否全部成功, 每条命令的结果列表, 错误说明)。
    每个结果形如 {"command": ..., "ok": bool, "result": <原始返回值>, "detail": str}。

    用 OAuth access token 连接——**不要用长期令牌**，长期令牌没有 refresh_token 上下文，
    onboarding/integration 步骤会 403。
    """
    try:
        import asyncio

        import websockets
    except ImportError:
        return False, [], "缺少 websockets 库，装一下：pip install websockets"

    import asyncio

    base = (base_url or HA_BASE_URL).rstrip("/")
    ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"

    async def _run() -> tuple[bool, list[dict], str]:
        results: list[dict] = []
        try:
            async with websockets.connect(ws_url, open_timeout=15) as ws:
                hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if hello.get("type") != "auth_required":
                    return False, results, "服务端未要求认证，可能不是 Home Assistant"

                await ws.send(json.dumps({"type": "auth", "access_token": access_token}))
                auth_result = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if auth_result.get("type") != "auth_ok":
                    return False, results, "WebSocket 认证失败：{}".format(
                        auth_result.get("message", ""))

                all_ok = True
                for idx, cmd in enumerate(commands, start=1):
                    payload = {"id": idx}
                    payload.update(cmd)
                    await ws.send(json.dumps(payload))
                    reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                    ok = bool(reply.get("success"))
                    if ok:
                        detail = ""
                    else:
                        detail = str((reply.get("error") or {}).get("message", reply))
                    results.append({"command": cmd.get("type", ""), "ok": ok,
                                    "result": reply.get("result"), "detail": detail})
                    if not ok:
                        all_ok = False
                return all_ok, results, ""
        except asyncio.TimeoutError:
            return False, results, "WebSocket 超时"
        except Exception as exc:  # websockets 异常类型随版本变化，统一兜住
            return False, results, "WebSocket 出错：{}".format(exc)

    try:
        return asyncio.run(_run())
    except RuntimeError as exc:
        return False, [], "无法建立 WebSocket 连接：{}".format(exc)


def _create_long_lived_token(access_token: str, client_name: str = "smart-home-control",
                             base_url: str = "") -> tuple[bool, str, str]:
    """通过 WebSocket 创建长期访问令牌。"""
    ok, results, err = _ws_session(access_token, [{
        "type": "auth/long_lived_access_token",
        "client_name": client_name,
        "lifespan": TOKEN_LIFESPAN_DAYS,
    }], base_url)

    if results and results[0]["ok"] and results[0]["result"]:
        return True, results[0]["result"], "长期访问令牌已创建"
    detail = results[0]["detail"] if results else err
    return False, "", "创建长期令牌失败：{}".format(detail or err)


def _complete_onboarding_steps(access_token: str, base_url: str = "",
                               location: str = "", language: str = "",
                               time_zone: str = "") -> list[dict]:
    """完成 onboarding 的剩余步骤，并设置位置/语言/时区。

    onboarding 共四步（const.py 的 STEPS）：user / core_config / analytics / integration，
    四步全 done 后 `async_is_onboarded()` 才为 true，前端根路径才不再 302 到 /onboarding.html。

    **必须用 OAuth 换来的 access token，不能用长期令牌**——实测（HA 2026.9.2）：

    - 用 OAuth access token：三步全部成功，实例完整初始化（根路径不再重定向）
    - 用长期令牌：前两步成功，但 `integration` 返回 403
      "Credentials for user not available"——该步骤内部要取请求携带的 refresh_token，
      长期令牌没有这个上下文。此时实例 `onboarded` 不为 true，网页仍会看到初始化向导。

    `core_config` 会顺带创建 google_translate / met / radio_browser / shopping_list 集成；
    位置/语言/时区则通过 WS `config/core/update` 设置（前端也是这么做的）。
    """
    base = (base_url or HA_BASE_URL).rstrip("/")
    headers = {"Authorization": "Bearer " + access_token, "Content-Type": "application/json"}
    results: list[dict] = []

    for path, payload, label in (
        ("/api/onboarding/analytics", None, "analytics"),
        ("/api/onboarding/core_config", None, "core_config"),
    ):
        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(base + path, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            results.append({"step": label, "ok": False, "detail": "无法连接: {}".format(exc)})
            continue
        if resp.status_code == 200:
            results.append({"step": label, "ok": True, "detail": "已完成"})
        elif resp.status_code == 403:
            # 已完成过的步骤会返回 403，属正常
            results.append({"step": label, "ok": True, "detail": "此前已完成，跳过"})
        else:
            results.append({"step": label, "ok": False,
                            "detail": "HTTP {} {}".format(resp.status_code, resp.text[:120])})

    # integration 步骤（走 HTTP，需要 access token 的 refresh_token 上下文）
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                base + "/api/onboarding/integration",
                headers=headers,
                json={"client_id": ONBOARD_CLIENT_ID, "redirect_uri": ONBOARD_CLIENT_ID},
            )
        if resp.status_code == 200:
            results.append({"step": "integration", "ok": True, "detail": "已完成"})
        elif resp.status_code == 403:
            results.append({"step": "integration", "ok": True, "detail": "此前已完成，跳过"})
        else:
            results.append({"step": "integration", "ok": False,
                            "detail": "HTTP {} {}".format(resp.status_code, resp.text[:120])})
    except httpx.HTTPError as exc:
        results.append({"step": "integration", "ok": False, "detail": "无法连接: {}".format(exc)})

    return results


def _apply_core_config(access_token: str, location: str = "", language: str = "",
                       time_zone: str = "", base_url: str = "") -> dict:
    """通过 WS `config/core/update` 设置位置/语言/时区（前端也是这么做的）。"""
    cmd: dict = {"type": "config/core/update"}
    if location:
        cmd["location_name"] = location
    if language:
        cmd["language"] = language
    if time_zone:
        cmd["time_zone"] = time_zone
    if len(cmd) == 1:  # 只有 type，没有要设的字段
        return {}

    ok, ws_results, err = _ws_session(access_token, [cmd], base_url)
    fields = "、".join(k for k in ("location_name", "language", "time_zone") if k in cmd)
    if ok:
        return {"step": "设置位置/语言/时区", "ok": True, "detail": "已设置：" + fields}
    detail = err or (ws_results[0]["detail"] if ws_results else "失败")
    return {"step": "设置位置/语言/时区", "ok": False, "detail": detail}


def _generate_password(length: int = 20) -> str:
    """生成一个强密码。

    字符集刻意只保留在任何地方粘贴都无需转义的符号（`@%-_=+`），不含引号、
    反斜杠、`$`、反引号、`*`、`^`、空格，也不含在 shell 里有特殊含义的
    `!`（历史展开）、`&`（后台执行）、`#`（注释）。20 位在这种字符集下约 122 位熵。
    """
    import secrets
    import string
    alphabet = string.ascii_letters + string.digits + "@%-_=+"
    symbols = "@%-_=+"
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        # 确保四类字符都有，避免某些密码校验规则不通过
        if (any(c.islower() for c in pwd) and any(c.isupper() for c in pwd)
                and any(c.isdigit() for c in pwd) and any(c in symbols for c in pwd)):
            return pwd


def cmd_onboard(args, headers=None, verify=None) -> None:
    """创建管理员账号并生成长期令牌，一步到位写进 config.json。

    仅适用于尚未初始化的实例。已在网页建过账号的，用 setup 即可。
    """
    username = (args.username or "").strip()
    username_source = "指定"
    if not username:
        username = _detect_username()
        username_source = "自动探测（系统登录名）"
    password = args.password or ""
    password_from = "命令行参数" if password else ""

    # 允许从环境变量读密码——避免密码出现在进程列表里
    pw_env = (getattr(args, "password_env", None) or "").strip()
    if not password and pw_env:
        password = os.environ.get(pw_env, "")
        password_from = "环境变量 " + pw_env if password else ""

    # 允许直接用钥匙串里存过的密码
    if not password and getattr(args, "use_stored_password", False):
        if not username:
            emit({"ok": False, "error": "--use-stored-password 需要同时指定 --username"}, 1)
        ok, stored = _keychain_load(username)
        if not ok:
            emit({
                "ok": False,
                "error": "钥匙串里没有 {} 的密码".format(username),
                "hint": "首次初始化请用 --password / --password-env，或让技能自动生成",
            }, 1)
        password = stored
        password_from = "系统钥匙串"

    # 都没给就自动生成——用户不必自己想密码
    generated = False
    if not password and getattr(args, "generate_password", False):
        password = _generate_password(args.password_length)
        password_from = "自动生成"
        generated = True

    base_url = ((getattr(args, "url", None) or HA_BASE_URL)).rstrip("/")
    if not password:
        emit({
            "ok": False,
            "error": "onboard 需要密码",
            "usage": "python scripts/ha_client.py onboard --generate-password",
            "tip": "推荐 --generate-password：自动生成强密码并存入 config.json 和系统钥匙串，"
                   "用户不用自己想、不用记。也可用 --password-env 从环境变量读。",
        }, 1)

    running, state, detail = _probe_state(base_url=base_url)
    if not running:
        emit({
            "ok": False,
            "error": "该地址没有运行中的 Home Assistant（{}）".format(detail),
            "url": base_url,
            "hint": "本地没有实例就先运行 bootstrap；已有实例请用 setup",
        }, 1)
    # 已初始化的实例：不带令牌的探测返回 no_token（不是 ready），
    # 所以必须用 no_token 判断，否则这个友好报错永远不会显示出来。
    if state in ("no_token", "ready"):
        emit({
            "ok": False,
            "error": "该实例已经初始化过了，无法再自动创建管理员账号",
            "hint": "直接生成长期访问令牌即可：打开 http://localhost:8123 → 左下角点用户名 → "
                    "安全 → 长期访问令牌 → 创建令牌，然后运行 setup",
            "current_state": detail,
        }, 1)

    steps: list[dict] = []

    ok, auth_code, msg = _onboard_admin(username, password, args.name, base_url)
    steps.append({"step": "创建管理员账号", "ok": ok, "detail": msg})
    if not ok:
        emit({
            "ok": False,
            "stage": "创建管理员账号",
            "error": msg,
            "steps": steps,
            "hint": "自动初始化失败不会损坏实例，可以在浏览器手动完成：{}".format(base_url),
        }, 1)

    ok, access_token, msg = _exchange_auth_code(auth_code, base_url=base_url)
    steps.append({"step": "换取访问令牌", "ok": ok, "detail": msg})
    if not ok:
        emit({
            "ok": False,
            "stage": "换取访问令牌",
            "error": msg,
            "steps": steps,
            "hint": "账号已创建成功（用户名 {}），但令牌自动获取失败。"
                    "可打开 {} 用该账号登录后手动创建长期令牌".format(username, base_url),
        }, 1)

    # 用 OAuth access token 完成剩余 onboarding 步骤——必须在换成长期令牌之前，
    # 因为 integration 步骤需要 access token 带有的 refresh_token 上下文
    for item in _complete_onboarding_steps(access_token, base_url):
        steps.append({"step": "初始化步骤 " + item["step"], "ok": item["ok"],
                      "detail": item["detail"]})

    # 位置 / 语言 / 时区：**不问用户**——时区从机器上探测
    time_zone = (args.time_zone or "").strip()
    tz_source = "指定"
    if not time_zone:
        time_zone, tz_source = _detect_time_zone(base_url)

    location = (args.location or "").strip()
    if not location:
        location = tz_to_city(time_zone)  # 用城市名当默认家名

    language = (args.language or "").strip() or "zh-Hans"

    settings = _apply_core_config(
        access_token,
        location=location,
        language=language,
        time_zone=time_zone,
        base_url=base_url,
    )
    if settings:
        settings["detail"] = "{}（时区来源：{}）".format(settings["detail"], tz_source)
        steps.append(settings)

    ok, long_token, msg = _create_long_lived_token(access_token, base_url=base_url)
    steps.append({"step": "创建长期令牌", "ok": ok, "detail": msg})
    if not ok:
        emit({
            "ok": False,
            "stage": "创建长期令牌",
            "error": msg,
            "steps": steps,
            "hint": "账号已创建成功（用户名 {}），可打开 {} 登录后手动创建长期令牌，"
                    "再运行 setup".format(username, base_url),
        }, 1)

    # 写入配置并回读校验（连用户名密码一起写，用户明确要求不必自己保管）
    try:
        _write_config_file(base_url, long_token, True,
                           username=username, password=password)
    except OSError as exc:
        emit({"ok": False, "stage": "写入配置",
              "error": "无法写入 {}: {}".format(_config_file_for_write(), exc), "steps": steps}, 1)

    reloaded = _load_config_file()
    verified = (reloaded.get("HA_URL") == base_url and bool(reloaded.get("HA_TOKEN"))
                and reloaded.get("HA_PASSWORD") == password)

    # 真跑一次确认令牌可用
    _r, final_state, version = _probe_state(long_token, base_url=base_url)
    steps.append({"step": "验证令牌", "ok": final_state == "ready",
                  "detail": "状态 " + final_state})

    token_ok = final_state == "ready"

    # 按需把管理员密码也存进系统钥匙串（用户要求不必自己记）
    in_keychain = False
    if token_ok and (getattr(args, "store_password", False) or generated):
        stored_ok, stored_msg = _keychain_store(username, password)
        in_keychain = stored_ok
        steps.append({"step": "保管密码", "ok": stored_ok, "detail": stored_msg})

    show = bool(getattr(args, "show_password", False))
    emit({
        "ok": token_ok,
        "onboarded": True,
        "username": username,
        "username_source": username_source,
        "url": base_url,
        "location_name": location,
        "time_zone": time_zone,
        "time_zone_source": tz_source,
        "password_source": password_from or None,
        "password_generated": generated,
        "password_stored_in_keychain": in_keychain,
        "password_in_config": bool(password),
        "config_file": str(_config_file_for_write()),
        "config_verified": verified,
        "ha_version": version,
        "token_works": token_ok,
        "steps": steps,
        "next": "现在可以直接说「把客厅的灯打开」了。注意：实例里还没有任何设备，"
                "需要先在 Home Assistant 里添加设备集成（比如小米：设置 → 设备与服务 → "
                "添加集成 → Xiaomi Home）",
        "credentials": {
            "token": "已写入 {}（权限 600）。日常控制只用它，不需要密码。".format(_config_file_for_write()),
            "username": username,
            "password": (password if show else "(已隐藏，加 --show-password 可显示)"),
            "password_location": "、".join(filter(None, [
                "{}（明文，权限 600）".format(_config_file_for_write()) if password else "",
                "系统钥匙串" if in_keychain else "",
            ])) or "未保存",
            "how_to_retrieve": "需要登录 HA 网页时用：python scripts/ha_client.py auth "
                               "--username {} --show".format(username) if in_keychain
                               else "直接读 {} 里的 HA_PASSWORD 字段".format(_config_file_for_write()),
        },
        "warning": "密码已明文写入 {}（你要求的做法）。该文件权限 600、"
                   "打包命令已排除它；请勿分享或提交到 git。".format(_config_file_for_write()) if password else None,
    })


# ---------------------------------------------------------------------------
# 设备接入：安装集成 + 生成设备清单
# ---------------------------------------------------------------------------
INTEGRATIONS = {
    "xiaomi_home": {
        "repo": "XiaoMi/ha_xiaomi_home",
        "asset": "xiaomi_home.zip",
        "domain": "xiaomi_home",
        "title": "Xiaomi Home（小米官方集成）",
        "note": "小米官方维护。云端接入；配了小米中枢网关时自动转本地控制。",
    },
    "xiaomi_miot": {
        "repo": "al-one/hass-xiaomi-miot",
        "asset": "xiaomi_miot.zip",
        "domain": "xiaomi_miot",
        "title": "Xiaomi Miot Auto（社区集成）",
        "note": "社区维护，覆盖设备更广。支持账号云模式与本地 token 模式。",
    },
}


def _ha_config_dir_from_docker(container: str = "") -> str:
    """从运行中的容器读出真实的配置目录挂载（不靠猜）。

    注意：macOS 上 /tmp 是 /private/tmp 的符号链接，Docker Desktop 也可能回报
    链接路径。这里统一 resolve 成真实路径，避免"容器在用、宿主机却说不存在"。
    """
    name = container or CONTAINER_NAME
    code, out, _ = _run_cmd([
        "docker", "inspect", "-f",
        "{{range .Mounts}}{{if eq .Destination \"/config\"}}{{.Source}}{{end}}{{end}}",
        name,
    ], timeout=15)
    raw = out.strip() if code == 0 else ""
    if not raw:
        return ""
    # 用 os.path.realpath 解析符号链接（对不存在的路径也能规范化）
    return os.path.realpath(raw)


def _download_asset(repo: str, asset_name: str, dest: Path,
                    tag: str = "") -> tuple[bool, str, str]:
    """下载集成源码，返回 (成功, 说明, 实际用的路径)。

    release 资产存在 `objects.githubusercontent.com` 上，国内网络常被墙（实测超时），
    而 `api.github.com` 和 `codeload.github.com` 一般是通的。所以：
      1. 先试官方 release 资产
      2. 失败则回退到 API 的 tarball 端点（走 codeload，同样是官方源；内容即该 tag 的源码树）
    tarball 里没有构建产物，但这类 HA 集成本来就是纯 Python 源码，两者内容等价。
    """
    api = "https://api.github.com/repos/{}/releases/latest".format(repo)
    tag = tag
    asset_url = ""
    version = ""

    try:
        with httpx.Client(timeout=httpx.Timeout(30.0, read=120.0),
                          follow_redirects=True) as client:
            resp = client.get(api, headers={"Accept": "application/vnd.github+json"})
            if resp.status_code != 200:
                return False, "", "查询 release 失败：HTTP {}".format(resp.status_code)
            release = resp.json()
            version = str(release.get("tag_name", "")).lstrip("v")
            for asset in release.get("assets", []):
                if asset.get("name") == asset_name:
                    asset_url = asset.get("browser_download_url", "")
                    break
    except httpx.HTTPError as exc:
        return False, "", "访问 GitHub API 出错：{}".format(exc)

    # 路线 1：官方 release 资产
    if asset_url:
        try:
            with httpx.Client(timeout=httpx.Timeout(30.0, read=180.0),
                              follow_redirects=True) as client:
                with client.stream("GET", asset_url) as stream:
                    if stream.status_code == 200:
                        with dest.open("wb") as fh:
                            for chunk in stream.iter_bytes(chunk_size=65536):
                                fh.write(chunk)
                        size = dest.stat().st_size
                        if size > 1024:
                            return True, "release 资产 {} v{}（{} KB）".format(
                                asset_name, version, size // 1024), "release"
        except httpx.HTTPError:
            pass  # 落到下一条路线
        if dest.exists():
            dest.unlink(missing_ok=True)

    # 路线 2：官方 tarball（走 codeload，绕开被墙的 objects.githubusercontent.com）
    tarball_url = "https://api.github.com/repos/{}/tarball/{}".format(
        repo, "v" + version if version else "HEAD")
    try:
        with httpx.Client(timeout=httpx.Timeout(30.0, read=180.0),
                          follow_redirects=True) as client:
            resp = client.get(tarball_url)
            if resp.status_code != 200:
                return False, "", ("release 资产下载失败，tarball 也返回 HTTP {}"
                                   .format(resp.status_code))
            dest.write_bytes(resp.content)
    except httpx.HTTPError as exc:
        return False, "", ("release 资产和 tarball 都下载失败：{}。"
                           "国内网络下 objects.githubusercontent.com 常被墙，"
                           "可配置代理或改用手动下载".format(exc))

    size = dest.stat().st_size if dest.exists() else 0
    if size < 1024:
        return False, "", "tarball 下载内容异常（仅 {} 字节）".format(size)
    return True, "官方源码 tarball v{}（{} KB，release 资产不可达）".format(
        version, size // 1024), "tarball"


def cmd_install_integration(args, headers=None, verify=None) -> None:
    """把小米集成装进 Home Assistant 的 custom_components 并重启。

    装完之后仍需**用户在浏览器完成小米账号授权**——那是 OAuth 跳转，无法代做。
    """
    import shutil
    import zipfile

    key = (args.name or "xiaomi_home").strip()
    if key not in INTEGRATIONS:
        emit({
            "ok": False,
            "error": "未知集成：{}".format(key),
            "available": {k: v["title"] for k, v in INTEGRATIONS.items()},
        }, 1)
    spec = INTEGRATIONS[key]
    container = (getattr(args, "container", None) or CONTAINER_NAME).strip()

    # 静默读一下配置，只为拿到真实的服务地址用于提示。未配置不影响安装
    # （不能用 load_config——它在未配置时会直接输出错误并退出）
    _cfg = _load_config_file()
    _url, _ = _resolve(_cfg, "HA_URL")
    if _url.startswith("http://127.0.0.1"):
        _url = _url.replace("127.0.0.1", "localhost")

    running, state, detail = _probe_state()
    if not running:
        emit({
            "ok": False,
            "error": "本地没有运行中的 Home Assistant（{}）".format(detail),
            "hint": "先运行 bootstrap 装一个",
        }, 1)

    # 1) 定位真实的配置目录（从容器挂载读，不猜）
    config_dir = _ha_config_dir_from_docker(container)
    if not config_dir:
        config_dir = str(HA_CONFIG_DIR)
        source = "默认路径（未能从容器 {} 读到挂载，请确认容器名或挂载方式）".format(container)
    else:
        source = "从容器 {} 的挂载读取".format(container)
    custom = Path(config_dir) / "custom_components"

    if not Path(config_dir).exists():
        emit({
            "ok": False,
            "error": "配置目录不存在：{}".format(config_dir),
            "hint": "容器可能不是用 -v 挂载的本地目录，无法直接装集成",
        }, 1)

    steps: list[dict] = [{"step": "定位配置目录", "ok": True,
                          "detail": "{}（{}）".format(config_dir, source)}]

    # 2) 已装过？
    target = custom / spec["domain"]
    if target.exists() and not args.force:
        emit({
            "ok": True,
            "already_installed": True,
            "path": str(target),
            "message": "{} 已装在 {}".format(spec["title"], target),
            "next": "如果还没添加：打开 {} → 设置 → 设备与服务 → 添加集成 → 搜「{}」".format(
                _url or HA_BASE_URL, spec["domain"]),
            "hint": "要重装加 --force",
            "steps": steps,
        })

    # 3) 下载
    if args.dry_run:
        emit({
            "ok": True,
            "dry_run": True,
            "would_download": "https://github.com/{}/releases/latest 的 {}".format(
                spec["repo"], spec["asset"]),
            "would_extract_to": str(custom),
            "config_dir": config_dir,
            "steps": steps,
        })

    custom.mkdir(parents=True, exist_ok=True)
    tmp_zip = Path(tempfile.mkdtemp(prefix="ha-int-")) / spec["asset"]
    ok, msg, path_kind = _download_asset(spec["repo"], spec["asset"], tmp_zip)
    steps.append({"step": "下载集成", "ok": ok, "detail": msg})
    if not ok:
        emit({"ok": False, "stage": "下载集成", "error": msg, "steps": steps}, 1)

    # 4) 解压（校验确实是集成包：必须含 manifest.json）
    extract_dir = tmp_zip.parent / "extracted"
    file_count = 0
    try:
        # 先判断拿到的是 zip 还是 tarball
        head = tmp_zip.read_bytes()[:2]
        if head == b"PK":
            with zipfile.ZipFile(tmp_zip) as zf:
                names = zf.namelist()
                if not any(n.endswith("manifest.json") for n in names):
                    raise ValueError("压缩包里没有 manifest.json，不是有效的 HA 集成包")
                for name in names:
                    if name.startswith("/") or ".." in Path(name).parts:
                        raise ValueError("压缩包内含非法路径：{}".format(name))
                zf.extractall(extract_dir)
                file_count = len(names)
        else:
            import tarfile
            with tarfile.open(tmp_zip, "r:gz") as tf:
                members = tf.getmembers()
                if not any(m.name.endswith("manifest.json") for m in members):
                    raise ValueError("tarball 里没有 manifest.json，不是有效的 HA 集成包")
                for m in members:
                    if m.name.startswith("/") or ".." in Path(m.name).parts:
                        raise ValueError("tarball 内含非法路径：{}".format(m.name))
                tf.extractall(extract_dir, filter="data")
                file_count = len(members)
    except (zipfile.BadZipFile, tarfile.TarError, ValueError, OSError) as exc:
        steps.append({"step": "解压", "ok": False, "detail": str(exc)})
        emit({"ok": False, "stage": "解压", "error": str(exc), "steps": steps}, 1)

    # 找到集成目录：tarball 顶层是 <repo>-<sha>/，zip 顶层可能是仓库根
    candidates = [p.parent for p in extract_dir.rglob("manifest.json")
                  if p.parent.name == spec["domain"]]
    if candidates:
        src = candidates[0]
    else:
        # 兜底：任取一个含 manifest.json 的目录
        any_manifest = [p.parent for p in extract_dir.rglob("manifest.json")]
        src = any_manifest[0] if any_manifest else extract_dir
    steps.append({"step": "解压", "ok": True,
                  "detail": "已解压 {} 个文件，集成目录 {}".format(
                      file_count, src.name)})

    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(target))
    steps.append({"step": "安装到 custom_components", "ok": True, "detail": str(target)})

    # 5) 读 manifest 确认装的是什么
    manifest: dict = {}
    try:
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        ver = str(manifest.get("version", "?")).lstrip("v")
        steps.append({"step": "校验 manifest", "ok": True,
                      "detail": "{} v{}".format(manifest.get("name", "?"), ver)})
    except (OSError, ValueError) as exc:
        steps.append({"step": "校验 manifest", "ok": False, "detail": str(exc)})

    # 6) 重启容器让集成生效
    code, out, err = _run_cmd(["docker", "restart", container], timeout=180)
    if code != 0:
        steps.append({"step": "重启 Home Assistant", "ok": False,
                      "detail": (err or out).strip()[:200]})
        emit({
            "ok": False,
            "stage": "重启",
            "error": "集成已装好，但重启失败，需要手动重启 Home Assistant 才能生效",
            "installed_to": str(target),
            "steps": steps,
        }, 1)
    steps.append({"step": "重启 Home Assistant", "ok": True, "detail": "已重启"})

    ready, last = _wait_for_ha(deadline_s=args.wait)
    steps.append({"step": "等待重启完成", "ok": ready, "detail": last})

    # 用实际的服务地址，别硬编码 8123（用户可能跑在别的端口或远程）
    web = _url or HA_BASE_URL

    emit({
        "ok": ready,
        "installed": True,
        "integration": spec["title"],
        "version": str(manifest.get("version", "")).lstrip("v") or None,
        "path": str(target),
        "config_dir": config_dir,
        "ha_ready": ready,
        "steps": steps,
        "next": [
            "1. 打开 {} → 设置 → 设备与服务 → 添加集成".format(web),
            "2. 搜索「{}」并添加".format(spec["domain"]),
            "3. **用你的小米账号登录授权**（OAuth 跳转，必须你本人操作，无法代做）",
            "4. 授权完成后告诉我，我跑 devices 读出设备清单",
        ],
        "notes": [spec["note"]],
    })


def cmd_setup_integration(args, headers=None, verify=None) -> None:
    """把集成的配置流程用 API 推进到"需要用户授权"那一步，并给出授权链接。

    能自动做：发起配置流程 → 同意 EULA → 填认证配置 → 生成 OAuth 授权链接。
    不能自动做：在小米登录页输入账号密码/验证码——那是小米的服务器，必须人来做。

    用户点完授权后，小米会把浏览器重定向到 HA 的 webhook，集成自动完成配置。
    """
    import re

    handler = (args.name or "xiaomi_home").strip()
    base = ((getattr(args, "url", None) or HA_BASE_URL)).rstrip("/")

    # 需要令牌——静默读配置，拿不到就明确报错
    cfg = _load_config_file()
    token, _ = _resolve(cfg, "HA_TOKEN")
    if not token:
        emit({
            "ok": False,
            "error": "还没有可用的长期令牌，无法调用 Home Assistant 的 API",
            "hint": "先运行 onboard 或 setup 完成配置",
        }, 1)

    H = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}

    def flow_post(fid: str, payload: dict | None = None) -> tuple[int, dict]:
        url = "{}/api/config/config_entries/flow{}".format(base, "/" + fid if fid else "")
        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(url, headers=H, json=payload or {})
        except httpx.HTTPError as exc:
            return 0, {"error": str(exc)}
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {"_raw": resp.text[:300]}

    steps: list[dict] = []

    # 1) 发起流程
    code, data = flow_post("", {"handler": handler})
    if code not in (200, 201):
        emit({
            "ok": False,
            "stage": "发起配置流程",
            "error": "HTTP {} {}".format(code, str(data)[:250]),
            "hint": "确认集成已安装（install-integration）且 Home Assistant 在运行",
            "steps": steps,
        }, 1)
    flow_id = data.get("flow_id", "")
    steps.append({"step": "发起配置流程", "ok": True,
                  "detail": "{}（flow {}）".format(handler, flow_id[:12])})

    # 2) 按步骤推进：eula → auth_config，直到 OAuth 步骤
    schema_answers: dict[str, dict] = {
        "eula": {"eula": True},
        "auth_config": {},  # 下面按 schema 的默认值填
    }

    for _ in range(6):
        step_id = data.get("step_id", "")
        stype = data.get("type", "")

        if stype == "progress" or step_id == "oauth":
            break
        if stype == "create_entry":
            emit({"ok": True, "already_configured": True, "steps": steps,
                  "message": "该集成已经配置完成，无需重复授权"})
        if stype == "abort":
            emit({"ok": False, "stage": step_id, "steps": steps,
                  "error": "流程中止：{}".format(data.get("reason", "未知原因"))}, 1)

        payload = dict(schema_answers.get(step_id, {}))
        # auth_config 的字段用 schema 给的默认值（cloud_server=cn、中文等）
        for field in data.get("data_schema") or []:
            name = field.get("name")
            if name and name not in payload and field.get("default") is not None:
                payload[name] = field["default"]

        code, data = flow_post(flow_id, payload)
        if code != 200:
            steps.append({"step": step_id, "ok": False, "detail": str(data)[:200]})
            emit({"ok": False, "stage": step_id, "error": str(data)[:300], "steps": steps}, 1)
        steps.append({"step": step_id, "ok": True,
                      "detail": "已提交：{}".format("、".join(payload) or "默认值")})

    # 3) 从 progress 步骤里取出授权链接
    placeholders = data.get("description_placeholders") or {}
    raw = str(placeholders.get("link_left", "")) + str(placeholders.get("link_right", ""))
    match = re.search(r'href="([^"]+)"', raw)
    auth_url = match.group(1) if match else ""

    if not auth_url:
        emit({
            "ok": False,
            "stage": "获取授权链接",
            "error": "已推进到 {} 步，但没解析出授权链接".format(data.get("step_id")),
            "raw": str(placeholders)[:400],
            "steps": steps,
        }, 1)

    redirect_uri = data.get("description_placeholders", {}).get("redirect_uri") or ""
    if not redirect_uri:
        m = re.search(r"redirect_uri=([^&]+)", auth_url)
        if m:
            from urllib.parse import unquote
            redirect_uri = unquote(m.group(1))

    emit({
        "ok": True,
        "awaiting_user_action": True,
        "auth_url": auth_url,
        "redirect_uri": redirect_uri,
        "flow_id": flow_id,
        "steps": steps,
        "what_to_do": [
            "1. 在浏览器里打开上面的 auth_url",
            "2. 用你的小米账号登录并同意授权",
            "3. 授权成功后小米会跳转到 Home Assistant，集成自动完成配置",
            "4. 回来说一声，我跑 devices 读出设备清单",
        ],
        "notes": [
            "登录那一步必须你本人做——那是小米的服务器，需要账号密码和可能的验证码，无法代做",
            "如果浏览器打不开回调地址，把 {} 加到 hosts 指向 127.0.0.1 即可".format(
                redirect_uri.split("/api/")[0].replace("http://", "").replace("https://", "")
                or "homeassistant.local"),
        ],
    })


def cmd_devices(args, headers=None, verify=None) -> None:
    """列出设备清单，按区域/类型归组，供生成配置和让用户确认。"""
    states = request("GET", "/api/states", headers, verify)
    if not isinstance(states, list):
        emit({"ok": False, "error": "Home Assistant 返回的实体列表格式异常"}, 1)

    IGNORED_DOMAINS = {"zone", "person", "persistent_notification", "update", "tts",
                       "conversation", "todo", "event", "tag", "automation", "script",
                       "input_boolean", "input_number", "input_select", "input_text",
                       "input_datetime", "sun", "backup", "device_tracker", "binary_sensor"}
    # 这些实体的 entity_id 前缀属于 HA 内部簿记，不是用户设备
    IGNORED_PREFIXES = ("sensor.backup_", "sensor.hacs", "update.", "sensor.supervisor_",
                        "sensor.home_assistant_")

    CONTROL_HINT = {
        "light": "开关 / 亮度 / 色温",
        "switch": "开关",
        "climate": "温度 / 模式 / 风速",
        "cover": "开合 / 位置（敏感域，需确认）",
        "media_player": "播放 / 音量 / 输入源",
        "fan": "开关 / 风速",
        "lock": "开关锁（敏感域，需确认）",
        "vacuum": "启停 / 回充（敏感域，需确认）",
        "water_heater": "温度 / 模式（敏感域，需确认）",
        "humidifier": "开关 / 湿度",
        "number": "设定数值",
        "select": "选择模式",
        "button": "触发动作",
        "scene": "执行场景",
    }
    READ_ONLY_DOMAINS = {"sensor", "binary_sensor"}

    grouped: dict[str, list[dict]] = {}
    skipped = 0
    for item in states:
        if not isinstance(item, dict):
            continue
        entity_id = str(item.get("entity_id", ""))
        if "." not in entity_id:
            continue
        domain = entity_id.split(".")[0]
        if domain in IGNORED_DOMAINS or entity_id.startswith(IGNORED_PREFIXES):
            skipped += 1
            continue
        attrs = item.get("attributes") or {}
        grouped.setdefault(domain, []).append({
            "entity_id": entity_id,
            "friendly_name": attrs.get("friendly_name") or entity_id,
            "state": item.get("state"),
            "area": attrs.get("area_id") or attrs.get("area") or None,
            "device_class": attrs.get("device_class"),
            "controllable": domain not in READ_ONLY_DOMAINS,
            "capabilities": CONTROL_HINT.get(domain, "只读" if domain in READ_ONLY_DOMAINS else ""),
        })

    for items in grouped.values():
        items.sort(key=lambda e: e["entity_id"])

    total = sum(len(v) for v in grouped.values())
    sensitive = sorted({d for d in grouped if d in SENSITIVE_DOMAINS})

    emit({
        "ok": True,
        "total_devices": total,
        "skipped_non_device_entities": skipped,
        "domains": {d: len(v) for d, v in sorted(grouped.items(), key=lambda kv: -len(kv[1]))},
        "devices": grouped,
        "sensitive_domains_present": sensitive,
        "note": "以上是 Home Assistant 里现有的设备。如果为空或缺少设备，"
                "说明还没在 HA 里添加对应的集成。",
    })


# ---------------------------------------------------------------------------
# 凭据保管：用系统钥匙串存管理员密码，而不是明文写进 config.json
#
# 边界要说清楚：
#   - HA_TOKEN（长期令牌）在 config.json —— 机器令牌，可随时在 HA 里吊销
#   - 管理员密码存在系统钥匙串 —— 那是账号凭据，明文落盘风险高得多
# macOS 用 security 命令；其他平台返回可操作的提示，不静默失败。
# ---------------------------------------------------------------------------
KEYCHAIN_SERVICE = "smart-home-control"


def _keychain_supported() -> bool:
    return sys.platform == "darwin"


def _keychain_store(account: str, secret: str) -> tuple[bool, str]:
    """把密码写进系统钥匙串（覆盖同名的旧值）。"""
    if not _keychain_supported():
        return False, "当前系统不是 macOS，暂不支持钥匙串保管"
    code, out, err = _run_cmd([
        "security", "add-generic-password",
        "-s", KEYCHAIN_SERVICE, "-a", account,
        "-w", secret, "-U",
    ], timeout=30)
    if code != 0:
        return False, (err or out).strip()[:200]
    return True, "已存入系统钥匙串（服务 {}，账户 {}）".format(KEYCHAIN_SERVICE, account)


def _keychain_load(account: str) -> tuple[bool, str]:
    """从钥匙串取密码。"""
    if not _keychain_supported():
        return False, ""
    code, out, _ = _run_cmd([
        "security", "find-generic-password",
        "-s", KEYCHAIN_SERVICE, "-a", account, "-w",
    ], timeout=30)
    if code != 0:
        return False, ""
    return True, out.strip()


def _keychain_delete(account: str) -> bool:
    code, _, _ = _run_cmd([
        "security", "delete-generic-password",
        "-s", KEYCHAIN_SERVICE, "-a", account,
    ], timeout=30)
    return code == 0


def cmd_auth(args, headers=None, verify=None) -> None:
    """查看 / 清除技能保管在系统钥匙串里的凭据。"""
    account = (args.username or "").strip()
    if not account:
        emit({
            "ok": False,
            "error": "需要指定 --username",
            "usage": "python scripts/ha_client.py auth --username <用户名> [--show|--forget]",
        }, 1)

    if args.forget:
        if not _keychain_supported():
            emit({"ok": False, "error": "当前系统不是 macOS，无法使用钥匙串"}, 1)
        ok = _keychain_delete(account)
        emit({
            "ok": ok,
            "deleted": ok,
            "message": "已从钥匙串删除 {} 的密码".format(account) if ok
                       else "钥匙串里没有 {} 的记录".format(account),
        }, 0 if ok else 1)

    if not _keychain_supported():
        emit({
            "ok": False,
            "error": "当前系统不是 macOS，技能暂时只能用 config.json 里的长期令牌",
            "hint": "令牌本身就够日常控制用；管理员密码请自己存在密码管理器里",
        }, 1)

    ok, secret = _keychain_load(account)
    if not ok:
        emit({
            "ok": False,
            "error": "钥匙串里没有 {} 的密码".format(account),
            "hint": "可能是初始化时没加 --store-password；用 --password-env 重新 onboard 即可补存",
        }, 1)

    # 默认不打印明文——避免密码进对话记录。要打印必须显式 --show
    emit({
        "ok": True,
        "found": True,
        "username": account,
        "length": len(secret),
        "password": secret if args.show else None,
        "masked": None if args.show else "*" * min(len(secret), 12),
        "note": "默认不显示明文。需要明文加 --show（注意：明文会进入对话记录）",
    })


# ---------------------------------------------------------------------------
# 环境自动探测：时区、用户名——这些机器上就有答案，不该问用户
# ---------------------------------------------------------------------------
# 常见 IANA 时区 → 城市名（用于 HA 的 location_name 和给用户的提示）
TZ_CITY = {
    "Asia/Shanghai": "上海", "Asia/Chongqing": "重庆", "Asia/Harbin": "哈尔滨",
    "Asia/Urumqi": "乌鲁木齐", "Asia/Hong_Kong": "香港", "Asia/Macau": "澳门",
    "Asia/Taipei": "台北", "Asia/Tokyo": "东京", "Asia/Seoul": "首尔",
    "Asia/Singapore": "新加坡", "Asia/Bangkok": "曼谷", "Asia/Kolkata": "加尔各答",
    "Europe/London": "伦敦", "Europe/Paris": "巴黎", "Europe/Berlin": "柏林",
    "Europe/Moscow": "莫斯科", "America/New_York": "纽约",
    "America/Chicago": "芝加哥", "America/Denver": "丹佛",
    "America/Los_Angeles": "洛杉矶", "Australia/Sydney": "悉尼",
    "Pacific/Auckland": "奥克兰",
}


def _detect_time_zone(base_url: str = "") -> tuple[str, str]:
    """探测时区。返回 (时区, 来源说明)。

    优先问运行中的 HA 容器（那才是真正生效的地方），
    其次读宿主机的 /etc/localtime，最后回退 UTC。
    """
    # 1) 容器里的时区（HA 就在那儿跑）
    code, out, _ = _run_cmd(["docker", "exec", CONTAINER_NAME, "cat", "/etc/timezone"], timeout=15)
    if code == 0 and out.strip():
        tz = out.strip().splitlines()[0].strip()
        if "/" in tz:
            return tz, "从 Home Assistant 容器读取"

    # 2) 宿主机的时区
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            tz = link.split("zoneinfo/", 1)[1]
            if "/" in tz:
                return tz, "从宿主机 /etc/localtime 读取"
    except OSError:
        pass

    # 3) 兜底：从本地时间与 UTC 的偏移猜一个
    import time
    if time.tzname and time.timezone == -28800:
        return "Asia/Shanghai", "按 UTC+8 偏移推断"

    return "UTC", "无法探测，回退 UTC"


def _detect_username() -> str:
    """探测一个合适的默认用户名——优先用系统登录名。"""
    try:
        import getpass
        name = getpass.getuser().strip()
    except Exception:
        name = ""
    if not name:
        name = os.environ.get("USER", "").strip()
    # 只保留安全字符，且 HA 用户名不能为空
    name = "".join(c for c in name if c.isalnum() or c in "-_")[:32]
    return name or "admin"


def tz_to_city(time_zone: str) -> str:
    """把时区换成一个能读的城市名。"""
    if time_zone in TZ_CITY:
        return TZ_CITY[time_zone]
    tail = time_zone.rsplit("/", 1)[-1]
    return tail.replace("_", " ") or time_zone


def cmd_env(args, headers=None, verify=None) -> None:
    """报告本机可自动探测到的环境信息（时区、默认用户名等）。"""
    tz, tz_source = _detect_time_zone()
    username = _detect_username()
    city = tz_to_city(tz)

    running, state, detail = _probe_state()

    # 如果 HA 里已经设过位置，以它为准。
    # 注意：不带令牌探测已初始化的实例只会返回 "no_token"（不是 "ready"），
    # 所以这里必须用 no_token 作为判断条件。
    ha_location = None
    if running and state in ("no_token", "ready"):
        cfg = _load_config_file()
        token, _ = _resolve(cfg, "HA_TOKEN")
        # 用 _resolve 取地址，不能依赖 _CONFIG["_url"]——那个全局只在 load_config()
        # 里被填充，而本命令不调用它
        target_url, _ = _resolve(cfg, "HA_URL")
        if token and target_url:
            try:
                with httpx.Client(timeout=10.0) as client:
                    resp = client.get(
                        target_url.rstrip("/") + "/api/config",
                        headers={"Authorization": "Bearer " + token})
                if resp.status_code == 200:
                    ha_location = resp.json().get("location_name")
            except (httpx.HTTPError, ValueError):
                pass

    emit({
        "ok": True,
        "time_zone": tz,
        "time_zone_source": tz_source,
        "city_guess": city,
        "username_guess": username,
        "ha_location_name": ha_location,
        "note": "这些值不用问用户，机器上就有。onboard 会自动采用，"
                "需要改时用 --time-zone / --location / --username 覆盖。",
    })


HOSTS_CALLBACK_HOST = "homeassistant.local"


def _hosts_entry_present(host: str = HOSTS_CALLBACK_HOST) -> bool:
    """检查 /etc/hosts 里是否已有该主机名的映射。"""
    try:
        with open("/etc/hosts", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.split("#", 1)[0].strip()
                if not stripped:
                    continue
                parts = stripped.split()
                if len(parts) >= 2 and host in parts[1:]:
                    return True
    except OSError:
        return False
    return False


def _hosts_resolves(host: str = HOSTS_CALLBACK_HOST) -> bool:
    """该主机名现在能否解析。"""
    import socket
    try:
        socket.gethostbyname(host)
        return True
    except OSError:
        return False


def _run_in_terminal(cmd: list[str], input_text: str, success_check,
                     what: str) -> None:
    """在真实的终端窗口里执行需要 TTY 的命令（如 sudo），然后报告结果。

    没有 TTY 时 sudo 无法提示密码，会直接报 "a terminal is required"。
    所以这里另开一个 Terminal 窗口执行——用户在窗口里输密码，
    密码不经过本进程，我们也看不到。
    """
    if sys.platform != "darwin":
        emit({
            "ok": False,
            "stage": "需要终端",
            "error": "当前环境没有终端，sudo 无法提示密码",
            "manual": "请在终端里手动执行：sudo tee -a /etc/hosts <<< '{}'".format(
                input_text.rstrip()),
        }, 1)

    # 写一个临时脚本，内容用 stdin 传给 tee（避免制表符被 shell 吞掉）
    script = Path(tempfile.mkdtemp(prefix="ha-fixdns-")) / "run.sh"
    script.write_text(
        "#!/bin/sh\n"
        "echo '即将执行：{what}'\n"
        "echo '系统会提示输入你的登录密码（输入时不显示字符）'\n"
        "echo\n"
        "printf '%s' '{payload}' | sudo tee -a /etc/hosts > /dev/null || "
        "{{ echo; echo '已取消或失败。'; read -r _; exit 1; }}\n"
        "echo\n"
        "if ping -c1 -W2 {host} >/dev/null 2>&1; then\n"
        "  echo '✓ 已写入并生效：{host} 现在可解析'\n"
        "else\n"
        "  echo '⚠ 已写入，但解析仍未生效，请检查 /etc/hosts'\n"
        "fi\n"
        "echo\n"
        "echo '这个窗口可以关掉了。'\n"
        "read -r _\n".format(
            what=what,
            payload=input_text.replace("'", "'\\''"),
            host=HOSTS_CALLBACK_HOST,
        ),
        encoding="utf-8",
    )
    script.chmod(0o700)

    code, out, err = _run_cmd([
        "osascript", "-e",
        'tell application "Terminal" to do script "{}"'.format(
            str(script).replace('"', '\\"')),
        "-e", 'tell application "Terminal" to activate',
    ], timeout=30)
    if code != 0:
        emit({
            "ok": False,
            "stage": "打开终端",
            "error": (err or out).strip()[:250],
            "manual": "请手动在终端执行：sudo tee -a /etc/hosts <<< '{}'".format(
                input_text.rstrip()),
        }, 1)

    emit({
        "ok": True,
        "terminal_opened": True,
        "script": str(script),
        "what": what,
        "message": "已打开终端窗口执行。请在那个窗口里输入你的登录密码，"
                   "完成后窗口会显示结果。",
        "after": "看到「✓ 已写入并生效」后回来告诉我，我继续完成小米授权",
    })


def cmd_fix_dns(args, headers=None, verify=None) -> None:
    """为小米集成授权修复回调域名解析（改 /etc/hosts）。

    背景：小米集成把 OAuth 回调地址硬编码为 http://homeassistant.local:8123，
    小米服务端白名单只认这一个值，改不了。而 Docker/容器部署的 Home Assistant
    没有 mDNS 广播，这个域名在宿主机上解析不了 → 授权后浏览器跳不回来。
    （上游 issue #1769 仍开着，属已知缺陷。）

    这个命令只做一件事：往 /etc/hosts 追加一行，把该域名指向本机。
    纯本机生效、不覆盖现有配置、可随时撤销。
    """
    host = (getattr(args, "host", None) or HOSTS_CALLBACK_HOST).strip()
    target = (getattr(args, "ip", None) or "127.0.0.1").strip()

    # --revert：删除本命令加的那一行
    if getattr(args, "revert", False):
        if not _hosts_entry_present(host):
            emit({"ok": True, "reverted": False,
                  "message": "/etc/hosts 里没有 {} 的记录，无需删除".format(host)})
        print(json.dumps({
            "about_to_do": {
                "action": "从 /etc/hosts 删除含 {} 的行".format(host),
                "scope": "仅本机生效",
                "needs": "系统会提示输入你的登录密码（sudo）",
            }
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        code, out, err = _run_cmd(
            ["sudo", "sh", "-c",
             "cp /etc/hosts /etc/hosts.bak-$(date +%s) && "
             "sed -i '' '/[[:space:]]{0}$/d' /etc/hosts".format(host)],
            timeout=120)
        if code != 0:
            emit({"ok": False, "stage": "还原 /etc/hosts",
                  "error": (err or out).strip()[:300]}, 1)
        emit({
            "ok": True,
            "reverted": True,
            "resolves_now": _hosts_resolves(host),
            "note": "已删除该行（原文件已备份为 /etc/hosts.bak-*）",
        })

    # 已经能解析就不用动
    if _hosts_resolves(host) and not args.force:
        emit({
            "ok": True,
            "already_ok": True,
            "host": host,
            "message": "{} 已经能解析，无需修改 /etc/hosts".format(host),
        })

    # 真实制表符分隔（hosts 文件要求 IP 与主机名之间是空白）
    entry = "{}\t{}\n".format(target, host)
    shown = "{}\\t{}".format(target, host)  # 仅用于展示，避免终端里看不出分隔

    if args.dry_run:
        emit({
            "ok": True,
            "dry_run": True,
            "would_append": shown,
            "to_file": "/etc/hosts",
            "current_entry_exists": _hosts_entry_present(host),
            "resolves_now": _hosts_resolves(host),
            "note": "只追加这一行；撤销请删除它",
        })

    if not sys.platform.startswith("linux") and sys.platform != "darwin":
        emit({
            "ok": False,
            "error": "当前系统（{}）暂不支持自动修改 hosts".format(sys.platform),
            "manual": "请手动在 hosts 文件里加入：{}".format(shown),
        }, 1)

    if _hosts_entry_present(host) and not args.force:
        emit({
            "ok": False,
            "error": "/etc/hosts 里已有 {} 的记录，但没有生效（可能指向了别的地址）".format(host),
            "hint": "手动检查 /etc/hosts，或用 --force 追加一条指向 {}".format(target),
        }, 1)

    # 明确告知将要执行的系统级操作——让用户看着它改
    print(json.dumps({
        "about_to_do": {
            "action": "向 /etc/hosts 追加一行",
            "line": shown,
            "reason": "小米集成的 OAuth 回调地址是 http://{0}:8123，"
                      "小米服务端白名单只认这个值改不了；而 {0} 在本机解析不了，"
                      "授权后浏览器跳不回来".format(host),
            "scope": "仅本机生效，不影响其他设备；不覆盖现有配置",
            "revert": "删除该行即可，或运行本命令加 --revert",
            "needs": "系统会提示输入你的登录密码（sudo）",
        }
    }, ensure_ascii=False, indent=2), file=sys.stderr)

    # 需要 TTY 才能让 sudo 提示密码；没有 TTY 时打开一个终端窗口执行
    if not sys.stdin.isatty():
        return _run_in_terminal(
            ["sudo", "tee", "-a", "/etc/hosts"],
            input_text=entry,
            success_check=lambda: _hosts_resolves(host),
            what="向 /etc/hosts 追加 '{}'".format(shown),
        )

    # 用 tee -a 从 stdin 追加：内容不经过 shell 解析，
    # 制表符/引号都不会被转义或吞掉（早先用 printf '%s\n' 包在 sh -c 里，
    # 制表符变成了字面量 \t，整行会变成一个字段而不生效）
    import shutil as _shutil
    if not _shutil.which("sudo"):
        emit({"ok": False, "error": "系统里没有 sudo 命令"}, 1)
    code, out, err = _run_cmd(["sudo", "tee", "-a", "/etc/hosts"],
                              timeout=120, input_text=entry)
    if code != 0:
        emit({
            "ok": False,
            "stage": "写入 /etc/hosts",
            "error": (err or out).strip()[:300] or "sudo 被取消或失败（退出码 {}）".format(code),
            "hint": "若你取消了密码输入，重新运行本命令即可",
        }, 1)

    ok_now = _hosts_resolves(host)
    emit({
        "ok": ok_now,
        "fixed": ok_now,
        "host": host,
        "points_to": target,
        "resolves_now": ok_now,
        "note": None if ok_now else "已写入但解析仍未生效，可能需要等待或检查 /etc/hosts",
        "revert": "python scripts/ha_client.py fix-dns --revert",
        "next": "现在可以运行 setup-integration 完成小米授权了",
    })


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ha_client.py",
        description="Home Assistant REST API 客户端（输出全部为 JSON）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_devices = sub.add_parser("devices", help="列出设备清单（按区域/类型归组）")

    p_inst = sub.add_parser("install-integration",
                            help="安装小米集成到 custom_components 并重启 HA")
    p_inst.add_argument("--name", default="xiaomi_home",
                        choices=sorted(INTEGRATIONS), help="装哪个集成，默认 xiaomi_home")
    p_inst.add_argument("--container", default=CONTAINER_NAME,
                        help="Home Assistant 的 Docker 容器名，默认 " + CONTAINER_NAME)
    p_inst.add_argument("--wait", type=float, default=300.0, help="重启后等待就绪的秒数")
    p_inst.add_argument("--force", action="store_true", help="已装过也强制重装")
    p_inst.add_argument("--dry-run", action="store_true", help="只检查不安装")

    p_setup_int = sub.add_parser("setup-integration",
                                 help="用 API 推进集成配置流程并给出授权链接（免开 HA 界面）")
    p_setup_int.add_argument("--name", default="xiaomi_home", help="集成域名，默认 xiaomi_home")
    p_setup_int.add_argument("--url", help="Home Assistant 地址，默认 " + HA_BASE_URL)

    p_dns = sub.add_parser("fix-dns",
                           help="修复小米授权回调域名解析（改 /etc/hosts，需 sudo）")
    p_dns.add_argument("--host", default=HOSTS_CALLBACK_HOST,
                       help="要映射的主机名，默认 " + HOSTS_CALLBACK_HOST)
    p_dns.add_argument("--ip", default="127.0.0.1", help="映射到哪个地址，默认 127.0.0.1")
    p_dns.add_argument("--dry-run", action="store_true", help="只显示要做什么，不实际修改")
    p_dns.add_argument("--force", action="store_true", help="已有记录时也强制追加")
    p_dns.add_argument("--revert", action="store_true", help="删除本命令添加的那一行")

    p_ctx = sub.add_parser("context", help="报告当前配置到哪一步（Agent 用）")

    sub.add_parser("env", help="报告本机自动探测到的时区/默认用户名（不必问用户）")

    p_onboard = sub.add_parser("onboard",
                               help="自动创建管理员账号并生成长期令牌（仅限未初始化的实例）")
    p_onboard.add_argument("--username", help="管理员用户名，留空则用系统登录名自动生成")
    p_onboard.add_argument("--password", help="管理员密码（会出现在进程列表里，建议改用 --password-env）")
    p_onboard.add_argument("--password-env",
                           help="从这个环境变量读密码，避免密码出现在进程列表和对话里")
    p_onboard.add_argument("--use-stored-password", dest="use_stored_password",
                           action="store_true", help="用钥匙串里已存的密码，不用再输")
    p_onboard.add_argument("--generate-password", dest="generate_password",
                           action="store_true", help="自动生成强密码，用户不用自己想")
    p_onboard.add_argument("--password-length", dest="password_length", type=int, default=20,
                           help="自动生成的密码长度，默认 20")
    p_onboard.add_argument("--store-password", dest="store_password",
                           action="store_true", help="把密码存进系统钥匙串")
    p_onboard.add_argument("--show-password", dest="show_password",
                           action="store_true",
                           help="在输出里显示明文密码（默认隐藏，因为会进对话记录）")
    p_onboard.add_argument("--name", help="显示名（默认同用户名）")
    p_onboard.add_argument("--url", help="Home Assistant 地址，默认 " + HA_BASE_URL)
    p_onboard.add_argument("--location", help="家的名称，留空则用探测到的城市名")
    p_onboard.add_argument("--language", help="界面语言，默认 zh-Hans")
    p_onboard.add_argument("--time-zone", dest="time_zone",
                           help="时区，留空则自动探测（推荐留空，机器上就有答案）")

    p_auth = sub.add_parser("auth", help="查看/清除钥匙串里保管的凭据")
    p_auth.add_argument("--username", help="要查询的用户名")
    p_auth.add_argument("--show", action="store_true", help="显示明文密码（注意会进对话记录）")
    p_auth.add_argument("--forget", action="store_true", help="从钥匙串删除该条记录")

    p_boot = sub.add_parser("bootstrap",
                            help="安装并启动 Home Assistant（Docker 方式），装完再配置令牌")
    p_boot.add_argument("--image", help="自定义镜像，默认 " + DEFAULT_IMAGE)
    p_boot.add_argument("--wait", type=float, default=300.0, help="等待就绪的秒数，默认 300")
    p_boot.add_argument("--force", action="store_true", help="忽略已有实例，强制继续")
    p_boot.add_argument("--dry-run", action="store_true", help="只检查环境，不实际安装")

    p_setup = sub.add_parser("setup", help="验证连接并写入 config.json（一次性配置）")
    p_setup.add_argument("--url", help="Home Assistant 地址，如 http://homeassistant.local:8123")
    p_setup.add_argument("--token", help="长期访问令牌")
    p_setup.add_argument("--verify-ssl", dest="verify_ssl", default="true",
                         choices=["true", "false"],
                         help="是否校验 TLS 证书，自签证书用 false（默认 true）")
    p_setup.add_argument("--dry-run", action="store_true", help="只验证不写配置")

    p_list = sub.add_parser("list", help="列出实体（客户端过滤）")
    p_list.add_argument("--domain", help="按域过滤，如 light")
    p_list.add_argument("--area", help="按区域过滤，如 客厅")
    p_list.add_argument("--query", help="按名称关键字过滤")

    p_state = sub.add_parser("state", help="查询实体状态")
    p_state.add_argument("entity_id")

    p_call = sub.add_parser("call", help="调用域服务")
    p_call.add_argument("domain")
    p_call.add_argument("service")
    p_call.add_argument("entity_id")
    p_call.add_argument("--data", help="附加参数，JSON 对象字符串")

    p_scene = sub.add_parser("scene", help="执行场景")
    p_scene.add_argument("entity_id")

    p_history = sub.add_parser("history", help="查询历史")
    p_history.add_argument("entity_id")
    p_history.add_argument("--hours", type=float, default=24)

    p_notify = sub.add_parser("notify", help="发送通知")
    p_notify.add_argument("target", help="如 notify.mobile_app_phone")
    p_notify.add_argument("message")

    return parser


def main() -> None:
    args = build_parser().parse_args()

    # context / onboard / bootstrap / install-integration / setup 自己处理输入
    if args.command == "context":
        cmd_context(args)
        return
    if args.command == "env":
        cmd_env(args)
        return
    if args.command == "onboard":
        cmd_onboard(args)
        return
    if args.command == "auth":
        cmd_auth(args)
        return
    if args.command == "bootstrap":
        cmd_bootstrap(args)
        return
    if args.command == "install-integration":
        cmd_install_integration(args)
        return
    if args.command == "setup-integration":
        cmd_setup_integration(args)
        return
    if args.command == "fix-dns":
        cmd_fix_dns(args)
        return
    if args.command == "setup":
        cmd_setup(args)
        return
    if args.command == "devices":
        headers, verify = load_config()
        cmd_devices(args, headers, verify)
        return

    headers, verify = load_config()
    handlers = {
        "list": cmd_list,
        "state": cmd_state,
        "call": cmd_call,
        "scene": cmd_scene,
        "history": cmd_history,
        "notify": cmd_notify,
    }
    handlers[args.command](args, headers, verify)


if __name__ == "__main__":
    main()
