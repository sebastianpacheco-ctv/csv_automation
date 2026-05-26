# Encoding preset

`Mezzanine_TradeDesk_29.97.epr` — preset oficial del equipo CTV de Seedtag para
exportar videos en Adobe Premiere Pro. Es la **fuente de verdad** que el bot
intenta reproducir vía FFmpeg en `src/converter.py`.

## Cómo se relaciona con el código

`src/converter.py` no usa el `.epr` directamente — FFmpeg no entiende ese
formato (es XML específico de Premiere). En su lugar, las flags de `ffmpeg`
en el `convert()` están escritas a mano para producir un fichero que
**conforme** al preset.

Mapeo:

| Preset Premiere (`.epr`) | FFmpeg en `converter.py` |
|---|---|
| Codec H.264 (avc1) | `-c:v libx264` |
| Profile High, Level 4.0 | `-profile:v high -level 4.0` |
| 1920×1080 | `-s 1920x1080` |
| 29.97 fps | `-r 30000/1001` |
| Bitrate target / max / buf | `-b:v {N}M -maxrate {N}M -bufsize {2N}M` |
| `30 Mbps` si duración ≤30s | constante `bitrate_short=30` |
| `15 Mbps` si duración >30s | constante `bitrate_long=15` |
| yuv420p | `-pix_fmt yuv420p` |
| MP4 web-optimizado | `-movflags +faststart` |
| AAC 256 kbps 48 kHz Stereo | `-c:a aac -b:a 256k -ar 48000 -ac 2` |
| `TargetLoudness = -24` (BS.1770 / EBU R128) | `loudnorm=I=-24` |
| TruePeak -2 dBTP | `TP=-2` |
| LoudnessRange 11 | `LRA=11` |

## Regla del equipo (Sebas 26-may-2026)

> Estos parámetros NO se modifican nunca durante una conversión, **salvo el
> bitrate** cuando el archivo resultante supera 150 MB (límite Jira). En ese
> caso el bot pregunta al usuario y, si confirma, vuelve a convertir bajando
> el bitrate al máximo que entre debajo del límite. Codec, resolución, fps,
> audio y loudness quedan intactos.

## Si el preset cambia en el futuro

1. Reemplazá el `.epr` aquí (o anyadí una nueva versión con sufijo de fecha).
2. Actualizá las flags FFmpeg en `src/converter.py` para que el output siga
   conforme.
3. Actualizá esta tabla.
4. Validá con un .mp4 de prueba que el resultante pase la review del equipo.

El archivo original vive en `/Volumes/seedtag-video-team/Proyecto_Template_CTV/06_Recursos/`.
Esta copia es para tener el preset junto al código en GitHub.
