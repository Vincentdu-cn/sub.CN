from .base import SubtitleResult, SubtitleProvider

PROVIDERS = {}


def register_provider(name, cls):
    PROVIDERS[name] = cls


def get_provider(name, **kwargs):
    if name not in PROVIDERS:
        raise ValueError(f"Unknown provider: {name}")
    return PROVIDERS[name](**kwargs)


def list_providers():
    return list(PROVIDERS.keys())


from .zimuku import ZimukuProvider
from .subhd import SubHDProvider
from .assrt import AssrtProvider
from .opensubtitles import OpenSubtitlesProvider

register_provider("zimuku", ZimukuProvider)
register_provider("subhd", SubHDProvider)
register_provider("assrt", AssrtProvider)
register_provider("opensubtitles", OpenSubtitlesProvider)
