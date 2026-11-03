"""Secrets des sources (mots de passe, clés) chiffrés en base.

AES-256-GCM avec une clé maîtresse SECRETS_KEY (variable d'environnement ou secret Docker),
jamais enregistrée en base. Chaque secret est lié à sa source et à son champ (données associées) :
recopié sur une autre source, il ne se déchiffre pas.

Format : « v1.<id de clé>.<base64(nonce + chiffré)> ». L'id de clé (8 caractères, dérivé de la clé)
sert à la rotation : SECRETS_KEY_PREVIOUS garde les anciennes clés le temps de tout rechiffrer.

    python -m ged.secret_box cle        # nouvelle clé à mettre dans .env
    python -m ged.secret_box rotation   # rechiffre tout avec SECRETS_KEY (anciennes : SECRETS_KEY_PREVIOUS)
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import secrets as _random
import sys

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = "v1"

# Champ secret de chaque type de source (même nom que dans la définition : password → password_env)
SECRET_FIELD = {
    "smb": "password", "ftp": "password", "alfresco": "password", "cmis": "password",
    "s3": "secret_access_key", "sharepoint": "client_secret",
}


class SecretError(ValueError):
    """Clé absente ou invalide, secret illisible (message destiné à l'administrateur)."""


def new_key() -> str:
    return base64.urlsafe_b64encode(_random.token_bytes(32)).decode().rstrip("=")


def _decode_key(text: str) -> bytes:
    text = text.strip()
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) != 32:
        raise SecretError("SECRETS_KEY invalide : 32 octets en base64 attendus (python -m ged.secret_box cle)")
    return raw


def _key_id(key: bytes) -> str:
    return hashlib.sha256(b"alambic-secrets" + key).hexdigest()[:8]


def _aad(source: str, field: str) -> bytes:
    return f"alambic-search/{source}/{field}".encode()


class SecretBox:
    def __init__(self, current: str, previous: list[str] | None = None):
        self._current = _decode_key(current)
        self.key_id = _key_id(self._current)
        self._keys = {self.key_id: self._current}
        for text in previous or []:
            key = _decode_key(text)
            self._keys.setdefault(_key_id(key), key)

    @classmethod
    def from_env(cls) -> SecretBox | None:
        """None si SECRETS_KEY n'est pas défini (les secrets restent alors dans l'environnement)."""
        current = _read_env("SECRETS_KEY")
        if not current:
            return None
        previous = [p for p in (_read_env("SECRETS_KEY_PREVIOUS") or "").split(",") if p.strip()]
        return cls(current, previous)

    def encrypt(self, source: str, field: str, plaintext: str) -> str:
        nonce = _random.token_bytes(12)
        data = AESGCM(self._current).encrypt(nonce, plaintext.encode(), _aad(source, field))
        return f"{VERSION}.{self.key_id}.{base64.b64encode(nonce + data).decode()}"

    def decrypt(self, source: str, field: str, token: str) -> str:
        try:
            version, kid, payload = token.split(".", 2)
            raw = base64.b64decode(payload)
        except (ValueError, binascii.Error):
            raise SecretError(f"secret de {source} illisible (format inconnu) : à ressaisir") from None
        if version != VERSION:
            raise SecretError(f"secret de {source} : format {version} inconnu")
        key = self._keys.get(kid)
        if key is None:
            raise SecretError(f"secret de {source} chiffré avec une autre clé ({kid}) que SECRETS_KEY "
                              f"({self.key_id}) : remettre l'ancienne clé dans SECRETS_KEY_PREVIOUS, ou ressaisir le secret")
        try:
            return AESGCM(key).decrypt(raw[:12], raw[12:], _aad(source, field)).decode()
        except InvalidTag:
            raise SecretError(f"secret de {source} altéré ou recopié d'une autre source : à ressaisir") from None

    def needs_rotation(self, token: str) -> bool:
        return token.split(".", 2)[1:2] != [self.key_id]


def _read_env(name: str) -> str | None:
    """Variable NAME, ou fichier indiqué par NAME_FILE (secrets Docker : /run/secrets/…)."""
    value = os.environ.get(name)
    if value:
        return value.strip()
    path = os.environ.get(f"{name}_FILE")
    if path:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return None


def rotate(store, box: SecretBox) -> tuple[int, list[str]]:
    """Rechiffre avec la clé courante les secrets chiffrés par une ancienne clé.
    Renvoie (nombre rechiffré, erreurs)."""
    done, errors = 0, []
    for source, field, token in store.list_all_source_secrets():
        if not box.needs_rotation(token):
            continue
        try:
            plain = box.decrypt(source, field, token)
        except SecretError as exc:
            errors.append(str(exc))
            continue
        store.set_source_secret(source, field, box.encrypt(source, field, plain))
        done += 1
    return done, errors


def main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    if command == "cle":
        print(f"SECRETS_KEY={new_key()}")
        print("# À mettre dans .env (jamais dans la base ni le dépôt). À sauvegarder à part : sans elle,", file=sys.stderr)
        print("# les secrets enregistrés dans la console sont à ressaisir.", file=sys.stderr)
        return 0
    if command == "rotation":
        from ged.config import Settings
        from ged.store import PgStore

        box = SecretBox.from_env()
        if box is None:
            print("SECRETS_KEY non défini", file=sys.stderr)
            return 2
        s = Settings.from_env()
        store = PgStore(s.database_url, s.embed_dim, pool_size=1)
        try:
            done, errors = rotate(store, box)
        finally:
            store.close()
        print(f"{done} secret(s) rechiffré(s) avec la clé {box.key_id}")
        for err in errors:
            print(f"  ! {err}", file=sys.stderr)
        if not errors:
            print("Vous pouvez retirer SECRETS_KEY_PREVIOUS.")
        return 1 if errors else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
