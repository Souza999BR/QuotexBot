"""Estado compartilhado e persistência de dados dos usuários.

Todos os campos sensíveis (senha Quotex, senha IMAP) são criptografados
em disco com Fernet (chave ENCRYPTION_KEY do .env). Em memória, os valores
ficam em texto puro para uso direto pelo bot.

IMPORTANTE — assinaturas públicas que bot.py espera:
    cifrar_config_usuario(config: dict) -> dict
    salvar_dados()
    USERS_DATA: dict[str, dict]
    CADASTRADOS: list[int]
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from config_bot import ENCRYPTION_KEY

logger = logging.getLogger(__name__)

_USERS_FILE = "users_data.json"
_CADASTRADOS_FILE = "cadastrados.json"

# ---------------------------------------------------------------------------
# Estado em memória
# ---------------------------------------------------------------------------

# {str(user_id): {campo: valor, ...}} — valores em texto puro.
USERS_DATA: dict[str, dict[str, Any]] = {}

# [int(user_id), ...] — IDs que já validaram a senha com /senha.
CADASTRADOS: list[int] = []

_fernet: Fernet | None = None
_SENSITIVE_FIELDS = {"senhaQuotex", "email_imap_password"}


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        if not ENCRYPTION_KEY:
            raise RuntimeError(
                "ENCRYPTION_KEY não configurado — não é possível criptografar/decriptografar dados."
            )
        key = ENCRYPTION_KEY.encode() if isinstance(ENCRYPTION_KEY, str) else ENCRYPTION_KEY
        _fernet = Fernet(key)
    return _fernet


def _encrypt(valor: str) -> str:
    return _get_fernet().encrypt(valor.encode()).decode()


def _decrypt(valor: str) -> str:
    try:
        return _get_fernet().decrypt(valor.encode()).decode()
    except (InvalidToken, Exception):
        return valor  # fallback: já estava em texto puro (migração)


# ---------------------------------------------------------------------------
# Carregamento na importação
# ---------------------------------------------------------------------------

def _load():
    global USERS_DATA, CADASTRADOS

    if os.path.exists(_USERS_FILE):
        try:
            with open(_USERS_FILE, "r", encoding="utf-8") as f:
                raw: dict = json.load(f)
            for uid, cfg in raw.items():
                decrypted = dict(cfg)
                for field in _SENSITIVE_FIELDS:
                    if field in decrypted and decrypted[field]:
                        decrypted[field] = _decrypt(decrypted[field])
                USERS_DATA[uid] = decrypted
        except (json.JSONDecodeError, OSError):
            logger.exception("Falha ao carregar %s — iniciando vazio.", _USERS_FILE)
            USERS_DATA = {}

    if os.path.exists(_CADASTRADOS_FILE):
        try:
            with open(_CADASTRADOS_FILE, "r", encoding="utf-8") as f:
                CADASTRADOS.extend(json.load(f))
        except (json.JSONDecodeError, OSError):
            logger.exception("Falha ao carregar %s — iniciando vazio.", _CADASTRADOS_FILE)


_load()


# ---------------------------------------------------------------------------
# Persistência
# ---------------------------------------------------------------------------

def salvar_dados():
    """Persiste USERS_DATA (criptografado) e CADASTRADOS em disco."""
    encrypted: dict[str, dict] = {}
    for uid, cfg in USERS_DATA.items():
        enc_cfg = dict(cfg)
        for field in _SENSITIVE_FIELDS:
            if field in enc_cfg and enc_cfg[field]:
                enc_cfg[field] = _encrypt(str(enc_cfg[field]))
        encrypted[uid] = enc_cfg

    try:
        with open(_USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(encrypted, f, ensure_ascii=False, indent=4)
    except OSError:
        logger.exception("Falha ao salvar %s", _USERS_FILE)

    try:
        with open(_CADASTRADOS_FILE, "w", encoding="utf-8") as f:
            json.dump(CADASTRADOS, f, ensure_ascii=False, indent=4)
    except OSError:
        logger.exception("Falha ao salvar %s", _CADASTRADOS_FILE)


# ---------------------------------------------------------------------------
# API pública — usada por bot.py
# ---------------------------------------------------------------------------

def cifrar_config_usuario(config: dict) -> dict:
    """Retorna uma cópia limpa do dicionário de configuração.

    bot.py chama esta função com um único argumento (o dict de configuração
    do usuário) e usa o valor retornado para atualizar USERS_DATA.
    A criptografia dos campos sensíveis acontece em salvar_dados(), que
    é chamada separadamente pelo bot logo em seguida.

    Uso em bot.py:
        USERS_DATA[str(user_id)] = cifrar_config_usuario(context.user_data)
        salvar_dados()

        # Atualização de campo único:
        config_atual[campo] = cifrar_config_usuario({campo: valor})[campo]
    """
    return dict(config)
