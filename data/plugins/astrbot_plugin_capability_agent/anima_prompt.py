"""Direct Anima prompt schema and local validation."""

from __future__ import annotations

CHARACTERS = {
    "alice": "aris (blue archive), halo, school uniform, fully clothed",
    "rio": "rio (blue archive), halo, fully clothed",
    "noa": "noa (blue archive), halo, fully clothed",
}

ANIMA_TOOL_DESCRIPTION = """直接用本地 Anima/ComfyUI 生成一张插画并发送。
普通文生图、本地生图、CG 默认调用本工具；不要交给 plugin_agent 再规划。
你就是当前对话中的爱丽丝，必须根据当前请求和已知场景，一次写完最终英文提示词。
把完整 positive prompt 填到 prompt，提交后由 ComfyUI 原样使用。
后端不会再把人物、人数、风格或质量词拼进 prompt，也不会启动其他 LLM 处理。
不要只转述“画一张”，不要等待另一个模型补充。
prompt 中应自行包含角色身份、外观、动作、构图、镜头、环境和风格等必要标签。
只画最新时刻的一幅画面，保持当前人物、位置、道具归属和动作一致，不擅自添加人物。
默认全身远景，明确需要时才用近景。保留当前衣着、颜色和有来源的光线。
【天童爱丽丝的默认绘图基线】
仅当画面中确实包含天童爱丽丝（Aris/Alice）时，采用以下已做本地对照的外观词；不把爱丽丝添加到其他角色的画面。
风格默认使用：blue archive style, best quality, very aesthetic, detailed anime illustration, clean lineart。
爱丽丝身份与外观使用：aris (blue archive), very long dark navy hair reaching the floor, blue eyes, matching blue irises, long asymmetrical bangs, dark hairband, side ribbon, cyan geometric square halo。
未指定换装、且当前场景没有明确其他衣着时，默认服装使用：white and black long uniform coat, blue coat lining, white collared shirt, aqua necktie, black pleated skirt with aqua trim, dark ankle socks, white lace-up boots。
角色独自出现时写 1girl, solo；有其他人物时按真实人数，不照抄 solo。日常衣着完整。
不要把默认形象误写成普通侧马尾、红领带、白裙、白色或黑色长筒袜，也不要用 school uniform 一个泛称代替上述默认服装。
用户明确换装，或当前上下文已明确换装时，保留身份、发色、眼睛和光环，替换默认服装段，不叠加两套衣服。表情、动作、道具、位置和光线按当前场景写；不因为默认示例而强加手柄或武器。
以上各段由你一次合成完整 prompt；后端不会自动补这些词。不要为同一角色叠加多个不同名字或互相冲突的风格。
【已核对的角色瞳色】画面中包含对应角色时：天童爱丽丝双眼同为蓝色（blue eyes, matching blue irises）；天宫心／天宮こころ双眼同为金黄色（golden eyes, matching golden irises）；迷迭香双眼同为浅绿色（green eyes, matching green irises）。这三人的默认外观都不是异色瞳，不把先前生成记录里的错误配色当设定，不在正面 prompt 写 heterochromia、mismatched eyes、pink and blue eyes。其他外观、衣着先参考已召回的角色知识，不从名字猜龙角、尾巴或换装。
只有用户明确要求 1K/2K/4K 或具体宽x高时才改变 resolution。图片由工具直接发送，不要重复调用。
参考图改图、gpt/bnn/bna 和插件查询仍使用 plugin_agent。"""

ANIMA_PROMPT_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "minLength": 1,
            "description": "最终完整的英文 positive prompt，原样提交给 ComfyUI；不要只写场景摘要。",
        },
        "sensitivity": {
            "type": "string",
            "enum": ["safe", "suggestive", "non_graphic_nudity", "explicit"],
            "default": "safe",
        },
        # Legacy fields remain accepted so older callers do not break. New calls
        # should send prompt only and let ComfyUI receive the exact final text.
        "characters": {
            "type": "array",
            "maxItems": 4,
            "description": "旧版兼容字段；新调用不要填写。",
            "items": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "enum": [*CHARACTERS, "original_adult", "other"],
                    },
                    "appearance": {"type": "string"},
                    "count": {"type": "integer", "minimum": 1, "maximum": 4},
                },
                "required": ["key", "appearance", "count"],
                "additionalProperties": False,
            },
        },
        "scene_and_action_prompt": {
            "type": "string",
            "description": "旧版兼容字段；新调用请把完整提示词放进 prompt。",
        },
    },
    "required": ["prompt"],
    "additionalProperties": False,
}


def validate_direct_prompt(
    prompt: str,
    request: str,
    allow_sensitive: bool,
    sensitivity: str = "safe",
    unrestricted: bool = False,
) -> str:
    """Return a final positive prompt that may be sent to ComfyUI verbatim.

    Args:
        prompt: Complete final positive prompt produced by the conversation model.
        request: Original user request (kept for API compatibility).
        allow_sensitive: Unused; no content boundary is enforced.
        sensitivity: Unused; no content boundary is enforced.
        unrestricted: Unused; no content boundary is enforced.

    Returns:
        The unchanged prompt after the basic non-empty check.

    Raises:
        ValueError: The prompt is empty.
    """
    text = str(prompt or "").strip()
    if not text:
        raise ValueError("绘图提示词为空。")
    return text


def validate_plan(
    plan: dict,
    request: str,
    allow_sensitive: bool,
    unrestricted: bool = False,
) -> tuple[str, str]:
    """Validate model output before it can become a ComfyUI positive prompt.

    Args:
        plan: Final structured image prompt supplied by the conversation model.
        request: Original drawing request (kept for API compatibility).
        allow_sensitive: Unused; no content boundary is enforced.
        unrestricted: Unused; no content boundary is enforced.

    Returns:
        Immutable character tags and validated scene tags.

    Raises:
        ValueError: The plan is malformed.
    """
    if not isinstance(plan, dict) or plan.get("allowed") is not True:
        raise ValueError(
            str(plan.get("reason", "这项绘图请求无法执行。"))[:160]
            if isinstance(plan, dict)
            else "绘图规划格式不正确。"
        )
    scene = plan.get("scene_and_action_prompt")
    cast = plan.get("characters")
    if not isinstance(scene, str) or not scene.strip():
        raise ValueError("绘图场景提示词为空。")
    if not isinstance(cast, list) or len(cast) > 4:
        raise ValueError("绘图人物列表不正确。")
    blocks, keys, count = [], [], 0
    for character in cast:
        if not isinstance(character, dict):
            raise ValueError("绘图人物格式不正确。")
        key = character.get("key")
        appearance = character.get("appearance", "")
        n = character.get("count")
        if (
            key not in {*CHARACTERS, "original_adult", "other"}
            or type(n) is not int
            or not 1 <= n <= 4
        ):
            raise ValueError("绘图人物身份或人数不正确。")
        if not isinstance(appearance, str):
            raise ValueError("绘图人物外观格式不正确。")
        if key in CHARACTERS:
            if appearance.strip() or n != 1 or key in keys:
                raise ValueError("固定角色外观不可由模型覆盖或重复。")
            blocks.append(CHARACTERS[key])
        elif appearance.strip():
            blocks.append(appearance.strip())
        else:
            raise ValueError("缺少绘图人物外观。")
        keys.append(key)
        count += n
    if count > 4 or "{{" in scene or any("{{" in block for block in blocks):
        raise ValueError("绘图人数超限或含有未填充占位符。")
    count_tag = (
        ("1girl" if count == 1 else f"{count}girls")
        if keys and all(key in CHARACTERS for key in keys)
        else (f"{count}person" if count else "")
    )
    return ", ".join(part for part in [count_tag, *blocks] if part), scene.strip()
