"""把 0.3.0 / 0.3.1 / 0.3.2 三个 Release 建到 GitHub 上，并挂上 exe。

沿用已有两个 Release 的命名与资产命名（voice-ctl-<版本>-win64-{lite,full}.exe）。
只有 0.3.2 还留着 exe 产物，所以只有它挂资产——旧版本的 exe 已经不在磁盘上了，
重新构建旧版本没有意义（源码在 tag 里，谁要谁自己 build）。

依赖：git credential manager 里的 token（走本机 127.0.0.1:7890 代理）。
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO = "xubochen520/voice-ctl"
PROXY = "http://127.0.0.1:7890"


def token() -> str:
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    for line in out.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    raise SystemExit("拿不到 GitHub token（git credential fill 没返回 password）")


TOK = token()


def opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"https": PROXY, "http": PROXY})
    )


def headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    h = {
        "Authorization": f"Bearer {TOK}",
        "User-Agent": "voice-ctl-release",
        "Accept": "application/vnd.github+json",
    }
    if extra:
        h.update(extra)
    return h


def api(path: str, *, method: str = "GET", payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        "https://api.github.com" + path, data=data, headers=headers(), method=method
    )
    try:
        with opener().open(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        raise SystemExit(f"{method} {path} -> HTTP {e.code}: {body}") from e


def upload(release_id: int, path: pathlib.Path, name: str) -> None:
    size = path.stat().st_size
    print(f"    上传 {name}（{size / 1024 / 1024:.1f}MB）…", flush=True)
    url = (
        f"https://uploads.github.com/repos/{REPO}/releases/{release_id}/assets"
        f"?name={name}"
    )
    req = urllib.request.Request(
        url,
        data=path.read_bytes(),
        headers=headers({"Content-Type": "application/octet-stream"}),
        method="POST",
    )
    with opener().open(req, timeout=1800) as r:
        got = json.load(r)
    print(f"      -> {got['browser_download_url']}", flush=True)


VERSIONS = [
    ("v0.3.0", "voice-ctl 0.3.0 —— 意图层：听懂整句话", "docs/RELEASE-NOTES-0.3.0.md", {}),
    ("v0.3.1", "voice-ctl 0.3.1 —— 内置 llama.cpp", "docs/RELEASE-NOTES-0.3.1.md", {}),
    (
        "v0.3.2",
        "voice-ctl 0.3.2 —— 按住说话不再吃掉开头",
        "docs/RELEASE-NOTES-0.3.2.md",
        {
            "lite": ROOT / "dist" / "voice-ctl.exe",
            "full": ROOT / "dist-full" / "voice-ctl.exe",
        },
    ),
    (
        "v0.3.3",
        "voice-ctl 0.3.3 —— 打开网站、说错能收回、一句话两件事",
        "docs/RELEASE-NOTES-0.3.3.md",
        {
            "lite": ROOT / "dist" / "voice-ctl.exe",
            "full": ROOT / "dist-full" / "voice-ctl.exe",
        },
    ),
]


def main() -> int:
    existing = {r["tag_name"] for r in api(f"/repos/{REPO}/releases")}
    print(f"已有 Release：{sorted(existing)}\n")
    for tag, name, notes_rel, assets in VERSIONS:
        notes = (ROOT / notes_rel).read_text(encoding="utf-8")
        if tag in existing:
            print(f"{tag} 已存在，跳过创建")
            rel = next(r for r in api(f"/repos/{REPO}/releases") if r["tag_name"] == tag)
        else:
            print(f"创建 Release {tag}：{name}")
            rel = api(
                f"/repos/{REPO}/releases",
                method="POST",
                payload={
                    "tag_name": tag,
                    "name": name,
                    "body": notes,
                    "draft": False,
                    "prerelease": False,
                },
            )
            print(f"  -> {rel['html_url']}")
        have = {a["name"] for a in rel.get("assets", [])}
        for kind, path in assets.items():
            fname = f"voice-ctl-{tag.lstrip('v')}-win64-{kind}.exe"
            if fname in have:
                print(f"    {fname} 已存在，跳过")
                continue
            if not path.is_file():
                print(f"    ⚠ 缺文件 {path}，跳过 {fname}")
                continue
            upload(rel["id"], path, fname)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
