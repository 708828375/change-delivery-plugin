"""Change Delivery 命令行引导入口。"""

import argparse
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """解析基础命令行参数，不执行交付副作用。"""
    parser = argparse.ArgumentParser(
        prog="change-delivery",
        description="Change Delivery local delivery orchestrator for Codex.",
    )
    parser.parse_args(argv)
    return 0
