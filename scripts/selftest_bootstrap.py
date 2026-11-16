#!/usr/bin/env python3
"""bootstrap / context 子命令的自测。

不碰真实 Docker、不装任何东西——用桩函数模拟各种环境状态，
只验证决策逻辑：什么情况下该装、该提示什么、该怎么报错。

用法：python scripts/selftest_bootstrap.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "ha_client.py"

spec = importlib.util.spec_from_file_location("ha_client", SCRIPT)
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

PASS, FAIL = [], []


class Emitted(Exception):
    """emit() 被打桩后抛出，用于捕获输出的 JSON。"""

    def __init__(self, payload, exit_code):
        self.payload = payload
        self.exit_code = exit_code


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print("  [{}] {}{}".format("PASS" if condition else "FAIL", name,
                               "" if condition else "  <- " + detail))


def capture(fn, *args, **kwargs) -> tuple[dict, int]:
    """跑一个命令函数并捕获它 emit 的 JSON。"""
    def fake_emit(payload, exit_code=0):
        raise Emitted(payload, exit_code)
    original = hc.emit
    hc.emit = fake_emit
    try:
        fn(*args, **kwargs)
        return {}, 0
    except Emitted as exc:
        return exc.payload, exc.exit_code
    finally:
        hc.emit = original


def sandbox_config(tmpdir: Path):
    """把配置解析隔离到临时目录。

    必须拦截 _config_candidates —— 它是所有配置读写的唯一入口。
    只改 CONFIG_FILE 是不够的（那只是兼容旧引用的一个值），新的动态解析会绕过它，
    导致测试**穿透到用户真实配置**。
    返回还原函数。
    """
    orig = (hc._config_candidates, hc._config_file, hc._config_file_for_write,
            getattr(hc, "CONFIG_FILE", None))
    path = tmpdir / "config.json"

    def restore():
        hc._config_candidates, hc._config_file, hc._config_file_for_write = orig[:3]
        if orig[3] is not None:
            hc.CONFIG_FILE = orig[3]

    hc._config_candidates = lambda: [path]
    hc._config_file = lambda: path
    hc._config_file_for_write = lambda: path
    hc.CONFIG_FILE = path
    return restore


class Args:
    image = None
    wait = 1.0
    force = False
    dry_run = False
    # onboard / install-integration 相关的新属性，默认置空
    password_length = 20
    generate_password = None
    show_password = None
    store_password = None
    use_stored_password = None
    password_env = None
    container = None
    location = None
    language = None
    time_zone = None


def stub_probe(state: str, detail: str = "", version: str = ""):
    """替换 _probe_state 为固定返回值。"""
    def fn(token: str = "", timeout: float = 5.0):
        if state == "down":
            return False, "down", detail or "无法连接 127.0.0.1:8123"
        return True, state, version or detail
    return fn


def stub_docker(ok: bool = True, mem_bytes: int = 8 * 1024**3, pull_ok: bool = True,
                run_ok: bool = True):
    def available():
        return (True, "29.5.2") if ok else (False, "Docker 未运行或不可用")

    def run_cmd(cmd, timeout=120.0):
        joined = " ".join(cmd)
        if "info" in cmd and "--format" in joined:
            return 0, str(mem_bytes), ""
        if cmd[:2] == ["docker", "pull"]:
            return (0, "", "") if pull_ok else (1, "", "manifest unknown")
        if cmd[:2] == ["docker", "run"]:
            return (0, "abc123def456", "") if run_ok else (1, "", "port is already allocated")
        if cmd[:2] == ["docker", "inspect"]:
            return 1, "", "No such object"
        if cmd[:2] == ["docker", "start"]:
            return 0, "homeassistant", ""
        return 0, "", ""
    return available, run_cmd


def with_stubs(probe_state_val, docker_stubs, fn, *args):
    """替换掉所有外部依赖后执行 fn。"""
    orig = (hc._probe_state, hc._docker_available, hc._run_cmd,
            hc._port_in_use, hc._container_state, hc._wait_for_ha)
    available, run_cmd = docker_stubs
    hc._probe_state = stub_probe(*probe_state_val) if isinstance(probe_state_val, tuple) else probe_state_val
    hc._docker_available = available
    hc._run_cmd = run_cmd
    hc._port_in_use = lambda port: False
    hc._container_state = lambda name: ("", "")
    hc._wait_for_ha = lambda deadline_s=300.0: (True, "HTTP 302")
    try:
        return capture(fn, *args)
    finally:
        (hc._probe_state, hc._docker_available, hc._run_cmd,
         hc._port_in_use, hc._container_state, hc._wait_for_ha) = orig


def main() -> int:
    args = Args()

    print("\n== bootstrap：环境检查 ==")
    out, code = with_stubs(("down",), stub_docker(ok=False),
                           hc.cmd_bootstrap, args)
    check("Docker 不可用时拒绝安装", out.get("ok") is False, str(out)[:200])
    check("给出安装 Docker 的提示", "docker.com" in str(out.get("hint", "")), str(out)[:200])
    check("退出码为 1", code == 1, str(code))

    out, _ = with_stubs(("down",), stub_docker(mem_bytes=1 * 1024**3),
                        hc.cmd_bootstrap, args)
    check("内存不足时拒绝安装", out.get("ok") is False and "2 GB" in str(out.get("error", "")),
          str(out)[:200])

    print("\n== bootstrap：已存在实例时不重复安装 ==")
    out, _ = with_stubs(("ready", "2026.9.1"), stub_docker(),
                        hc.cmd_bootstrap, args)
    check("已就绪时跳过安装", out.get("already_running") is True, str(out)[:200])
    check("报告版本号", out.get("version") == "2026.9.1", str(out.get("version")))
    check("提示下一步拿令牌", "长期访问令牌" in str(out.get("next", "")), str(out)[:200])

    out, _ = with_stubs(("uninitialized",), stub_docker(), hc.cmd_bootstrap, args)
    check("未初始化时提示去初始化", out.get("initialized") is False, str(out)[:200])
    check("给出网页地址", out.get("web_ui") == "http://localhost:8123", str(out.get("web_ui")))

    print("\n== bootstrap：完整安装流程（全部打桩）==")
    out, _ = with_stubs(("down",), stub_docker(), hc.cmd_bootstrap, args)
    check("安装成功", out.get("ok") is True, str(out)[:300])
    check("报告容器已创建", out.get("installed") is True, str(out)[:200])
    steps = {s["step"]: s for s in out.get("steps", [])}
    check("记录了环境检查步骤", "docker" in steps and "内存" in steps and "端口 8123" in steps,
          str(list(steps)))
    check("记录了拉取镜像和创建容器", "拉取镜像" in steps and "创建容器" in steps, str(list(steps)))
    check("配置目录落在 ~/.homeassistant",
          out.get("config_dir", "").endswith("/.homeassistant"), str(out.get("config_dir")))
    check("说明了休眠限制", "休眠" in str(out.get("notes", "")), str(out)[:300])

    print("\n== bootstrap：失败路径 ==")
    out, code = with_stubs(("down",), stub_docker(pull_ok=False, ),
                           hc.cmd_bootstrap, args)
    check("拉取镜像失败时报错", out.get("ok") is False and out.get("stage") == "拉取镜像",
          str(out)[:300])
    check("列出尝试过的镜像源", len(out.get("images_tried", [])) == 2,
          str(out.get("images_tried")))

    out, _ = with_stubs(("down",), stub_docker(run_ok=False), hc.cmd_bootstrap, args)
    check("创建容器失败时报错", out.get("ok") is False and out.get("stage") == "创建容器",
          str(out)[:300])

    orig_wait = hc._wait_for_ha
    try:
        out, _ = with_stubs(("down",), stub_docker(), hc.cmd_bootstrap, args)
    finally:
        hc._wait_for_ha = orig_wait

    print("\n== bootstrap --dry-run 不实际安装 ==")
    dry = Args()
    dry.dry_run = True
    out, _ = with_stubs(("down",), stub_docker(), hc.cmd_bootstrap, dry)
    check("dry-run 返回计划不执行", out.get("dry_run") is True, str(out)[:200])
    check("计划里含 docker run 命令", "docker" in str(out.get("would_run", [])), str(out)[:200])

    print("\n== context：状态判定 ==")
    ctx_args = Args()

    def ctx(probe_state_val, cfg: dict):
        """跑 cmd_context。

        probe_state_val 可以是一个 (state, detail) 元组，也可以是一个函数。
        注意：真实的 _probe_state 在**不带令牌**时永远不会返回 "ready"
        （没令牌就不去请求 /api/config），所以桩也必须区分这两种调用，
        否则测不出 cmd_context 里的条件分支错误。
        """
        orig_probe, orig_load = hc._probe_state, hc._load_config_file
        if callable(probe_state_val):
            hc._probe_state = probe_state_val
        else:
            state_val, detail = probe_state_val

            def split_probe(token="", timeout=5.0, base_url=""):
                if not token:
                    # 不带令牌：只可能是 no_token / uninitialized / down
                    if state_val in ("uninitialized", "down"):
                        return (state_val != "down", state_val, detail)
                    # 已初始化的实例，不带令牌只能是 no_token——这正是曾经漏掉的 case
                    return True, "no_token", "尚未提供长期访问令牌"
                # 带令牌：才可能 ready
                return (state_val != "down"), state_val, detail
            hc._probe_state = split_probe
        hc._load_config_file = lambda: cfg
        try:
            return capture(hc.cmd_context, ctx_args)
        finally:
            hc._probe_state, hc._load_config_file = orig_probe, orig_load

    out, _ = ctx(("uninitialized", "等待初始化"), {})
    check("未初始化 → ha_running_uninitialized",
          out.get("phase") == "ha_running_uninitialized", str(out.get("phase")))

    # HA 已初始化但本机没配令牌。注意：这时**不带令牌**的探测只会返回 no_token，
    # 不可能返回 ready——桩也照这个现实建模。
    out, _ = ctx(("ready", "2026.9.1"), {})
    check("已初始化但没配令牌 → ha_running_need_token",
          out.get("phase") == "ha_running_need_token", str(out.get("phase")))

    out, _ = ctx(("ready", "2026.9.1"),
                 {"HA_URL": "http://127.0.0.1:8123", "HA_TOKEN": "good"})
    check("全部就绪 → ready", out.get("phase") == "ready", str(out.get("phase")))
    check("令牌验证通过", out.get("config", {}).get("token_verified") is True,
          str(out.get("config")))

    out, _ = ctx(("down", "无法连接"), {"HA_URL": "https://ha.example.com", "HA_TOKEN": "x"})
    check("远程地址且本地无实例 → configured_remote",
          out.get("phase") == "configured_remote", str(out.get("phase")))

    out, _ = ctx(("down", "无法连接"), {})
    check("没装也没配 → no_ha", out.get("phase") == "no_ha", str(out.get("phase")))
    check("提示可以 bootstrap 或指向已有实例",
          "bootstrap" in str(out.get("next_step", "")), str(out.get("next_step")))

    print("\n== onboard：参数与状态校验 ==")
    on_args = Args()

    on_args.username, on_args.password, on_args.name, on_args.url = None, None, None, None
    on_args.password_env = on_args.use_stored_password = on_args.store_password = None
    out, code = capture(hc.cmd_onboard, on_args)
    check("缺密码时报错（用户名会自动探测）",
          out.get("ok") is False and "需要密码" in str(out.get("error", "")), str(out)[:200])
    check("推荐用 --generate-password",
          "generate-password" in str(out.get("tip", "")), str(out)[:250])

    orig_probe = hc._probe_state
    try:
        on_args.username, on_args.password = "admin", "pw123456"
        hc._probe_state = lambda *a, **k: (True, "ready", "已初始化")
        out, _ = capture(hc.cmd_onboard, on_args)
        check("已初始化的实例拒绝 onboard",
              out.get("ok") is False and "已经初始化过" in str(out.get("error", "")), str(out)[:200])
        check("给出改用令牌的指引", "长期访问令牌" in str(out.get("hint", "")), str(out)[:200])

        hc._probe_state = lambda *a, **k: (False, "down", "无法连接")
        out, _ = capture(hc.cmd_onboard, on_args)
        check("实例不在运行时拒绝", out.get("ok") is False and "没有运行中" in str(out.get("error", "")),
              str(out)[:200])
    finally:
        hc._probe_state = orig_probe

    print("\n== onboard：完整链路（全部打桩，不碰真实 config.json）==")
    import tempfile
    tmpdir = Path(tempfile.mkdtemp(prefix="ha-onboard-selftest-"))
    restore_cfg = sandbox_config(tmpdir)
    orig_funcs = (hc._probe_state, hc._onboard_admin, hc._exchange_auth_code,
                  hc._create_long_lived_token, hc._complete_onboarding_steps,
                  hc._apply_core_config)
    try:
        on_args.username, on_args.password, on_args.name = "testadmin", "TestPass123!", None
        on_args.url = None
        on_args.location = on_args.language = on_args.time_zone = None
        # 桩：只有第一次调用返回 uninitialized（前置检查），之后返回 ready（令牌验证）
        probe_calls = {"n": 0}

        def probe_stub(*a, **k):
            probe_calls["n"] += 1
            if probe_calls["n"] == 1:
                return (True, "uninitialized", "等待初始化")
            return (True, "ready", "2026.9.2")

        hc._probe_state = probe_stub
        hc._onboard_admin = lambda *a, **k: (True, "AUTHCODE", "管理员账号已创建")
        hc._exchange_auth_code = lambda *a, **k: (True, "ACCESS", "已换取访问令牌")
        hc._create_long_lived_token = lambda *a, **k: (True, "LONGTOKEN", "长期访问令牌已创建")
        hc._complete_onboarding_steps = lambda *a, **k: [
            {"step": "analytics", "ok": True, "detail": "已完成"},
            {"step": "core_config", "ok": True, "detail": "已完成"},
            {"step": "integration", "ok": True, "detail": "已完成"},
        ]
        hc._apply_core_config = lambda *a, **k: {}

        out, _ = capture(hc.cmd_onboard, on_args)
        check("onboard 全流程成功", out.get("ok") is True, str(out)[:300])
        check("令牌验证通过", out.get("token_works") is True, str(out)[:200])
        check("配置回读校验通过", out.get("config_verified") is True, str(out)[:200])
        check("config.json 确实写入了", hc.CONFIG_FILE.exists(), str(hc.CONFIG_FILE))
        check("写入的令牌正确",
              json.loads(hc.CONFIG_FILE.read_text())["HA_TOKEN"] == "LONGTOKEN",
              str(hc.CONFIG_FILE))
        steps = {s["step"]: s for s in out.get("steps", [])}
        check("记录了建账号步骤", steps.get("创建管理员账号", {}).get("ok") is True, str(list(steps)))
        check("integration 步骤成功", steps.get("初始化步骤 integration", {}).get("ok") is True,
              str(list(steps)))
        check("凭据说明区分令牌与密码",
              "token" in out.get("credentials", {}) and "password" in out.get("credentials", {}),
              str(out.get("credentials")))
        check("密码默认隐藏不显示明文",
              out.get("credentials", {}).get("password") == "(已隐藏，加 --show-password 可显示)",
              str(out.get("credentials", {}).get("password")))
        check("密码写进了 config.json",
              json.loads(hc.CONFIG_FILE.read_text()).get("HA_PASSWORD") == "TestPass123!",
              str(hc.CONFIG_FILE))
        check("警告说明密码是明文存储",
              "明文" in str(out.get("warning", "")), str(out.get("warning"))[:200])

        print("\n== onboard：自动生成密码 ==")
        probe_calls["n"] = 0
        hc.CONFIG_FILE.unlink(missing_ok=True)
        orig_store2 = hc._keychain_store
        gen_saved = {}

        def fake_store2(acct, secret):
            gen_saved["acct"], gen_saved["secret"] = acct, secret
            return True, "已存入系统钥匙串"
        hc._keychain_store = fake_store2
        on_args.password = None
        on_args.generate_password = True
        out, _ = capture(hc.cmd_onboard, on_args)
        check("自动生成密码完成 onboard", out.get("ok") is True, str(out)[:250])
        check("标明密码是自动生成的", out.get("password_generated") is True,
              str(out.get("password_generated")))
        check("标明密码来源为自动生成",
              out.get("password_source") == "自动生成", str(out.get("password_source")))
        check("生成后自动存入钥匙串", out.get("password_stored_in_keychain") is True,
              str(out.get("password_stored_in_keychain")))
        saved_pw = json.loads(hc.CONFIG_FILE.read_text()).get("HA_PASSWORD", "")
        check("生成的密码写入了 config.json", len(saved_pw) == 20, "长度 " + str(len(saved_pw)))
        check("存入钥匙串的密码与配置一致", gen_saved.get("secret") == saved_pw,
              "钥匙串 {} vs 配置 {}".format(len(gen_saved.get("secret") or ""), len(saved_pw)))
        check("生成的密码含四类字符",
              any(c.islower() for c in saved_pw) and any(c.isupper() for c in saved_pw)
              and any(c.isdigit() for c in saved_pw)
              and any(c in "@%-_=+" for c in saved_pw), saved_pw)
        check("生成的密码无 shell 危险字符",
              not any(c in saved_pw for c in "\"'\\`$;|&()<> *^?[]{}~"), saved_pw)
        hc._keychain_store = orig_store2
        on_args.generate_password = None
        on_args.password = "TestPass123!"

        print("\n== onboard：--show-password 才给明文 ==")
        probe_calls["n"] = 0
        hc.CONFIG_FILE.unlink(missing_ok=True)
        on_args.show_password = True
        out, _ = capture(hc.cmd_onboard, on_args)
        check("--show-password 显示明文",
              out.get("credentials", {}).get("password") == "TestPass123!",
              str(out.get("credentials", {}).get("password")))
        on_args.show_password = None

        print("\n== onboard：--store-password 存入钥匙串 ==")
        probe_calls["n"] = 0
        hc.CONFIG_FILE.unlink(missing_ok=True)
        stored = {"acct": None, "secret": None}

        def fake_store(acct, secret):
            stored["acct"], stored["secret"] = acct, secret
            return True, "已存入系统钥匙串"
        orig_store = hc._keychain_store
        hc._keychain_store = fake_store
        on_args.store_password = True
        out, _ = capture(hc.cmd_onboard, on_args)
        check("onboard 成功并保管密码", out.get("ok") is True, str(out)[:250])
        check("确实写入了钥匙串", stored["acct"] == "testadmin"
              and stored["secret"] == "TestPass123!", str(stored))
        check("回报已存入钥匙串", out.get("password_stored_in_keychain") is True,
              str(out.get("password_stored_in_keychain")))
        hc._keychain_store = orig_store
        on_args.store_password = None

        print("\n== onboard：密码从环境变量读取 ==")
        probe_calls["n"] = 0
        hc.CONFIG_FILE.unlink(missing_ok=True)
        on_args.password = None
        on_args.password_env = "TEST_HA_PW"
        os.environ["TEST_HA_PW"] = "FromEnv12345!"
        out, _ = capture(hc.cmd_onboard, on_args)
        check("从环境变量取到密码", out.get("ok") is True, str(out)[:250])
        check("标明密码来源是环境变量",
              out.get("password_source") == "环境变量 TEST_HA_PW", str(out.get("password_source")))
        del os.environ["TEST_HA_PW"]
        on_args.password_env = None
        on_args.password = "TestPass123!"

        print("\n== onboard：从钥匙串取密码 ==")
        probe_calls["n"] = 0
        hc.CONFIG_FILE.unlink(missing_ok=True)
        orig_kc = hc._keychain_load
        on_args.password = None
        on_args.use_stored_password = True
        hc._keychain_load = lambda acct: (True, "StoredPass123!")
        out, _ = capture(hc.cmd_onboard, on_args)
        check("用钥匙串密码完成 onboard", out.get("ok") is True, str(out)[:250])
        check("标明密码来源是钥匙串",
              out.get("password_source") == "系统钥匙串", str(out.get("password_source")))

        hc._keychain_load = lambda acct: (False, "")
        probe_calls["n"] = 0
        hc.CONFIG_FILE.unlink(missing_ok=True)
        out, _ = capture(hc.cmd_onboard, on_args)
        check("钥匙串没记录时明确报错",
              out.get("ok") is False and "钥匙串里没有" in str(out.get("error", "")), str(out)[:200])
        hc._keychain_load = orig_kc
        on_args.use_stored_password = None
        on_args.password = "TestPass123!"

        print("\n== onboard：位置/语言/时区 ==")
        probe_calls["n"] = 0
        hc.CONFIG_FILE.unlink(missing_ok=True)
        hc._apply_core_config = lambda *a, **k: {
            "step": "设置位置/语言/时区", "ok": True, "detail": "已设置：location_name、time_zone"}
        out, _ = capture(hc.cmd_onboard, on_args)
        steps = {s["step"]: s for s in out.get("steps", [])}
        check("位置设置步骤被记录", "设置位置/语言/时区" in steps, str(list(steps)))
        hc._apply_core_config = lambda *a, **k: {}

        print("\n== onboard：失败路径不写配置 ==")
        hc.CONFIG_FILE.unlink(missing_ok=True)
        probe_calls["n"] = 0  # 重置桩，让前置检查仍走 uninitialized 分支
        hc._onboard_admin = lambda *a, **k: (False, "", "该实例已经初始化过了")
        out, code = capture(hc.cmd_onboard, on_args)
        check("建账号失败时中止", out.get("ok") is False and code == 1, str(out)[:200])
        check("失败时不写 config.json", not hc.CONFIG_FILE.exists(), str(hc.CONFIG_FILE))

        probe_calls["n"] = 0  # 重置桩，避免前一次调用影响
        hc._onboard_admin = lambda *a, **k: (True, "AUTHCODE", "ok")
        hc._exchange_auth_code = lambda *a, **k: (False, "", "换取令牌失败")
        out, _ = capture(hc.cmd_onboard, on_args)
        check("换令牌失败时中止", out.get("ok") is False, str(out)[:200])
        check("提示账号已建、可手动取令牌", "账号已创建成功" in str(out.get("hint", "")), str(out)[:250])
        check("仍不写 config.json", not hc.CONFIG_FILE.exists(), str(hc.CONFIG_FILE))
    finally:
        (hc._probe_state, hc._onboard_admin, hc._exchange_auth_code,
         hc._create_long_lived_token, hc._complete_onboarding_steps,
         hc._apply_core_config) = orig_funcs
        restore_cfg()
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n== devices：设备清单 ==")
    dev_args = Args()
    orig_request = hc.request
    try:
        hc.request = lambda *a, **k: [
            {"entity_id": "light.living_room", "state": "on",
             "attributes": {"friendly_name": "客厅主灯"}},
            {"entity_id": "light.bedroom", "state": "off",
             "attributes": {"friendly_name": "卧室灯"}},
            {"entity_id": "switch.kettle", "state": "off",
             "attributes": {"friendly_name": "热水壶"}},
            {"entity_id": "climate.bedroom", "state": "cool",
             "attributes": {"friendly_name": "卧室空调"}},
            {"entity_id": "cover.curtain", "state": "open",
             "attributes": {"friendly_name": "客厅窗帘"}},
            {"entity_id": "lock.front_door", "state": "locked",
             "attributes": {"friendly_name": "大门"}},
            {"entity_id": "sensor.temperature", "state": "24.5",
             "attributes": {"friendly_name": "客厅温度", "device_class": "temperature"}},
            {"entity_id": "sensor.backup_manager_state", "state": "idle", "attributes": {}},
            {"entity_id": "zone.home", "state": "1", "attributes": {}},
            {"entity_id": "person.someone", "state": "home", "attributes": {}},
            {"entity_id": "update.hacs", "state": "on", "attributes": {}},
        ]
        out, _ = capture(hc.cmd_devices, dev_args, {}, True)
        check("devices 返回成功", out.get("ok") is True, str(out)[:200])
        check("设备总数正确（排除内部实体）", out.get("total_devices") == 7,
              str(out.get("total_devices")))
        check("过滤掉 zone/person/update",
              "zone" not in out.get("domains", {}) and "person" not in out.get("domains", {}),
              str(list(out.get("domains", {}))))
        check("过滤掉 backup 内部传感器",
              not any("backup" in e["entity_id"]
                      for e in out.get("devices", {}).get("sensor", [])),
              str(out.get("devices", {}).get("sensor", [])))
        check("识别出敏感域 cover 与 lock",
              set(out.get("sensitive_domains_present", [])) == {"cover", "lock"},
              str(out.get("sensitive_domains_present")))
        lights = out.get("devices", {}).get("light", [])
        check("灯标记为可控", lights and lights[0].get("controllable") is True, str(lights[:1]))
        sensors = out.get("devices", {}).get("sensor", [])
        check("sensor 标记为不可控", sensors and sensors[0].get("controllable") is False,
              str(sensors[:1]))
        check("保留中文友好名",
              any(e["entity_id"] == "light.living_room" and e["friendly_name"] == "客厅主灯"
                  for e in lights), str(lights))
    finally:
        hc.request = orig_request

    print("\n== install-integration：参数校验 ==")
    inst_args = Args()
    inst_args.name = "not_exist"
    inst_args.container = "homeassistant"
    inst_args.force = False
    inst_args.dry_run = False
    inst_args.wait = 1.0
    out, code = capture(hc.cmd_install_integration, inst_args)
    check("未知集成名被拒", out.get("ok") is False and "available" in out, str(out)[:200])
    check("列出可用集成",
          set(out.get("available", {})) == {"xiaomi_home", "xiaomi_miot"},
          str(out.get("available")))

    orig_probe2 = hc._probe_state
    try:
        inst_args.name = "xiaomi_home"
        hc._probe_state = lambda *a, **k: (False, "down", "无法连接")
        out, _ = capture(hc.cmd_install_integration, inst_args)
        check("HA 未运行时拒绝安装",
              out.get("ok") is False and "没有运行中" in str(out.get("error", "")), str(out)[:200])
    finally:
        hc._probe_state = orig_probe2

    print("\n== install-integration：配置目录定位 ==")
    orig_mount, orig_exists = hc._ha_config_dir_from_docker, None
    import tempfile as _tf
    int_tmp = Path(_tf.mkdtemp(prefix="ha-inst-selftest-"))
    try:
        hc._ha_config_dir_from_docker = lambda c="": str(int_tmp)
        hc._probe_state = lambda *a, **k: (True, "ready", "2026.9.1")
        inst_args.name = "xiaomi_home"
        inst_args.container = "test"
        inst_args.dry_run = True
        inst_args.force = False
        out, _ = capture(hc.cmd_install_integration, inst_args)
        check("dry-run 返回下载计划", out.get("dry_run") is True, str(out)[:300])
        check("指向正确的配置目录", out.get("config_dir") == str(int_tmp), str(out.get("config_dir")))
        check("写明下载地址含仓库名",
              "XiaoMi/ha_xiaomi_home" in str(out.get("would_download", "")),
              str(out.get("would_download")))

        # 已安装的幂等分支
        (int_tmp / "custom_components" / "xiaomi_home").mkdir(parents=True)
        inst_args.dry_run = False
        out, _ = capture(hc.cmd_install_integration, inst_args)
        check("已装过时提示而不重装", out.get("already_installed") is True, str(out)[:250])
    finally:
        hc._ha_config_dir_from_docker = orig_mount
        hc._probe_state = orig_probe2
        import shutil as _sh
        _sh.rmtree(int_tmp, ignore_errors=True)

    print("\n== 凭据保管：钥匙串 ==")
    auth_args = Args()
    auth_args.username = None
    auth_args.show = False
    auth_args.forget = False
    out, _ = capture(hc.cmd_auth, auth_args)
    check("auth 缺用户名时报错", out.get("ok") is False, str(out)[:200])

    orig_kc_funcs = (hc._keychain_load, hc._keychain_delete, hc._keychain_supported)
    try:
        auth_args.username = "someuser"
        hc._keychain_load = lambda acct: (False, "")
        out, _ = capture(hc.cmd_auth, auth_args)
        check("钥匙串无记录时明确报错",
              out.get("ok") is False and "没有" in str(out.get("error", "")), str(out)[:200])

        hc._keychain_load = lambda acct: (True, "SuperSecret123")
        out, _ = capture(hc.cmd_auth, auth_args)
        check("查到时默认不显示明文", out.get("password") is None, str(out)[:250])
        check("给出掩码长度", out.get("masked") == "*" * 12, str(out.get("masked")))
        check("报告密码长度", out.get("length") == 14, str(out.get("length")))

        auth_args.show = True
        out, _ = capture(hc.cmd_auth, auth_args)
        check("显式 --show 才给明文",
              out.get("password") == "SuperSecret123", str(out)[:250])
        auth_args.show = False

        deleted = {"called": False}

        def fake_delete(acct):
            deleted["called"] = True
            return True
        hc._keychain_delete = fake_delete
        auth_args.forget = True
        out, _ = capture(hc.cmd_auth, auth_args)
        check("--forget 调用删除", deleted["called"] is True, str(out)[:200])
    finally:
        (hc._keychain_load, hc._keychain_delete, hc._keychain_supported) = orig_kc_funcs

    print("\n== env：环境自动探测 ==")
    env_args = Args()
    orig_probe3 = hc._probe_state
    orig_run = hc._run_cmd
    try:
        hc._probe_state = lambda *a, **k: (False, "down", "无法连接")
        hc._run_cmd = lambda cmd, timeout=120.0: (1, "", "no such container")
        # 容器不可用时回退到宿主机 /etc/localtime（当前机器是 Asia/Shanghai）
        out, _ = capture(hc.cmd_env, env_args)
        check("env 返回成功", out.get("ok") is True, str(out)[:200])
        check("探测到时区", bool(out.get("time_zone")), str(out.get("time_zone")))
        check("时区格式为 IANA（含 /）", "/" in str(out.get("time_zone")), str(out.get("time_zone")))
        check("给出城市名", bool(out.get("city_guess")), str(out.get("city_guess")))
        check("给出默认用户名", bool(out.get("username_guess")), str(out.get("username_guess")))
        check("说明不用问用户", "不用问用户" in str(out.get("note", "")), str(out.get("note")))

        # 容器可用时优先读容器的时区
        hc._run_cmd = lambda cmd, timeout=120.0: (0, "Europe/Berlin\n", "")
        out, _ = capture(hc.cmd_env, env_args)
        check("优先采用容器时区", out.get("time_zone") == "Europe/Berlin", str(out.get("time_zone")))
        check("标明来源是容器", "容器" in str(out.get("time_zone_source", "")),
              str(out.get("time_zone_source")))
    finally:
        hc._probe_state, hc._run_cmd = orig_probe3, orig_run

    print("\n== 时区→城市映射 ==")
    check("Asia/Shanghai → 上海", hc.tz_to_city("Asia/Shanghai") == "上海")
    check("未知时区回退到末段",
          hc.tz_to_city("America/Some_Place") == "Some Place",
          hc.tz_to_city("America/Some_Place"))
    check("Europe/London 有中文名", hc.tz_to_city("Europe/London") == "伦敦")

    print("\n== 用户名自动探测 ==")
    uname = hc._detect_username()
    check("探测到用户名", bool(uname), uname)
    check("用户名只含安全字符",
          all(c.isalnum() or c in "-_" for c in uname), uname)
    check("绝不为空", len(uname) > 0, uname)

    print("\n" + "=" * 60)
    print("通过 {} 项，失败 {} 项".format(len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for name in FAIL:
            print("  - " + name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
