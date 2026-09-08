from __future__ import annotations

import base64
import re

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from ui_tars.action_parser import parse_action_to_structure_output
from ui_tars.prompt import COMPUTER_USE_DOUBAO

from app.config import settings


class UITARSError(RuntimeError):
    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw


def build_instruction(goal: str, history: list[str], extra: str = "") -> str:
    parts = [goal.strip()]
    if extra:
        parts.append(extra.strip())
    if history:
        parts.append("已执行步骤:\n" + "\n".join(history[-8:]))
    return "\n\n".join(parts)


def render_computer_prompt(instruction: str) -> str:
    return COMPUTER_USE_DOUBAO.format(language="Chinese", instruction=instruction)


def create_llm() -> ChatOpenAI:
    if not settings.ark_api_key:
        raise UITARSError("未配置 ARK_API_KEY，请在 workflow-desktop/.env 中填写")
    return ChatOpenAI(
        api_key=settings.ark_api_key,
        base_url=settings.ark_base_url,
        model=settings.ark_model,
        temperature=0,
        max_tokens=512,
        timeout=120,
    )


async def infer_action(
    llm: ChatOpenAI,
    png_bytes: bytes,
    instruction: str,
    image_width: int,
    image_height: int,
) -> tuple[list[dict], str]:
    prompt = render_computer_prompt(instruction)
    b64 = base64.b64encode(png_bytes).decode("ascii")
    message = HumanMessage(
        content=[
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            },
        ]
    )
    response = await llm.ainvoke([message])
    raw = (response.content or "").strip()
    raw = _normalize_model_output(_strip_fences(raw))
    try:
        parsed = parse_action_to_structure_output(
            raw,
            factor=1000,
            origin_resized_height=image_height,
            origin_resized_width=image_width,
            model_type="doubao",
        )
    except Exception as exc:
        raise UITARSError(
            f"无法解析模型输出，已停止以免误点击: {exc}\n模型输出:\n{raw or '(空)'}",
            raw=raw,
        ) from exc
    if not parsed:
        raise UITARSError("模型没有返回可执行动作", raw=raw)
    return parsed, raw


_KNOWN_ACTIONS = (
    "click",
    "left_double",
    "right_single",
    "drag",
    "hotkey",
    "type",
    "scroll",
    "wait",
    "finished",
    "hover",
    "left_single",
    "select",
)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _normalize_model_output(text: str) -> str:
    """豆包偶尔只输出 wait() / finished()，解析器要求必须带 Action: 前缀。"""
    text = text.strip()
    if "Action:" in text:
        return text
    names = "|".join(_KNOWN_ACTIONS)
    match = re.search(
        rf"(?:^|\n)\s*((?:{names})\s*\(.*\))\s*$",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return text
    action = match.group(1).strip()
    prefix = text[: match.start(1)].strip()
    if prefix:
        return f"{prefix}\nAction: {action}"
    return f"Action: {action}"
