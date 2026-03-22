import gettext
from pathlib import Path


localedir: Path = Path(__file__).parent

def _compile_mo_if_missing() -> None:
    mo_path: Path = localedir.joinpath("en", "LC_MESSAGES", "vnpy.mo")
    if mo_path.exists():
        return

    po_path: Path = localedir.joinpath("en", "LC_MESSAGES", "vnpy.po")
    if not po_path.exists():
        return

    try:
        from babel.messages.mofile import write_mo
        from babel.messages.pofile import read_po
    except Exception:
        return

    try:
        with open(po_path, encoding="utf-8") as po_f:
            catalog = read_po(po_f)

        mo_path.parent.mkdir(parents=True, exist_ok=True)
        with open(mo_path, "wb") as mo_f:
            write_mo(mo_f, catalog)
    except Exception:
        return


_compile_mo_if_missing()

translations: gettext.GNUTranslations | gettext.NullTranslations = gettext.translation(
    "vnpy",
    localedir=localedir,
    fallback=True,
)

_ = translations.gettext
