#!/usr/bin/env python3
"""ha_client.py 的端到端自测：起一个内存版 Home Assistant，跑通全流程。

用法：python scripts/selftest.py
不需要真实 Home Assistant，也不会碰到真实的 config.json（用临时目录）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "ha_client.py"
TOKEN = "test-token-12345"

STATES = [
    {"entity_id": "light.living_room", "state": "on",
     "attributes": {"friendly_name": "客厅主灯", "brightness": 153}},
    {"entity_id": "light.bedroom", "state": "off",
     "attributes": {"friendly_name": "卧室灯"}},
    {"entity_id": "climate.bedroom", "state": "cool",
     "attributes": {"friendly_name": "卧室空调", "temperature": 26}},
    {"entity_id": "cover.curtain", "state": "open",
     "attributes": {"friendly_name": "客厅窗帘"}},
    {"entity_id": "lock.front_door", "state": "locked",
     "attributes": {"friendly_name": "大门门锁"}},
    {"entity_id": "sensor.living_room_temperature", "state": "24.5",
     "attributes": {"friendly_name": "客厅温度"}},
    {"entity_id": "scene.movie_mode", "state": "scening",
     "attributes": {"friendly_name": "电影模式"}},
    {"entity_id": "notify.mobile_app_phone", "state": "unknown", "attributes": {}},
]

CALLS: list[tuple[str, dict]] = []
PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    mark = "PASS" if condition else "FAIL"
    print("  [{}] {}{}".format(mark, name, "" if condition else "  <- " + detail))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默
        pass

    def _send(self, code: int, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == "Bearer " + TOKEN

    def do_GET(self):
        if not self._authorized():
            return self._send(401, {"message": "Unauthorized"})
        if self.path == "/api/":
            return self._send(200, {"message": "API running.", "version": "2024.9.1",
                                    "location_name": "测试之家"})
        if self.path == "/api/states":
            return self._send(200, STATES)
        if self.path.startswith("/api/states/"):
            entity_id = self.path.split("/api/states/", 1)[1]
            for item in STATES:
                if item["entity_id"] == entity_id:
                    return self._send(200, item)
            return self._send(404, {"message": "Entity not found"})
        if self.path.startswith("/api/history/period/"):
            return self._send(200, [[
                {"entity_id": "sensor.living_room_temperature", "state": "24.0",
                 "last_changed": "2026-09-17T10:00:00+00:00"},
                {"state": "24.5", "last_changed": "2026-09-17T11:00:00+00:00"},
            ]])
        return self._send(404, {"message": "Not found"})

    def do_POST(self):
        if not self._authorized():
            return self._send(401, {"message": "Unauthorized"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            payload = {}
        CALLS.append((self.path, payload))
        return self._send(200, [{"result": "ok"}])


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    base = "http://127.0.0.1:{}".format(port)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    tmp = Path(tempfile.mkdtemp(prefix="ha-selftest-"))
    skills_root = tmp / "smart-home-control"
    (skills_root / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, skills_root / "scripts" / "ha_client.py")
    client = skills_root / "scripts" / "ha_client.py"
    cfg = skills_root / "config.json"

    # 规范位置有没有真实配置？有的话"无配置"场景没法真测——脚本会（正确地）找到它
    canonical_exists = (Path.home() / ".config" / "smart-home-control"
                        / "config.json").exists()

    def run2(args, env_extra=None, use_config_file=False, isolated_cfg=False):
        """跑一次客户端。

        **默认隔离**：不设 HA_CONFIG_FILE 时脚本会去 ~/.config 找用户真实配置，
        测试就穿透了。所以默认钉到一个不存在的临时路径。

        参数：
          use_config_file=True —— 用测试自己那份 cfg（在"技能目录"下）
          isolated_cfg=True    —— 用另一份**技能目录之外**的临时路径，
                                  用于测"指定了配置路径但文件不存在"的场景
          isolated_cfg="none"  —— 完全不设 HA_CONFIG_FILE（按真实查找顺序走）。
                                  注意：开发机规范位置若有真配置，这个场景测不了。
        """
        env = dict(os.environ)
        for key in ("HA_URL", "HA_TOKEN", "HA_VERIFY_SSL", "HA_CONFIG_FILE"):
            env.pop(key, None)
        if isolated_cfg != "none":
            if use_config_file:
                env["HA_CONFIG_FILE"] = str(cfg)
            elif isolated_cfg is True:
                env["HA_CONFIG_FILE"] = str(tmp / "isolated" / "config.json")
            else:
                env["HA_CONFIG_FILE"] = str(tmp / "isolated-none.json")
        env.update(env_extra or {})
        proc = subprocess.run([sys.executable, str(client), *args],
                              capture_output=True, text=True, env=env, timeout=30)
        try:
            return proc.returncode, json.loads(proc.stdout)
        except ValueError:
            return proc.returncode, {"_raw": proc.stdout, "_err": proc.stderr}

    try:
        print("\n== 1. 未配置时的行为 ==")
        code, out = run2(["list"], isolated_cfg="none")
        if canonical_exists:
            # 开发机上规范位置有真实配置，脚本会正确地找到它——这个断言不适用
            check("（跳过：本机 ~/.config 有真实配置，无法测未配置场景）", True)
        else:
            check("未配置时返回 ok:false", out.get("ok") is False, str(out)[:200])
            check("给出可操作的 hint", "hint" in out, str(out)[:200])
            check("列出缺失项", out.get("missing") == ["HA_URL", "HA_TOKEN"], str(out)[:200])

        print("\n== 2. 令牌/地址错误时的提示 ==")
        code, out = run2(["setup", "--url", base, "--token", "wrong-token", "--dry-run"])
        check("错误令牌被识别", out.get("ok") is False and "401" in str(out.get("error", "")),
              str(out)[:200])
        code, out = run2(["setup", "--url", "http://127.0.0.1:1", "--token", TOKEN, "--dry-run"])
        check("连不上时报连接失败", out.get("ok") is False and out.get("stage") == "连接验证",
              str(out)[:200])

        print("\n== 3. 正常配置流程 ==")
        code, out = run2(["setup", "--url", base, "--token", TOKEN], use_config_file=True)
        check("setup 成功", out.get("ok") is True, str(out)[:300])
        check("读到 HA 版本", out.get("ha_version") == "2024.9.1", str(out.get("ha_version")))
        check("实体数正确", out.get("entity_count") == len(STATES), str(out.get("entity_count")))
        check("识别出敏感域（cover/lock）",
              set(out.get("sensitive_domains_present", {})) == {"cover", "lock"},
              str(out.get("sensitive_domains_present")))
        check("配置回读校验通过", out.get("config_verified") is True, str(out.get("config_verified")))
        check("config.json 已生成", cfg.exists())
        mode = oct(cfg.stat().st_mode)[-3:] if cfg.exists() else "---"
        check("config.json 权限为 600", mode == "600", "实际 " + mode)
        check("地址结尾斜杠被清理", out.get("url") == base, str(out.get("url")))

        print("\n== 4. 配置生效：查询 ==")
        code, out = run2(["list"], use_config_file=True)
        check("list 返回全部实体", out.get("count") == len(STATES), str(out.get("count")))
        check("默认未启用白名单", out.get("whitelist_enabled") is False,
              str(out.get("whitelist_enabled")))
        code, out = run2(["list", "--domain", "light"], use_config_file=True)
        check("按域过滤", out.get("count") == 2, str(out.get("count")))
        code, out = run2(["list", "--query", "客厅"], use_config_file=True)
        check("按中文名搜索（客厅 x3）", out.get("count") == 3, str(out.get("count")))
        code, out = run2(["list", "--query", "卧室"], use_config_file=True)
        check("按中文名搜索（卧室 x2）", out.get("count") == 2, str(out.get("count")))
        code, out = run2(["state", "sensor.living_room_temperature"], use_config_file=True)
        check("state 读取成功", out.get("state") == "24.5", str(out)[:200])
        code, out = run2(["history", "sensor.living_room_temperature", "--hours", "6"], use_config_file=True)
        check("history 返回数据点", len(out.get("points") or []) == 2, str(out)[:200])

        print("\n== 5. 控制与安全闸门 ==")
        CALLS.clear()
        code, out = run2(["call", "light", "turn_on", "light.living_room",
                          "--data", '{"brightness_pct":60}'], use_config_file=True)
        check("普通设备直接放行", out.get("ok") is True, str(out)[:200])
        check("请求真的发出去了", len(CALLS) == 1 and CALLS[0][0] == "/api/services/light/turn_on",
              str(CALLS))
        check("参数正确合并", CALLS and CALLS[0][1] == {"entity_id": "light.living_room",
                                                        "brightness_pct": 60}, str(CALLS))

        CALLS.clear()
        code, out = run2(["scene", "scene.movie_mode"], use_config_file=True)
        check("场景可执行", out.get("ok") is True and len(CALLS) == 1, str(out)[:200])

        CALLS.clear()
        code, out = run2(["call", "lock", "unlock", "lock.front_door"], use_config_file=True)
        check("门锁被拦下要求确认", out.get("requires_confirmation") is True, str(out)[:200])
        check("门锁请求未发出", len(CALLS) == 0, str(CALLS))

        CALLS.clear()
        code, out = run2(["call", "cover", "open_cover", "cover.curtain"], use_config_file=True)
        check("窗帘被拦下要求确认", out.get("requires_confirmation") is True, str(out)[:200])
        check("窗帘请求未发出", len(CALLS) == 0, str(CALLS))

        CALLS.clear()
        code, out = run2(["notify", "notify.mobile_app_phone", "晚安模式已执行"], use_config_file=True)
        check("通知可发送", out.get("ok") is True and len(CALLS) == 1, str(out)[:200])

        code, out = run2(["notify", "mobil_app_phone", "测试"], use_config_file=True)
        check("非法通知目标被拒", out.get("ok") is False, str(out)[:200])

        code, out = run2(["call", "light", "turn_on", "light.living_room", "--data", "{bad json"], use_config_file=True)
        check("非法 JSON 被拒", out.get("ok") is False, str(out)[:200])

        print("\n== 6. 环境变量回退 ==")
        cfg.unlink(missing_ok=True)
        # 指定一个不存在的配置路径（技能目录之外），这样脚本只看它，环境变量才轮到生效
        code, out = run2(["list"], {"HA_URL": base, "HA_TOKEN": TOKEN}, isolated_cfg=True)
        check("env 变量仍可用（无 config.json）", out.get("count") == len(STATES),
              str(out)[:200])
        code, out = run2(["list"], {"HA_URL": base}, isolated_cfg=True)
        check("只配一半时明确报错", out.get("ok") is False and out.get("missing") == ["HA_TOKEN"],
              str(out)[:200])

        print("\n== 7. 白名单 / 黑名单模式 ==")
        cfg.write_text(json.dumps({
            "HA_URL": base, "HA_TOKEN": TOKEN, "HA_VERIFY_SSL": True,
            "ALLOWED_ENTITIES": ["light.living_room"],
        }), encoding="utf-8")
        code, out = run2(["call", "light", "turn_on", "light.living_room"], use_config_file=True)
        check("白名单内放行", out.get("ok") is True, str(out)[:200])
        code, out = run2(["call", "light", "turn_on", "light.bedroom"], use_config_file=True)
        check("白名单外被拒", out.get("ok") is False and "白名单" in str(out.get("error", "")),
              str(out)[:200])

        cfg.write_text(json.dumps({
            "HA_URL": base, "HA_TOKEN": TOKEN, "HA_VERIFY_SSL": True,
            "BLOCKED_ENTITIES": ["switch.kitchen"],
        }), encoding="utf-8")
        code, out = run2(["call", "switch", "turn_on", "switch.kitchen"], use_config_file=True)
        check("黑名单被拒", out.get("ok") is False and "BLOCKED_ENTITIES" in str(out.get("error", "")),
              str(out)[:200])
        code, out = run2(["call", "light", "turn_on", "light.bedroom"], use_config_file=True)
        check("黑名单不影响其他设备", out.get("ok") is True, str(out)[:200])

        print("\n== 8. 自定义敏感设备 ==")
        cfg.write_text(json.dumps({
            "HA_URL": base, "HA_TOKEN": TOKEN, "HA_VERIFY_SSL": True,
            "SENSITIVE_ENTITIES": ["switch.kitchen"],
        }), encoding="utf-8")
        code, out = run2(["call", "switch", "turn_on", "switch.kitchen"], use_config_file=True)
        check("自定义敏感实体需确认", out.get("requires_confirmation") is True, str(out)[:200])

        print("\n== 9. 配置文件损坏时的表现 ==")
        cfg.write_text("{ 这不是合法 JSON", encoding="utf-8")
        code, out = run2(["list"], use_config_file=True)
        check("损坏配置时明确报损坏、而不是静默穿透",
              out.get("ok") is False and out.get("config_state") == "corrupt", str(out)[:200])

    finally:
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    print("通过 {} 项，失败 {} 项".format(len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for name in FAIL:
            print("  - " + name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
