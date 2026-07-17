from bot.tools.kunpeng.ksys import KsysTool
from bot.tools.kunpeng.tuner import TunerTool


def register_kunpeng_tools(registry) -> None:
    registry.register(KsysTool())
    registry.register(TunerTool())


__all__ = ["KsysTool", "TunerTool", "register_kunpeng_tools"]
