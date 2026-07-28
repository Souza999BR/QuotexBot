"""Login e loop de operações por usuário do Telegram.

Modos de operação:
  /iniciar    → loop manual, roda até o usuário chamar /parar
  /automatico → scheduler que inicia o loop todo dia útil 08:00-10:30
  /parar      → para ambos os modos

IMPORTANTE: O PIN de login foi removido do fluxo. Se a Quotex solicitar PIN,
  o usuário é orientado a desabilitar essa opção nas configurações da conta.
  Isso elimina interrupções no login automático.

Isolamento de sessão:
  Cada usuário tem seu próprio diretório sessions/<user_id>/ para que
  session.json não seja compartilhado entre contas.
  Quando o token/sessão está desatualizado, o session.json é apagado e
  recriado automaticamente, sem intervenção do usuário.

Assinaturas que bot.py espera:
    async iniciar_estrategia_com_pin(user_id) -> str
    async iniciar_automatico(user_id) -> str
    cancelar_estrategia(user_id) -> bool
    obter_historico_hoje(user_id) -> list[dict]
    EXECUTANDO: dict[str, bool]
    MODO_AUTO: dict[str, bool]
    enviar_telegram(chat_id, texto) -> coroutine
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
import time as _time
from datetime import datetime, timedelta
from datetime import time as dtime
from typing import Optional

from telegram import Bot

from config_bot import TELEGRAM_BOT_TOKEN, ADMIN_USERNAME
from pyquotex.stable_api import Quotex
from pyquotex.exceptions import PinRequiredError, LoginFailedError
from estrategia import analisar
import estado_diario
import auditoria
from shared import USERS_DATA

logger = logging.getLogger(__name__)

MAX_TENTATIVAS_LOGIN = 5

# Janela de operações do modo automático
AUTO_ABERTURA  = dtime(8, 0, 0)
AUTO_FECHAMENTO = dtime(10, 30, 0)

# ---------------------------------------------------------------------------
# Estado em memória por usuário
# ---------------------------------------------------------------------------

# {str(user_id): bool} — True enquanto o loop de operações estiver rodando
EXECUTANDO: dict[str, bool] = {}

# {str(user_id): bool} — True enquanto o scheduler automático estiver ativo
MODO_AUTO: dict[str, bool] = {}

# {str(user_id): Quotex} — clientes ativos
_CLIENTES: dict[str, Quotex] = {}

# {str(user_id): asyncio.AbstractEventLoop} — loop de cada thread de usuário
_LOOPS: dict[str, asyncio.AbstractEventLoop] = {}

# {str(user_id): list[dict]} — histórico de operações por usuário (até 200)
HISTORICO: dict[str, list[dict]] = {}


# ---------------------------------------------------------------------------
# Helper Telegram
# ---------------------------------------------------------------------------

async def enviar_telegram(chat_id: int | str, texto: str):
    """Envia uma mensagem para um chat do Telegram (fire-and-forget)."""
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        async with bot:
            await bot.send_message(chat_id=int(chat_id), text=texto)
    except Exception:
        logger.exception("Falha ao enviar mensagem Telegram para %s", chat_id)


# ---------------------------------------------------------------------------
# Mensagens formatadas de entrada e resultado
# ---------------------------------------------------------------------------

def _msg_entrada(simbolo: str, direcao: str, minutos: int) -> str:
    dir_emoji = "🟩CALL" if direcao.lower() == "call" else "🟥PUT"
    hora = datetime.now().strftime("%H:%M")
    return (
        "📊 𝗘𝗡𝗧𝗥𝗔𝗗𝗔 𝗖𝗢𝗡𝗙𝗜𝗥𝗠𝗔𝗗𝗔\n\n"
        f"📊 ATIVO: {simbolo}\n"
        f"⏰ {hora}\n"
        f"⏳ M{minutos}\n"
        f" {dir_emoji}\n"
        "⚠ Proteção Opcional\n\n"
        f"📲 Contato: @{ADMIN_USERNAME}"
    )


def _msg_resultado(is_win: bool, lucro: float, lucro_acumulado: float) -> str:
    if is_win:
        return (
            f"✅✅win✅✅\n"
            f"💰 Lucro: R$ {lucro:+.2f} | Acumulado hoje: R$ {lucro_acumulado:.2f}"
        )
    return (
        f"❌loss❌\n"
        f"💸 Perda: R$ {lucro:.2f} | Acumulado hoje: R$ {lucro_acumulado:.2f}"
    )


# ---------------------------------------------------------------------------
# Histórico de operações
# ---------------------------------------------------------------------------

def registrar_operacao(uid: str, operacao: dict):
    """Adiciona uma operação ao histórico em memória do usuário."""
    if uid not in HISTORICO:
        HISTORICO[uid] = []
    # Garante que a data está registrada para filtragem diária
    if "data" not in operacao:
        operacao["data"] = datetime.now().strftime("%d/%m/%Y")
    HISTORICO[uid].append(operacao)
    # Mantém apenas as últimas 200 operações por usuário
    if len(HISTORICO[uid]) > 200:
        HISTORICO[uid] = HISTORICO[uid][-200:]


def obter_historico_hoje(user_id: int | str) -> list[dict]:
    """Retorna apenas as operações de HOJE do usuário."""
    uid = str(user_id)
    hoje = datetime.now().strftime("%d/%m/%Y")
    return [op for op in HISTORICO.get(uid, []) if op.get("data") == hoje]


# ---------------------------------------------------------------------------
# Gerenciamento de sessão por usuário
# ---------------------------------------------------------------------------

def _session_dir(user_id: str) -> str:
    """Retorna (e cria se necessário) o diretório de sessão exclusivo do usuário."""
    path = os.path.join("Sessions", user_id)
    os.makedirs(path, exist_ok=True)
    return path


def _limpar_session(uid: str):
    """Remove o session.json do usuário para forçar novo login/token."""
    pasta = _session_dir(uid)
    session_file = os.path.join(pasta, "session.json")
    try:
        if os.path.exists(session_file):
            os.remove(session_file)
            logger.info("session.json de %s removido para renovação de token.", uid)
        else:
            logger.debug("session.json de %s não existe, nada a remover.", uid)
    except Exception as exc:
        logger.warning("Erro ao remover session.json de %s: %s", uid, exc)


def _criar_session_vazia(uid: str):
    """Cria um session.json em branco para o usuário."""
    pasta = _session_dir(uid)
    session_file = os.path.join(pasta, "session.json")
    try:
        session_vazia = {
            "cookies": None,
            "token": None,
            "user_agent": None,
        }
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(session_vazia, f, indent=4)
        logger.info("session.json vazio criado para %s.", uid)
    except Exception as exc:
        logger.warning("Erro ao criar session.json vazio para %s: %s", uid, exc)


def _criar_novo_cliente(uid: str, config: dict) -> Quotex:
    """Cria e configura um novo cliente Quotex para o usuário."""
    pasta = _session_dir(uid)
    client = Quotex(
        email=config["emailQuotex"],
        password=config["senhaQuotex"],
        root_path=pasta,
        lang="pt",
    )
    client.pin_code = None
    client.email_imap = config.get("email_imap") or None
    client.email_imap_password = config.get("email_imap_password") or None
    modo = "REAL" if str(config.get("tipo", "demo")).lower() == "real" else "PRACTICE"
    client.set_account_mode(modo)
    return client


# ---------------------------------------------------------------------------
# API pública chamada pelo bot.py
# ---------------------------------------------------------------------------

async def iniciar_estrategia_com_pin(user_id: int | str) -> str:
    """Inicia o loop MANUAL de operações do usuário em uma thread daemon.

    Retorna imediatamente com um dos códigos:
      "SEM_CONFIGURACAO" | "JA_EM_EXECUCAO" | "INICIADO"
    """
    uid = str(user_id)

    if uid not in USERS_DATA or not USERS_DATA[uid].get("emailQuotex"):
        return "SEM_CONFIGURACAO"

    if EXECUTANDO.get(uid) or MODO_AUTO.get(uid):
        return "JA_EM_EXECUCAO"

    config = dict(USERS_DATA[uid])
    chat_id = int(uid)

    t = threading.Thread(
        target=_executar_loop_em_thread,
        args=(uid, config, chat_id, None),   # None = sem limite de horário
        daemon=True,
        name=f"quotex-manual-{uid}",
    )
    t.start()
    return "INICIADO"


async def iniciar_automatico(user_id: int | str) -> str:
    """Ativa o scheduler automático (08:00-10:30, seg-sex) para o usuário.

    Retorna imediatamente com um dos códigos:
      "SEM_CONFIGURACAO" | "JA_EM_EXECUCAO" | "INICIADO"
    """
    uid = str(user_id)

    if uid not in USERS_DATA or not USERS_DATA[uid].get("emailQuotex"):
        return "SEM_CONFIGURACAO"

    if EXECUTANDO.get(uid) or MODO_AUTO.get(uid):
        return "JA_EM_EXECUCAO"

    config = dict(USERS_DATA[uid])
    chat_id = int(uid)

    t = threading.Thread(
        target=_executar_automatico_em_thread,
        args=(uid, config, chat_id),
        daemon=True,
        name=f"quotex-auto-{uid}",
    )
    t.start()
    return "INICIADO"


def cancelar_estrategia(user_id: int | str) -> bool:
    """Para o loop de operações E o scheduler automático do usuário.

    Retorna True se havia algo ativo para cancelar.
    """
    uid = str(user_id)
    cancelou = False

    if EXECUTANDO.get(uid):
        EXECUTANDO[uid] = False
        cancelou = True
        logger.info("Loop de operações cancelado para user %s", uid)

    if MODO_AUTO.get(uid):
        MODO_AUTO[uid] = False
        cancelou = True
        logger.info("Modo automático cancelado para user %s", uid)

    return cancelou


# ---------------------------------------------------------------------------
# Scheduler do modo automático
# ---------------------------------------------------------------------------

def _proxima_abertura(agora: datetime) -> datetime:
    """Retorna o próximo datetime de abertura (08:00) em dia útil (seg-sex)."""
    candidato = agora.replace(
        hour=AUTO_ABERTURA.hour,
        minute=AUTO_ABERTURA.minute,
        second=0,
        microsecond=0,
    )
    # Se já passou do horário de abertura hoje, começa amanhã
    if agora >= candidato:
        candidato += timedelta(days=1)
    # Avança até segunda-feira se cair no fim de semana
    while candidato.weekday() >= 5:  # 5=sábado, 6=domingo
        candidato += timedelta(days=1)
    return candidato


async def _loop_automatico(uid: str, config: dict, chat_id: int):
    """Scheduler: aguarda janela 08:00-10:30 seg-sex e roda as operações."""
    MODO_AUTO[uid] = True

    await enviar_telegram(
        chat_id,
        "🤖 Modo Automático Ativado!\n\n"
        "📅 Operações abertas automaticamente de seg-sex, 08:00 às 10:30.\n"
        "Use /parar para desativar a qualquer momento.",
    )

    while MODO_AUTO.get(uid):
        agora = datetime.now()
        dia_semana = agora.weekday()   # 0=seg … 4=sex, 5=sáb, 6=dom
        hora_atual = agora.time()

        # --- Dentro da janela em dia útil ---
        if dia_semana < 5 and AUTO_ABERTURA <= hora_atual < AUTO_FECHAMENTO:
            if not EXECUTANDO.get(uid):
                await enviar_telegram(
                    chat_id,
                    f"🟢 Janela aberta ({AUTO_ABERTURA.strftime('%H:%M')}-"
                    f"{AUTO_FECHAMENTO.strftime('%H:%M')}). Iniciando operações...",
                )
                # Roda o loop de operações DIRETAMENTE neste coroutine
                # (sem thread extra — já estamos em uma thread daemon)
                await _loop_operacoes(uid, config, chat_id, limite_horario=AUTO_FECHAMENTO)

                if MODO_AUTO.get(uid):
                    await enviar_telegram(
                        chat_id,
                        f"⏹ Janela fechada ({AUTO_FECHAMENTO.strftime('%H:%M')}). "
                        "Operações encerradas. Aguardando próxima abertura...",
                    )

        # --- Fora da janela: calcula e aguarda o próximo horário ---
        else:
            proxima = _proxima_abertura(agora)
            segundos_totais = (proxima - agora).total_seconds()
            msg_proxima = proxima.strftime("%d/%m às %H:%M")

            await enviar_telegram(
                chat_id,
                f"⏰ Modo automático em espera.\n"
                f"📅 Próxima abertura: {msg_proxima}",
            )
            logger.info(
                "Auto user %s: aguardando %.0fs até %s", uid, segundos_totais, msg_proxima
            )

            # Dorme em fatias de 60s para responder rapidamente ao /parar
            while MODO_AUTO.get(uid) and segundos_totais > 0:
                await asyncio.sleep(min(60.0, segundos_totais))
                segundos_totais -= 60.0

    MODO_AUTO.pop(uid, None)
    EXECUTANDO.pop(uid, None)
    logger.info("Modo automático encerrado para user %s", uid)


def _executar_automatico_em_thread(uid: str, config: dict, chat_id: int):
    """Executa o scheduler automático em uma thread daemon dedicada."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _LOOPS[uid] = loop
    try:
        loop.run_until_complete(_loop_automatico(uid, config, chat_id))
    except Exception:
        logger.exception("Erro no modo automático (user %s)", uid)
    finally:
        _LOOPS.pop(uid, None)
        EXECUTANDO.pop(uid, None)
        MODO_AUTO.pop(uid, None)
        loop.close()


# ---------------------------------------------------------------------------
# Conexão — sem PIN, com renovação automática de sessão
# ---------------------------------------------------------------------------

async def _conectar(
    uid: str,
    client: Quotex,
    config: dict,
    chat_id: int,
) -> tuple[bool, Quotex]:
    """Tenta conectar o cliente à Quotex.

    Estratégia de retry (importante para servidores em nuvem como Discloud):
      - Nas primeiras tentativas preserva o session.json existente: apenas
        recria o objeto cliente com backoff progressivo. Isso evita forçar
        um novo login HTTP que pode ser bloqueado por Cloudflare em IPs de
        datacenter.
      - Só apaga o session.json na ÚLTIMA tentativa, como último recurso.
      - Se a Quotex exigir PIN, orienta o usuário a desativar e encerra.
    """
    tentativas = 0
    # Atraso base para backoff progressivo (segundos): 5, 10, 20, 30, 30...
    _DELAYS = [5, 10, 20, 30, 30]

    while tentativas < MAX_TENTATIVAS_LOGIN:

        try:
            check, reason = await asyncio.wait_for(
                client.connect(),
                timeout=60,
            )

            if check:
                return True, client

            logger.error("Falha na conexão (user %s): %s", uid, reason)
            await enviar_telegram(
                chat_id,
                f"❌ Falha na conexão com a Quotex: {reason}\n"
                "Verifique sua conexão e tente novamente.",
            )
            return False, client

        except asyncio.TimeoutError:
            tentativas += 1
            delay = _DELAYS[min(tentativas - 1, len(_DELAYS) - 1)]
            logger.warning(
                "Timeout de conexão user %s (%d/%d) — aguardando %ds",
                uid, tentativas, MAX_TENTATIVAS_LOGIN, delay,
            )
            auditoria.registrar(uid, "timeout_conexao", f"tentativa={tentativas}")

            eh_ultima = tentativas >= MAX_TENTATIVAS_LOGIN
            if eh_ultima:
                _limpar_session(uid)
                _criar_session_vazia(uid)
                logger.info("Última tentativa: session.json de %s limpo.", uid)

            try:
                client = _criar_novo_cliente(uid, config)
                _CLIENTES[uid] = client
            except Exception as exc:
                logger.exception("Erro ao recriar cliente (user %s): %s", uid, exc)

            await enviar_telegram(
                chat_id,
                f"⚠️ A conexão demorou demais (tentativa {tentativas}/{MAX_TENTATIVAS_LOGIN}).\n"
                f"🔄 Tentando novamente em {delay}s...",
            )
            await asyncio.sleep(delay)
            continue

        except LoginFailedError as exc:
            tentativas += 1
            delay = _DELAYS[min(tentativas - 1, len(_DELAYS) - 1)]
            eh_ultima = tentativas >= MAX_TENTATIVAS_LOGIN

            logger.error(
                "Login falhou (user %s) tentativa %d/%d: %s",
                uid, tentativas, MAX_TENTATIVAS_LOGIN, exc,
            )
            auditoria.registrar(uid, "login_falhou_renovando_sessao", str(exc))

            if eh_ultima:
                # Última tentativa: limpa session.json e força novo login HTTP
                _limpar_session(uid)
                _criar_session_vazia(uid)
                logger.info(
                    "Última tentativa de login para %s — session.json limpo.", uid
                )
                await enviar_telegram(
                    chat_id,
                    "🔄 Renovando sessão completa e fazendo última tentativa...",
                )
            else:
                # Tentativas iniciais: preserva cookies existentes para evitar
                # novo login HTTP (que pode ser bloqueado por Cloudflare em
                # servidores de datacenter). Apenas recria o objeto cliente.
                await enviar_telegram(
                    chat_id,
                    f"⚠️ Falha no login (tentativa {tentativas}/{MAX_TENTATIVAS_LOGIN}).\n"
                    f"🔄 Tentando reconectar em {delay}s...",
                )

            try:
                client = _criar_novo_cliente(uid, config)
                _CLIENTES[uid] = client
            except Exception as recreate_exc:
                logger.exception(
                    "Erro ao recriar cliente (user %s): %s", uid, recreate_exc,
                )
                await enviar_telegram(
                    chat_id,
                    f"❌ Erro ao recriar cliente: {recreate_exc}\n"
                    "Verifique e-mail/senha em /ajustaconfig.",
                )
                return False, client

            await asyncio.sleep(delay)
            continue

        except PinRequiredError as exc:
            logger.warning(
                "PIN solicitado para user %s — orientando a desativar PIN na Quotex.",
                uid,
            )
            auditoria.registrar(uid, "pin_solicitado_desabilitar", str(exc))

            await enviar_telegram(
                chat_id,
                "🔐 A Quotex está solicitando autenticação por PIN.\n\n"
                "⚠️ Para que o login automático funcione corretamente, "
                "você precisa DESABILITAR o PIN de verificação na sua conta Quotex:\n\n"
                "1️⃣ Acesse quotex.io e faça login manualmente\n"
                "2️⃣ Vá em Configurações → Segurança\n"
                "3️⃣ Desative a opção de verificação por PIN/e-mail\n\n"
                "Após desativar, use /iniciar novamente.",
            )
            return False, client

        except Exception as exc:
            logger.exception(
                "Erro inesperado na conexão (user %s): %s", uid, exc,
            )
            auditoria.registrar(uid, "erro_conexao", str(exc))
            await enviar_telegram(
                chat_id,
                f"❌ Erro inesperado ao conectar: {exc}",
            )
            return False, client

    # Esgotou todas as tentativas
    await enviar_telegram(
        chat_id,
        "❌ Não foi possível conectar após várias tentativas.\n\n"
        "Possíveis causas:\n"
        "• E-mail ou senha incorretos → /ajustaconfig\n"
        "• Servidor bloqueado pela Quotex (tente novamente em alguns minutos)\n"
        "• PIN ativo na conta → desative em Configurações → Segurança no site",
    )
    auditoria.registrar(uid, "conexao_esgotada")
    return False, client


# ---------------------------------------------------------------------------
# Busca de velas
# ---------------------------------------------------------------------------

async def _buscar_velas(client: Quotex, simbolo: str, period_s: int, qtd: int = 50):
    """Busca candles históricas para o símbolo/timeframe solicitado."""
    try:
        candles = await client.get_candles(simbolo, _time.time(), qtd, period_s)
        return candles or []
    except Exception as exc:
        logger.warning("Falha ao buscar velas %ss para %s: %s", period_s, simbolo, exc)
        return []


# ---------------------------------------------------------------------------
# Loop de operações (manual e automático compartilham este mesmo loop)
# ---------------------------------------------------------------------------

async def _loop_operacoes(
    uid: str,
    config: dict,
    chat_id: int,
    limite_horario: Optional[dtime] = None,
):
    """Loop principal de operações.

    Parâmetros:
      uid             — ID do usuário (string)
      config          — cópia do dict de configuração do usuário
      chat_id         — ID do chat Telegram para enviar mensagens
      limite_horario  — se definido (datetime.time), o loop para quando o
                        horário atual ultrapassar esse valor (usado pelo modo
                        automático para encerrar às 10:30).
    """
    # --- Montagem do cliente ---
    client = _criar_novo_cliente(uid, config)
    _CLIENTES[uid] = client
    EXECUTANDO[uid] = True

    await enviar_telegram(chat_id, "⏳ Conectando à Quotex...")

    ok, client = await _conectar(uid, client, config, chat_id)
    if not ok:
        EXECUTANDO[uid] = False
        _CLIENTES.pop(uid, None)
        return

    auditoria.registrar(uid, "login_quotex_ok")
    await enviar_telegram(chat_id, "✅ Conectado! Iniciando operações automáticas.")

    # --- Parâmetros de operação ---
    simbolo              = config.get("simbolo", "EURUSD")
    valor_entrada        = float(config.get("valor_entrada", 5))
    minutos              = int(config.get("time", 5))
    tempo_s              = minutos * 60
    stop_win             = float(config.get("stop_win", 50))
    stop_loss            = float(config.get("stop_loss", 30))
    usar_martingale      = str(config.get("usar_martingale", "N")).upper() == "S"
    fator_martingale     = float(config.get("fator_martingale", 2.0))
    confianca_minima     = float(config.get("confianca_minima", 75))
    limite_perdas        = int(config.get("limite_perdas_consecutivas", 3))

    valor_atual = valor_entrada

    # --- Loop principal ---
    while EXECUTANDO.get(uid):

        # Verificação de horário limite (modo automático)
        if limite_horario and datetime.now().time() >= limite_horario:
            logger.info("Horário limite %s atingido para user %s — encerrando loop.", limite_horario, uid)
            break

        if estado_diario.esta_parado_hoje(uid):
            motivo = estado_diario._registro(uid).get("motivo", "stop diário")
            await enviar_telegram(chat_id, f"🛑 Operações suspensas ({motivo}). Retome amanhã.")
            break

        perdas_consec = estado_diario.obter_perdas_consecutivas(uid)
        if perdas_consec >= limite_perdas:
            auditoria.registrar(uid, "limite_perdas_consecutivas", f"perdas={perdas_consec}")
            estado_diario.marcar_parado(uid, "perdas_consecutivas")
            await enviar_telegram(
                chat_id,
                f"🛑 Limite de {limite_perdas} perdas consecutivas atingido. Encerrando por hoje.",
            )
            break

        # Busca velas M5 (300s) e M15 (900s)
        try:
            velas_m5  = await _buscar_velas(client, simbolo, 300, 60)
            velas_m15 = await _buscar_velas(client, simbolo, 900, 60)
        except Exception as exc:
            logger.exception("Erro ao buscar velas (user %s): %s", uid, exc)
            await asyncio.sleep(15)
            continue

        if not velas_m5 or not velas_m15:
            await asyncio.sleep(15)
            continue

        # Análise de sinal
        try:
            resultado = analisar(velas_m5, velas_m15, confianca_minima)
        except Exception as exc:
            logger.exception("Erro na análise de sinal (user %s): %s", uid, exc)
            await asyncio.sleep(10)
            continue

        direcao: Optional[str] = resultado.get("direcao")

        if not direcao:
            logger.debug("Sem sinal para %s (user %s): %s", simbolo, uid, resultado.get("motivo_ignorado"))
            await asyncio.sleep(20)
            continue

        # Abertura de operação ANTES de enviar mensagem ao Telegram
        try:
            status, buy_data = await client.buy(valor_atual, simbolo, direcao, tempo_s)
        except Exception as exc:
            logger.exception("Erro ao abrir operação (user %s): %s", uid, exc)
            await asyncio.sleep(10)
            continue

        if not status:
            logger.warning("Falha ao abrir operação (user %s) — sinal ignorado.", uid)
            await asyncio.sleep(5)
            continue

        # Operação aberta com sucesso → agora avisa o Telegram
        await enviar_telegram(chat_id, _msg_entrada(simbolo, direcao, minutos))

        # ID da operação para verificar resultado
        op_id = None
        if isinstance(buy_data, dict):
            op_id = buy_data.get("id") or buy_data.get("requestId")
        if op_id is None:
            op_id = client.api.buy_id

        # Aguarda o vencimento
        await asyncio.sleep(tempo_s + 2)

        # Verifica resultado
        try:
            lucro, is_win = await client.check_win(op_id)
        except Exception as exc:
            logger.exception("Erro ao verificar resultado (user %s): %s", uid, exc)
            lucro, is_win = 0.0, False

        lucro_acumulado = estado_diario.registrar_resultado(uid, lucro)
        await enviar_telegram(chat_id, _msg_resultado(is_win, lucro, lucro_acumulado))

        # Registra no histórico e auditoria
        op_registro = {
            "quando": datetime.now().strftime("%H:%M"),
            "data":   datetime.now().strftime("%d/%m/%Y"),
            "ativo":  simbolo,
            "direcao": direcao.upper(),
            "minutos": minutos,
            "valor":  valor_atual,
            "lucro":  lucro,
            "win":    is_win,
            "acumulado": lucro_acumulado,
        }
        registrar_operacao(uid, op_registro)
        auditoria.registrar(
            uid, "operacao_realizada",
            f"dir={direcao} valor={valor_atual:.2f} lucro={lucro:.2f} win={is_win}",
        )

        # Martingale / reset de valor
        if is_win:
            valor_atual = valor_entrada
            estado_diario.zerar_perdas_consecutivas(uid)
        elif usar_martingale:
            valor_atual = round(valor_atual * fator_martingale, 2)

        # Stop Win / Stop Loss
        lucro_hoje = estado_diario.obter_lucro_total(uid)
        if lucro_hoje >= stop_win:
            auditoria.registrar(uid, "stop_win_atingido", f"lucro={lucro_hoje:.2f}")
            estado_diario.marcar_parado(uid, "stop_win")
            await enviar_telegram(
                chat_id,
                f"🏆 Stop Win atingido! Lucro do dia: R$ {lucro_hoje:.2f}.\n"
                "Operações encerradas por hoje.",
            )
            break

        if lucro_hoje <= -abs(stop_loss):
            auditoria.registrar(uid, "stop_loss_atingido", f"lucro={lucro_hoje:.2f}")
            estado_diario.marcar_parado(uid, "stop_loss")
            await enviar_telegram(
                chat_id,
                f"🛑 Stop Loss atingido! Perda do dia: R$ {lucro_hoje:.2f}.\n"
                "Operações encerradas por hoje.",
            )
            break

        await asyncio.sleep(5)

    EXECUTANDO[uid] = False
    _CLIENTES.pop(uid, None)
    logger.info("Loop de operações encerrado para user %s", uid)


def _executar_loop_em_thread(uid: str, config: dict, chat_id: int, limite_horario: Optional[dtime]):
    """Executa o loop assíncrono de operações em uma thread daemon dedicada (modo manual)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _LOOPS[uid] = loop
    try:
        loop.run_until_complete(_loop_operacoes(uid, config, chat_id, limite_horario))
    except Exception:
        logger.exception("Erro inesperado no loop de operações (user %s)", uid)
    finally:
        _LOOPS.pop(uid, None)
        EXECUTANDO.pop(uid, None)
        loop.close()
