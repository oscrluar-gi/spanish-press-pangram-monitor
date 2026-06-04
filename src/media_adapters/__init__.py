from __future__ import annotations

from src.media_adapters.abc import AbcAdapter
from src.media_adapters.base import MediaAdapter
from src.media_adapters.prisa import AsAdapter, CincoDiasAdapter, PrisaAdapter
from src.media_adapters.spanish_press import (
    ElConfidencialAdapter,
    EldiarioAdapter,
    EleconomistaAdapter,
    LaRazonAdapter,
    LavanguardiaAdapter,
    PublicoAdapter,
)
from src.media_adapters.unidad_editorial import ElMundoAdapter, ExpansionAdapter, MarcaAdapter
from src.models import MediaConfig

ADAPTERS: dict[str, MediaAdapter] = {
    "abc": AbcAdapter(),
    "as": AsAdapter(),
    "cincodias": CincoDiasAdapter(),
    "elconfidencial": ElConfidencialAdapter(),
    "eldiario": EldiarioAdapter(),
    "eleconomista": EleconomistaAdapter(),
    "elmundo": ElMundoAdapter(),
    "expansion": ExpansionAdapter(),
    "larazon": LaRazonAdapter(),
    "lavanguardia": LavanguardiaAdapter(),
    "marca": MarcaAdapter(),
    "prisa": PrisaAdapter(),
    "publico": PublicoAdapter(),
}

DEFAULT_ADAPTER = MediaAdapter()


def get_media_adapter(media: MediaConfig) -> MediaAdapter:
    if not media.adapter:
        return DEFAULT_ADAPTER
    return ADAPTERS.get(media.adapter.lower(), DEFAULT_ADAPTER)
