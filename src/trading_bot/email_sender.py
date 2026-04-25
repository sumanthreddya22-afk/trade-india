import smtplib
import time
from email.message import EmailMessage


class EmailSender:
    """Gmail SMTP_SSL sender with bounded retries."""

    def __init__(
        self,
        user: str,
        app_password: str,
        to: str,
        host: str = "smtp.gmail.com",
        port: int = 465,
        retries: int = 3,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        self._user = user
        self._password = app_password
        self._to = to
        self._host = host
        self._port = port
        self._retries = retries
        self._backoff = retry_backoff_seconds

    def send(self, *, subject: str, html_body: str, text_body: str | None = None) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self._user
        msg["To"] = self._to
        msg.set_content(text_body or "View this email in HTML.")
        msg.add_alternative(html_body, subtype="html")

        last_exc: Exception | None = None
        for attempt in range(1, self._retries + 1):
            try:
                with smtplib.SMTP_SSL(self._host, self._port) as smtp:
                    smtp.login(self._user, self._password)
                    smtp.sendmail(self._user, [self._to], msg.as_string())
                return
            except smtplib.SMTPException as e:
                last_exc = e
                if attempt < self._retries:
                    time.sleep(self._backoff * attempt)
        assert last_exc is not None
        raise last_exc
