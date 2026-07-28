class DeliveryError(Exception):
    """交付编排基础错误。"""


class ConfigurationError(DeliveryError):
    """项目配置无效。"""


class BlockedError(DeliveryError):
    """需要人工决策。"""


class TerminalError(DeliveryError):
    """无法自动恢复。"""
