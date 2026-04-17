#!/usr/bin/env python
"""清理根目录随机命名文件 + logs 下无序/历史 log 目录，仅保留 e2e_demo。"""
from pathlib import Path

# 根目录允许的文件与目录（与 .cursor/rules/no-random-files-in-root.mdc 一致）
ALLOWED_FILES = {
    ".gitignore",
    "README.md",
    "pyproject.toml",
    "requirements.txt",
    "run_phase0_demo.py",
    "run_phase1_demo.py",
    "run_phase1_real_rpa.py",
    "run_demo.py",
    "run_server.py",
    "run_poffices_agent.py",
    "progress.json",
    "progress.html",
}

# logs 下仅保留的子目录（当前主入口 run_demo.py 写入 e2e_demo）
LOGS_KEEP_DIRS = {"e2e_demo"}


def is_random_filename(name: str) -> bool:
    """判断是否为随机字母数字命名（无扩展名）。"""
    if "." in name:
        return False
    if name in ALLOWED_FILES:
        return False
    if len(name) < 4 or len(name) > 25:
        return False
    return all(c.isalnum() or c == "_" for c in name)


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    deleted = []

    # 1. 根目录随机文件
    for p in root.iterdir():
        if not p.is_file():
            continue
        if p.name in ALLOWED_FILES:
            continue
        if is_random_filename(p.name):
            try:
                p.unlink()
                deleted.append(p.name)
            except OSError as e:
                print(f"无法删除 {p.name}: {e}")

    # 2. logs 下无序/历史子目录中的文件（旧实验目录名、showcase、real_rpa 等）
    log_dir = root / "logs"
    if log_dir.is_dir():
        for sub in log_dir.iterdir():
            if not sub.is_dir() or sub.name in LOGS_KEEP_DIRS:
                continue
            for f in sub.iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                        deleted.append(f.relative_to(root))
                    except OSError as e:
                        print(f"无法删除 {f}: {e}")

    print(f"已删除 {len(deleted)} 个无序/杂乱文件")
    if deleted:
        for n in deleted[:25]:
            print(f"  - {n}")
        if len(deleted) > 25:
            print(f"  ... 及另外 {len(deleted) - 25} 个")


if __name__ == "__main__":
    main()
