"""Answer in the language the page is showing (the VI / EN button)."""


def ui_language() -> str:
    from flask import g, has_app_context

    return (g.get("ui_language") if has_app_context() else None) or "vi"


def ui_text(vi: str, en: str) -> str:
    """The Vietnamese or the English text, following the page's language."""
    return en if ui_language() == "en" else vi
