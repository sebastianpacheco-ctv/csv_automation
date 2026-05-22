"""
CSV Ticket Automation — Seedtag CTV Team
==========================================
Detecta tickets Standard Video (CSV/COV) en la cola 1597,
convierte el video y lo sube a Studio Seedtag.

Flujo:
  1. Polling cola Jira 1597 cada 60s
  2. Notificacion en Slack #csv-tickets
  3. Esperar "ok" en el hilo
  4. Cambiar estado a Building
  5. Descargar video (adjunto o link)
  6. Convertir con FFmpeg
  7. Subir a Studio Seedtag
  8. Comentar en Jira con specs + link Studio
  9. Resumen en Slack (Done lo pone el usuario tras revisar)
"""

import os
import re
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

from jira_client import JiraClient
from slack_client import SlackClient
from converter import VideoConverter
from uploader import FilestageUploader
from studio_api import StudioAPIClient, StudioVideoNotReadyError, StudioJWTExpiredError

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(Path(os.getenv("LOGS_DIR", "./logs")) / "automation.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

POLL_INTERVAL = 60  # segundos entre chequeos

# ID del nuevo formulario dedicado para CSV/COV
CSV_REQUEST_TYPE_ID = "1916"


# ── Deteccion de tickets CSV/COV ──────────────────────────────────────────────

def _extract_text(node) -> str:
    """Extrae texto plano de un nodo ADF de Jira recursivamente."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        return " ".join(_extract_text(c) for c in node.get("content", []))
    return ""


def _matches_keywords(text: str) -> bool:
    """
    Detecta keywords CSV/COV con regex para evitar falsos positivos
    (coverage, recover, Vancouver, etc.)
    """
    text = text.lower()
    for kw in ["standard video", "csv-ctv", "cov-ctv"]:
        if kw in text:
            return True
    for kw in ["csv", "cov", "pcov"]:
        if re.search(r"(?<![a-z])" + kw + r"(?![a-z])", text):
            return True
    return False


def is_csv_ticket(issue: dict) -> bool:
    """
    Detecta si el ticket requiere conversion Standard Video (CSV/COV).

    Criterio 1: viene del formulario 1916 (nuevo form dedicado CSV/COV)
    Criterio 2: keywords CSV/COV en titulo o campos del formulario (legacy)

    NO busca en comentarios — son conversacion interna del equipo.
    """
    fields = issue.get("fields", {})

    # Criterio 1: nuevo formulario 1916
    rt = (fields.get("customfield_10800") or {})
    if isinstance(rt, dict):
        if (rt.get("requestType") or {}).get("id", "") == CSV_REQUEST_TYPE_ID:
            return True

    # Criterio 2: keywords en campos del formulario (tickets legacy)
    texts = [
        fields.get("summary") or "",
        _extract_text(fields.get("description") or {}),
    ]
    for key, val in fields.items():
        if not key.startswith("customfield_"):
            continue
        if isinstance(val, str):
            texts.append(val)
        elif isinstance(val, dict):
            texts.append(_extract_text(val))

    return _matches_keywords(" | ".join(texts))


def is_multiformat_ticket(issue: dict) -> bool:
    """True si el ticket pide mas de un tipo de formato."""
    fields = issue.get("fields", {})
    qtys = [
        float(fields.get("customfield_15865") or 0),  # Standard Video (CTV)
        float(fields.get("customfield_15866") or 0),  # Standard Display (Open Web)
        float(fields.get("customfield_15867") or 0),  # Formato adicional
    ]
    return sum(1 for q in qtys if q > 0) > 1


def get_csv_quantity(issue: dict) -> int:
    """Total de unidades pedidas en el ticket."""
    return int(float(issue.get("fields", {}).get("customfield_15827") or 0))


def get_queue_status(jira: JiraClient) -> str:
    """Consulta la cola 1597 y devuelve resumen para Slack."""
    try:
        issues = jira.get_omniscreen_video_issues()
    except Exception as e:
        return f"⚠️ Error consultando la cola: {e}"

    if not issues:
        return "✅ *Cola CSV/COV vacía* — no hay tickets pendientes."

    lines = [f"📋 *Cola CSV/COV — {len(issues)} ticket(s)*\n"]
    for issue in issues:
        fields = issue.get("fields", {})
        key = issue.get("key", "?")
        summary = (fields.get("summary") or "")[:50]
        status = fields.get("status", {}).get("name", "?")
        entity = (fields.get("customfield_14324") or {}).get("value", "?")
        f1 = int(float(fields.get("customfield_15865") or 0))
        f2 = int(float(fields.get("customfield_15866") or 0))
        deadline_raw = fields.get("customfield_11300") or fields.get("duedate") or ""
        deadline = _format_deadline(deadline_raw)

        format_parts = []
        if f1 > 0:
            format_parts.append(f"Standard Video (CTV): {f1}")
        if f2 > 0:
            format_parts.append(f"Standard Display (Open Web): {f2}")
        format_str = " | ".join(format_parts) or "ver ticket"

        lines.append(
            f"• *<https://seedtag.atlassian.net/browse/{key}|{key}>* — {summary}\n"
            f"  {entity} | {format_str}\n"
            f"  Deadline: {deadline} | Estado: {status}"
        )
    return "\n\n".join(lines)


def _format_deadline(deadline_raw: str) -> str:
    """Convierte el deadline de Jira a formato legible en UTC."""
    if not deadline_raw:
        return "—"
    if "T" in deadline_raw:
        try:
            dt = datetime.fromisoformat(deadline_raw.replace(".000", ""))
            return dt.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
        except Exception:
            pass
    return deadline_raw[:10]


# ── Proceso de un ticket ──────────────────────────────────────────────────────

def process_ticket(issue: dict, jira: JiraClient, slack: SlackClient,
                   converter: VideoConverter, studio: StudioAPIClient,
                   filestage: FilestageUploader):

    ticket_key = issue["key"]
    ticket_url = f"{os.getenv('JIRA_BASE_URL')}/browse/{ticket_key}"
    fields = issue["fields"]
    summary = fields.get("summary", "Sin titulo")

    entity_field = fields.get("customfield_14324") or {}
    operator_entity = entity_field.get("value", "US") if isinstance(entity_field, dict) else "US"

    f1 = int(float(fields.get("customfield_15865") or 0))  # Standard Video (CTV)
    f2 = int(float(fields.get("customfield_15866") or 0))  # Standard Display (Open Web)
    f3 = int(float(fields.get("customfield_15867") or 0))  # Formato adicional

    log.info(f"Procesando {ticket_key}: {summary} | {operator_entity} | "
             f"CTV:{f1} OW:{f2} F3:{f3} | Multi:{is_multiformat_ticket(issue)}")

    # ── 1. Notificar en Slack ──────────────────────────────────────────────
    msg = slack.notify_new_ticket(
        ticket_key=ticket_key,
        summary=summary,
        ticket_url=ticket_url,
        format1_qty=f1,
        format2_qty=f2,
        format3_qty=f3,
        operator_entity=operator_entity,
        csv_qty=get_csv_quantity(issue),
        multiformat=is_multiformat_ticket(issue),
        deadline=_format_deadline(fields.get("customfield_11300") or fields.get("duedate") or ""),
    )
    log.info(f"Slack notificado — ts: {msg['ts']}")

    # ── 2. Esperar "ok" en el hilo ────────────────────────────────────────
    confirmed = slack.wait_for_confirmation(msg["channel"], msg["ts"], timeout=3600)
    if not confirmed:
        slack.send_message(f"⏰ *{ticket_key}* no confirmado en 1 hora. Saltado.")
        return

    slack.send_message(f"✅ Confirmado. Procesando *{ticket_key}*...")

    # ── 3. Cambiar estado a Building ──────────────────────────────────────
    try:
        transitions = jira.get_transitions(ticket_key)
        if "Start Building" in transitions:
            # customfield_15826 = Seedtag Specs — requerido para la transicion
            jira.transition(ticket_key, transitions["Start Building"],
                            fields={"customfield_15826": {"id": "27743"}})
            log.info(f"{ticket_key} → Building")
    except Exception as e:
        log.warning(f"No se pudo cambiar estado a Building: {e}")

    # ── 4-8. Proceso (revert a To Build si falla) ─────────────────────────
    try:
        # ── 4. Descargar video ────────────────────────────────────────────
        tmp_dir = Path(os.getenv("TMP_DIR", "./tmp")) / ticket_key
        tmp_dir.mkdir(parents=True, exist_ok=True)

        input_path = jira.download_video(issue, tmp_dir)
        if not input_path:
            transfer_url = jira.find_transfer_link(issue)
            if transfer_url:
                slack.send_message(
                    f"⚠️ *{ticket_key}*: No pude descargar el video automaticamente.\n"
                    f"Descargalo y adjuntalo al ticket: {transfer_url}"
                )
            else:
                slack.send_message(f"❌ *{ticket_key}*: No se encontro video adjunto ni link.")
            raise RuntimeError("No se encontro video")

        slack.send_message(f"📥 Descargado: `{input_path.name}` — convirtiendo...")

        # ── 5. Convertir ──────────────────────────────────────────────────
        output_path = converter.convert(input_path)
        if not output_path:
            raise RuntimeError("Error en conversion FFmpeg")

        size_mb = output_path.stat().st_size / (1024 * 1024)
        slack.send_message(
            f"🎬 Convertido: `{output_path.name}` — {size_mb:.1f} MB ({converter.last_bitrate} Mbps)"
        )

        # ── 6. Filestage ──────────────────────────────────────────────────
        filestage_url = None
        filestage_line = ""
        try:
            filestage_url = filestage.upload(output_path, summary, operator_entity)
            if filestage_url:
                filestage_line = f"\n• Filestage: {filestage_url}"
                slack.send_message(f"📋 Filestage ✓")
        except Exception as fe:
            log.warning(f"Filestage fallo: {fe}")
            slack.send_message(f"⚠️ Filestage error — continuo con Studio")

        # ── 7. Studio Seedtag (via GraphQL API) ───────────────────────────
        studio_url = None
        try:
            industry = (fields.get("customfield_15831") or {}).get("value", "")
            country = studio.map_country(operator_entity)
            category = studio.map_category(industry)
            result = studio.process_video_to_creative(
                file_path=output_path,
                ticket_title=summary,  # name del creative = summary del ticket
                country=country,
                category=category,
            )
            studio_url = result["preview_url"]
            slack.send_message(
                f"🎯 Studio ✓ — <{studio_url}|Preview> · "
                f"video_id=`{result['video_id']}`"
            )
        except StudioVideoNotReadyError as nre:
            # El vídeo se subió pero el procesado tarda más de 90s.
            # Avisar en Slack y dejar que un humano cree el creative manualmente.
            slack.send_message(
                f"⚠️ *{ticket_key}* — Studio sigue procesando el vídeo tras "
                f"{nre.elapsed_seconds}s (último estado: `{nre.last_state}`).\n"
                f"video_id: `{nre.video_id}`\n"
                f"Cuando aparezca el ✓ verde en Studio, crea el creative manualmente."
            )
        except StudioJWTExpiredError as jwt_err:
            log.error(f"Studio JWT expirado: {jwt_err}")
            slack.send_message(
                f"🚨 *Studio — JWT caducado* (HTTP {jwt_err.status_code})\n"
                f"Extrae un JWT nuevo de design_automations@seedtag.com en "
                f"DevTools → Application → Cookies → seedtag_jwt, actualiza "
                f"STUDIO_JWT_COOKIE (o el sidecar `.studio_jwt`) y reinicia el bot.\n"
                f"Mientras tanto sube `{output_path.name}` manualmente al ticket {ticket_key}."
            )
        except Exception as e:
            log.error(f"Studio fallo: {e}")
            slack.send_message(
                f"⚠️ *Studio error* — sube `{output_path.name}` manualmente al ticket {ticket_key}."
            )

        # ── 7. Comentar en Jira ───────────────────────────────────────────
        studio_line = f"\n• Preview Studio: {studio_url}" if studio_url else ""
        jira.add_comment(ticket_key,
            f"✅ Standard Video (CSV/COV) convertido automaticamente.\n\n"
            f"Especificaciones:\n"
            f"• Codec: H.264 | Resolucion: 1920x1080 | FPS: 29.97\n"
            f"• Bitrate: {converter.last_bitrate} Mbps | Tamano: {size_mb:.1f} MB\n"
            f"• Audio: AAC 256kbps 48kHz Stereo | Loudness: -24 LUFS"
            f"{filestage_line}"
            f"{studio_line}"
        )

        # ── 8. Resumen en Slack ───────────────────────────────────────────
        # El usuario cambia el estado a Done tras revisar el resultado
        slack.send_message(
            f"✅ *{ticket_key}* listo para revision:\n"
            f"• Archivo: `{output_path.name}`\n"
            f"• {size_mb:.1f} MB | {converter.last_bitrate} Mbps\n"
            f"• Studio: {'✓' if studio_url else '⚠️ subir manualmente'}\n"
            f"• <{ticket_url}|Ver en Jira> — marca como Done cuando lo revises"
        )
        log.info(f"✅ {ticket_key} procesado — pendiente revision del equipo")

    except Exception as e:
        log.error(f"Error procesando {ticket_key}: {e}", exc_info=True)
        slack.send_message(f"❌ *{ticket_key}* fallo durante el proceso. Revirtiendo a To Build...")
        try:
            revert = jira.get_transitions(ticket_key)
            for rname in ["Stop Building", "Reopen", "Back to To Build", "To Build"]:
                if rname in revert:
                    jira.transition(ticket_key, revert[rname])
                    log.info(f"{ticket_key} → revertido a {rname}")
                    break
            else:
                log.warning(f"Sin transicion de revert. Disponibles: {list(revert.keys())}")
        except Exception as re_err:
            log.error(f"Error revirtiendo estado: {re_err}")


# ── Loop principal ────────────────────────────────────────────────────────────

def main():
    log.info("🚀 CSV Ticket Automation arrancando...")

    jira = JiraClient(
        base_url=os.getenv("JIRA_BASE_URL"),
        email=os.getenv("JIRA_EMAIL"),
        api_token=os.getenv("JIRA_API_TOKEN"),
        project_key=os.getenv("JIRA_PROJECT_KEY"),
    )
    slack = SlackClient(
        token=os.getenv("SLACK_BOT_TOKEN"),
        channel=os.getenv("SLACK_CHANNEL", "csv-tickets"),
        channel_id=os.getenv("SLACK_CHANNEL_ID"),
    )
    converter = VideoConverter(
        tmp_dir=os.getenv("TMP_DIR", "./tmp"),
        bitrate_short=int(os.getenv("BITRATE_SHORT", 30)),
        bitrate_long=int(os.getenv("BITRATE_LONG", 15)),
        duration_threshold=int(os.getenv("DURATION_THRESHOLD", 30)),
    )
    studio = StudioAPIClient(
        jwt_cookie=os.getenv("STUDIO_JWT_COOKIE"),
        sidecar_path=Path(os.getenv("TMP_DIR", "./tmp")) / ".studio_jwt",
    )
    filestage = FilestageUploader(
        session_cookie=os.getenv("FILESTAGE_SESSION_COOKIE"),
        api_key=os.getenv("FILESTAGE_API_KEY"),
        email=os.getenv("STUDIO_EMAIL"),
        password=os.getenv("STUDIO_PASSWORD"),
    )

    # Asignar callback de status para responder durante la espera del ok
    slack.status_callback = lambda: get_queue_status(jira)

    seen_tickets: set[str] = set()
    seen_status: set[str] = set()
    last_studio_heartbeat: float = 0.0
    HEARTBEAT_INTERVAL = 24 * 3600
    log.info(f"Polling Jira cada {POLL_INTERVAL}s...")

    while True:
        try:
            if time.monotonic() - last_studio_heartbeat >= HEARTBEAT_INTERVAL:
                try:
                    studio.heartbeat()
                except StudioJWTExpiredError as jwt_err:
                    slack.send_message(
                        f"🚨 *Studio — JWT caducado en heartbeat* (HTTP {jwt_err.status_code}).\n"
                        f"Extrae uno nuevo y actualiza STUDIO_JWT_COOKIE o el sidecar `.studio_jwt`."
                    )
                last_studio_heartbeat = time.monotonic()

            # Detectar comando "status" en el canal
            try:
                history = slack.client.conversations_history(
                    channel=slack.channel_id, limit=10
                )
                for msg in history.get("messages", []):
                    text = msg.get("text", "").strip().lower()
                    ts = msg.get("ts", "")
                    if text == "status" and not msg.get("bot_id") and ts not in seen_status:
                        seen_status.add(ts)
                        log.info("Comando 'status' recibido")
                        slack.client.chat_postMessage(
                            channel=slack.channel_id,
                            text=get_queue_status(jira),
                            thread_ts=ts
                        )
            except Exception as e:
                log.warning(f"Error leyendo canal para status: {e}")

            # Polling Jira
            issues = jira.get_omniscreen_video_issues()
            new = [i for i in issues if i["key"] not in seen_tickets and is_csv_ticket(i)]
            for issue in new:
                seen_tickets.add(issue["key"])
                process_ticket(issue, jira, slack, converter, studio, filestage)

        except Exception as e:
            log.error(f"Error en loop: {e}", exc_info=True)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
