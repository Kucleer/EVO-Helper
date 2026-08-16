"""One-shot SMTP delivery for persisted in-game security alerts."""

from __future__ import annotations

import smtplib
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage

from evo_helper.config import Settings
from evo_helper.domain.records import PlanetScoutAlert


@dataclass(frozen=True)
class AlertEmailDelivery:
    status: str
    error: str | None = None
    delivered_at_utc: datetime | None = None


def format_planet_scout_alert(alert: PlanetScoutAlert) -> str:
    """Plain-text message that can be safely delivered by all SMTP providers."""
    probe_count = (
        "未读到拦截数量"
        if alert.intercepted_probes is None
        else f"已拦截 {alert.intercepted_probes} 个敌方侦察探测器"
    )
    source_name = f"（{alert.source_name}）" if alert.source_name else ""
    return "\n".join(
        (
            "EVO 安全告警：你的行星被侦察",
            "",
            f"游戏邮件时间：{alert.raw_time_text}",
            f"侦察来源：[{alert.source}]{source_name}",
            f"被侦察行星：[{alert.target}]",
            f"防御结果：{probe_count}",
            "",
            "该邮件已作为新告警记录；同一封游戏邮件不会重复推送。",
        )
    )


def deliver_planet_scout_alert(settings: Settings, alert: PlanetScoutAlert) -> AlertEmailDelivery:
    """Attempt exactly one SMTP send; configuration absence is a recorded outcome."""
    if not settings.smtp_ready:
        return AlertEmailDelivery("NOT_CONFIGURED", "SMTP 邮件配置不完整")

    # `smtp_ready` checks these together. Bind the narrowed values once so
    # neither the SMTP library nor a future refactor can accidentally receive
    # an optional secret/configuration value.
    host = settings.smtp_host
    username = settings.smtp_username
    password = settings.smtp_password
    recipient = settings.alert_email_to
    assert host is not None
    assert username is not None
    assert password is not None
    assert recipient is not None

    message = EmailMessage()
    message["From"] = settings.smtp_from or username
    message["To"] = recipient
    message["Subject"] = f"[EVO 安全告警] 行星 {alert.target} 被侦察"
    message.set_content(format_planet_scout_alert(alert))
    try:
        if settings.smtp_use_ssl:
            with smtplib.SMTP_SSL(host, settings.smtp_port, timeout=20) as client:
                client.login(username, password)
                client.send_message(message)
        else:
            with smtplib.SMTP(host, settings.smtp_port, timeout=20) as client:
                client.starttls()
                client.login(username, password)
                client.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        return AlertEmailDelivery("FAILED", str(error)[:1000])
    return AlertEmailDelivery("SENT", delivered_at_utc=datetime.now(UTC))
