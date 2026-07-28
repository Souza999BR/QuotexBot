"""Module for Quotex http settings resource."""

from .resource import Resource


class Settings(Resource):
    """Class for Quotex user settings (profile) resource."""

    url = ""

    def __init__(self, api):
        super().__init__(api)
        # get_headers() retorna os headers do browser já configurados em api.py
        self.headers = self.get_headers()

    def get_headers(self) -> dict:
        """Retorna os headers HTTP atuais do browser (definidos em api.py)."""
        return dict(self.api.browser.headers)

    def _build_headers(self) -> dict:
        """Monta os headers completos para requisições autenticadas."""
        headers = self.get_headers()
        cookies = self.api.session_data.get("cookies")
        user_agent = self.api.session_data.get("user_agent")
        if cookies:
            headers["Cookie"] = cookies
        if user_agent:
            headers["User-Agent"] = user_agent
        headers["referer"] = f"{self.api.https_url}/{self.api.lang}/trade"
        headers["content-type"] = "application/json"
        headers["accept"] = "application/json"
        return headers

    def get_settings(self) -> dict:
        """Busca os dados do perfil/configurações do usuário na Quotex.

        :returns: dict com chave "data" contendo nickname, id, demoBalance, etc.
        """
        self.url = f"{self.api.https_url}/api/v1/cabinets/getCurrentTrader"
        response = self.send_http_request(
            method="GET",
            headers=self._build_headers(),
        )
        if response:
            try:
                return response.json()
            except Exception:
                return {}
        return {}

    def set_time_offset(self, time_offset: int) -> dict:
        """Atualiza o fuso horário do usuário na Quotex.

        :param time_offset: offset em minutos
        :returns: dict com chave "data" contendo timeOffset atualizado.
        """
        self.url = f"{self.api.https_url}/api/v1/cabinets/set-time-offset"
        response = self.send_http_request(
            method="POST",
            data={"timeOffset": time_offset},
            headers=self._build_headers(),
        )
        if response:
            try:
                return response.json()
            except Exception:
                return {}
        return {}
