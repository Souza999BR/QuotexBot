"""Sistema de licenças/senhas de acesso ao bot.

Regras de negócio (definidas pelo administrador do bot):
- O cadastro é automático: o próprio usuário roda `/cadastro` e o bot gera
  uma senha individual, válida por 30 dias — mas essa senha NÃO é entregue
  automaticamente a ele. Ela só existe na lista do administrador.
- O usuário só consegue liberar o acesso (para configurar e usar o bot) se
  pedir a senha diretamente ao administrador (@<ADMIN_USERNAME>) e digitar
  exatamente a mesma senha que está na lista do admin, com /senha.
- Cada usuário tem sua própria senha — nunca uma senha compartilhada entre
  vários usuários.
- Toda senha expira automaticamente depois de `SENHA_VALIDADE_DIAS` dias.
  Quando expira, uma nova é gerada automaticamente, o acesso anterior é
  suspenso, e a lista atualizada (usuário -> nova senha) é enviada ao
  administrador — nunca diretamente ao usuário, que precisa pedi-la de
  novo ao admin (renovação de assinatura).
- É proibido compartilhar a senha recebida. Se a mesma senha for usada por
  um usuário diferente daquele a quem ela foi emitida, o acesso do titular
  original é revogado imediatamente e o administrador é avisado.

Este módulo não depende do Telegram — ele só guarda o estado. O envio de
mensagens (para o admin) é feito por quem chama estas funções (bot.py),
passando uma função `enviar(chat_id, texto)`.
"""
import json
import os
import secrets
import string
import logging
import asyncio
import datetime
from threading import Lock

from config_bot import SENHA_LENGTH, SENHA_VALIDADE_DIAS
import auditoria

logger = logging.getLogger(__name__)

ARQUIVO_LICENCAS = "licencas.json"
_lock = Lock()
_licencas = {}

ALFABETO_SENHA = string.ascii_uppercase + string.digits


def _gerar_senha():
    return "".join(secrets.choice(ALFABETO_SENHA) for _ in range(SENHA_LENGTH))


def _validade():
    return (datetime.date.today() + datetime.timedelta(days=SENHA_VALIDADE_DIAS)).isoformat()


def _carregar():
    global _licencas
    if os.path.exists(ARQUIVO_LICENCAS):
        try:
            with open(ARQUIVO_LICENCAS, "r", encoding="utf-8") as f:
                _licencas = json.load(f)
        except (json.JSONDecodeError, OSError):
            _licencas = {}
    else:
        _licencas = {}


def _salvar():
    with open(ARQUIVO_LICENCAS, "w", encoding="utf-8") as f:
        json.dump(_licencas, f, ensure_ascii=False, indent=4)


_carregar()


def usuario_existe(user_id):
    return str(user_id) in _licencas


def esta_autorizado(user_id):
    reg = _licencas.get(str(user_id))
    return bool(reg and reg.get("autorizado"))


def obter_ou_criar_licenca(user_id):
    """Cadastro automático e autoatendido (comando /cadastro).

    Se o usuário já tiver uma senha válida (ainda dentro do prazo), retorna
    a mesma senha sem gerar outra (idempotente). Caso contrário, gera uma
    nova senha, válida por `SENHA_VALIDADE_DIAS` dias.

    Retorna (senha, é_nova: bool). A senha NUNCA é enviada ao usuário por
    aqui — só existe na lista do administrador — o próprio usuário precisa
    pedi-la diretamente a ele.
    """
    uid = str(user_id)
    hoje = datetime.date.today()

    with _lock:
        reg = _licencas.get(uid)
        if reg and datetime.date.fromisoformat(reg["expira_em"]) >= hoje:
            return reg["senha"], False

        senha = _gerar_senha()
        _licencas[uid] = {
            "senha": senha,
            "autorizado": reg.get("autorizado", False) if reg else False,
            "criado_em": reg.get("criado_em", hoje.isoformat()) if reg else hoje.isoformat(),
            "expira_em": _validade(),
            "compartilhada": False,
        }
        _salvar()
        auditoria.registrar(uid, "licenca_criada", f"expira em {_licencas[uid]['expira_em']}")
        return senha, True


def forcar_nova_senha(user_id):
    """Reemite a senha de um usuário manualmente (uso exclusivo do admin,
    ex.: suporte, "perdi minha senha"). Ao contrário do /cadastro
    autoatendido, esta ação é sempre disparada pelo próprio administrador,
    então o bot pode entregar a nova senha diretamente ao usuário."""
    uid = str(user_id)
    hoje = datetime.date.today()
    with _lock:
        reg = _licencas.get(uid, {})
        senha = _gerar_senha()
        _licencas[uid] = {
            "senha": senha,
            "autorizado": reg.get("autorizado", False),
            "criado_em": reg.get("criado_em", hoje.isoformat()),
            "expira_em": _validade(),
            "compartilhada": False,
        }
        _salvar()
    auditoria.registrar(uid, "senha_reemitida_admin")
    return senha


def revogar_usuario(user_id):
    uid = str(user_id)
    with _lock:
        if uid in _licencas:
            _licencas[uid]["autorizado"] = False
            _salvar()
            return True
        return False


def validar_senha(user_id, senha_informada):
    """Valida a senha enviada por um usuário no comando /senha.

    Retorna uma tupla (ok: bool, motivo: str) onde motivo é um dos:
    - "ok"                 -> senha correta, é a senha do próprio usuário
    - "nao_encontrada"     -> senha não corresponde a nenhum usuário cadastrado
    - "expirada"           -> a senha é do próprio usuário, mas já venceu
    - "compartilhamento"   -> a senha é válida, mas pertence a OUTRO usuário
                               (a senha do titular original acaba de ser
                               revogada por este motivo)
    """
    uid = str(user_id)
    senha_informada = senha_informada.strip()
    hoje = datetime.date.today()

    with _lock:
        titular = None
        for outro_uid, reg in _licencas.items():
            if reg.get("senha") == senha_informada:
                titular = outro_uid
                break

        if titular is None:
            auditoria.registrar(uid, "senha_invalida")
            return False, "nao_encontrada"

        reg_titular = _licencas[titular]
        expirada = datetime.date.fromisoformat(reg_titular["expira_em"]) < hoje

        if titular == uid:
            if expirada:
                auditoria.registrar(uid, "senha_expirada")
                return False, "expirada"
            reg_titular["autorizado"] = True
            _salvar()
            auditoria.registrar(uid, "login_senha_ok")
            return True, "ok"

        # Mesma senha usada por um chat_id diferente do titular -> compartilhamento.
        reg_titular["autorizado"] = False
        reg_titular["compartilhada"] = True
        reg_titular["senha"] = _gerar_senha()  # invalida a senha vazada
        _salvar()
        auditoria.registrar(
            titular, "bloqueio_compartilhamento", f"senha usada por outro id: {uid}"
        )
        return False, "compartilhamento"


def listar_senhas():
    """Retorna [(user_id, senha, expira_em)] de todos os usuários autorizados (uso do admin)."""
    return [
        (uid, reg["senha"], reg["expira_em"])
        for uid, reg in _licencas.items()
        if reg.get("autorizado")
    ]


async def agendar_renovacao_automatica(enviar_relatorio_admin):
    """Task de background: renova (gera nova senha + suspende o acesso)
    de todo usuário cuja senha tenha expirado, e reporta ao administrador.

    Não avisa o usuário diretamente — a renovação de acesso passa sempre
    pelo administrador, que decide a quem repassar a nova senha (ex.:
    conforme pagamento em dia)."""
    while True:
        hoje = datetime.date.today()
        renovadas = {}

        with _lock:
            for uid, reg in _licencas.items():
                if datetime.date.fromisoformat(reg["expira_em"]) < hoje:
                    nova_senha = _gerar_senha()
                    reg["senha"] = nova_senha
                    reg["expira_em"] = _validade()
                    reg["autorizado"] = False
                    reg["compartilhada"] = False
                    renovadas[uid] = nova_senha
            if renovadas:
                _salvar()

        if renovadas:
            for uid_renovado in renovadas:
                auditoria.registrar(uid_renovado, "licenca_renovada")
            linhas = "\n".join(f"🔐 {uid}: {senha}" for uid, senha in renovadas.items())
            logger.info("🔐 Senha de %d usuário(s) renovada(s) automaticamente.", len(renovadas))
            await enviar_relatorio_admin(
                "📋 Renovação automática de senha (venceram 30 dias).\n"
                "O acesso desses usuários foi suspenso até pedirem a nova senha:\n\n"
                f"{linhas}"
            )

        # Verifica a cada hora quem venceu.
        await asyncio.sleep(3600)
