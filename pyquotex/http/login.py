import re
import json
import sys
import asyncio
import logging
from pathlib import Path

from pyquotex.http.navigator import Browser
from pyquotex.http.automail import get_pin
from pyquotex.exceptions import PinRequiredError, LoginFailedError


logger = logging.getLogger(__name__)


class Login(Browser):
    """Class for Quotex login resource."""

    url = ""
    cookies = None
    ssid = None

    base_url = "qxbroker.com"
    https_base_url = f"https://{base_url}"


    def __init__(self, api, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.api = api
        self.html = None

        self.headers = self.get_headers()

        self.full_url = (
            f"{self.https_base_url}/{api.lang}"
        )


    def get_token(self):

        self.headers.update({

            "Connection": "keep-alive",

            "Accept-Encoding":
                "gzip, deflate, br",

            "Accept-Language":
                "pt-BR,pt;q=0.8,en-US;q=0.5,en;q=0.3",

            "Accept":
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8",

            "Referer":
                f"{self.full_url}/sign-in",

            "Upgrade-Insecure-Requests":
                "1",

            "Sec-Ch-Ua-Mobile":
                "?0",

            "Sec-Ch-Ua-Platform":
                '"Linux"',

            "Sec-Fetch-Site":
                "same-origin",

            "Sec-Fetch-User":
                "?1",

            "Sec-Fetch-Dest":
                "document",

            "Sec-Fetch-Mode":
                "navigate",

            "Dnt":
                "1"
        })


        try:

            response = self.send_request(
                method="GET",
                url=f"{self.full_url}/sign-in/modal/"
            )

            self.response = response


        except Exception as e:

            logger.error(
                f"Erro carregando página login: {e}"
            )

            return None



        if not getattr(self, "response", None):

            logger.error(
                "Nenhuma resposta recebida no get_token()"
            )

            return None


        try:

            html = self.get_soup()

        except Exception as e:

            logger.error(
                f"Erro lendo HTML token: {e}"
            )

            return None



        match = html.find(
            "input",
            {"name": "_token"}
        )


        token = (
            None
            if not match
            else match.get("value")
        )


        if not token:

            logger.warning(
                "Token CSRF não encontrado"
            )


        return token



    async def awaiting_pin(self, data, input_message):

        self.headers["Content-Type"] = (
            "application/x-www-form-urlencoded"
        )

        self.headers["Referer"] = (
            f"{self.full_url}/sign-in/modal"
        )


        data["keep_code"] = 1


        code = getattr(
            self.api,
            "pin_code",
            None
        )


        if not code:

            email_imap = getattr(
                self.api,
                "email_imap",
                None
            )

            email_imap_password = getattr(
                self.api,
                "email_imap_password",
                None
            )


            if email_imap and email_imap_password:

                logger.info(
                    "Buscando PIN por IMAP..."
                )

                code = await get_pin(
                    email_imap,
                    email_imap_password
                )



        if not code:

            raise PinRequiredError(
                input_message
            )



        code = str(code).strip()



        if not code.isdigit():

            raise PinRequiredError(
                "PIN inválido. Informe somente números."
            )


        data["code"] = code


        self.api.pin_code = None



        await asyncio.sleep(1)



        self.response = self.send_request(
            method="POST",
            url=f"{self.full_url}/sign-in/modal",
            data=data
        )




    def get_profile(self):

        self.response = self.send_request(
            method="GET",
            url=f"{self.full_url}/trade"
        )


        if not self.response:

            return None, None



        script = self.get_soup().find_all(
            "script",
            {
                "type":
                "text/javascript"
            }
        )


        script = (
            script[0].get_text()
            if script
            else "{}"
        )


        match = re.sub(
            "window.settings = ",
            "",
            script.strip().replace(";", "")
        )


        try:

            settings = json.loads(match)

        except Exception:

            settings = {}



        self.cookies = self.get_cookies()

        self.ssid = settings.get(
            "token"
        )



        self.api.session_data["cookies"] = (
            self.cookies
        )

        self.api.session_data["token"] = (
            self.ssid
        )

        self.api.session_data["user_agent"] = (
            self.headers["User-Agent"]
        )



        output_file = Path(
            f"{self.api.resource_path}/session.json"
        )


        output_file.parent.mkdir(
            exist_ok=True,
            parents=True
        )


        output_file.write_text(
            json.dumps(
                {
                    "cookies": self.cookies,
                    "token": self.ssid,
                    "user_agent":
                    self.headers["User-Agent"]
                },
                indent=4
            )
        )


        return self.response, settings




    async def _post(self, data):


        self.response = self.send_request(
            method="POST",
            url=f"{self.full_url}/sign-in/",
            data=data
        )


        if not self.response:

            return False, "Sem resposta do servidor"



        required_keep_code = self.get_soup().find(
            "input",
            {
                "name":
                "keep_code"
            }
        )


        if required_keep_code:

            auth_body = self.get_soup().find(
                "main",
                {
                    "class":
                    "auth__body"
                }
            )


            input_message = (
                f'{auth_body.find("p").text}: '
                if auth_body and auth_body.find("p")
                else
                "Informe o PIN enviado para seu e-mail:"
            )


            await self.awaiting_pin(
                data,
                input_message
            )


        await asyncio.sleep(1)


        success = self.success_login()


        return success




    def success_login(self):

        if (
            self.response
            and
            "trade" in self.response.url
        ):

            return True, "Login successful."



        html = self.get_soup()



        match = (
            html.find(
                "div",
                {
                    "class":
                    "hint--danger"
                }
            )
            or
            html.find(
                "div",
                {
                    "class":
                    "input-control-cabinet__hint"
                }
            )
        )



        message = (
            match.text.strip()
            if match
            else
            ""
        )


        return False, (
            f"Login failed. {message}"
        )




    async def __call__(
        self,
        username,
        password,
        user_data_dir=None
    ):


        # limpa sessão antiga quebrada
        try:

            self.api.session.cookies.clear()

        except Exception:

            pass



        token = self.get_token()



        if not token:

            raise LoginFailedError(
                "Não foi possível obter token de login."
            )



        data = {

            "_token":
                token,

            "email":
                username,

            "password":
                password,

            "remember":
                1

        }



        status, msg = await self._post(
            data
        )



        if not status:

            raise LoginFailedError(
                msg
            )



        self.get_profile()



        return status, msg