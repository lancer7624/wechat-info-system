"""Resolve runtime state separately from this portable source directory."""
import os
import json
from pathlib import Path
import sys

FROZEN = bool(getattr(sys, 'frozen', False))
PROJECT_ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1])).resolve()


def _default_data_root():
    if not FROZEN:
        return PROJECT_ROOT.parent / 'wechat-chat-analyzer-data'
    settings = Path(sys.executable).resolve().parent / 'desktop-settings.json'
    if settings.is_file():
        try:
            value = json.loads(settings.read_text(encoding='utf-8')).get('data_root')
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise ValueError
            return Path(value)
        except (OSError, ValueError, TypeError, AttributeError):
            raise RuntimeError('桌面数据目录设置无效，请检查 desktop-settings.json。') from None
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'WechatConversationAssistant'


DATA_ROOT = Path(os.environ['WECHAT_ANALYZER_DATA']).expanduser().resolve() if os.environ.get('WECHAT_ANALYZER_DATA') else _default_data_root().resolve()
KEY_FILE = DATA_ROOT / 'private' / 'db_keys.json'
# The historical .deps runtime was installed for CPython 3.14. Other versions
# must not load its compiled extensions; virtualenvs and frozen builds are isolated.
DEPENDENCIES = DATA_ROOT / ('.deps' if sys.version_info[:2] == (3, 14)
                            else f'.deps-py{sys.version_info.major}{sys.version_info.minor}')


def enable_dependencies():
    if not FROZEN and sys.prefix == sys.base_prefix and DEPENDENCIES.is_dir():
        sys.path.insert(0, str(DEPENDENCIES))
