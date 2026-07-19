# QuotexBot — Versão Corrigida

## Bugs corrigidos

### 1. PIN inválido mesmo enviando o código correto (`stable_api.py`)

**Causa:** `Quotex.connect()` criava uma nova instância de `QuotexAPI` mas não repassava
`pin_code`, `email_imap` e `email_imap_password` para ela. Como `Login.awaiting_pin()`
lê esses valores de `self.api` (a `QuotexAPI`), o PIN enviado pelo usuário nunca era
encontrado e a autenticação sempre falhava com "Código PIN inválido ou expirado".

**Correção:** As três linhas abaixo foram adicionadas em `Quotex.connect()` logo após
inicializar `self.api`:
```python
self.api.pin_code = self.pin_code
self.api.email_imap = self.email_imap
self.api.email_imap_password = self.email_imap_password
```

---

### 2. `shared.py` ausente (bot travava na inicialização)

**Causa:** O arquivo não existia no projeto. O `bot.py` o importa diretamente:
```python
from shared import USERS_DATA, CADASTRADOS, salvar_dados, cifrar_config_usuario
```

**Correção:** `shared.py` foi criado com:
- `USERS_DATA` — dict em memória com configurações decriptadas de cada usuário
- `CADASTRADOS` — lista de IDs autorizados
- `salvar_dados()` — persiste ambos em disco (criptografando campos sensíveis com Fernet)
- `cifrar_config_usuario(user_id, config)` — grava configuração de um usuário

---

### 3. `quotex_login.py` ausente + sem loop de operação por usuário

**Causa:** O arquivo não existia. O `bot.py` importa:
```python
from quotex_login import iniciar_estrategia_com_pin, submeter_pin, cancelar_estrategia, EXECUTANDO, enviar_telegram
```

**Correção:** `quotex_login.py` foi criado com:
- **Loop por usuário em thread daemon separada** — cada `/iniciar` abre sua própria
  thread com seu próprio `asyncio.EventLoop`, evitando que um usuário bloqueie os outros.
- **Sessão isolada por usuário** — cada usuário tem seu próprio diretório
  `sessions/<user_id>/` para que `session.json` não seja compartilhado entre contas.
- **Fluxo de PIN completo:**
  - `iniciar_estrategia_com_pin()` — inicia o loop em background
  - `submeter_pin()` — entrega o PIN da thread principal para a thread do usuário via `asyncio.Event`
  - `cancelar_estrategia()` — para o loop graciosamente
  - `EXECUTANDO` — dict de estado por usuário
  - `enviar_telegram()` — helper para enviar mensagens ao Telegram

---

## Estrutura de arquivos

```
QuotexBot/
├── bot.py                        # Bot Telegram (sem alterações)
├── shared.py                     # ✅ NOVO — estado compartilhado + persistência
├── quotex_login.py               # ✅ NOVO — loop de operações por usuário
├── config_bot.py
├── auditoria.py
├── estado_diario.py
├── estrategia.py
├── licencas.py
└── pyquotex/
    ├── stable_api.py             # ✅ CORRIGIDO — repasse de pin_code/email_imap
    ├── api.py
    ├── http/
    │   ├── login.py              # Lê api.pin_code (funcionava, agora é alimentado)
    │   └── automail.py
    └── ...
```

## Configuração

Copie `.env.example` para `.env` e preencha:
```
TELEGRAM_BOT_TOKEN=...
ADMIN_CHAT_ID=...
ADMIN_USERNAME=...
ENCRYPTION_KEY=...   # gere com: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Execução

```bash
pip install -r requirements.txt
python bot.py
```
