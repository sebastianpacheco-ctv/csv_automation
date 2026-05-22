"""
Video Converter — FFmpeg con bitrate adaptativo.

Specs Seedtag Standard Video (Mezzanine_TradeDesk preset):
  Codec video : H.264 (avc1)  |  Resolución: 1920×1080  |  FPS: 29.97
  Bitrate     : 30 Mbps (≤30s) / 15 Mbps (>30s)
  Audio       : AAC 256kbps, 48kHz, Stereo, -24 LUFS TP -2
"""
import subprocess, json, logging
from pathlib import Path

log = logging.getLogger(__name__)


class VideoConverter:
    def __init__(self, tmp_dir="./tmp", bitrate_short=30,
                 bitrate_long=15, duration_threshold=30):
        self.tmp_dir = Path(tmp_dir)
        self.bitrate_short = bitrate_short
        self.bitrate_long = bitrate_long
        self.duration_threshold = duration_threshold
        self.last_bitrate = None

    def get_duration(self, input_path: Path) -> float:
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
               "-show_streams", str(input_path)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"ffprobe error: {r.stderr}")
        for stream in json.loads(r.stdout).get("streams", []):
            if d := stream.get("duration"):
                return float(d)
        raise RuntimeError("No se pudo obtener duración")

    def convert(self, input_path: Path) -> Path | None:
        try:
            duration = self.get_duration(input_path)
        except Exception as e:
            log.error(f"Error duración: {e}")
            return None

        bitrate = self.bitrate_short if duration <= self.duration_threshold else self.bitrate_long
        self.last_bitrate = bitrate
        estimated_mb = (bitrate * 1_000_000 * duration) / (8 * 1024 * 1024)
        log.info(f"Duración: {duration:.1f}s → {bitrate} Mbps (~{estimated_mb:.1f} MB)")

        # Nombre claro para el cliente: ticket + STANDARD_VIDEO_CONVERTED
        import re
        stem = input_path.stem
        # Extraer nombre base limpio
        output = input_path.parent / f"{stem.replace("_STANDARD_VIDEO_CONVERTED", "")}_STANDARD_VIDEO_CONVERTED.mp4"
        cmd = [
            "ffmpeg", "-y", "-i", str(input_path),
            "-c:v", "libx264", "-preset", "slow", "-profile:v", "high", "-level", "4.0",
            "-r", "30000/1001", "-s", "1920x1080",
            "-b:v", f"{bitrate}M", "-maxrate", f"{bitrate}M", "-bufsize", f"{bitrate*2}M",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2",
            "-af", "loudnorm=I=-24:TP=-2:LRA=11",
            str(output)
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            log.error(f"FFmpeg error:\n{r.stderr}")
            return None

        final_mb = output.stat().st_size / (1024 * 1024)
        log.info(f"Conversión OK: {output.name} ({final_mb:.1f} MB)")
        if final_mb > 200:
            log.warning(f"⚠️ {final_mb:.1f} MB supera el límite de 200 MB")
        return output
