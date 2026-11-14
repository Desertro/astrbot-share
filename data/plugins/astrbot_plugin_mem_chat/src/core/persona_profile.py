"""Resolve bundled character resources consistently across mem_chat."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PersonaProfile:
    """Resource locations and identity for one bundled character."""

    key: str
    name: str
    full_name: str
    persona_path: Path
    state_rules_path: Path
    rag_dir: Path
    rag_type: str
    rag_hash_key: str
    attention_names: tuple[str, ...]


def get_persona_profile(config: Mapping, umo: str = "") -> PersonaProfile:
    """Resolve the selected character, retaining Alice for legacy configs.

    Args:
        config: Plugin configuration with an optional persona_profile selector.
        umo: Message origin or a bare group ID; private chats use the default.

    Returns:
        The character identity and its isolated prompt and RAG resources.
    """
    root = Path(__file__).resolve().parents[2]
    selection = str(config.get("persona_profile", "alice")).strip().lower()
    origin = str(umo or "").strip()
    parts = origin.split(":")
    group_id = parts[2] if len(parts) >= 3 and parts[1] == "GroupMessage" else ""
    if origin and ":" not in origin:
        group_id = origin
    overrides = config.get("group_persona_overrides", {})
    if group_id and isinstance(overrides, dict):
        selected = str(overrides.get(group_id, "")).strip().lower()
        if selected in ("alice", "rio", "noa"):
            selection = selected
    if selection == "noa":
        base = root / "profiles" / "noa"
        return PersonaProfile(
            "noa",
            "诺亚",
            "生盐诺亚",
            base / "personas.txt",
            base / "state_rules.txt",
            base / "persona_rag",
            "persona_rag_noa",
            "last_persona_rag_hash_noa",
            ("诺亚", "生盐诺亚", "乃爱", "乃愛", "ノア", "Noa"),
        )
    if selection == "rio":
        base = root / "profiles" / "rio"
        return PersonaProfile(
            "rio",
            "莉音",
            "调月莉音",
            base / "personas.txt",
            base / "state_rules.txt",
            base / "persona_rag",
            "persona_rag_rio",
            "last_persona_rag_hash_rio",
            ("莉音", "调月莉音", "莉央", "リオ", "Rio"),
        )
    return PersonaProfile(
        "alice",
        "爱丽丝",
        "天童爱丽丝",
        root / "personas" / "personas.txt",
        root / "personas" / "state_rules.txt",
        root / "persona_rag",
        "persona_rag",
        "last_persona_rag_hash",
        ("爱丽丝", "天童爱丽丝"),
    )
