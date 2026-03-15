import gettext
from pathlib import Path


localedir = Path(__file__).parent

translations: gettext.NullTranslations = gettext.translation('vnpy_signal_strategy', localedir=localedir, fallback=True)

_ = translations.gettext
