from bot.core.models import RunRequest, RunResult

__all__ = ["AgentRunner", "RunRequest", "RunResult"]


def __getattr__(name: str):
    if name == "AgentRunner":
        from bot.core.agent import AgentRunner

        return AgentRunner
    raise AttributeError(name)
