"""Prompt 加载器 — 从 prompts/ 目录读取 .md 提示词模板

用法:
    from services.prompt_loader import get_prompt
    prompt = get_prompt("planner.md")
    prompt = get_prompt("agent_step_planner.md", role="backend", goal="...")
"""

from pathlib import Path

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


def get_prompt(name: str, **kwargs: str) -> str:
    """Load a prompt template from prompts/{name} and format with kwargs."""
    path = _PROMPT_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    content = path.read_text(encoding="utf-8")
    if kwargs:
        try:
            content = content.format(**kwargs)
        except KeyError as e:
            raise KeyError(f"Prompt {name} 有未替换的占位符: {e}")
    return content
