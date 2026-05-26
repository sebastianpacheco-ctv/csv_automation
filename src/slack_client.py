"""
Slack Client — notificaciones ricas y espera de confirmación ✅
"""
import time
import logging
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

log = logging.getLogger(__name__)
CONFIRM_EMOJI = "white_check_mark"


class SlackClient:
    def __init__(self, token: str, channel: str, channel_id: str = None):
        import ssl, certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        self.client = WebClient(token=token, ssl=ssl_ctx)
        self.channel = channel
        self.channel_id = channel_id or channel
        self.status_callback = None  # se asigna desde main.py
        self._seen_status: set = set()
        log.info(f"Slack listo — canal: #{channel} (ID: {self.channel_id})")

    def _ensure_channel(self):
        try:
            self.client.conversations_create(name=self.channel, is_private=False)
            log.info(f"Canal #{self.channel} creado")
        except SlackApiError as e:
            if e.response["error"] == "name_taken":
                log.info(f"Canal #{self.channel} ya existe")
            else:
                raise

    def notify_new_ticket(self, ticket_key: str, summary: str, ticket_url: str,
                          format1_qty: int, format2_qty: int, format3_qty: int,
                          operator_entity: str, csv_qty: int = 0,
                          multiformat: bool = False, deadline: str = "") -> dict:

        # Mostrar formatos con nombres correctos
        format_lines = []
        if format1_qty > 0:
            format_lines.append(f"• Standard Video (CTV): {int(format1_qty)}")
        if format2_qty > 0:
            format_lines.append(f"• Standard Display (Open Web): {int(format2_qty)}")
        if format3_qty > 0:
            format_lines.append(f"• Formato adicional: {int(format3_qty)}")
        formats_text = "\n".join(format_lines) if format_lines else "• (ver ticket)"

        deadline_text = f"\n*Deadline:* {deadline}" if deadline else ""
        multiformat_warning = (
            "\n⚠️ *Ticket multi-formato* — verifica en Jira antes de confirmar."
        ) if multiformat else ""

        blocks = [
            {"type": "header",
             "text": {"type": "plain_text", "text": f"🎬 Nuevo ticket CSV/COV: {ticket_key}"}},
            {"type": "section",
             "text": {"type": "mrkdwn",
                      "text": f"*{summary}*\n<{ticket_url}|Ver en Jira>"}},
            {"type": "section",
             "text": {"type": "mrkdwn",
                      "text": (f"*Mercado:* {operator_entity}\n"
                               f"{formats_text}"
                               f"{deadline_text}"
                               f"{multiformat_warning}")}},
            {"type": "divider"},
            {"type": "section",
             "text": {"type": "mrkdwn",
                      "text": "Verifica en Jira que el formato es correcto y responde *ok* en este hilo para arrancar. El bot esperara hasta 1 hora."}}
        ]
        r = self.client.chat_postMessage(
            channel=self.channel_id,
            text=f"Nuevo ticket CSV: {ticket_key}",
            blocks=blocks
        )
        return {"channel": r["channel"], "ts": r["ts"]}

    def send_message(self, text: str):
        self.client.chat_postMessage(channel=self.channel_id, text=text)

    def send_thread_message(self, thread_ts: str, text: str) -> str:
        """Postea en el hilo de `thread_ts`. Devuelve el ts del mensaje nuevo."""
        r = self.client.chat_postMessage(
            channel=self.channel_id, thread_ts=thread_ts, text=text
        )
        return r["ts"]

    def wait_for_yes_no(self, thread_ts: str, after_ts: str,
                         timeout: int = 600, poll_interval: int = 10) -> str | None:
        """Espera respuesta yes/no en el hilo `thread_ts`, solo cuenta mensajes
        posteriores a `after_ts` (para distinguir la pregunta de respuestas
        previas).

        Devuelve:
          'yes' si alguien dice si/sí/yes/y/dale/adelante/comprime
          'no'  si alguien dice no/n/cancel/salta/saltar
          None  si timeout.
        """
        deadline = time.time() + timeout
        YES = {"si", "sí", "yes", "y", "dale", "adelante", "comprime", "recomprime"}
        NO = {"no", "n", "cancel", "salta", "saltar", "skip"}
        log.info(f"Esperando yes/no en hilo {thread_ts} (timeout {timeout}s)...")
        while time.time() < deadline:
            try:
                r = self.client.conversations_replies(
                    channel=self.channel_id, ts=thread_ts, limit=50,
                )
                for m in r.get("messages", []):
                    mts = m.get("ts", "")
                    if mts <= after_ts:
                        continue
                    if m.get("bot_id"):
                        continue
                    text = m.get("text", "").strip().lower()
                    words = set(text.split())
                    if words & YES:
                        log.info(f"Respuesta YES en hilo: {text!r}")
                        return "yes"
                    if words & NO:
                        log.info(f"Respuesta NO en hilo: {text!r}")
                        return "no"
            except SlackApiError as e:
                log.warning(f"Error leyendo hilo {thread_ts}: {e}")
            time.sleep(poll_interval)
        log.info(f"Timeout esperando yes/no en hilo {thread_ts}")
        return None

    def wait_for_confirmation(self, channel: str, message_ts: str,
                              timeout: int = 3600, poll_interval: int = 15) -> bool:
        """
        Espera a que alguien responda "ok" en el hilo del mensaje.
        SOLO lee mensajes del canal autorizado (ALLOWED_CHANNEL).
        Aunque el bot sea añadido a otro canal, ignorará cualquier mensaje
        que no venga de ALLOWED_CHANNEL.
        """
        # ── RESTRICCION DE SEGURIDAD ─────────────────────────────────────
        # El bot SOLO procesa mensajes del canal autorizado
        if channel != self.channel_id:
            log.warning(f"Intento de leer canal no autorizado: {channel}. Ignorado.")
            return False
        # ─────────────────────────────────────────────────────────────────

        deadline = time.time() + timeout
        CONFIRM_WORDS = {"ok", "si", "sí", "yes", "dale", "adelante", "procesar", "go"}
        STATUS_WORDS = {"status", "estado", "queue", "cola"}
        log.info(f"Esperando 'ok' en hilo del canal autorizado #{self.channel}...")

        while time.time() < deadline:
            try:
                # Verificar confirmacion en el hilo
                r = self.client.conversations_replies(
                    channel=self.channel_id,
                    ts=message_ts,
                    limit=20
                )
                messages = r.get("messages", [])
                for msg in messages[1:]:
                    text = msg.get("text", "").strip().lower()
                    if any(word in text for word in CONFIRM_WORDS):
                        user = msg.get("user", "alguien")
                        log.info(f"✅ Confirmacion recibida de {user}: \'{text}\'")
                        return True

                # Verificar comando "status" en el canal mientras espera
                if self.status_callback:
                    try:
                        history = self.client.conversations_history(
                            channel=self.channel_id, limit=5
                        )
                        for hmsg in history.get("messages", []):
                            hmsg_text = hmsg.get("text", "").strip().lower()
                            hmsg_ts = hmsg.get("ts", "")
                            hmsg_bot = hmsg.get("bot_id", "")
                            if hmsg_text == "status" and not hmsg_bot and hmsg_ts not in self._seen_status:
                                self._seen_status.add(hmsg_ts)
                                status_text = self.status_callback()
                                self.client.chat_postMessage(
                                    channel=self.channel_id,
                                    text=status_text,
                                    thread_ts=hmsg_ts
                                )
                    except Exception as e:
                        log.warning(f"Error leyendo status durante espera: {e}")

            except SlackApiError as e:
                log.warning(f"Error leyendo hilo: {e}")
            time.sleep(poll_interval)
        return False

    def _get_channel_id(self, channel_name: str) -> str:
        """Obtiene el ID del canal autorizado por nombre."""
        try:
            r = self.client.conversations_list(types="public_channel", limit=200)
            for ch in r.get("channels", []):
                if ch["name"] == channel_name:
                    return ch["id"]
        except SlackApiError:
            pass
        return ""
