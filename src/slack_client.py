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
        self.approval_check = None   # se asigna desde main.py: aprobar/rechazar
                                     # un ticket desde el dashboard (.bot_approval.json)
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
                          multiformat: bool = False, deadline: str = "",
                          plan: dict | None = None) -> dict:

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
        ]

        # Bloque de PLAN si el caller lo provee. Formato:
        #   plan = {"videos": [{canonical, duration_s, bitrate_mbps, estimated_size_mb}, ...]}
        # Un ticket puede tener 1 o varios videos; cada uno se procesa como
        # creative separado.
        if plan and plan.get("videos"):
            videos = plan["videos"]
            n = len(videos)
            header = (f"*🎯 Plan de procesado* ({n} videos → {n} creatives):"
                      if n > 1 else "*🎯 Plan de procesado:*")
            chunks = [header]
            for i, v in enumerate(videos, start=1):
                prefix = f"*Video {i}:* " if n > 1 else ""
                line = prefix + f"`{v.get('canonical','?')}.mp4`"
                meta = []
                if v.get("duration_s") is not None:
                    meta.append(f"{v['duration_s']:.1f}s")
                if v.get("bitrate_mbps") is not None:
                    meta.append(f"{v['bitrate_mbps']} Mbps")
                if v.get("estimated_size_mb") is not None:
                    meta.append(f"~{v['estimated_size_mb']:.1f} MB")
                if meta:
                    line += "  (" + " · ".join(meta) + ")"
                chunks.append(line)
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(chunks)},
            })

        blocks.append({"type": "divider"})
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn",
                                "text": ("Responde en este hilo:\n"
                                         "• `ok` → procesar con el plan\n"
                                         f"• `no` → cancelar este ticket (luego podes reactivarlo con `reactivar {ticket_key}` en el canal)\n"
                                         "Timeout 1 hora.\n"
                                         "(Si supera 150 MB, te preguntare si recomprimo.)")}})

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

    def wait_for_ticket_response(self, channel: str, message_ts: str,
                                  timeout: int = 3600, poll_interval: int = 15
                                  ) -> dict | None:
        """Espera la respuesta del usuario al mensaje inicial del ticket.

        Devuelve:
          {'action': 'ok'}     # procesar
          {'action': 'cancel'} # cancelar
          None                 # timeout

        Patrones que reconoce (case-insensitive):
          'ok' / 'si' / 'sí' / 'yes' / 'dale' / 'adelante' / 'procesar' / 'go'
            -> action='ok'
          'no' / 'cancel' / 'cancelar' / 'salta' / 'saltar' / 'skip'
            -> action='cancel'

        Nota: el bitrate ya NO se acepta como override aqui. El bot SIEMPRE
        usa los parametros default (30 Mbps <=30s / 15 Mbps >30s). El unico
        ajuste posible viene mas tarde si el .mp4 supera 150 MB: en ese caso
        el bot pregunta aparte si recomprime (ver wait_for_yes_no).
        """
        if channel != self.channel_id:
            log.warning(f"Intento de leer canal no autorizado: {channel}. Ignorado.")
            return None

        deadline = time.time() + timeout
        CONFIRM_WORDS = {"ok", "si", "sí", "yes", "dale", "adelante", "procesar", "go"}
        CANCEL_WORDS = {"no", "cancel", "cancelar", "salta", "saltar", "skip"}
        log.info(f"Esperando respuesta en hilo del canal #{self.channel}...")

        while time.time() < deadline:
            # Aprobación desde el dashboard (escribe .bot_approval.json). Se
            # chequea aquí porque durante la espera el bot no procesa el control
            # file. Tiene que ir antes de leer Slack: el dashboard postea con el
            # token del bot y el filtro de bot_id de abajo lo ignoraría.
            if self.approval_check:
                dec = self.approval_check(message_ts)
                if dec == "ok":
                    log.info("Respuesta 'ok' desde el dashboard")
                    return {"action": "ok"}
                if dec in ("no", "cancel"):
                    log.info("Respuesta 'no' desde el dashboard")
                    return {"action": "cancel"}
            try:
                r = self.client.conversations_replies(
                    channel=self.channel_id, ts=message_ts, limit=20,
                )
                for msg in r.get("messages", [])[1:]:
                    if msg.get("bot_id"):
                        continue
                    text = msg.get("text", "").strip().lower()
                    words = set(text.split())
                    if words & CANCEL_WORDS:
                        log.info(f"Respuesta cancel en hilo: {text!r}")
                        return {"action": "cancel"}
                    if words & CONFIRM_WORDS:
                        log.info(f"Respuesta ok en hilo: {text!r}")
                        return {"action": "ok"}

                # status callback: si alguien escribe "status" en el canal,
                # responde con el resumen de la cola en un hilo aparte.
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
        return None

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
