"""Module for Quotex HTTP browser — uses curl_cffi to bypass Cloudflare TLS fingerprinting.

Cloudflare detects bots via TLS fingerprint (JA3/JA4). Plain `requests` and
`httpx` have fingerprints that are trivially blocked on datacenter IPs (e.g.
Discloud, AWS, GCP). curl_cffi impersonates Chrome's exact TLS stack, making
the connection indistinguishable from a real browser.
"""
import logging

try:
    from curl_cffi import requests as cffi_requests
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    import requests as cffi_requests  # fallback — may get 403 on datacenter IPs
    _CURL_CFFI_AVAILABLE = False

logger = logging.getLogger(__name__)

if _CURL_CFFI_AVAILABLE:
    logger.debug("navigator: usando curl_cffi (Chrome TLS impersonation).")
else:
    logger.warning(
        "navigator: curl_cffi não instalado — usando requests padrão. "
        "O login pode falhar com HTTP 403 em servidores de datacenter. "
        "Instale: pip install curl_cffi"
    )

# Chrome 120 impersonation — corresponde ao User-Agent e Sec-Ch-Ua em api.py
_IMPERSONATE = "chrome120"


class Browser:
    """HTTP client que impersona o Chrome para contornar o Cloudflare."""

    def __init__(self):
        self.headers: dict = {}
        self._session = None

    def _get_session(self):
        """Retorna (ou cria) a sessão curl_cffi com impersonação Chrome."""
        if self._session is None:
            if _CURL_CFFI_AVAILABLE:
                self._session = cffi_requests.Session(impersonate=_IMPERSONATE)
            else:
                self._session = cffi_requests.Session()
        return self._session

    def set_headers(self):
        """Inicializa cabeçalhos padrão (populados depois por api.py)."""
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        }

    def send_request(self, method: str, url: str, data=None, params=None):
        """Envia requisição HTTP usando curl_cffi (Chrome TLS fingerprint).

        :param method: "GET" ou "POST"
        :param url:    URL de destino
        :param data:   payload do corpo (POST)
        :param params: query string params
        :returns:      objeto Response compatível com requests
        """
        session = self._get_session()

        # Aplica os cabeçalhos atuais à sessão
        session.headers.update(self.headers)

        method = method.upper()
        try:
            if method == "GET":
                response = session.get(url, params=params, timeout=30)
            elif method == "POST":
                response = session.post(url, data=data, params=params, timeout=30)
            else:
                response = session.request(method, url, data=data, params=params, timeout=30)

            logger.info(
                "[%s] HTTP %s %s",
                "Browser" if _CURL_CFFI_AVAILABLE else "Browser(fallback)",
                response.status_code,
                url,
            )
            return response

        except Exception as exc:
            logger.error("Erro na requisição HTTP para %s: %s", url, exc)
            raise
