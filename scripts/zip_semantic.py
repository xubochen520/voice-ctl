"""把 dist-semantic 打成发布用的 zip。

为什么要 zip 而不是直接传目录：GitHub Release 的资产是单个文件，
而语义版是**目录版**（1.8GB 不能每次启动解包到临时目录，见 RELEASE-NOTES-0.3.4）。

两件必须做对的事：
  1. **保留顶层 `voice-ctl/` 目录**。否则用户解压时 exe 和 _internal 会被
     平铺到他选的目录里，和已有文件混在一起——目录版的结构是 exe 必须
     挨着 _internal，平铺错了就起不来。
  2. **走 ZIP_DEFLATED 但要逐文件判断**。873MB 的 int8 ONNX 已经量化过，
     几乎不可压（省不到 1%），压它只是白烧几分钟 CPU；而 Python 源码、
     tokenizer.json、DLL 都能压掉不少。所以按后缀区分。
"""
from __future__ import annotations

import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "dist-semantic" / "voice-ctl"
OUT = ROOT / "dist-semantic" / "voice-ctl-semantic.zip"

# 已经压过的二进制，再压几乎不省空间，却要多花几分钟
STORE_EXT = {".onnx", ".gguf", ".zip", ".gz", ".bz2", ".xz", ".7z"}


def main() -> int:
    if not SRC.is_dir():
        print(f"✗ 找不到 {SRC}")
        return 2

    files = sorted(p for p in SRC.rglob("*") if p.is_file())
    total = sum(p.stat().st_size for p in files)
    print(f"打包 {len(files)} 个文件，共 {total / 1024**3:.2f} GB -> {OUT.name}")

    if OUT.exists():
        OUT.unlink()

    t0 = time.perf_counter()
    stored = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for i, p in enumerate(files, 1):
            # 保留顶层 voice-ctl/，解压出来就是个完整目录
            arc = Path(SRC.name) / p.relative_to(SRC)
            if p.suffix.lower() in STORE_EXT:
                zi = zipfile.ZipInfo(str(arc), date_time=time.localtime(p.stat().st_mtime)[:6])
                zi.compress_type = zipfile.ZIP_STORED
                zi.external_attr = 0o644 << 16
                with open(p, "rb") as fh, zf.open(zi, "w") as dst:
                    while chunk := fh.read(8 << 20):
                        dst.write(chunk)
                stored += 1
            else:
                zf.write(p, arc)
            if i % 500 == 0 or p.suffix.lower() in STORE_EXT:
                print(f"  [{i}/{len(files)}] {arc}", flush=True)

    took = time.perf_counter() - t0
    out_mb = OUT.stat().st_size / 1024**2
    print(f"\n✓ 完成：{OUT}")
    print(f"  体积   {out_mb:.0f} MB（原始 {total / 1024**2:.0f} MB，"
          f"压掉 {100 * (1 - OUT.stat().st_size / total):.0f}%）")
    print(f"  其中 {stored} 个文件按不压缩存入（已压过的二进制）")
    print(f"  耗时   {took:.0f} 秒")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
