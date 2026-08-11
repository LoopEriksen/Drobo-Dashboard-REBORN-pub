"""
Where alerts go.

Deliberately small. The agent doesn't care how a notification is delivered, so
adding APNs later means adding one class here and nothing else changes.

ntfy is the recommended starting point: free, has an iOS and Android app, and
needs no account -- you pick an unguessable topic name and subscribe to it.
Because anyone who knows the topic name can read it, treat the topic like a
password and don't put anything sensitive in the message text.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .models import SEV_CRITICAL, SEV_WARNING


class Notifier:
    name = "base"

    def send(self, title: str, message: str, severity: str) -> None:
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    name = "console"

    def send(self, title: str, message: str, severity: str) -> None:
        marker = {SEV_CRITICAL: "!!", SEV_WARNING: " !"}.get(severity, "  ")
        print(f"[alert]{marker} {title}: {message}", flush=True)


class NtfyNotifier(Notifier):
    """POST to an ntfy topic. Works with ntfy.sh or a self-hosted server."""

    name = "ntfy"

    # ntfy priorities: 1 min .. 5 max
    _PRIORITY = {SEV_CRITICAL: "5", SEV_WARNING: "4"}
    _TAGS = {SEV_CRITICAL: "rotating_light", SEV_WARNING: "warning"}

    def __init__(self, server: str, topic: str):
        self.server = (server or "https://ntfy.sh").rstrip("/")
        self.topic = topic

    def send(self, title: str, message: str, severity: str) -> None:
        if not self.topic:
            return
        req = urllib.request.Request(
            f"{self.server}/{self.topic}",
            data=message.encode("utf-8"),
            method="POST",
            headers={
                "Title": title,
                "Priority": self._PRIORITY.get(severity, "3"),
                "Tags": self._TAGS.get(severity, "floppy_disk"),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except (urllib.error.URLError, OSError) as exc:
            # Never let a failed notification take the agent down -- the whole
            # point is that it keeps watching the array.
            print(f"[notify] ntfy send failed: {exc}", flush=True)


class EmailNotifier(Notifier):
    """
    Send an alert by email, over SMTP.

    The original Drobo Dashboard could email you when a drive failed, and this
    is the replacement for it. Email earns its place next to the ntfy push for
    a boring reason: it is the channel that still works in five years when the
    app on your phone has stopped being installable, which on this project is
    not a hypothetical -- it is exactly what happened to DroboPix.

    WHY THE AGENT SENDS IT, NOT THE DROBO. The device has its own mail settings
    (the firmware carries a DRINasEmailAlertConfig section and an AlertMailer),
    but making the Drobo send them would mean writing configuration to it, and
    eCmdSetConfig is in DO_NOT_SEND and staying there. Sending from the agent
    also means alerts still arrive when the Drobo is the thing that is broken --
    a Drobo that cannot be reached obviously cannot email you about it, which is
    the single alert you would most want to receive.

    CREDENTIALS. The password comes from config.json, which is gitignored, and
    it never appears in a log line, an error message, or the API. Delivery
    failures are reported with the server and the error type but never the
    exception's full text, because a rejected SMTP login habitually echoes the
    username back and sometimes more.
    """

    name = "email"

    #: SMTP is slow and this runs on the alerting path. Bounded so a mail
    #: server that has gone away cannot hold up a poll cycle.
    TIMEOUT = 15.0

    def __init__(self, host: str, port: int, username: str, password: str,
                 sender: str, recipients: list[str], use_tls: bool = True):
        self.host = host
        self.port = port
        self.username = username
        self._password = password        # never logged, never returned
        self.sender = sender or username
        self.recipients = recipients
        self.use_tls = use_tls

    def send(self, title: str, message: str, severity: str) -> None:
        # Imported here rather than at module scope so that a config with no
        # email backend never even loads smtplib -- which is also what makes
        # the "contacts nothing when off" test provable by boobytrapping it.
        import smtplib
        from email.message import EmailMessage

        mail = EmailMessage()
        mail["Subject"] = title
        mail["From"] = self.sender
        mail["To"] = ", ".join(self.recipients)
        mail.set_content(
            f"{message}\n\n"
            f"-- \n"
            f"Sent by Drobo Dashboard REBORN, the agent watching your Drobo.\n"
            f"Severity: {severity}\n")

        try:
            if self.use_tls:
                with smtplib.SMTP(self.host, self.port, timeout=self.TIMEOUT) as smtp:
                    smtp.starttls()
                    if self.username:
                        smtp.login(self.username, self._password)
                    smtp.send_message(mail)
            else:
                with smtplib.SMTP(self.host, self.port, timeout=self.TIMEOUT) as smtp:
                    if self.username:
                        smtp.login(self.username, self._password)
                    smtp.send_message(mail)
        except Exception as exc:
            # Deliberately NOT str(exc): a rejected SMTP login commonly echoes
            # the credentials back in the server's response, and this string
            # goes to stdout and into the agent's log.
            raise RuntimeError(
                f"could not send mail via {self.host}:{self.port} "
                f"({type(exc).__name__})") from None


class NotifierGroup(Notifier):
    name = "group"

    def __init__(self, members: list[Notifier]):
        self.members = members

    def send(self, title: str, message: str, severity: str) -> None:
        for member in self.members:
            try:
                member.send(title, message, severity)
            except Exception as exc:  # noqa: BLE001 - a broken backend must not stop the others
                print(f"[notify] backend {member.name} failed: {exc}", flush=True)


def build(cfg: dict) -> Notifier:
    members: list[Notifier] = []
    for backend in cfg.get("backends", ["console"]):
        if backend == "console":
            members.append(ConsoleNotifier())
        elif backend == "ntfy":
            ntfy = cfg.get("ntfy", {})
            if not ntfy.get("topic"):
                print("[notify] ntfy is enabled but no topic is set -- skipping", flush=True)
                continue
            members.append(NtfyNotifier(ntfy.get("server", ""), ntfy["topic"]))
        elif backend == "email":
            email = cfg.get("email", {})
            # Every one of these is required, and a partial configuration is
            # reported rather than half-used: an alert that silently fails to
            # send is worse than one that was never configured, because you
            # believe you are covered.
            missing = [k for k in ("host", "to") if not email.get(k)]
            if missing:
                print(f"[notify] email is enabled but {', '.join(missing)} "
                      f"{'is' if len(missing) == 1 else 'are'} not set -- skipping",
                      flush=True)
                continue
            recipients = email["to"]
            if isinstance(recipients, str):
                recipients = [r.strip() for r in recipients.split(",") if r.strip()]
            members.append(EmailNotifier(
                host=email["host"],
                port=int(email.get("port", 587)),
                username=email.get("username", ""),
                password=email.get("password", ""),
                sender=email.get("from", ""),
                recipients=recipients,
                use_tls=bool(email.get("use_tls", True)),
            ))
        else:
            print(f"[notify] unknown backend {backend!r} -- ignoring", flush=True)
    if not members:
        members.append(ConsoleNotifier())
    return NotifierGroup(members)
