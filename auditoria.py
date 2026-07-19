"""Registro de auditoria (requisito: rastrear todas as ações importantes).

Grava, em `auditoria.log` (formato JSON Lines — uma linha = um evento em
JSON), os eventos relevantes do sistema: login, logout, renovação de
licença, troca de senha, solicitação de PIN, operações realizadas, Stop
Win/Loss atingido e bloqueios por compartilhamento.

Cada linha é independente e pode ser lida com qualquer editor de texto ou
processada depois (ex.: `grep`, `jq`, pandas). Não é um arquivo sensível
por si só (não guarda senhas nem credenciais), mas ainda assim está no
`.gitignore` por conter os IDs de Telegram dos usuários.
"""
import json
import logging
import datetime
from threading import Lock

logger = logging.getLogger(__name__)

ARQUIVO_AUDITORIA = "auditoria.log"
_lock = Lock()


def registrar(user_id, evento, detalhes=""):
    """Adiciona um evento ao log de auditoria.

    `evento` é um identificador curto, ex.: "login_quotex_ok",
    "pin_solicitado", "stop_win_atingido", "bloqueio_compartilhamento".
    """
    registro = {
        "quando": datetime.datetime.now().isoformat(timespec="seconds"),
        "user_id": str(user_id) if user_id is not None else None,
        "evento": evento,
        "detalhes": detalhes,
    }
    linha = json.dumps(registro, ensure_ascii=False)
    try:
        with _lock:
            with open(ARQUIVO_AUDITORIA, "a", encoding="utf-8") as f:
                f.write(linha + "\n")
    except OSError:
        logger.exception("Falha ao gravar no log de auditoria: %s", registro)
    logger.info("AUDITORIA | %s", linha)
