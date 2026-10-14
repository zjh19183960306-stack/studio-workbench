#!/usr/bin/env python3
"""Watch link-download dir; hardlink completed videos into the video library."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

SRC = Path("/root/autodl-tmp/studio/链接下载")
DST = Path("/root/autodl-tmp/videos")
STATE = Path("/root/autodl-tmp/studio/.link_downloads/vlib_auto_state.json")
LOG = Path("/root/autodl-tmp/vlib_auto_link.log")
MIN_BYTES = 100 * 1024
STABLE_SECS = 8
POLL_SECS = 5
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
SKIP_SUFFIX = {
    ".aria2",
    ".xltd",
    ".part",
    ".tmp",
    ".!bt",
    ".download",
    ".crdownload",
    ".baiduyun.p.downloading",
}


def log(msg: str) -> None:
    line = time.strftime("%F %T") + " " + msg
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"seen": {}, "linked_inos": []}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE)


def dst_inodes() -> dict[int, Path]:
    out: dict[int, Path] = {}
    if not DST.exists():
        return out
    for p in DST.iterdir():
        try:
            if p.is_symlink():
                st = p.stat()
            elif p.is_file():
                st = p.stat()
            else:
                continue
            out[st.st_ino] = p
        except OSError:
            continue
    return out


def is_incomplete(p: Path) -> bool:
    name = p.name
    low = name.lower()
    for s in SKIP_SUFFIX:
        if low.endswith(s):
            return True
    if (p.parent / (name + ".aria2")).exists():
        return True
    # aria2 control file beside basename without ext variants
    if Path(str(p) + ".aria2").exists():
        return True
    return False


def candidates() -> list[Path]:
    if not SRC.is_dir():
        return []
    found: list[Path] = []
    for p in SRC.rglob("*"):
        try:
            if not p.is_file() or p.is_symlink():
                continue
            if any(part.startswith(".") for part in p.relative_to(SRC).parts[:-1]):
                continue
            if p.suffix.lower() not in VIDEO_EXT:
                continue
            if is_incomplete(p):
                continue
            st = p.stat()
            if st.st_size <= MIN_BYTES:
                continue
            found.append(p)
        except OSError:
            continue
    return found


def unique_dest(name: str) -> Path:
    base = Path(name).name
    if not base or base.startswith("."):
        base = "vlib_auto.bin"
    stem = Path(base).stem
    suf = Path(base).suffix
    dest = DST / base
    n = 2
    while dest.exists() or dest.is_symlink():
        dest = DST / f"{stem}_{n}{suf}"
        n += 1
        if n > 9999:
            raise RuntimeError("too many name collisions")
    return dest


def try_link(src: Path, st_ino: int, linked: set[int]) -> str:
    DST.mkdir(parents=True, exist_ok=True)
    existing = dst_inodes()
    if st_ino in existing or st_ino in linked:
        return "skip_same_inode"

    # Replace symlink that already points at this file with a hardlink.
    for p in list(DST.iterdir()):
        try:
            if p.is_symlink() and p.resolve() == src.resolve():
                p.unlink()
                break
        except OSError:
            continue

    existing = dst_inodes()
    if st_ino in existing:
        return "skip_same_inode"

    dest = unique_dest(src.name)
    try:
        os.link(src, dest)
    except OSError as e:
        return f"error:{e}"
    linked.add(st_ino)
    return f"linked:{dest.name}:size={src.stat().st_size}"


def main() -> int:
    log("vlib_auto_link start")
    st = load_state()
    seen: dict[str, list] = {k: v for k, v in st.get("seen", {}).items()}
    linked: set[int] = set(int(x) for x in st.get("linked_inos", []))
    linked |= set(dst_inodes())

    while True:
        now = time.time()
        changed = False
        for p in candidates():
            key = str(p)
            try:
                s = p.stat()
            except OSError:
                continue
            size, mtime, ino = s.st_size, s.st_mtime, s.st_ino
            if ino in linked:
                continue
            prev = seen.get(key)
            if not prev or prev[0] != size or prev[1] != mtime:
                seen[key] = [size, mtime, now]
                changed = True
                continue
            if now - prev[2] < STABLE_SECS:
                continue
            result = try_link(p, ino, linked)
            log(f"{result} key_hash={abs(hash(key))%10**8} size={size}")
            if result.startswith("linked") or result.startswith("skip"):
                seen.pop(key, None)
                changed = True
            else:
                # keep waiting on transient errors
                seen[key] = [size, mtime, now]
                changed = True

        # drop stale seen entries for vanished paths
        for k in list(seen):
            if not Path(k).is_file():
                seen.pop(k, None)
                changed = True

        if changed:
            save_state({"seen": seen, "linked_inos": sorted(linked)})
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)