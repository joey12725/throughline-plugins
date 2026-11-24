"""Beeper's asset store and enrichment cache, from the shared media package.

Files are stored as ``<sha256>.<ext>`` and linked as
``throughline-asset://beeper/<sha256>.<ext>``; the namespace stays ``beeper``
so links written before the shared package existed are unchanged.
"""

from throughline_media import assets as shared
from throughline_media.assets import Cache, StoreError, extension

NAMESPACE = "beeper"
DEFAULT_BASE_URL = shared.base_url_for(NAMESPACE)

__all__ = [
    "DEFAULT_BASE_URL",
    "AssetStore",
    "Cache",
    "LocalAssetStore",
    "StoreError",
    "default_state_dir",
    "extension",
]


def default_state_dir():
    return shared.default_state_dir(NAMESPACE)


class AssetStore(shared.AssetStore):
    namespace = NAMESPACE
    base_url = DEFAULT_BASE_URL


class LocalAssetStore(shared.LocalAssetStore):
    def __init__(self, directory, base_url=None):
        super().__init__(directory, base_url, namespace=NAMESPACE)
