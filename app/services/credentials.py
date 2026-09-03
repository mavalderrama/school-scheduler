"""Claves de LLM por familia, cifradas en reposo.

Fernet con `CREDENTIALS_KEY`: un volcado de la base sin esa clave no sirve de nada. El
valor en claro no se guarda, no se registra y no se muestra entero en ningún sitio; el
admin lo enseña enmascarado.

Por qué cifrar y no confiar en los permisos de Postgres: la copia nocturna de
`scripts/backup.sh` se lleva la tabla entera a un fichero, y ese fichero acaba en sitios
donde la base no está.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings
from app.log import get_logger

log = get_logger(__name__)

MASK = "••••"


class CredentialError(RuntimeError):
    """La clave no se puede cifrar o descifrar. Nunca lleva el secreto en el mensaje."""


@lru_cache(maxsize=4)
def _cipher(key: str) -> Fernet:
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise CredentialError(
            "CREDENTIALS_KEY no es una clave Fernet válida. Genera una con "
            '`python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"`.'
        ) from exc


def encrypt(secret: str, settings: Settings) -> str:
    """Cifra una clave para guardarla. Cadena vacía se guarda vacía, no cifrada."""
    if not secret:
        return ""
    if not settings.credentials_key:
        raise CredentialError("hace falta CREDENTIALS_KEY para guardar claves de familia")
    return _cipher(settings.credentials_key).encrypt(secret.encode("utf-8")).decode("utf-8")


def decrypt(stored: str, settings: Settings) -> str:
    """Descifra una clave guardada. Un token corrupto no revienta el bot: se avisa y se
    trata como si no hubiera clave, que es un fallo recuperable y visible en `/estado`."""
    if not stored:
        return ""
    if not settings.credentials_key:
        raise CredentialError("hace falta CREDENTIALS_KEY para leer claves de familia")
    try:
        return _cipher(settings.credentials_key).decrypt(stored.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        # Sin detalles: el mensaje acaba en logs.
        raise CredentialError(
            "la clave guardada no se puede descifrar con CREDENTIALS_KEY"
        ) from exc


def mask(secret: str) -> str:
    """Lo que se puede enseñar: los últimos cuatro caracteres y nada más."""
    if not secret:
        return "(sin clave)"
    return f"{MASK}{secret[-4:]}" if len(secret) > 4 else MASK
