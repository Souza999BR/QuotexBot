"""Controle diário de Stop Win / Stop Loss.

Regra de negócio (definida pelo administrador do bot):
- Quando um usuário atinge o Stop Win ou o Stop Loss configurado, as
  operações daquele usuário são pausadas pelo restante do dia.
- As operações só voltam automaticamente no dia seguinte.
- O "fechamento do dia" acontece às 23:59 (horário do servidor): é nesse
  momento que o lucro acumulado do dia e o estado de "parado" são
  zerados para todos os usuários, liberando novas entradas.

O estado é persistido em disco para sobreviver a reinícios do bot.
"""
import json
import os
import asyncio
import logging
import datetime
from threading import Lock

from config_bot import RESET_DIARIO_HORA, RESET_DIARIO_MINUTO

logger = logging.getLogger(__name__)

ARQUIVO_ESTADO = "estado_diario.json"
_lock = Lock()
_estado = {}


def _hoje():
    return datetime.date.today().isoformat()


def _carregar():
    global _estado
    if os.path.exists(ARQUIVO_ESTADO):
        try:
            with open(ARQUIVO_ESTADO, "r", encoding="utf-8") as f:
                _estado = json.load(f)
        except (json.JSONDecodeError, OSError):
            _estado = {}
    else:
        _estado = {}


def _salvar():
    with open(ARQUIVO_ESTADO, "w", encoding="utf-8") as f:
        json.dump(_estado, f, ensure_ascii=False, indent=4)


_carregar()


def _registro(user_id):
    uid = str(user_id)
    with _lock:
        registro = _estado.get(uid)
        if not registro or registro.get("data") != _hoje():
            registro = {
                "data": _hoje(),
                "lucro_total": 0.0,
                "parado": False,
                "motivo": None,
                "perdas_consecutivas": 0,
            }
            _estado[uid] = registro
            _salvar()
        registro.setdefault("perdas_consecutivas", 0)
        return registro


def obter_lucro_total(user_id):
    return _registro(user_id)["lucro_total"]


def esta_parado_hoje(user_id):
    return _registro(user_id)["parado"]


def registrar_resultado(user_id, lucro_rodada):
    """Soma o resultado de uma operação ao acumulado do dia e atualiza a
    sequência de perdas consecutivas (zera a cada resultado positivo)."""
    uid = str(user_id)
    with _lock:
        registro = _registro(user_id)
        registro["lucro_total"] = registro["lucro_total"] + lucro_rodada
        if lucro_rodada < 0:
            registro["perdas_consecutivas"] += 1
        else:
            registro["perdas_consecutivas"] = 0
        _estado[uid] = registro
        _salvar()
        return registro["lucro_total"]


def obter_perdas_consecutivas(user_id):
    return _registro(user_id)["perdas_consecutivas"]


def marcar_parado(user_id, motivo):
    """Interrompe as operações do usuário pelo restante do dia.

    `motivo` deve ser "stop_win", "stop_loss" ou "perdas_consecutivas".
    """
    uid = str(user_id)
    with _lock:
        registro = _registro(user_id)
        registro["parado"] = True
        registro["motivo"] = motivo
        _estado[uid] = registro
        _salvar()


def zerar_perdas_consecutivas(user_id):
    """Zera manualmente o contador de perdas consecutivas (ex.: após pausa)."""
    uid = str(user_id)
    with _lock:
        registro = _registro(user_id)
        registro["perdas_consecutivas"] = 0
        _estado[uid] = registro
        _salvar()


def resetar_todos():
    """Zera o acumulado e o estado de 'parado' de todos os usuários.

    Chamado automaticamente todos os dias no horário de fechamento
    (RESET_DIARIO_HORA:RESET_DIARIO_MINUTO).
    """
    with _lock:
        hoje = _hoje()
        for uid in list(_estado.keys()):
            _estado[uid] = {
                "data": hoje,
                "lucro_total": 0.0,
                "parado": False,
                "motivo": None,
            }
        _salvar()
    logger.info("♻️ Reset diário de Stop Win / Stop Loss executado (%s).", hoje)


async def agendar_reset_diario():
    """Task de background: reseta o estado diário todo dia no horário definido."""
    while True:
        agora = datetime.datetime.now()
        proximo = agora.replace(
            hour=RESET_DIARIO_HORA, minute=RESET_DIARIO_MINUTO, second=0, microsecond=0
        )
        if proximo <= agora:
            proximo += datetime.timedelta(days=1)
        segundos = (proximo - agora).total_seconds()
        await asyncio.sleep(segundos)
        resetar_todos()
