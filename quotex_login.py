"""Login e loop de operações por usuário do Telegram.

Modos de operação:
  /iniciar  → loop manual, roda até o usuário chamar /parar
  /automatico → scheduler que inicia o loop todo dia útil 08:00-10:30
  /parar    → para ambos os modos

Fluxo de PIN:
  1. connect() lança PinRequiredError → bot avisa o usuário com /pin
  2. /pin XXXXXX → submeter_pin(user_id, codigo) define client.pin_code
  3. O asyncio.Event é destravado → connect() é refeita com o PIN

Isolamento de sessão:
  Cada usuário tem seu próprio diretório sessions/<user_id>/ para que
  session.json não seja compartilhado entre contas.

Assinaturas que bot.py espera:
    async iniciar_estrategia_com_pin(user_id) -> str
    async iniciar_automatico(user_id) -> str
    async submeter_pin(user_id, pin_code)
    cancelar_estrategia(user_id) -> bool
    obter_historico_hoje(user_id) -> list[dict]
    EXECUTANDO: dict[str, bool]
    MODO_AUTO: dict[str, bool]
    enviar_telegram(chat_id, texto) -> coroutine
"""

import asyncio
import logging
import os
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

MAX_PIN_TENTATIVAS = 3

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

# {str(user_id): asyncio.Event} — destravado quando o usuário envia /pin
_PIN_EVENTOS: dict[str, asyncio.Event] = {}

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


async def submeter_pin(user_id: int | str, pin_code: str):
    """Entrega o PIN ao loop de login do usuário.

    Pode ser chamado de qualquer thread (incluindo o loop principal do bot).
    """
    uid = str(user_id)
    client = _CLIENTES.get(uid)
    if client is None:
        logger.warning("submeter_pin: nenhum cliente ativo para user %s", uid)
        return

    client.pin_code = pin_code.strip()

    loop = _LOOPS.get(uid)
    event = _PIN_EVENTOS.get(uid)
    if loop and event:
        loop.call_soon_threadsafe(event.set)
    else:
        logger.warning("submeter_pin: evento/loop não encontrado para user %s", uid)


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
        "🤖 *Modo Automático Ativado!*\n\n"
        "📅 Operações abertas automaticamente de *seg-sex, 08:00 às 10:30*.\n"
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
                f"📅 Próxima abertura: *{msg_proxima}*",
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
# Loop de operações (manual e automático compartilham este mesmo loop)
# ---------------------------------------------------------------------------

async def _conectar_com_pin(uid: str, client: Quotex, chat_id: int) -> bool:
    """Tenta conectar, resolvendo o fluxo de PIN se a Quotex exigir."""
    tentativas = 0
    while tentativas < MAX_PIN_TENTATIVAS:
        try:
            check, reason = await client.connect()
            if check:
                return True
            logger.error("Falha na conexão (user %s): %s", uid, reason)
            await enviar_telegram(
                chat_id,
                f"❌ Falha na conexão com a Quotex: {reason}\n"
                "Verifique sua conexão e tente novamente.",
            )
            return False

        except PinRequiredError as exc:
            tentativas += 1
            logger.info("PIN solicitado para user %s (tentativa %d/%d)", uid, tentativas, MAX_PIN_TENTATIVAS)
            auditoria.registrar(uid, "pin_solicitado", str(exc))
            await enviar_telegram(
                chat_id,
                "🔐 A Quotex está pedindo o código PIN enviado para o seu e-mail.\n"
                f"Envie com: /pin XXXXXX\n"
                f"(Tentativa {tentativas}/{MAX_PIN_TENTATIVAS})",
            )
            event = asyncio.Event()
            _PIN_EVENTOS[uid] = event
            await event.wait()
            _PIN_EVENTOS.pop(uid, None)

        except LoginFailedError as exc:
            logger.error("Login falhou para user %s: %s", uid, exc)
            auditoria.registrar(uid, "login_falhou", str(exc))
            await enviar_telegram(
                chat_id,
                f"❌ Login falhou: {exc.message}\n"
                "Verifique e-mail/senha em /ajustaconfig ou desativa a autenticação de dois fatores de login no quotex e tente novamente /iniciar.",
            )
            return False

    await enviar_telegram(
        chat_id,
        "❌ Código PIN inválido ou expirado após 3 tentativas. Tente novamente.",
    )
    auditoria.registrar(uid, "pin_esgotado")
    return False


async def _buscar_velas(client: Quotex, simbolo: str, period_s: int, qtd: int = 50):
    """Busca candles históricas para o símbolo/timeframe solicitado."""
    try:
        candles = await client.get_candles(simbolo, _time.time(), qtd, period_s)
        return candles or []
    except Exception as exc:
        logger.warning("Falha ao buscar velas %ss para %s: %s", period_s, simbolo, exc)
        return []


def _session_dir(user_id: str) -> str:
    path = os.path.join("sessions", user_id)
    os.makedirs(path, exist_ok=True)
    return path


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
    client = Quotex(
        email=config["emailQuotex"],
        password=config["senhaQuotex"],
        root_path=_session_dir(uid),
        lang="pt",
    )
    client.pin_code = None
    client.email_imap = config.get("email_imap") or None
    client.email_imap_password = config.get("email_imap_password") or None

    _CLIENTES[uid] = client
    EXECUTANDO[uid] = True

    modo = "REAL" if str(config.get("tipo", "demo")).lower() == "real" else "PRACTICE"
    client.set_account_mode(modo)

    await enviar_telegram(chat_id, "⏳ Conectando à Quotex...")

    ok = await _conectar_com_pin(uid, client, chat_id)
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

        # Mensagem de entrada
        await enviar_telegram(chat_id, _msg_entrada(simbolo, direcao, minutos))

        # Abertura de operação
        try:
            status, buy_data = await client.buy(valor_atual, simbolo, direcao, tempo_s)
        except Exception as exc:
            logger.exception("Erro ao abrir operação (user %s): %s", uid, exc)
            await asyncio.sleep(10)
            continue

        if not status:
            await enviar_telegram(chat_id, "⚠️ Falha ao abrir operação. Aguardando próximo sinal...")
            await asyncio.sleep(5)
            continue

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
