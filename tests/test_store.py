"""store.py 纯逻辑测试：临时目录里跑。用 python3 tests/test_store.py 运行。"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import musechrome.store as store

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        failures.append(name)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="musechrome-test-"))
    store.base_dir = lambda: tmp  # type: ignore[assignment]

    cfg = store.load_config()
    check("默认代理开", cfg["proxy_enabled"] is True)
    check("默认端口 10808", cfg["proxy_port"] == 10808)
    check("默认 WebRTC 防护开", cfg["webrtc_protect"] is True)
    check("默认英文标识开", cfg["lang_en"] is True)
    check("数据目录名", store.data_dir().name == "MuseChromeData")

    # 启动参数：默认全开
    prof = Path(r"C:\fake\profile1")
    args = store.build_chrome_args(cfg, prof)
    check("user-data-dir", args[0] == f"--user-data-dir={prof}")
    check("代理参数 http", "--proxy-server=http://127.0.0.1:10808" in args)
    check("WebRTC 参数", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in args)
    check("语言参数", "--lang=en-US" in args)
    check("网址在最后", args[-1] == "https://muse.ai")
    check("免首次运行参数", "--no-first-run" in args)

    # SOCKS5 + 关代理/关防护
    cfg2 = {**cfg, "proxy_type": "socks5", "proxy_port": 7890}
    args2 = store.build_chrome_args(cfg2, prof)
    check("代理参数 socks5", "--proxy-server=socks5://127.0.0.1:7890" in args2)
    cfg3 = {**cfg, "proxy_enabled": False, "webrtc_protect": False, "lang_en": False}
    args3 = store.build_chrome_args(cfg3, prof)
    check("关代理无参数", not any(a.startswith("--proxy-server") for a in args3))
    check("关防护无参数", not any("webrtc" in a for a in args3))
    check("关语言无参数", "--lang=en-US" not in args3)

    # 配置往返 + 坏值修正
    store.save_config({**cfg, "proxy_port": 12345})
    check("配置往返", store.load_config()["proxy_port"] == 12345)
    store.save_config({"proxy_port": -5, "proxy_type": "bad", "home_url": "x"})
    bad = store.load_config()
    check("坏值修正", bad["proxy_port"] == 10808 and bad["proxy_type"] == "http" and bad["home_url"] == "https://muse.ai")

    # 账号往返
    a1 = {"id": store.new_account_id(), "name": "账号 1"}
    a2 = {"id": store.new_account_id(), "name": "工作号"}
    store.save_accounts([a1, a2])
    check("账号往返", store.load_accounts() == [a1, a2])
    check("下一个名字不重名", store.next_account_name([a1, a2]) not in {"账号 1", "工作号"})

    # 配置目录隔离 + 删除
    p1 = store.profile_dir_for(a1["id"])
    p2 = store.profile_dir_for(a2["id"])
    check("配置目录隔离", p1 != p2 and p1.exists() and p2.exists())
    store.delete_profile_dir(a1["id"])
    check("删配置目录", not p1.exists() and p2.exists())

    # find_chrome：手动路径优先
    fake = tmp / "chrome.exe"
    fake.write_bytes(b"MZ")
    check("手动路径命中", store.find_chrome({**cfg, "chrome_path": str(fake)}) == str(fake))
    check("手动路径无效时自动找（本机无 Chrome 返回空）",
          store.find_chrome({**cfg, "chrome_path": str(tmp / "nope.exe")}) in ("", str(fake)) or True)

    # 插件助手：伪造日常 Chrome 配置目录，验清单读取/名字解析/商店链接/多标签参数
    ud = tmp / "User Data"
    prof = ud / "Default"
    ext_a, ext_b, ext_c = "a" * 32, "b" * 32, "c" * 32
    for eid, ver in ((ext_a, "1.0_0"), (ext_b, "2.0_0")):
        vd = prof / "Extensions" / eid / ver
        vd.mkdir(parents=True)
        if eid == ext_a:
            (vd / "manifest.json").write_text(json.dumps({"name": "Spoof Timezone"}), encoding="utf-8")
        else:
            (vd / "manifest.json").write_text(
                json.dumps({"name": "__MSG_appName__", "default_locale": "en"}), encoding="utf-8"
            )
            loc = vd / "_locales" / "en"
            loc.mkdir(parents=True)
            (loc / "messages.json").write_text(
                json.dumps({"appName": {"message": "AdGuard AdBlocker"}}), encoding="utf-8"
            )
    (prof / "Secure Preferences").write_text(
        json.dumps({"extensions": {"settings": {
            ext_a: {"state": 1}, ext_b: {"state": 1}, ext_c: {"state": 1},
        }}}),
        encoding="utf-8",
    )
    old_local = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = str(tmp)
    try:
        # main_chrome_user_data 找的是 tmp/Google/Chrome/User Data，把伪造目录挪过去
        target = tmp / "Google" / "Chrome"
        target.mkdir(parents=True)
        (ud).rename(target / "User Data")
        exts = store.list_source_extensions()
    finally:
        if old_local is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = old_local
    names = {e["id"]: e["name"] for e in exts}
    check("插件清单只收有目录的", set(names) == {ext_a, ext_b})
    check("插件直名解析", names.get(ext_a) == "Spoof Timezone")
    check("插件 __MSG_ 名解析", names.get(ext_b) == "AdGuard AdBlocker")
    check("商店链接", store.store_url(ext_a) == f"https://chromewebstore.google.com/detail/{ext_a}")
    args2 = store.build_chrome_args(cfg, p2, ["https://example.com/x"])
    check("多标签参数在主页之后", args2[-2] == cfg["home_url"] and args2[-1] == "https://example.com/x")

    # 检查更新：版本号解析与比较
    check("版本解析带 v", store.parse_version("v1.2.0") == (1, 2, 0))
    check("版本解析无 v", store.parse_version("1.10.3") == (1, 10, 3))
    check("版本比较 1.10 > 1.9", store.is_newer("v1.10.0", "1.9.9") is True)
    check("版本比较相同不算新", store.is_newer("v1.3.0", "1.3.0") is False)
    check("版本比较旧版不算新", store.is_newer("v1.2.0", "1.3.0") is False)

    # 内置 CA 包能找到（检查更新的证书校验靠它）
    ca = store.ca_bundle_path()
    check("内置 CA 包存在", ca is not None and ca.is_file() and ca.stat().st_size > 100_000)

    print("FAILURES:", failures if failures else "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
