from unittest.mock import MagicMock, patch

import pytest

from trading_bot.email_sender import EmailSender


def test_send_uses_smtp_ssl():
    with patch("trading_bot.email_sender.smtplib.SMTP_SSL") as MockSMTP:
        instance = MockSMTP.return_value.__enter__.return_value
        sender = EmailSender(user="from@x.com", app_password="p", to="to@y.com")
        sender.send(subject="Hello", html_body="<b>hi</b>")
        instance.login.assert_called_once_with("from@x.com", "p")
        assert instance.sendmail.called
        args = instance.sendmail.call_args[0]
        assert args[0] == "from@x.com"
        assert args[1] == ["to@y.com"]
        assert "Subject: Hello" in args[2]
        assert "<b>hi</b>" in args[2]


def test_send_retries_on_smtp_error():
    with patch("trading_bot.email_sender.smtplib.SMTP_SSL") as MockSMTP:
        with patch("trading_bot.email_sender.time.sleep"):
            import smtplib

            MockSMTP.return_value.__enter__.side_effect = [
                smtplib.SMTPException("nope"),
                smtplib.SMTPException("nope2"),
                MockSMTP.return_value.__enter__.return_value,
            ]
            sender = EmailSender(user="from@x.com", app_password="p", to="to@y.com", retries=3)
            sender.send(subject="Hello", html_body="<b>hi</b>")
            assert MockSMTP.call_count == 3


def test_send_raises_after_max_retries():
    with patch("trading_bot.email_sender.smtplib.SMTP_SSL") as MockSMTP:
        with patch("trading_bot.email_sender.time.sleep"):
            import smtplib

            MockSMTP.return_value.__enter__.side_effect = smtplib.SMTPException("nope")
            sender = EmailSender(user="from@x.com", app_password="p", to="to@y.com", retries=2)
            with pytest.raises(smtplib.SMTPException):
                sender.send(subject="Hello", html_body="<b>hi</b>")
            assert MockSMTP.call_count == 2
