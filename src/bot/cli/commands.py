"""One catalog for command discovery, completion and busy-state routing."""

from dataclasses import dataclass

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    arguments: str = ""
    during_run: bool = False
    optional_arguments: bool = False

    def matches(self, text: str) -> bool:
        return (
            text == self.name
            and (not self.arguments or self.optional_arguments)
            or (bool(self.arguments) and text.startswith(self.name + " "))
        )


COMMANDS = (
    CommandSpec("/help", "查看命令和快捷键", during_run=True),
    CommandSpec("/status", "查看会话、上下文和用量", during_run=True),
    CommandSpec("/tools", "列出可用工具", during_run=True),
    CommandSpec("/details", "查看工具列表或已保存的输出", "[编号|last] [字节偏移]", True, True),
    CommandSpec("/todo", "查看当前计划", during_run=True),
    CommandSpec("/agents", "查看后台任务", during_run=True),
    CommandSpec("/agents tasks", "查看后台任务", during_run=True),
    CommandSpec("/agents list", "列出 Agent 定义", during_run=True),
    CommandSpec("/agents reload", "重新加载 Agent"),
    CommandSpec("/agents trust", "信任当前项目 Agent 内容"),
    CommandSpec("/agents untrust", "取消项目 Agent 信任"),
    CommandSpec("/skills", "查看 Skill", during_run=True),
    CommandSpec("/skills reload", "重新扫描 Skill"),
    CommandSpec("/model", "查看模型", during_run=True),
    CommandSpec("/model", "切换本会话模型", "<模型名称>"),
    CommandSpec("/permissions", "查看权限", during_run=True),
    CommandSpec("/permissions", "切换权限模式", "<safe|read-only|full-access>"),
    CommandSpec("/compact", "压缩当前上下文"),
    CommandSpec("/compact rebuild", "重新构建上下文摘要"),
    CommandSpec("/compact rollback", "回滚上下文摘要", "<id>"),
    CommandSpec("/remember", "保存显式记忆", "<内容>"),
    CommandSpec("/memories", "查看长期记忆", during_run=True),
    CommandSpec("/forget", "删除记忆", "<id 或 key>"),
    CommandSpec("/memory extract", "提取记忆", "[run-id]", optional_arguments=True),
    CommandSpec("/new", "创建新会话"),
    CommandSpec("/cancel", "取消当前运行", during_run=True),
    CommandSpec("/exit", "退出 bot（运行中先 /cancel）"),
    CommandSpec("/quit", "退出 bot（运行中先 /cancel）"),
)


def find_command(text: str) -> CommandSpec | None:
    return next((item for item in COMMANDS if item.matches(text)), None)


class CommandCompleter(Completer):
    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or "\n" in text:
            return
        seen = set()
        choices = [(item.name, item.description) for item in COMMANDS]
        choices.extend(
            (f"/permissions {mode}", "切换权限模式")
            for mode in ("safe", "read-only", "full-access")
        )
        choices.append(("/details last", "最近一次工具调用的详情"))
        for name, description in choices:
            if name.startswith(text) and name not in seen:
                seen.add(name)
                yield Completion(name, start_position=-len(text), display_meta=description)
