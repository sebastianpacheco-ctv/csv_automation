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
import json
import time
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

from jira_client import JiraClient
from slack_client import SlackClient
from converter import VideoConverter
from uploader import FilestageUploader
from studio_api import StudioAPIClient, StudioVideoNotReadyError, StudioJWTExpiredError

load_dotenv()

_logs_dir = Path(os.getenv("LOGS_DIR", "./logs"))
_logs_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(
            _logs_dir / "automation.log",
            maxBytes=10 * 1024 * 1024,  # 10 MB por archivo
            backupCount=5,               # 5 archivos rotados -> 60 MB max
            encoding="utf-8",
        ),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

POLL_INTERVAL = 60  # segundos entre chequeos

# ID del nuevo formulario dedicado para CSV/COV
CSV_REQUEST_TYPE_ID = "1916"

# Limite que el equipo CTV se autoimpone para adjuntos en Jira: 150 MB.
# Por encima de eso, el bot NO intenta adjuntar y avisa en Slack para que
# el equipo decida (subirlo manualmente al ticket, dejarlo solo en Studio, etc).
MAX_JIRA_ATTACH_MB = 150


def _load_seen_tickets(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text()))
    except Exception as e:
        log.warning(f"No se pudo leer {path}: {e}; arrancando con set vacio")
        return set()


def _save_seen_tickets(path: Path, seen: set[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(seen)))
    except Exception as e:
        log.warning(f"No se pudo escribir {path}: {e}")


def _load_canceled(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text()))
    except Exception as e:
        log.warning(f"No se pudo leer {path}: {e}")
        return set()


def _save_canceled(path: Path, items: set[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(items)))
    except Exception as e:
        log.warning(f"No se pudo escribir {path}: {e}")


def _load_pending_studio(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception as e:
        log.warning(f"No se pudo leer {path}: {e}")
        return []


def _save_pending_studio(path: Path, items: list[dict]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(items, indent=2))
    except Exception as e:
        log.warning(f"No se pudo escribir {path}: {e}")


def _add_pending_studio(path: Path, entry: dict) -> None:
    """Anyade (o reemplaza por ticket_key) una entrada pendiente."""
    items = _load_pending_studio(path)
    items = [i for i in items if i.get("ticket_key") != entry["ticket_key"]]
    items.append(entry)
    _save_pending_studio(path, items)


def _process_pending_studio(studio, jira, slack, pending_path: Path,
                             max_age_hours: float = 2.0) -> None:
    """Segunda pasada: para cada entry pendiente, consulta el estado del video
    en Studio. Si COMPLETED -> crea el creative y postea segundo comentario
    en Jira con el link. Si ERROR/FAILED o lleva mas de `max_age_hours` ->
    quita del pending y avisa en Slack.
    """
    items = _load_pending_studio(pending_path)
    if not items:
        return
    now = datetime.now(timezone.utc)
    remaining = []
    for it in items:
        try:
            created = datetime.fromisoformat(it["created_at"])
            age_h = (now - created).total_seconds() / 3600
            if age_h > max_age_hours:
                slack.send_message(
                    f"⚠️ *{it['ticket_key']}*: Studio sigue PROGRESSING tras "
                    f"{age_h:.1f}h. Quito del pending. video_id=`{it['video_id']}`. "
                    f"Crea el creative manualmente cuando Studio termine."
                )
                log.warning(f"{it['ticket_key']}: pending Studio expirado tras {age_h:.1f}h")
                continue

            video = studio.get_video(it["video_id"])
            state = video.get("state", "?")
            formats = video.get("formats") or []

            if state in ("ERROR", "FAILED"):
                slack.send_message(
                    f"❌ *{it['ticket_key']}*: Studio devolvio state=`{state}` para "
                    f"video_id=`{it['video_id']}`. Quito del pending."
                )
                log.error(f"{it['ticket_key']}: Studio terminó en {state}")
                continue

            if state != "COMPLETED":
                remaining.append(it)
                continue

            # state == COMPLETED -> crear creative
            log.info(f"{it['ticket_key']}: Studio COMPLETED, creando creative...")
            ad_template = studio.build_csv_ctv_ad_template(
                video_id=it["video_id"],
                name=it["summary"],
                formats=formats,
                country=it.get("country"),
                category=it.get("category"),
            )
            creative_id = studio.create_cov_creative(ad_template)
            preview_url = studio.get_preview_link(creative_id)
            log.info(f"{it['ticket_key']}: creative tardio creado id={creative_id}")

            # Postear segundo comentario en Jira con el link
            try:
                jira.add_comment_adf(it["ticket_key"], {
                    "type": "doc", "version": 1,
                    "content": [
                        {"type": "paragraph", "content": [
                            {"type": "text",
                             "text": "🎯 Studio termino el procesado del video. Creative creado."},
                        ]},
                        {"type": "paragraph", "content": [
                            {"type": "text", "text": "Preview Studio: "},
                            {"type": "text", "text": preview_url,
                             "marks": [{"type": "link", "attrs": {"href": preview_url}}]},
                        ]},
                    ],
                })
            except Exception as ce:
                log.warning(f"{it['ticket_key']}: no se pudo añadir comentario tardio: {ce}")

            slack.send_message(
                f"🎯 *{it['ticket_key']}*: Studio COMPLETED, creative creado.\n"
                f"<{preview_url}|Preview> · creative_id=`{creative_id}`"
            )
            # No añadir a remaining -> se elimina del pending

        except StudioJWTExpiredError as jwt_err:
            slack.send_message(
                f"🚨 *Studio JWT caducado* en chequeo de tickets pending "
                f"(HTTP {jwt_err.status_code}). Refresca el JWT."
            )
            remaining.append(it)  # reintentar cuando vuelva el JWT
            # No tiene sentido seguir con el resto: todos fallarian igual.
            remaining.extend(items[items.index(it) + 1:])
            break
        except Exception as e:
            log.warning(f"{it.get('ticket_key', '?')}: error revisando pending Studio: {e}")
            remaining.append(it)  # reintentar en siguiente iter

    if len(remaining) != len(items):
        _save_pending_studio(pending_path, remaining)
        log.info(f"Pending Studio: {len(items)} -> {len(remaining)}")


def _build_canonical_filename(summary: str) -> str:
    """Construye el nombre canonico para el .mp4 procesado.

    Reglas (acordadas con el equipo CTV):
    - Base = summary del ticket, sanitizado (uppercase, [A-Z0-9_-] solo, sin
      espacios ni acentos, sin doble underscore consecutivo).
    - Si el summary YA contiene 'CTV_CSV', usar tal cual.
    - Si NO lo contiene, añadir '_CTV_CSV' al final.

    Este nombre se usa para: el archivo .mp4 convertido, el filename del
    upload a Studio, el nombre con que se adjunta a Jira. Los tres coinciden.
    """
    sanitized = StudioAPIClient._sanitize_video_filename(summary)
    if "CTV_CSV" in sanitized:
        return sanitized
    return f"{sanitized}_CTV_CSV"


def _build_done_comment_multi(results: list[dict], total: int) -> dict:
    """Comentario ADF agregando N videos procesados. Cada result trae:
    {canonical, studio_url, video_id, creative_id, attached_filename,
     attach_skip_reason, studio_error}.
    """
    content = []
    if total > 1:
        content.append({"type": "paragraph", "content": [
            {"type": "text", "text": f"✅ {total} videos CSV/COV convertidos y procesados."},
        ]})
    else:
        content.append({"type": "paragraph", "content": [
            {"type": "text", "text": "✅ Video CSV/COV convertido y procesado."},
        ]})

    for i, r in enumerate(results, start=1):
        label = f"Video {i} — " if total > 1 else ""
        # Linea de Studio
        if r.get("studio_url"):
            content.append({"type": "paragraph", "content": [
                {"type": "text", "text": f"{label}Preview Studio: "},
                {"type": "text", "text": r["studio_url"],
                 "marks": [{"type": "link", "attrs": {"href": r["studio_url"]}}]},
            ]})
        elif r.get("studio_error"):
            content.append({"type": "paragraph", "content": [
                {"type": "text", "text": f"{label}⚠️ Studio no termino ("},
                {"type": "text", "text": r["studio_error"], "marks": [{"type": "code"}]},
                {"type": "text", "text": "). Cuando el video este COMPLETED el bot crea el creative, o hacelo manual."},
            ]})
        # Linea de adjunto
        if r.get("attached_filename"):
            content.append({"type": "paragraph", "content": [
                {"type": "text", "text": f"{label}Archivo adjuntado: "},
                {"type": "text", "text": r["attached_filename"], "marks": [{"type": "code"}]},
            ]})
        elif r.get("attach_skip_reason"):
            content.append({"type": "paragraph", "content": [
                {"type": "text", "text": f"{label}⚠️ .mp4 no adjuntado: "},
                {"type": "text", "text": r["attach_skip_reason"], "marks": [{"type": "code"}]},
            ]})
        # Linea de backup GCS (si esta activo)
        if r.get("gcs_url"):
            content.append({"type": "paragraph", "content": [
                {"type": "text", "text": f"{label}Backup (GCS): "},
                {"type": "text", "text": r["gcs_url"],
                 "marks": [{"type": "link", "attrs": {"href": r["gcs_url"]}}]},
            ]})

    content.append({"type": "paragraph", "content": [
        {"type": "text",
         "text": "Nota: los adjuntos originales pueden borrarse manualmente si "
                 "ya no se necesitan. El bot nunca borra adjuntos."},
    ]})
    return {"type": "doc", "version": 1, "content": content}


def _cleanup_ticket_files(ticket_dir: Path) -> None:
    """Borra .mp4 raw + convertido tras procesar con exito.
    Conserva .studio_video_id (idempotencia) y cualquier otro fichero no .mp4.
    """
    if not ticket_dir.exists():
        return
    for f in ticket_dir.glob("*.mp4"):
        try:
            size_mb = f.stat().st_size / 1024 / 1024
            f.unlink()
            log.info(f"Cleanup: borrado {f.name} ({size_mb:.1f} MB)")
        except Exception as e:
            log.warning(f"Cleanup: no se pudo borrar {f}: {e}")


def _check_old_tmp_folders(tmp_dir: Path, slack, last_check_path: Path,
                            age_days: int = 30) -> None:
    """Escanea tmp/ y avisa en Slack (UNA vez al dia max) si hay carpetas
    de tickets con mtime > age_days. No borra nada (regla absoluta).
    """
    import time as _time
    if last_check_path.exists():
        if _time.time() - last_check_path.stat().st_mtime < 24 * 3600:
            return
    cutoff = _time.time() - age_days * 24 * 3600
    old = []
    if tmp_dir.exists():
        for sub in tmp_dir.iterdir():
            if sub.is_dir() and not sub.name.startswith(".") and sub.stat().st_mtime < cutoff:
                old.append(sub.name)
    if old:
        listado = "\n".join(f"• `tmp/{name}/`" for name in sorted(old))
        slack.send_message(
            f"🧹 *Carpetas en `tmp/` con mas de {age_days} dias* ({len(old)}):\n"
            f"{listado}\n"
            f"Si ya no las necesitas, borralas a mano. El bot nunca borra por su cuenta."
        )
    try:
        last_check_path.parent.mkdir(parents=True, exist_ok=True)
        last_check_path.touch()
    except Exception as e:
        log.warning(f"No se pudo actualizar {last_check_path}: {e}")


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


def _handle_qr_skip(ticket_key: str, ticket_url: str,
                    jira: JiraClient, slack: SlackClient) -> bool:
    """QR check. Si el ticket trae un link en el field 'Advertiser's website
    for QR' de la form, el bot NO procesa el video. Solo:
      1. Transiciona Triage → To Build via 'Send to Operations' (para que el
         ticket no se quede en Triage y el equipo lo vea en la columna correcta).
      2. Postea aviso en Slack para que un humano genere el QR + creative.
    NO descarga, NO convierte, NO sube a Studio, NO adjunta, NO comenta.

    El field NO es un customfield de Jira; vive dentro de Atlassian Forms y
    solo se accede via forms.cloud (ver JiraClient.get_form_answers).

    Devuelve True si el ticket fue saltado por QR (el caller debe `return`),
    False si no hay QR y el procesado normal debe continuar.
    """
    try:
        form_answers = jira.get_form_answers(ticket_key)
    except Exception as e:
        log.warning(f"{ticket_key}: error leyendo forms: {e}")
        form_answers = {}
    qr_url = ""
    qr_label = ""
    for label, value in form_answers.items():
        if "advertiser" in label.lower() and "qr" in label.lower():
            qr_url = value.strip()
            qr_label = label
            break
    if not qr_url:
        return False

    # Transicionar Triage -> To Build
    try:
        transitions = jira.get_transitions(ticket_key)
        if "Send to Operations" in transitions:
            jira.transition(ticket_key, transitions["Send to Operations"])
            log.info(f"{ticket_key} (QR) → To Build")
        else:
            log.warning(f"{ticket_key} (QR): sin 'Send to Operations'. "
                        f"Disponibles: {list(transitions.keys())}")
    except Exception as e:
        log.warning(f"{ticket_key} (QR): no se pudo mover a To Build: {e}")

    slack.send_message(
        f"🔲 *{ticket_key}* tiene QR (campo '{qr_label}').\n"
        f"URL: {qr_url[:200]}\n"
        f"*El bot NO procesa el video* — hay que generar el QR e incluirlo "
        f"manualmente. Ticket movido a To Build.\n"
        f"<{ticket_url}|Ver en Jira>"
    )
    log.info(f"{ticket_key}: skip procesado por QR ({qr_url[:80]})")
    return True


def _build_video_plans(video_paths: list, canonical_stem: str,
                       converter: VideoConverter, ticket_key: str) -> list[dict]:
    """Construye el plan por video que se muestra en Slack y se itera al
    procesar. Por cada video: nombre canonico (con _Vn si hay varios),
    duracion, bitrate default y tamanyo estimado. Si no se puede medir un
    archivo, deja duration/bitrate/estimated en None (el plan igual se muestra).
    """
    multi = len(video_paths) > 1
    video_plans = []
    for i, vp in enumerate(video_paths, start=1):
        cname = f"{canonical_stem}_V{i}" if multi else canonical_stem
        try:
            dur = converter.get_duration(vp)
            br = (converter.bitrate_short if dur <= converter.duration_threshold
                  else converter.bitrate_long)
            est = (br + 0.256) * dur / 8
        except Exception as e:
            log.warning(f"{ticket_key}: no se pudo medir {vp.name} ({e})")
            dur, br, est = None, None, None
        video_plans.append({
            "path": vp, "canonical": cname,
            "duration_s": dur, "bitrate_mbps": br, "estimated_size_mb": est,
        })
    return video_plans


# ── Proceso de un ticket ──────────────────────────────────────────────────────

def process_ticket(issue: dict, jira: JiraClient, slack: SlackClient,
                   converter: VideoConverter, studio: StudioAPIClient,
                   filestage: FilestageUploader, gcs=None):

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

    # ── 0. QR check (ver _handle_qr_skip). Si hay QR, no se procesa el video.
    if _handle_qr_skip(ticket_key, ticket_url, jira, slack):
        return

    # ── 1. Pre-confirmacion: descargar TODOS los videos + plan por video ──
    # Un ticket puede traer 1 o varios .mp4 originales. El bot procesa
    # CADA uno como un creative separado en Studio. Antes de pedir 'ok',
    # bajamos todos y sacamos duracion para mostrar el plan completo.
    tmp_dir = Path(os.getenv("TMP_DIR", "./tmp")) / ticket_key
    tmp_dir.mkdir(parents=True, exist_ok=True)

    video_paths = jira.download_all_videos(issue, tmp_dir)
    if not video_paths:
        transfer_url = jira.find_transfer_link(issue)
        if transfer_url:
            slack.send_message(
                f"⚠️ *{ticket_key}*: No pude descargar el video automaticamente.\n"
                f"Descargalo y adjuntalo al ticket: {transfer_url}"
            )
        else:
            slack.send_message(f"❌ *{ticket_key}*: No se encontro video adjunto ni link.")
        log.warning(f"{ticket_key}: sin video, ticket saltado (queda en seen_tickets)")
        return

    canonical_stem = _build_canonical_filename(summary)
    video_plans = _build_video_plans(video_paths, canonical_stem, converter, ticket_key)

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
        plan={"videos": video_plans},
    )
    log.info(f"Slack notificado — ts: {msg['ts']} ({len(video_plans)} video(s))")

    # Helper: agrupa todos los mensajes de progreso de este ticket en el hilo
    # de la notificacion inicial, manteniendo el canal principal limpio.
    def post_thread(text: str) -> None:
        slack.send_thread_message(msg["ts"], text)

    # ── 2. Esperar respuesta en el hilo (ok / no) ─────────────────────────
    response = slack.wait_for_ticket_response(msg["channel"], msg["ts"], timeout=3600)
    if response is None:
        post_thread(f"⏰ *{ticket_key}* no confirmado en 1 hora. Saltado.")
        return
    if response.get("action") == "cancel":
        # Marcar como cancelado para permitir reactivacion explicita despues
        canceled_path = Path(os.getenv("TMP_DIR", "./tmp")) / ".canceled_tickets.json"
        canceled = _load_canceled(canceled_path)
        canceled.add(ticket_key)
        _save_canceled(canceled_path, canceled)
        post_thread(
            f"❌ *{ticket_key}* cancelado por el usuario.\n"
            f"Si fue por error, escribi `reactivar {ticket_key}` en el canal y "
            f"el bot lo retomara en el siguiente poll."
        )
        return

    post_thread(f"✅ Confirmado. Procesando *{ticket_key}*...")

    # ── 3. Cambiar estado a To Build (Triage → To Build) ──────────────────
    # Workflow real: Triage --[Send to Operations]--> To Build --[Start Building]--> Building
    try:
        transitions = jira.get_transitions(ticket_key)
        if "Send to Operations" in transitions:
            jira.transition(ticket_key, transitions["Send to Operations"])
            log.info(f"{ticket_key} → To Build")
        else:
            log.warning(f"{ticket_key}: sin transicion 'Send to Operations'. "
                        f"Disponibles: {list(transitions.keys())}")
    except Exception as e:
        log.warning(f"No se pudo cambiar estado a To Build: {e}")

    # ── 4. Procesar CADA video (convert + Studio + attach; 1 creative c/u) ─
    # country/category son a nivel ticket (mismos para todos los videos).
    industry = (fields.get("customfield_15831") or {}).get("value", "")
    country = studio.map_country(operator_entity)
    category = studio.map_category(industry)
    total = len(video_plans)
    pending_path = Path(os.getenv("TMP_DIR", "./tmp")) / ".pending_studio.json"

    def _process_one(vplan: dict, idx: int) -> dict:
        """Convierte + sube a Studio + adjunta UN video. Devuelve result dict.
        Cierra sobre el scope de process_ticket (jira, slack, studio, converter,
        msg, post_thread, ticket_key, ticket_url, summary, country, category).
        """
        lbl = f"[{idx}/{total}] " if total > 1 else ""
        raw_path = vplan["path"]
        canonical = vplan["canonical"]
        res = {"canonical": canonical, "studio_url": None, "video_id": None,
               "creative_id": None, "attached_filename": None,
               "attach_skip_reason": None, "studio_error": None, "gcs_url": None}

        post_thread(f"{lbl}📥 `{raw_path.name}` — convirtiendo...")
        out = converter.convert(raw_path)
        if not out:
            res["studio_error"] = "conversion FFmpeg fallo"
            post_thread(f"{lbl}❌ Error de conversion FFmpeg. Salto este video.")
            return res

        # Renombrar al canonico de este video (con _Vn si hay varios)
        cpath = out.parent / f"{canonical}.mp4"
        if cpath != out:
            if cpath.exists():
                cpath.unlink()
            out.rename(cpath)
        out = cpath
        size_mb = out.stat().st_size / (1024 * 1024)
        post_thread(f"{lbl}🎬 Convertido: `{out.name}` — {size_mb:.1f} MB ({converter.last_bitrate} Mbps)")

        # Studio
        try:
            sres = studio.process_video_to_creative(
                file_path=out, ticket_title=summary, country=country, category=category,
            )
            res["studio_url"] = sres["preview_url"]
            res["video_id"] = sres["video_id"]
            res["creative_id"] = sres["creative_id"]
            post_thread(f"{lbl}🎯 Studio ✓ — <{sres['preview_url']}|Preview> · video_id=`{sres['video_id']}`")
        except StudioVideoNotReadyError as nre:
            res["studio_error"] = f"Studio PROGRESSING tras {nre.elapsed_seconds}s"
            post_thread(
                f"{lbl}⚠️ Studio sigue procesando (video_id=`{nre.video_id}`). "
                f"El bot lo revisa cada 60s y postea el link cuando complete."
            )
            _add_pending_studio(pending_path, {
                "ticket_key": ticket_key, "video_id": nre.video_id,
                "summary": summary, "country": country, "category": category,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
        except StudioJWTExpiredError as jwt_err:
            res["studio_error"] = f"JWT caducado HTTP {jwt_err.status_code}"
            post_thread(f"{lbl}🚨 Studio JWT caducado. El .mp4 queda adjunto al ticket.")
            slack.send_message(
                f"🚨 *Studio — JWT caducado en {ticket_key}* (HTTP {jwt_err.status_code}).\n"
                f"Extrae uno nuevo de design_automations@seedtag.com (DevTools → Cookies "
                f"→ seedtag_jwt), actualiza STUDIO_JWT_COOKIE/.studio_jwt y reinicia el bot."
            )
        except Exception as e:
            res["studio_error"] = f"{type(e).__name__}: {e}"
            post_thread(f"{lbl}⚠️ Studio error: `{type(e).__name__}: {e}`. El .mp4 queda adjunto al ticket.")

        # Attach a Jira (con recompresion interactiva si >150MB)
        attach_skip = None
        if size_mb > MAX_JIRA_ATTACH_MB:
            try:
                dur = converter.get_duration(raw_path)
                target_mb = MAX_JIRA_ATTACH_MB - 5
                target_video_mbps = max(int((target_mb * 8) / dur - 0.3), 3)
            except Exception:
                target_video_mbps = 10
            q_ts = slack.send_thread_message(msg["ts"],
                f"{lbl}⚠️ `{out.name}` pesa *{size_mb:.1f} MB* (>{MAX_JIRA_ATTACH_MB} MB para Jira).\n"
                f"Recomprimo a *~{target_video_mbps} Mbps*? Responde *si*/*no* (10 min).")
            answer = slack.wait_for_yes_no(msg["ts"], q_ts, timeout=600)
            if answer == "yes":
                out.unlink()
                re_out = converter.convert(raw_path, override_bitrate_mbps=target_video_mbps)
                if re_out:
                    cp = re_out.parent / f"{canonical}.mp4"
                    if cp != re_out:
                        if cp.exists():
                            cp.unlink()
                        re_out.rename(cp)
                    out = cp
                    size_mb = out.stat().st_size / (1024 * 1024)
                    post_thread(f"{lbl}✅ Re-convertido: {size_mb:.1f} MB ({converter.last_bitrate} Mbps)")
                else:
                    attach_skip = "re-conversion fallo"
            else:
                attach_skip = (f"{size_mb:.1f} MB > {MAX_JIRA_ATTACH_MB} MB; "
                               f"usuario {'dijo no' if answer == 'no' else 'no respondio'}")

        if size_mb <= MAX_JIRA_ATTACH_MB and not attach_skip:
            try:
                jira.attach_file(ticket_key, out)
                res["attached_filename"] = out.name
            except Exception as att_err:
                import requests as _rq
                if isinstance(att_err, _rq.HTTPError) and att_err.response is not None:
                    attach_skip = f"HTTP {att_err.response.status_code} — {att_err.response.text[:200]}"
                else:
                    attach_skip = f"{type(att_err).__name__}: {att_err}"
                post_thread(f"{lbl}⚠️ Error al adjuntar `{out.name}`: `{attach_skip}`")
        res["attach_skip_reason"] = attach_skip

        # ── GCS (opcional, inerte si gcs es None) ─────────────────────────
        # Sube el .mp4 convertido a GCS y devuelve un link permanente para
        # el comentario de Jira. Solo activo si GCS_BUCKET esta seteada.
        if gcs is not None:
            gcs_url = gcs.upload(out, ticket_key)
            if gcs_url:
                res["gcs_url"] = gcs_url
                post_thread(f"{lbl}☁️ GCS ✓ — <{gcs_url}|backup del .mp4>")
            else:
                post_thread(f"{lbl}⚠️ No se pudo subir el .mp4 a GCS (ver logs).")
        return res

    try:
        results = [_process_one(vp, i) for i, vp in enumerate(video_plans, start=1)]

        # ── Comentario agregado en Jira (todos los creatives) ──
        jira.add_comment_adf(ticket_key, _build_done_comment_multi(results, total))

        # ── Resumen en Slack ──
        ok_count = sum(1 for r in results if r.get("studio_url"))
        post_thread(
            f"✅ *{ticket_key}* procesado: {ok_count}/{total} creative(s) listos en Studio.\n"
            f"<{ticket_url}|Ver en Jira> — marca como Done cuando lo revises."
        )
        log.info(f"✅ {ticket_key} procesado — {ok_count}/{total} creatives en Studio")

        # ── Transicion final → Building (una vez por ticket) ──────────────
        # AMBAS transiciones (Start Building / Send to Building) requieren que
        # el ticket tenga customfield_15826 (Seedtag Specs) rellenado. El field
        # NO esta en el screen de la transicion, hay que setearlo con PUT antes.
        try:
            jira.set_fields(ticket_key, {"customfield_15826": {"id": "27743"}})
            transitions = jira.get_transitions(ticket_key)
            for tname in ("Start Building", "Send to Building"):
                if tname in transitions:
                    jira.transition(ticket_key, transitions[tname])
                    log.info(f"{ticket_key} → Building (via '{tname}')")
                    break
            else:
                log.warning(f"{ticket_key}: sin transicion a Building. "
                            f"Disponibles: {list(transitions.keys())}")
        except Exception as e:
            log.warning(f"No se pudo cambiar estado a Building: {e}")

        # ── Cleanup: borrar .mp4 (raw + convertidos). Conservar sidecars. ──
        _cleanup_ticket_files(tmp_dir)

    except Exception as e:
        # Regla del equipo: el bot NUNCA vuelve a Triage ni a Brand. Si algo
        # falla durante el proceso, se deja el ticket en el estado actual
        # (Triage si la primera transicion no llego a ejecutarse, o To Build
        # si si lo hizo) y se avisa en Slack + comentario en Jira. Un humano
        # decide que hacer.
        log.error(f"Error procesando {ticket_key}: {e}", exc_info=True)
        # Aviso doble: en hilo (contexto del ticket) y en main (visibilidad
        # critica — fallo total del flujo).
        post_thread(
            f"❌ *{ticket_key}* fallo durante el proceso.\n"
            f"Error: `{type(e).__name__}: {e}`\n"
            f"El ticket queda en su estado actual — el bot no revierte. "
            f"Un humano debe revisar."
        )
        slack.send_message(
            f"❌ *{ticket_key}* fallo durante el proceso. "
            f"Ver detalles en el hilo del ticket."
        )
        try:
            jira.add_comment_adf(ticket_key, {
                "type": "doc", "version": 1,
                "content": [{
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "⚠️ El bot fallo al procesar este ticket. Error: "},
                        {"type": "text", "text": f"{type(e).__name__}: {e}",
                         "marks": [{"type": "code"}]},
                    ],
                }, {
                    "type": "paragraph",
                    "content": [
                        {"type": "text",
                         "text": "El bot NO cambia el estado del ticket — un humano debe revisar "
                                 "y decidir si re-procesar, enviar al cliente o cerrar."},
                    ],
                }],
            })
        except Exception as ce:
            log.warning(f"No se pudo dejar comentario de error en {ticket_key}: {ce}")


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
        on_refresh_failure=slack.send_message,
    )

    # GCS uploader — inerte si GCS_BUCKET no esta seteada (caso burn-in local).
    # Se activa al migrar a GCP seteando GCS_BUCKET en el entorno de la VM.
    import gcs_uploader
    gcs = gcs_uploader.from_env()
    if gcs:
        log.info(f"GCS activado: bucket={gcs.bucket_name} prefix={gcs.prefix} public={gcs.public}")

    # Asignar callback de status para responder durante la espera del ok
    slack.status_callback = lambda: get_queue_status(jira)

    tmp_root = Path(os.getenv("TMP_DIR", "./tmp"))
    seen_tickets_path = tmp_root / ".seen_tickets.json"
    seen_tickets: set[str] = _load_seen_tickets(seen_tickets_path)
    if seen_tickets:
        log.info(f"Cargados {len(seen_tickets)} tickets ya vistos de {seen_tickets_path}")
    seen_status: set[str] = set()
    last_studio_heartbeat: float = 0.0
    HEARTBEAT_INTERVAL = 24 * 3600
    last_tmp_check_path = tmp_root / ".last_tmp_check"
    pending_studio_path = tmp_root / ".pending_studio.json"
    pending_count = len(_load_pending_studio(pending_studio_path))
    if pending_count:
        log.info(f"Pending Studio: {pending_count} tickets esperando COMPLETED")
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

            # Segunda pasada: revisar tickets cuyo video en Studio quedo
            # PROGRESSING al terminar el flujo principal; si llego a COMPLETED,
            # crear el creative + segundo comentario Jira con el link.
            try:
                _process_pending_studio(studio, jira, slack, pending_studio_path)
            except Exception as e:
                log.warning(f"Error en _process_pending_studio: {e}")

            _check_old_tmp_folders(tmp_root, slack, last_tmp_check_path)

            # Detectar comandos en el canal: 'status' y 'reactivar SDS-XXX'
            try:
                history = slack.client.conversations_history(
                    channel=slack.channel_id, limit=10
                )
                for msg in history.get("messages", []):
                    text = msg.get("text", "").strip()
                    text_lower = text.lower()
                    ts = msg.get("ts", "")
                    if msg.get("bot_id") or ts in seen_status:
                        continue
                    # 'status' -> resumen de la cola
                    if text_lower == "status":
                        seen_status.add(ts)
                        log.info("Comando 'status' recibido")
                        slack.client.chat_postMessage(
                            channel=slack.channel_id,
                            text=get_queue_status(jira),
                            thread_ts=ts,
                        )
                        continue
                    # 'reactivar SDS-XXXXX' -> sacar de seen_tickets si estaba
                    # cancelado por el usuario (lista canceled_tickets.json).
                    m = re.match(r"reactivar\s+(SDS-\d+)", text, re.IGNORECASE)
                    if m:
                        seen_status.add(ts)
                        target = m.group(1).upper()
                        canceled_path = tmp_root / ".canceled_tickets.json"
                        canceled = _load_canceled(canceled_path)
                        if target in canceled:
                            canceled.discard(target)
                            _save_canceled(canceled_path, canceled)
                            seen_tickets.discard(target)
                            _save_seen_tickets(seen_tickets_path, seen_tickets)
                            slack.client.chat_postMessage(
                                channel=slack.channel_id, thread_ts=ts,
                                text=f"🔄 *{target}* reactivado. Lo retomo en el siguiente poll (≤60s).",
                            )
                            log.info(f"Comando reactivar: {target} re-habilitado")
                        else:
                            slack.client.chat_postMessage(
                                channel=slack.channel_id, thread_ts=ts,
                                text=(f"❓ *{target}* no estaba cancelado. Solo se puede "
                                      f"reactivar tickets que dijiste `no` y estan en la "
                                      f"lista de cancelados."),
                            )
                            log.info(f"Comando reactivar: {target} no estaba en canceled")
            except Exception as e:
                log.warning(f"Error leyendo canal para comandos: {e}")

            # Polling Jira
            issues = jira.get_omniscreen_video_issues()
            new = [i for i in issues if i["key"] not in seen_tickets and is_csv_ticket(i)]
            for issue in new:
                seen_tickets.add(issue["key"])
                _save_seen_tickets(seen_tickets_path, seen_tickets)
                process_ticket(issue, jira, slack, converter, studio, filestage, gcs=gcs)

        except Exception as e:
            log.error(f"Error en loop: {e}", exc_info=True)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
