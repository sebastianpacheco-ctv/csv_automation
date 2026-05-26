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


def _build_done_comment(studio_url: str | None,
                        filestage_url: str | None,
                        attached_filename: str | None,
                        attach_skip_reason: str | None = None) -> dict:
    """Construye el doc ADF para el comentario que el bot deja en Jira tras
    procesar un ticket. Sin specs tecnicas; links clickables; nota de que el
    adjunto original puede borrarse a mano (el bot nunca borra).
    """
    paragraphs = []
    paragraphs.append({
        "type": "paragraph",
        "content": [
            {"type": "text", "text": "✅ Video CSV/COV convertido y subido automaticamente."},
        ],
    })

    if studio_url:
        paragraphs.append({
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Preview Studio: "},
                {
                    "type": "text",
                    "text": studio_url,
                    "marks": [{"type": "link", "attrs": {"href": studio_url}}],
                },
            ],
        })
    else:
        paragraphs.append({
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "⚠️ Studio: subir el video manualmente "
                                          "(el bot no pudo terminar)."},
            ],
        })

    if filestage_url:
        paragraphs.append({
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Filestage: "},
                {
                    "type": "text",
                    "text": filestage_url,
                    "marks": [{"type": "link", "attrs": {"href": filestage_url}}],
                },
            ],
        })

    if attached_filename:
        paragraphs.append({
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Archivo convertido adjuntado a este ticket: "},
                {"type": "text", "text": attached_filename, "marks": [{"type": "code"}]},
            ],
        })
    elif attach_skip_reason:
        paragraphs.append({
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "⚠️ No se pudo adjuntar el .mp4 convertido: "},
                {"type": "text", "text": attach_skip_reason, "marks": [{"type": "code"}]},
                {"type": "text", "text": ". Si lo necesitas en el ticket, "
                                          "descargalo de Studio y subelo a mano."},
            ],
        })

    paragraphs.append({
        "type": "paragraph",
        "content": [
            {"type": "text",
             "text": "Nota: el adjunto original puede borrarse manualmente si "
                     "ya no lo necesitas. El bot nunca borra adjuntos."},
        ],
    })

    return {"type": "doc", "version": 1, "content": paragraphs}


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

    # ── 0. QR check: si el ticket trae un link en el field "Advertiser's
    # website for QR" de la form, el bot NO procesa el video. Solo:
    #   1. Transiciona Triage → To Build via 'Send to Operations' (para que
    #      el ticket no se quede en Triage y el equipo lo vea en columna
    #      correcta).
    #   2. Postea aviso en Slack para que un humano genere el QR + creative.
    # NO descarga, NO convierte, NO sube a Studio, NO adjunta, NO comenta.
    # Nota: el field NO es un customfield de Jira; vive dentro de Atlassian
    # Forms y solo se accede via forms.cloud (ver JiraClient.get_form_answers).
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
    if qr_url:
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
        return

    # ── 1. Pre-confirmacion: descargar + probar duracion + calcular plan ──
    # Antes de pedir 'ok' al usuario, descargamos el adjunto y sacamos
    # duracion para poder mostrarle nombre canonico + bitrate + tamanyo
    # estimado. Asi el usuario decide con info real.
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
        log.warning(f"{ticket_key}: sin video, ticket saltado (queda en seen_tickets)")
        return

    plan = None
    try:
        duration_s = converter.get_duration(input_path)
        default_bitrate = (converter.bitrate_short if duration_s <= converter.duration_threshold
                           else converter.bitrate_long)
        canonical_stem = _build_canonical_filename(summary)
        # Estimacion: video + audio AAC (~0.256 Mbps) en duration_s segundos
        estimated_mb = (default_bitrate + 0.256) * duration_s / 8
        plan = {
            "filename": f"{canonical_stem}.mp4",
            "duration_s": duration_s,
            "bitrate_mbps": default_bitrate,
            "estimated_size_mb": estimated_mb,
        }
    except Exception as e:
        log.warning(f"{ticket_key}: no se pudo computar plan ({e}), notifico sin plan")

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
        plan=plan,
    )
    log.info(f"Slack notificado — ts: {msg['ts']}")

    # ── 2. Esperar respuesta en el hilo (ok / ok Nmbps / no) ──────────────
    response = slack.wait_for_ticket_response(msg["channel"], msg["ts"], timeout=3600)
    if response is None:
        slack.send_message(f"⏰ *{ticket_key}* no confirmado en 1 hora. Saltado.")
        return
    if response.get("action") == "cancel":
        slack.send_message(f"❌ *{ticket_key}* cancelado por el usuario.")
        return

    slack.send_message(f"✅ Confirmado. Procesando *{ticket_key}*...")

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

    # ── 4-8. Proceso ──────────────────────────────────────────────────────
    try:
        slack.send_message(f"📥 Descargado: `{input_path.name}` — convirtiendo...")

        # ── 5. Convertir (parametros default: 30 Mbps si <=30s, 15 Mbps si mas).
        # No hay override desde Slack en el "ok" inicial. El unico override
        # posible es si el .mp4 supera 150 MB: el bot pregunta aparte si
        # recomprime y aplica el override_bitrate_mbps en ese flujo.
        output_path = converter.convert(input_path)
        if not output_path:
            raise RuntimeError("Error en conversion FFmpeg")

        # Renombrar al nombre canonico del ticket — el .mp4, el upload a
        # Studio, y el adjunto a Jira van a tener todos el mismo nombre.
        # Regla: summary sanitizado; si no contiene CTV_CSV, se anyade.
        canonical_stem = _build_canonical_filename(summary)
        canonical_path = output_path.parent / f"{canonical_stem}.mp4"
        if canonical_path != output_path:
            if canonical_path.exists():
                canonical_path.unlink()  # ejecucion anterior dejó residuo
            output_path.rename(canonical_path)
            log.info(f"Renombrado a nombre canonico: {canonical_path.name}")
        output_path = canonical_path

        size_mb = output_path.stat().st_size / (1024 * 1024)
        slack.send_message(
            f"🎬 Convertido: `{output_path.name}` — {size_mb:.1f} MB ({converter.last_bitrate} Mbps)"
        )

        # ── 6. Filestage ─── DESACTIVADO ─────────────────────────────────
        # Sacado del flujo el 26-may-2026: el equipo CTV no usa Filestage
        # para review (los comentarios del cliente van directo a Jira) y
        # s3-complete venia fallando con 400 consistentemente. La clase
        # FilestageUploader sigue en src/uploader.py por si se quiere
        # reactivar en algun momento.
        filestage_url = None

        # ── 7. Studio Seedtag (via GraphQL API) ───────────────────────────
        studio_url = None
        try:
            industry = (fields.get("customfield_15831") or {}).get("value", "")
            country = studio.map_country(operator_entity)
            category = studio.map_category(industry)
            result = studio.process_video_to_creative(
                file_path=output_path,
                ticket_title=summary,    # name del creative = summary del ticket
                # video_filename omitido: upload_video sanitiza file_path.name
                # que ya esta en formato canonico tras el rename del paso 5.
                country=country,
                category=category,
            )
            studio_url = result["preview_url"]
            slack.send_message(
                f"🎯 Studio ✓ — <{studio_url}|Preview> · "
                f"video_id=`{result['video_id']}`"
            )
        except StudioVideoNotReadyError as nre:
            # El vídeo se subió pero el procesado tarda mas de lo esperado.
            # Avisar en Slack + guardar en pending_studio para segunda pasada:
            # el loop principal revisara cada 60s y crearia el creative + link
            # cuando Studio llegue a COMPLETED.
            slack.send_message(
                f"⚠️ *{ticket_key}* — Studio sigue procesando el vídeo tras "
                f"{nre.elapsed_seconds}s (último estado: `{nre.last_state}`).\n"
                f"video_id: `{nre.video_id}`\n"
                f"El bot lo revisara cada 60s; cuando este COMPLETED, creara el "
                f"creative y postea el link aqui + en Jira.\n"
                f"Si quieres adelantarte: el .mp4 convertido va a quedar "
                f"adjuntado a <{ticket_url}|este ticket>, podes bajarlo desde "
                f"ahi si necesitas hacer algo manualmente."
            )
            _add_pending_studio(
                Path(os.getenv("TMP_DIR", "./tmp")) / ".pending_studio.json",
                {
                    "ticket_key": ticket_key,
                    "video_id": nre.video_id,
                    "summary": summary,
                    "country": country,
                    "category": category,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            log.info(f"{ticket_key}: anyadido a pending_studio (video_id={nre.video_id})")
        except StudioJWTExpiredError as jwt_err:
            log.error(f"Studio JWT expirado: {jwt_err}")
            slack.send_message(
                f"🚨 *Studio — JWT caducado* (HTTP {jwt_err.status_code})\n"
                f"Extrae un JWT nuevo de design_automations@seedtag.com en "
                f"DevTools → Application → Cookies → seedtag_jwt, actualiza "
                f"STUDIO_JWT_COOKIE (o el sidecar `.studio_jwt`) y reinicia el bot.\n"
                f"Mientras tanto, el .mp4 convertido (`{output_path.name}`) va a "
                f"quedar adjuntado a <{ticket_url}|este ticket>. Cuando refresques "
                f"el JWT, bajalo del ticket y subelo a Studio manualmente."
            )
        except Exception as e:
            log.error(f"Studio fallo: {e}")
            slack.send_message(
                f"⚠️ *{ticket_key}* — Studio error: `{type(e).__name__}: {e}`.\n"
                f"El .mp4 convertido (`{output_path.name}`) se adjuntara a "
                f"<{ticket_url}|este ticket>. Bajalo de ahi y subelo a Studio "
                f"manualmente."
            )

        # ── 7. Adjuntar el .mp4 convertido al ticket Jira ─────────────────
        # NO se borra el adjunto original (regla absoluta). Si pesa mas de
        # MAX_JIRA_ATTACH_MB, preguntamos al usuario en el hilo si quiere
        # que el bot recomprima a un bitrate que quepa.
        attached_ok = False
        attach_skip_reason = None
        if size_mb > MAX_JIRA_ATTACH_MB:
            duration_s = converter.get_duration(input_path)
            # Bitrate objetivo: que el video + audio sumen <=(MAX-5) MB.
            # Audio AAC ~256 kbps = 0.256 Mbps. Damos margen extra de 0.3 Mbps.
            target_mb = MAX_JIRA_ATTACH_MB - 5
            target_total_mbps = (target_mb * 8) / duration_s
            target_video_mbps = max(int(target_total_mbps - 0.3), 3)
            slack_q_ts = slack.send_thread_message(msg["ts"],
                f"⚠️ *{ticket_key}*: el .mp4 pesa *{size_mb:.1f} MB* (>{MAX_JIRA_ATTACH_MB} MB para Jira).\n"
                f"Puedo recomprimir a *~{target_video_mbps} Mbps* (esperado ~{target_mb} MB).\n"
                f"Responde *si* para recomprimir, *no* para saltar el adjunto. Timeout 10 min."
            )
            log.info(f"{ticket_key}: solicitada confirmacion de recompresion ({size_mb:.1f}MB → {target_video_mbps}Mbps)")
            answer = slack.wait_for_yes_no(msg["ts"], slack_q_ts, timeout=600)
            if answer == "yes":
                slack.send_thread_message(msg["ts"],
                    f"🔄 Recomprimiendo a {target_video_mbps} Mbps...")
                # Borrar el .mp4 viejo antes de re-convertir
                output_path.unlink()
                new_output = converter.convert(input_path, override_bitrate_mbps=target_video_mbps)
                if new_output:
                    # Renombrar al canonical
                    canonical_path = new_output.parent / f"{canonical_stem}.mp4"
                    if canonical_path != new_output:
                        if canonical_path.exists():
                            canonical_path.unlink()
                        new_output.rename(canonical_path)
                    output_path = canonical_path
                    size_mb = output_path.stat().st_size / (1024 * 1024)
                    slack.send_thread_message(msg["ts"],
                        f"✅ Re-convertido: {size_mb:.1f} MB ({converter.last_bitrate} Mbps)")
                    log.info(f"{ticket_key}: re-convertido a {size_mb:.1f}MB")
                else:
                    attach_skip_reason = "re-conversion fallo"
                    slack.send_thread_message(msg["ts"],
                        f"❌ Re-conversion fallo. No adjunto el .mp4.")
            else:
                attach_skip_reason = (f"archivo de {size_mb:.1f} MB > {MAX_JIRA_ATTACH_MB} MB; "
                                      f"usuario {'respondio NO' if answer == 'no' else 'no respondio a tiempo'}")
                log.info(f"{ticket_key}: skip attach — {attach_skip_reason}")

        # Si el tamaño esta OK (original o tras recompresion), intentar attach
        if size_mb <= MAX_JIRA_ATTACH_MB and not attach_skip_reason:
            try:
                jira.attach_file(ticket_key, output_path)
                attached_ok = True
            except Exception as att_err:
                # Errores tipicos: nombre duplicado en el ticket, 413 size del
                # servidor, 401/403 auth. Loguea el body completo en Slack.
                import requests as _rq
                if isinstance(att_err, _rq.HTTPError) and att_err.response is not None:
                    body = att_err.response.text[:300]
                    attach_skip_reason = f"HTTP {att_err.response.status_code} — {body}"
                else:
                    attach_skip_reason = f"{type(att_err).__name__}: {att_err}"
                slack.send_message(
                    f"⚠️ *{ticket_key}*: error al adjuntar el .mp4 a Jira.\n"
                    f"`{attach_skip_reason}`\n"
                    f"Causas comunes: nombre duplicado en el ticket o limite del servidor. "
                    f"Subelo manualmente si lo necesitas."
                )
                log.warning(f"{ticket_key}: attach fallo — {attach_skip_reason}")

        # ── 8. Comentar en Jira (ADF con links clickables) ────────────────
        jira.add_comment_adf(ticket_key, _build_done_comment(
            studio_url=studio_url,
            filestage_url=filestage_url,
            attached_filename=output_path.name if attached_ok else None,
            attach_skip_reason=attach_skip_reason,
        ))

        # ── 9. Resumen en Slack ───────────────────────────────────────────
        slack.send_message(
            f"✅ *{ticket_key}* listo para revision:\n"
            f"• Archivo: `{output_path.name}`\n"
            f"• {size_mb:.1f} MB | {converter.last_bitrate} Mbps\n"
            f"• Studio: {'✓' if studio_url else '⚠️ subir manualmente'}\n"
            f"• <{ticket_url}|Ver en Jira> — marca como Done cuando lo revises"
        )
        log.info(f"✅ {ticket_key} procesado — pendiente revision del equipo")

        # ── 10. Transicion final: → Building ──────────────────────────────
        # El nombre de la transicion depende del estado actual:
        #   Triage   → Building  via 'Send to Building'
        #   To Build → Building  via 'Start Building'
        # AMBAS requieren que el ticket tenga customfield_15826 (Seedtag Specs)
        # rellenado. El field NO esta en el screen de la transicion, asi que
        # hay que setearlo con PUT al issue ANTES de transicionar (descubierto
        # via burn-in 26-may: el body de POST /transitions con el field daba
        # 'cannot be set, not on appropriate screen').
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

        # ── 11. Cleanup: borrar .mp4 (raw + convertido). Conservar .studio_video_id.
        _cleanup_ticket_files(tmp_dir)

    except Exception as e:
        # Regla del equipo: el bot NUNCA vuelve a Triage ni a Brand. Si algo
        # falla durante el proceso, se deja el ticket en el estado actual
        # (Triage si la primera transicion no llego a ejecutarse, o To Build
        # si si lo hizo) y se avisa en Slack + comentario en Jira. Un humano
        # decide que hacer.
        log.error(f"Error procesando {ticket_key}: {e}", exc_info=True)
        slack.send_message(
            f"❌ *{ticket_key}* fallo durante el proceso.\n"
            f"Error: `{type(e).__name__}: {e}`\n"
            f"El ticket queda en su estado actual — el bot no revierte. "
            f"Un humano debe revisar."
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
                _save_seen_tickets(seen_tickets_path, seen_tickets)
                process_ticket(issue, jira, slack, converter, studio, filestage)

        except Exception as e:
            log.error(f"Error en loop: {e}", exc_info=True)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
