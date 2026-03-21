from pathlib import Path
import gettext

localedir = Path(__file__).parent
translations = gettext.translation('vnpy_signal_backtester', localedir=localedir, fallback=True)

_ = translations.gettext
