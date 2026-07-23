# == bot.py ==
import logging
import threading
import asyncio
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    MessageHandler, ConversationHandler, CallbackQueryHandler, filters
)
from telegram.error import TimedOut, TelegramError, NetworkError
from telegram.request import HTTPXRequest

from shared import USERS_DATA, CADASTRADOS, salvar_dados, cifrar_config_usuario
from quotex_login import (
    iniciar_estrategia_com_pin, iniciar_automatico,
    submeter_pin, cancelar_estrategia,
    EXECUTANDO, MODO_AUTO,
    enviar_telegram, obter_historico_hoje,
)
import licencas
import estado_diario
from config_bot import TELEGRAM_BOT_TOKEN, ADMIN_CHAT_ID, ADMIN_USERNAME, validar_configuracao

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bot")

# === Estados ===
(
    EMAIL_QUOTEX, SENHA_QUOTEX, LOGIN_AUTOMATICO, EMAIL_IMAP, SENHA_IMAP,
    VALOR_ENTRADA, TIPO_CONTA, TEMPO_EXPIRACAO, SIMBOLO,
    STOP_WIN, STOP_LOSS, USAR_MARTINGALE, FATOR_MARTINGALE, CONFIRMAR,
    AJUSTAR_CAMPO, NOVO_VALOR, VALIDAR_SENHA, NOVO_USUARIO_ID
) = range(18)

CAMPOS_SENSIVEIS_LOG = {"senhaQuotex", "senha", "email_imap_password"}


def _eh_admin(update: Update) -> bool:
    return str(update.effective_chat.id) == str(ADMIN_CHAT_ID)


def _pedir_ao_admin(user_id) -> str:
    return (
        f"Peça sua senha de acesso ao administrador @{ADMIN_USERNAME}, "
        f"informando o seu ID: {user_id}"
    )


# === Comandos ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Bem-vindo ao Bot Souza Quotex!\n\n"
        "Primeiro acesso:\n"
        "1. /cadastro - Fazer seu cadastro\n"
        f"2. Peça sua senha de acesso ao administrador (@{ADMIN_USERNAME})\n"
        "3. /senha - Liberar o acesso com a senha recebida\n\n"
        "Depois de liberado:\n"
        "/config - Configurar sua conta Quotex\n"
        "/ajustaconfig - Ajustar configurações existentes\n"
        "/iniciar - Iniciar operações manualmente (roda até /parar)\n"
        "/automatico - Modo automático: opera seg-sex das 08:00 às 10:30\n"
        "/pin - Enviar o código PIN quando a Quotex solicitar\n"
        "/parar - Parar /iniciar ou /automatico\n"
        "/historico - Ver operações de hoje\n"
        "/meusdados - Exibir suas configurações\n"
        "/help - Suporte e informações"
    )


# === Cadastro automático ===
async def cadastro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    senha_gerada, nova = licencas.obter_ou_criar_licenca(user_id)

    if nova:
        await update.message.reply_text(
            "✅ Cadastro realizado com sucesso!\n"
            f"{_pedir_ao_admin(user_id)}\n"
            "Depois, use /senha para liberar o acesso."
        )
        await enviar_telegram(
            ADMIN_CHAT_ID,
            "🆕 Novo cadastro recebido.\n"
            f"ID: {user_id}\n"
            f"Senha gerada: {senha_gerada}",
        )
    else:
        await update.message.reply_text(
            f"ℹ️ Você já está cadastrado. {_pedir_ao_admin(user_id)}\n"
            "Depois, use /senha."
        )


# === Fluxo da senha pessoal ===
async def senha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not licencas.usuario_existe(user_id):
        await update.message.reply_text("⚠️ Você ainda não fez o cadastro. Use /cadastro primeiro.")
        return ConversationHandler.END
    await update.message.reply_text("🔐 Envie a sua senha pessoal de acesso.")
    return VALIDAR_SENHA


async def validar_senha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    texto = update.message.text.strip()

    ok, motivo = licencas.validar_senha(user_id, texto)

    if ok:
        if user_id not in CADASTRADOS:
            CADASTRADOS.append(user_id)
            salvar_dados()
        await update.message.reply_text("🔓 Acesso liberado! Agora use /config para configurar sua conta Quotex.")
        return ConversationHandler.END

    if motivo == "compartilhamento":
        await update.message.reply_text(
            "🚫 Esta senha pertence a outro usuário. Compartilhar senha é proibido "
            "e o acesso do titular original foi revogado. "
            f"{_pedir_ao_admin(user_id)}"
        )
        await enviar_telegram(
            ADMIN_CHAT_ID,
            f"⚠️ Compartilhamento de senha detectado.\nTentativa feita por: {user_id}\n"
            "A senha original foi revogada automaticamente.",
        )
    elif motivo == "expirada":
        await update.message.reply_text(
            f"⌛ Sua senha expirou. {_pedir_ao_admin(user_id)}"
        )
    else:
        await update.message.reply_text(f"❌ Senha inválida. {_pedir_ao_admin(user_id)}")

    return ConversationHandler.END


# === Fluxo de configuração inicial ===
async def config_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not licencas.esta_autorizado(user_id):
        await update.message.reply_text("❌ Você precisa estar autorizado. Use /senha primeiro.")
        return ConversationHandler.END

    context.user_data.clear()
    await update.message.reply_text("📧 Qual o e-mail da Quotex?")
    return EMAIL_QUOTEX


async def receber_email(update, context):
    context.user_data['emailQuotex'] = update.message.text.strip()
    await update.message.reply_text("🔒 Qual a senha da Quotex?")
    return SENHA_QUOTEX


async def receber_senha(update, context):
    context.user_data['senhaQuotex'] = update.message.text.strip()
    await update.message.reply_text(
        "🤖 Deseja ativar o login automático?\n"
        "Se SIM, sempre que a Quotex pedir o código PIN de verificação, o bot vai "
        "buscá-lo sozinho no seu e-mail (via IMAP), sem você precisar fazer nada.\n"
        "Se NÃO, sempre que a Quotex pedir o código, o bot vai te avisar aqui e você "
        "precisará checar o e-mail e responder com /pin 123456.\n"
        "Responda S ou N:"
    )
    return LOGIN_AUTOMATICO


async def receber_login_automatico(update, context):
    resposta = update.message.text.strip().upper()
    if resposta not in ["S", "N"]:
        await update.message.reply_text("❌ Digite apenas 'S' para sim ou 'N' para não.")
        return LOGIN_AUTOMATICO

    context.user_data['login_automatico'] = resposta
    if resposta == "N":
        context.user_data['email_imap'] = ""
        context.user_data['email_imap_password'] = ""
        await update.message.reply_text("💰 Valor da entrada:")
        return VALOR_ENTRADA

    await update.message.reply_text(
        "📧 Envie o e-mail usado para receber o código PIN da Quotex "
        "(geralmente o mesmo e-mail da conta). Ele só será acessado para ler "
        "o código, via IMAP."
    )
    return EMAIL_IMAP


async def receber_email_imap(update, context):
    context.user_data['email_imap'] = update.message.text.strip()
    await update.message.reply_text(
        "🔑 Envie a senha de aplicativo (App Password) desse e-mail, para acesso IMAP.\n"
        "⚠️ No Gmail, crie uma 'senha de app' em myaccount.google.com/apppasswords — "
        "não use a senha normal da conta."
    )
    return SENHA_IMAP


async def receber_senha_imap(update, context):
    context.user_data['email_imap_password'] = update.message.text.strip()
    await update.message.reply_text("💰 Valor da entrada:")
    return VALOR_ENTRADA


async def receber_valor(update, context):
    context.user_data['valor_entrada'] = update.message.text.strip()
    await update.message.reply_text("🏦 Tipo da conta (real/demo):")
    return TIPO_CONTA


async def receber_tipo(update, context):
    tipo = update.message.text.strip().lower()
    if tipo not in ["real", "demo"]:
        await update.message.reply_text("❌ Digite apenas 'real' ou 'demo'.")
        return TIPO_CONTA

    context.user_data['tipo'] = tipo
    await update.message.reply_text("⏱️ Tempo de expiração (em minutos, ex: 5):")
    return TEMPO_EXPIRACAO


async def receber_tempo(update, context):
    tempo = update.message.text.strip()
    if not tempo.isdigit():
        await update.message.reply_text("❌ O tempo deve ser um número (ex: 5).")
        return TEMPO_EXPIRACAO

    context.user_data['time'] = tempo
    await update.message.reply_text("💱 Qual o símbolo do ativo (ex: EURUSD, AUDJPY, GBPJPY)?")
    return SIMBOLO


async def receber_simbolo(update, context):
    context.user_data['simbolo'] = update.message.text.strip().upper()
    await update.message.reply_text("🎯 Stop Win:")
    return STOP_WIN


async def receber_stop_win(update, context):
    try:
        context.user_data['stop_win'] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ O Stop Win deve ser numérico.")
        return STOP_WIN
    await update.message.reply_text("🛑 Stop Loss:")
    return STOP_LOSS


async def receber_stop_loss(update, context):
    try:
        context.user_data['stop_loss'] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ O Stop Loss deve ser numérico.")
        return STOP_LOSS
    await update.message.reply_text("♻️ Usar Martingale? (S/N)")
    return USAR_MARTINGALE


async def receber_martingale(update, context):
    resposta = update.message.text.strip().upper()
    if resposta not in ["S", "N"]:
        await update.message.reply_text("❌ Digite apenas 'S' para sim ou 'N' para não.")
        return USAR_MARTINGALE

    context.user_data['usar_martingale'] = resposta
    await update.message.reply_text("💹 Fator Martingale (ex: 1.0, 2.0):")
    return FATOR_MARTINGALE


async def receber_fator(update, context):
    try:
        fator = float(update.message.text)
    except ValueError:
        await update.message.reply_text("❌ O fator Martingale deve ser numérico (ex: 1.0).")
        return FATOR_MARTINGALE

    context.user_data['fator_martingale'] = fator
    resumo = "\n".join(
        f"{k}: {'••••••' if k in CAMPOS_SENSIVEIS_LOG else v}"
        for k, v in context.user_data.items()
    )
    await update.message.reply_text(f"✅ Configuração recebida:\n{resumo}\nConfirmar? (sim/não)")
    return CONFIRMAR


async def confirmar_config(update, context):
    user_id = update.message.from_user.id
    resposta = update.message.text.lower()

    if resposta == "sim":
        USERS_DATA[str(user_id)] = cifrar_config_usuario(context.user_data)
        salvar_dados()
        await update.message.reply_text("✅ Configuração salva com sucesso!")
    else:
        await update.message.reply_text("❌ Configuração cancelada.")

    return ConversationHandler.END


# === Ajustar configurações existentes ===
async def ajusta_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_chat.id)

    if uid not in USERS_DATA:
        await update.message.reply_text("❌ Você ainda não está cadastrado. Use /config.")
        return ConversationHandler.END

    botoes = [
        [InlineKeyboardButton("📧 Email", callback_data="emailQuotex")],
        [InlineKeyboardButton("🔐 Senha", callback_data="senhaQuotex")],
        [InlineKeyboardButton("🤖 Login automático (S/N)", callback_data="login_automatico")],
        [InlineKeyboardButton("📧 Email IMAP (PIN automático)", callback_data="email_imap")],
        [InlineKeyboardButton("🔑 Senha IMAP (PIN automático)", callback_data="email_imap_password")],
        [InlineKeyboardButton("💰 Valor Entrada", callback_data="valor_entrada")],
        [InlineKeyboardButton("🏦 Tipo de Conta (real/demo)", callback_data="tipo")],
        [InlineKeyboardButton("⏳ Tempo Expiração (min)", callback_data="time")],
        [InlineKeyboardButton("💱 Símbolo", callback_data="simbolo")],
        [InlineKeyboardButton("🎯 Stop Win", callback_data="stop_win")],
        [InlineKeyboardButton("🛑 Stop Loss", callback_data="stop_loss")],
        [InlineKeyboardButton("♻️ Usar Martingale (S/N)", callback_data="usar_martingale")],
        [InlineKeyboardButton("🔁 Fator Martingale", callback_data="fator_martingale")],
        [InlineKeyboardButton("📈 Confiança mínima da estratégia (%)", callback_data="confianca_minima")],
        [InlineKeyboardButton("⛔ Limite de perdas consecutivas", callback_data="limite_perdas_consecutivas")],
    ]

    teclado = InlineKeyboardMarkup(botoes)
    await update.message.reply_text("🔧 Qual configuração deseja ajustar?", reply_markup=teclado)
    return AJUSTAR_CAMPO


async def escolher_campo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    campo = query.data
    context.user_data["campo_ajuste"] = campo
    nome_amigavel = campo.replace("_", " ").capitalize()

    await query.message.reply_text(
        f"✏️ Envie o novo valor para *{nome_amigavel}*:",
        parse_mode="Markdown"
    )
    return NOVO_VALOR


async def receber_novo_valor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_chat.id)
    campo = context.user_data.get("campo_ajuste")
    valor = update.message.text.strip()

    if uid not in USERS_DATA:
        await update.message.reply_text("❌ Usuário não encontrado. Use /config primeiro.")
        return ConversationHandler.END

    if campo == "tipo":
        valor = valor.lower()
        if valor not in ["real", "demo"]:
            await update.message.reply_text("⚠️ Valor inválido. Digite apenas *real* ou *demo*.", parse_mode="Markdown")
            return NOVO_VALOR
    elif campo in ("usar_martingale", "login_automatico"):
        valor = valor.upper()
        if valor not in ["S", "N"]:
            await update.message.reply_text("⚠️ Valor inválido. Digite apenas *S* ou *N*.", parse_mode="Markdown")
            return NOVO_VALOR
    elif campo in ("stop_win", "stop_loss", "fator_martingale", "confianca_minima"):
        try:
            valor = float(valor)
        except ValueError:
            await update.message.reply_text("⚠️ O valor deve ser numérico.")
            return NOVO_VALOR
        if campo == "confianca_minima" and not (0 < valor <= 100):
            await update.message.reply_text("⚠️ A confiança mínima deve ser um número entre 0 e 100.")
            return NOVO_VALOR
    elif campo == "limite_perdas_consecutivas":
        try:
            valor = int(valor)
        except ValueError:
            await update.message.reply_text("⚠️ O valor deve ser um número inteiro.")
            return NOVO_VALOR
        if valor < 1:
            await update.message.reply_text("⚠️ O limite deve ser de pelo menos 1.")
            return NOVO_VALOR

    config_atual = dict(USERS_DATA[uid])
    if campo in ("emailQuotex", "senhaQuotex", "email_imap", "email_imap_password"):
        # Recifra apenas o campo alterado, sem tocar nos demais.
        config_atual[campo] = cifrar_config_usuario({campo: valor})[campo]
    else:
        config_atual[campo] = valor

    USERS_DATA[uid] = config_atual
    salvar_dados()

    await update.message.reply_text(
        f"✅ O campo *{campo.replace('_', ' ').capitalize()}* foi atualizado com sucesso!",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


# === Exibir dados ===
async def meusdados(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_chat.id)
    if uid not in USERS_DATA:
        await update.message.reply_text("❌ Nenhum dado encontrado. Use /config.")
        return

    dados = USERS_DATA[uid]
    texto = (
        f"📄 *Seus dados:*\n"
        f"Email: {dados.get('emailQuotex', '—')[:3]}••• (oculto)\n"
        f"Tipo: {dados.get('tipo')}\n"
        f"Valor Entrada: R$ {dados.get('valor_entrada')}\n"
        f"Martingale: {dados.get('usar_martingale')} | Fator: {dados.get('fator_martingale')}\n"
        f"Stop Win: {dados.get('stop_win')} | Stop Loss: {dados.get('stop_loss')}\n"
        f"Confiança mínima da estratégia: {dados.get('confianca_minima', 75)}%\n"
        f"Limite de perdas consecutivas: {dados.get('limite_perdas_consecutivas', 3)}\n"
        f"Login automático (PIN por e-mail): {dados.get('login_automatico', 'N')}\n"
        f"Parado hoje: {'Sim' if estado_diario.esta_parado_hoje(uid) else 'Não'}"
    )
    await update.message.reply_text(texto, parse_mode="Markdown")


# === Início / controle das operações ===
async def iniciar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not licencas.esta_autorizado(user_id):
        await update.message.reply_text("❌ Você não está autorizado. Use /senha primeiro.")
        return

    await update.message.reply_text("🚀 Conectando à Quotex...")
    resultado = await iniciar_estrategia_com_pin(user_id)

    respostas = {
        "SEM_CONFIGURACAO": "❌ Configure sua conta primeiro com /config.",
        "JA_EM_EXECUCAO":   "⚠️ Você já tem uma operação em andamento. Use /parar antes de reiniciar.",
        "INICIADO":         "✅ Operações manuais iniciadas! Use /parar para encerrar.",
    }
    texto = respostas.get(resultado)
    if texto:
        await update.message.reply_text(texto)


async def automatico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ativa o modo automático: opera seg-sex das 08:00 às 10:30."""
    user_id = update.message.from_user.id
    if not licencas.esta_autorizado(user_id):
        await update.message.reply_text("❌ Você não está autorizado. Use /senha primeiro.")
        return

    resultado = await iniciar_automatico(user_id)

    respostas = {
        "SEM_CONFIGURACAO": "❌ Configure sua conta primeiro com /config.",
        "JA_EM_EXECUCAO":   "⚠️ Você já tem uma operação ou modo automático ativo. Use /parar antes.",
        "INICIADO":         (
            "🤖 Modo Automático ativado!\n"
            "📅 Operações abertas automaticamente de seg-sex, 08:00 às 10:30.\n"
            "Use /parar para desativar."
        ),
    }
    texto = respostas.get(resultado, "⚠️ Resposta inesperada. Tente novamente.")
    await update.message.reply_text(texto)


async def receber_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not context.args:
        await update.message.reply_text("Use: /pin 123456")
        return
    codigo = context.args[0].strip()
    if not codigo.isdigit():
        await update.message.reply_text("❌ O código PIN deve conter apenas números.")
        return
    await submeter_pin(user_id, codigo)


async def parar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    uid_str = str(uid)
    ativo = EXECUTANDO.get(uid_str) or MODO_AUTO.get(uid_str)
    if ativo and cancelar_estrategia(uid):
        await update.message.reply_text("🛑 Parando... Aguarde o encerramento da operação atual.")
    else:
        await update.message.reply_text("⚠️ Nenhuma operação ou modo automático ativo.")


async def historico_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe as operações de HOJE do usuário."""
    uid = str(update.effective_chat.id)
    from datetime import datetime
    hoje_fmt = datetime.now().strftime("%d/%m/%Y")
    ops = obter_historico_hoje(uid)

    if not ops:
        await update.message.reply_text(
            f"📭 Nenhuma operação registrada hoje ({datetime.now().strftime('%d/%m/%Y')}).\n"
            "Use /iniciar ou /automatico para começar."
        )
        return

    linhas = []
    for op in ops:
        emoji = "✅" if op["win"] else "❌"
        linhas.append(
            f"{emoji} {op['quando']} | {op['ativo']} M{op['minutos']} "
            f"{op['direcao']} | R$ {op['lucro']:+.2f}"
        )

    lucro_total = sum(o["lucro"] for o in ops)
    wins   = sum(1 for o in ops if o["win"])
    perdas = len(ops) - wins

    texto = (
        f"📊 *Operações de hoje ({datetime.now().strftime('%d/%m')}):*\n\n"
        + "\n".join(linhas)
        + f"\n\n✅ Wins: {wins}  |  ❌ Losses: {perdas}\n"
        f"💰 Lucro do dia: R$ {lucro_total:+.2f}"
    )
    await update.message.reply_text(texto, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❓ Para suporte, contate o administrador do bot.")


# === Administração (senhas pessoais / licenças) ===
async def admin_novo_usuario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _eh_admin(update):
        await update.message.reply_text("❌ Acesso negado. Comando apenas para o administrador.")
        return ConversationHandler.END
    await update.message.reply_text("🆔 Envie o ID do Telegram do novo usuário (ele deve ter enviado /start antes):")
    return NOVO_USUARIO_ID


async def admin_receber_novo_usuario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if not texto.isdigit():
        await update.message.reply_text("❌ Envie apenas o número do ID do Telegram.")
        return ConversationHandler.END

    # Reemissão manual, feita pelo próprio admin (ex.: suporte). Diferente do
    # /cadastro autoatendido, aqui é o admin quem decide entregar a senha
    # diretamente ao usuário.
    senha_gerada = licencas.forcar_nova_senha(texto)
    await update.message.reply_text(f"✅ Senha reemitida para o usuário {texto}.\nSenha: {senha_gerada}")
    try:
        await enviar_telegram(
            texto,
            "🔐 Uma nova senha de acesso pessoal foi emitida para você.\n"
            f"Senha: {senha_gerada}\n"
            "Use /senha para liberar o acesso. Não compartilhe esta senha — "
            "se ela for usada por outra pessoa, seu acesso será revogado.",
        )
    except Exception:
        pass
    return ConversationHandler.END


async def admin_senhas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _eh_admin(update):
        await update.message.reply_text("❌ Acesso negado. Comando apenas para o administrador.")
        return
    senhas = licencas.listar_senhas()
    if not senhas:
        await update.message.reply_text("⚠️ Nenhum usuário autorizado ainda.")
        return
    lista = "\n".join(f"🔐 {uid}: {s} (expira em {exp})" for uid, s, exp in senhas)
    await update.message.reply_text(f"📋 *Senhas ativas:*\n\n{lista}", parse_mode="Markdown")


async def admin_revogar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _eh_admin(update):
        await update.message.reply_text("❌ Acesso negado. Comando apenas para o administrador.")
        return
    if not context.args:
        await update.message.reply_text("Use: /revogar <id_telegram>")
        return
    uid = context.args[0].strip()
    if licencas.revogar_usuario(uid):
        await update.message.reply_text(f"✅ Acesso de {uid} revogado.")
    else:
        await update.message.reply_text("⚠️ Usuário não encontrado.")


async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Operação cancelada.")
    return ConversationHandler.END


# === Tratamento global de erros ===
async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.warning("Erro ao processar update: %s", context.error)
    try:
        if update and hasattr(update, "message") and update.message:
            await context.bot.send_message(
                chat_id=update.message.chat_id,
                text="⚠️ Ocorreu um erro de conexão com o Telegram. Tente novamente.",
            )
    except Exception:
        pass
    try:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"🚨 Falha no sistema: {context.error}",
        )
    except Exception:
        pass


# === Renovação automática de senhas + reset diário (tasks de background) ===
async def _enviar_relatorio_admin(texto):
    await enviar_telegram(ADMIN_CHAT_ID, texto)


async def _pos_inicializacao(application):
    application.create_task(estado_diario.agendar_reset_diario())
    application.create_task(licencas.agendar_renovacao_automatica(_enviar_relatorio_admin))


# === Inicialização ===
def _build_app():
    """Constrói e configura a Application do PTB."""
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(HTTPXRequest())
        .post_init(_pos_inicializacao)
        .build()
    )

    senha_conv = ConversationHandler(
        entry_points=[CommandHandler("senha", senha)],
        states={VALIDAR_SENHA: [MessageHandler(filters.TEXT & ~filters.COMMAND, validar_senha)]},
        fallbacks=[CommandHandler("cancel", cancelar)],
    )

    config_conv = ConversationHandler(
        entry_points=[CommandHandler("config", config_start)],
        states={
            EMAIL_QUOTEX: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_email)],
            SENHA_QUOTEX: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_senha)],
            LOGIN_AUTOMATICO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_login_automatico)],
            EMAIL_IMAP: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_email_imap)],
            SENHA_IMAP: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_senha_imap)],
            VALOR_ENTRADA: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_valor)],
            TIPO_CONTA: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_tipo)],
            TEMPO_EXPIRACAO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_tempo)],
            SIMBOLO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_simbolo)],
            STOP_WIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_stop_win)],
            STOP_LOSS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_stop_loss)],
            USAR_MARTINGALE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_martingale)],
            FATOR_MARTINGALE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_fator)],
            CONFIRMAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirmar_config)],
        },
        fallbacks=[CommandHandler("cancel", cancelar)],
    )

    ajusta_conv = ConversationHandler(
        entry_points=[CommandHandler("ajustaconfig", ajusta_config)],
        states={
            AJUSTAR_CAMPO: [CallbackQueryHandler(escolher_campo_callback)],
            NOVO_VALOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, receber_novo_valor)],
        },
        fallbacks=[CommandHandler("cancel", cancelar)],
    )

    admin_novo_usuario_conv = ConversationHandler(
        entry_points=[CommandHandler("addusuario", admin_novo_usuario)],
        states={
            NOVO_USUARIO_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_receber_novo_usuario)],
        },
        fallbacks=[CommandHandler("cancel", cancelar)],
    )

    app.add_handler(senha_conv)
    app.add_handler(config_conv)
    app.add_handler(ajusta_conv)
    app.add_handler(admin_novo_usuario_conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cadastro", cadastro))
    app.add_handler(CommandHandler("iniciar", iniciar))
    app.add_handler(CommandHandler("automatico", automatico))
    app.add_handler(CommandHandler("pin", receber_pin))
    app.add_handler(CommandHandler("parar", parar))
    app.add_handler(CommandHandler("meusdados", meusdados))
    app.add_handler(CommandHandler("historico", historico_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("senhas", admin_senhas))
    app.add_handler(CommandHandler("revogar", admin_revogar))
    app.add_error_handler(handle_error)

    return app


async def _async_main():
    """Corrotina principal — compatível com Python 3.12+ / 3.14.

    Usa a API async do PTB (async with app) em vez de run_polling(),
    pois run_polling() chama asyncio.get_event_loop() que no Python 3.14
    lança RuntimeError quando não há loop em execução.

    O loop de retry fica aqui (async) para poder usar await asyncio.sleep()
    sem bloquear a thread.
    """
    while True:
        try:
            logger.info("🤖 Iniciando bot Telegram...")
            app = _build_app()
            logger.info("✅ Bot pronto e rodando!")

            async with app:
                await app.start()
                await app.updater.start_polling(drop_pending_updates=True)
                # Aguarda indefinidamente; PTB reconecta internamente.
                # O processo é encerrado pelo Discloud via SIGTERM.
                await asyncio.Event().wait()
                await app.updater.stop()
                await app.stop()

        except (KeyboardInterrupt, SystemExit):
            logger.info("Bot encerrado.")
            return
        except (NetworkError, TimedOut, TelegramError) as e:
            logger.warning("Erro de conexão: %s. Reconectando em 5s...", e)
            await asyncio.sleep(5)
        except Exception as e:
            logger.exception("Erro inesperado: %s", e)
            await asyncio.sleep(10)


def iniciar_bot():
    validar_configuracao()
    asyncio.run(_async_main())


if __name__ == "__main__":
    iniciar_bot()
