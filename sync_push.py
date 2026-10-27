"""
同步并推送 (sync_push.py)

做三件事：
1. 把本工作区最新的会话记录归档到 `会话记录/`
2. git add -A + commit
3. git push

用法：
  python sync_push.py                 # 自动生成提交信息
  python sync_push.py -m "补完第三条对标拆解"
  python sync_push.py --dry-run       # 只看会提交什么，不实际提交

注意：`.gitignore` 里默认挡住了 `会话记录/`（因为仓库是公开的）。
把仓库改成 private 后，删掉那一行，对话全文才会一起同步。
"""

from __future__ import annotations

import argparse
import datetime
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOME = Path.home() / ".workbuddy-ai" / "projects"


def workspace_key(path: Path) -> str:
    """把 E:\\Workbuddy\\xxx 转成 .workbuddy-ai/projects 下的目录名 e-Workbuddy-xxx"""
    s = str(path).replace("\\", "/")
    s = re.sub(r"^([A-Za-z]):", lambda m: m.group(1).lower(), s)
    return s.replace("/", "-").lstrip("-")


def archive_sessions() -> list[str]:
    """把本工作区的会话 jsonl 复制到 会话记录/，返回新增/更新的文件名"""
    src_dir = HOME / workspace_key(ROOT)
    if not src_dir.is_dir():
        print(f"  (没找到会话目录: {src_dir})")
        return []

    dst_dir = ROOT / "会话记录"
    dst_dir.mkdir(exist_ok=True)

    changed = []
    for f in sorted(src_dir.glob("*.jsonl")):
        if "rollback" in f.name:
            continue
        dst = dst_dir / f"{f.stem[:8]}.jsonl"
        if dst.exists() and dst.stat().st_size == f.stat().st_size:
            continue
        shutil.copy2(f, dst)
        changed.append(dst.name)
    return changed


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="归档会话 + 提交 + 推送")
    ap.add_argument("-m", "--message", default=None, help="提交信息")
    ap.add_argument("--dry-run", action="store_true", help="只显示状态，不提交")
    ap.add_argument("--no-session", action="store_true", help="跳过会话归档")
    args = ap.parse_args(argv)

    if not (ROOT / ".git").is_dir():
        print("当前目录还不是 git 仓库，先运行：git init -b main", file=sys.stderr)
        return 1

    print("[1/3] 归档会话记录")
    if args.no_session:
        print("  已跳过")
    else:
        changed = archive_sessions()
        print(f"  {len(changed)} 个会话有更新" + (f"：{', '.join(changed)}" if changed else ""))

    print("[2/3] 暂存改动")
    r = git("add", "-A")
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        return 1

    status = git("status", "--porcelain").stdout.strip()
    if not status:
        print("  没有改动，无需提交")
        return 0

    n = len(status.splitlines())
    print(f"  {n} 个文件有改动")
    if args.dry_run:
        print(status)
        return 0

    msg = args.message or f"sync: {datetime.datetime.now():%Y-%m-%d %H:%M}"
    r = git("commit", "-m", msg)
    if r.returncode != 0:
        print(r.stdout + r.stderr, file=sys.stderr)
        return 1
    print(f"  已提交：{msg}")

    print("[3/3] 推送")
    r = git("push")
    if r.returncode != 0:
        print(r.stdout + r.stderr, file=sys.stderr)
        print("\n推送失败。常见原因：没配凭据 / 远端有更新（先 git pull --rebase）")
        return 1
    print("  已推送")
    return 0


if __name__ == "__main__":
    sys.exit(main())
