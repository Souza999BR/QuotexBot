"""Custom exceptions for the pyquotex login flow."""


class PinRequiredError(Exception):
    """Raised when Quotex asks for the e-mail verification PIN code.

    This is only raised when the Quotex platform itself asks for the code
    (i.e. the ``keep_code`` field is present in the sign-in response). It is
    never raised speculatively.

    The bot must catch this, ask the specific Telegram user for the code
    (e.g. via ``/pin 123456``) and retry the login with ``client.pin_code``
    set — never block on a blocking ``input()`` call, since the bot serves
    many users concurrently and has no terminal attached when running on a
    host like Discloud.
    """

    def __init__(self, message="A Quotex está solicitando o código PIN enviado por e-mail."):
        self.message = message
        super().__init__(message)


class InvalidPinError(Exception):
    """Raised when the PIN code supplied by the user was rejected/invalid."""

    def __init__(self, message="Código PIN inválido ou expirado."):
        self.message = message
        super().__init__(message)


class LoginFailedError(Exception):
    """Raised when the Quotex sign-in request itself fails.

    This covers wrong e-mail/password, an account blocked by Quotex, or the
    sign-in page changing shape (e.g. a Cloudflare/captcha wall). It must
    never be handled by exiting the process: the bot serves many users at
    once, and one user's bad credentials must not take down everyone else's
    session. The caller (the Telegram bot layer) catches this per user and
    reports the specific reason back to that user and the admin.
    """

    def __init__(self, message="Login failed."):
        self.message = message
        super().__init__(message)
