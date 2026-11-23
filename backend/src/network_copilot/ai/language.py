"""Answer in the language the page is showing (the VI / EN button)."""


def ui_language() -> str:
    from flask import g, has_app_context

    return (g.get("ui_language") if has_app_context() else None) or "vi"


def ui_text(vi: str, en: str) -> str:
    """The Vietnamese or the English text, following the page's language."""
    return en if ui_language() == "en" else vi


_REPLY_RULE = {
    "en": "The user's page is shown in English. Write every human-readable text in your "
          "answer (explanations, notes, diagram titles and node or link labels) in English, "
          "whatever language the conversation or the project data use. Keep JSON keys, CLI "
          "commands, hostnames and interface names unchanged.",
    "vi": "The user's page is shown in Vietnamese. Write every human-readable text in your "
          "answer (explanations, notes, diagram titles and node or link labels) in Vietnamese. "
          "Keep JSON keys, CLI commands, hostnames and interface names unchanged.",
}


def with_reply_language(system_prompt: str) -> str:
    """Append the page language to a system prompt when the request carries one."""
    from flask import g, has_app_context

    chosen = g.get("ui_language") if has_app_context() else None
    rule = _REPLY_RULE.get(chosen)
    return f"{system_prompt.rstrip()}\n\n{rule}\n" if rule else system_prompt
