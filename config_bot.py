"""Central configuration for the Telegram bot.

Everything sensitive is read from environment variables (a `.env` file is
supported via python-dotenv) instead of being hard-coded in the source, so
the project can be shared/committed/deployed (e.g. to Discloud) without
leaking the bot token, the admin's chat id or the encryption key.

Copy `.env.example` to `.env` and fill in your own values before running.
"""
import os
import sys
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")

# Usuário do Telegram do administrador (sem o @), mostrado nas mensagens
# do bot para o usuário saber a quem pedir a senha de acesso.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "souza999br")

# Symmetric key used to encrypt Quotex credentials at rest (users_data.json).
# Generate one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")

# Length (in characters) of auto-generated access passwords.
SENHA_LENGTH = int(os.getenv("SENHA_LENGTH", "10"))

# Validade (em dias) de cada senha de acesso individual antes de renovar
# automaticamente.
SENHA_VALIDADE_DIAS = int(os.getenv("SENHA_VALIDADE_DIAS", "30"))

# Hour:minute (24h, server local time) at which the daily Stop Win / Stop
# Loss counters reset, and the day's operations are allowed to resume.
RESET_DIARIO_HORA = int(os.getenv("RESET_DIARIO_HORA", "23"))
RESET_DIARIO_MINUTO = int(os.getenv("RESET_DIARIO_MINUTO", "59"))


def validar_configuracao():
    """Fail fast and loudly if required secrets are missing."""
    faltando = []
    if not TELEGRAM_BOT_TOKEN:
        faltando.append("TELEGRAM_BOT_TOKEN")
    if not ADMIN_CHAT_ID:
        faltando.append("ADMIN_CHAT_ID")
    if not ENCRYPTION_KEY:
        faltando.append("ENCRYPTION_KEY")

    if faltando:
        print(
            "❌ Configuração incompleta. Defina estas variáveis de ambiente "
            f"(arquivo .env): {', '.join(faltando)}"
        )
        print(
            "   Gere uma ENCRYPTION_KEY com: "
            "python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
        sys.exit(1)
