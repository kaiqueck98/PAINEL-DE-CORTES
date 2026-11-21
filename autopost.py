import json
import os
import math
import re
import shutil
import subprocess
import tempfile
import sqlite3
import uuid
from contextlib import contextmanager


def _media_command(args: list[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Não foi possível executar {args[0]}: {exc}") from exc
    if result.returncode:
        raise RuntimeError(f"Falha na análise/conversão: {result.stderr[-1800:]}")
    return result


def _media_tools() -> tuple[str, str]:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("Instale ffmpeg e ffprobe no PATH antes de rodar automaticamente.")
    return ffmpeg, ffprobe


def analisar_qualidade(video: str | os.PathLike) -> dict:
    """Heurística: 720p, >=1.5 Mbps, RMS >=-30 dBFS, blur <=5.

    ffprobe lê metadados; ffmpeg mede áudio completo e desfoque a 1 fps.
    Os limites são configuráveis via AUTOPOST_MIN_BITRATE, AUTOPOST_MIN_AUDIO_DB
    e AUTOPOST_MAX_BLUR. Não representam garantia perceptual de qualidade.
    Falhas de análise interrompem o envio, em vez de aprovar um arquivo ilegível.
    """
    path = os.path.abspath(os.fspath(video))
    if not os.path.isfile(path):
        raise RuntimeError("O vídeo precisa estar baixado antes da análise.")
    ffmpeg, ffprobe = _media_tools()
    metadata = json.loads(_media_command([ffprobe, "-v", "error", "-show_streams",
                         "-show_format", "-of", "json", path], 60).stdout)
    streams = metadata.get("streams", [])
    visual = next((s for s in streams if s.get("codec_type") == "video"
                   and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not visual:
        raise RuntimeError("Arquivo sem faixa de vídeo válida.")
    def numeric(value: object) -> float:
        try:
            parsed = float(value)
            return parsed if math.isfinite(parsed) else 0.0
        except (ValueError, TypeError):
            return 0.0
    width, height = int(visual.get("width", 0)), int(visual.get("height", 0))
    bitrate = numeric(visual.get("bit_rate"))
    estimated = bitrate <= 0
    if estimated:
        bitrate = max(0, numeric(metadata.get("format", {}).get("bit_rate"))
                      - sum(numeric(s.get("bit_rate")) for s in streams if s.get("codec_type") == "audio"))
    args = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-i", path,
            "-map", f"0:{visual['index']}", "-vf", "fps=1,blurdetect"]
    if audio:
        args += ["-map", f"0:{audio['index']}", "-af", "volumedetect"]
    log = _media_command(args + ["-f", "null", "-"]).stderr
    blur_match = re.search(r"blur mean:\s*([\d.eE+\-]+|inf|nan)", log, re.I)
    volume_match = re.search(r"mean_volume:\s*([\d.\-]+|-inf)\s*dB", log)
    if not blur_match or (audio and not volume_match):
        raise RuntimeError("FFmpeg não retornou as métricas de desfoque/áudio esperadas.")
    blur = float(blur_match.group(1))
    volume = float(volume_match.group(1)) if volume_match else None
    reasons = []
    if min(width, height) < 720:
        reasons.append("resolução abaixo de 720p")
    if bitrate < float(os.environ.get("AUTOPOST_MIN_BITRATE", "1500000")):
        reasons.append("bitrate baixo ou desconhecido")
    if not math.isfinite(blur) or blur > float(os.environ.get("AUTOPOST_MAX_BLUR", "5")):
        reasons.append("possível desfoque")
    if volume is None or not math.isfinite(volume) or volume < float(os.environ.get("AUTOPOST_MIN_AUDIO_DB", "-30")):
        reasons.append("áudio ausente, silencioso ou baixo")
    return {"qualidade": "RUIM" if reasons else "BOA", "motivos": reasons,
            "width": width, "height": height, "bitrate": bitrate,
            "bitrate_estimado": estimated, "blur": blur if math.isfinite(blur) else None,
            "audio_db": volume if volume is not None and math.isfinite(volume) else None,
            "has_audio": audio is not None}


def verificar_qualidade(video: str | os.PathLike) -> str:
    return analisar_qualidade(video)["qualidade"]


def melhorar_video(video: str | os.PathLike, *, analise: dict | None = None,
                   output_dir: str | None = None) -> str:
    """Mantém BOA intacta; processa RUIM sem sobrescrever o original.

    Cada job deve usar seu próprio diretório. Sem output_dir, cria um diretório
    isolado junto ao original. Nitidez não recupera detalhe perdido nem áudio ausente.
    """
    source = os.path.abspath(os.fspath(video))
    report = analise if analise is not None else analisar_qualidade(source)
    if report["qualidade"] == "BOA":
        return source
    ffmpeg, ffprobe = _media_tools()
    directory = output_dir or tempfile.mkdtemp(prefix="autopost-", dir=os.path.dirname(source))
    os.makedirs(directory, exist_ok=True)
    output = os.path.join(directory, "video_melhorado.mp4")
    if os.path.normcase(os.path.abspath(output)) == os.path.normcase(source):
        raise RuntimeError("A saída não pode sobrescrever o vídeo original.")
    command = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-y", "-i", source,
               "-map", "0:v:0", "-map", "0:a:0?", "-vf",
               "scale=trunc(iw/2)*2:trunc(ih/2)*2,unsharp=5:5:0.7:5:5:0,eq=brightness=0.02:contrast=1.05",
               "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
    if report["has_audio"]:
        command += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    command += ["-movflags", "+faststart", output]
    try:
        _media_command(command, 3600)
        info = json.loads(_media_command([ffprobe, "-v", "error", "-show_streams", "-of", "json", output], 60).stdout)
        if not any(s.get("codec_type") == "video" for s in info.get("streams", [])):
            raise RuntimeError("O arquivo melhorado não contém vídeo válido.")
    except Exception:
        if os.path.isfile(output):
            os.remove(output)
        raise
    return output


class AutopostStoreError(Exception):
    pass


class AutopostStore:
    def __init__(self, database_path):
        self.database_path = database_path

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        database_directory = os.path.dirname(os.path.abspath(self.database_path))
        os.makedirs(database_directory, exist_ok=True)
        try:
            with self._connection() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS autopost_schedules (
                        id TEXT PRIMARY KEY,
                        folder_id TEXT NOT NULL,
                        folder_name TEXT NOT NULL,
                        videos_per_day INTEGER NOT NULL,
                        times_json TEXT NOT NULL,
                        destinations_json TEXT NOT NULL,
                        title_template TEXT NOT NULL DEFAULT '',
                        hashtags_template TEXT NOT NULL DEFAULT '',
                        timezone TEXT NOT NULL,
                        status TEXT NOT NULL,
                        last_status TEXT,
                        last_message TEXT,
                        last_run_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS autopost_ticks (
                        schedule_id TEXT NOT NULL REFERENCES autopost_schedules(id),
                        run_date TEXT NOT NULL,
                        run_time TEXT NOT NULL,
                        status TEXT NOT NULL,
                        message TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (schedule_id, run_date, run_time)
                    );
                    CREATE TABLE IF NOT EXISTS autopost_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        schedule_id TEXT NOT NULL REFERENCES autopost_schedules(id),
                        run_date TEXT NOT NULL,
                        run_time TEXT NOT NULL,
                        video_id TEXT NOT NULL,
                        video_name TEXT NOT NULL,
                        folder_name TEXT NOT NULL,
                        platform TEXT NOT NULL,
                        status TEXT NOT NULL,
                        message TEXT NOT NULL DEFAULT '',
                        remote_id TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE (schedule_id, video_id, platform)
                    );
                    CREATE INDEX IF NOT EXISTS autopost_history_recent
                        ON autopost_history (created_at DESC);
                    """
                )
                schedule_columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(autopost_schedules)")
                }
                if "title_template" not in schedule_columns:
                    connection.execute(
                        "ALTER TABLE autopost_schedules ADD COLUMN title_template TEXT NOT NULL DEFAULT ''"
                    )
                if "hashtags_template" not in schedule_columns:
                    connection.execute(
                        "ALTER TABLE autopost_schedules ADD COLUMN hashtags_template TEXT NOT NULL DEFAULT ''"
                    )
        except sqlite3.Error as exc:
            raise AutopostStoreError("Não foi possível inicializar o banco de agendamentos.") from exc

    @staticmethod
    def _schedule_dict(row):
        schedule = dict(row)
        schedule["times"] = json.loads(schedule.pop("times_json"))
        schedule["destinations"] = json.loads(schedule.pop("destinations_json"))
        schedule.setdefault("title_template", "")
        schedule.setdefault("hashtags_template", "")
        return schedule

    def create_schedule(
        self,
        folder_id,
        folder_name,
        videos_per_day,
        times,
        destinations,
        timezone,
        created_at,
        title_template="",
        hashtags_template="",
        reuse=False,
    ):
        schedule_id = uuid.uuid4().hex
        try:
            with self._connection() as connection:
                if reuse:
                    connection.execute("BEGIN IMMEDIATE")
                    existing = connection.execute(
                        "SELECT id FROM autopost_schedules WHERE folder_id = ? AND times_json = ? "
                        "AND destinations_json = ? AND status IN ('active', 'paused') ORDER BY created_at LIMIT 1",
                        (folder_id, json.dumps(times), json.dumps(destinations)),
                    ).fetchone()
                    if existing:
                        connection.execute(
                            "UPDATE autopost_schedules SET status='active', title_template=?, "
                            "hashtags_template=?, videos_per_day=?, updated_at=? WHERE id=?",
                            (title_template, hashtags_template, videos_per_day, created_at, existing["id"]),
                        )
                        return existing["id"]
                connection.execute(
                    """
                    INSERT INTO autopost_schedules (
                        id, folder_id, folder_name, videos_per_day, times_json,
                        destinations_json, title_template, hashtags_template,
                        timezone, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        schedule_id,
                        folder_id,
                        folder_name,
                        videos_per_day,
                        json.dumps(times),
                        json.dumps(destinations),
                        title_template,
                        hashtags_template,
                        timezone,
                        created_at,
                        created_at,
                    ),
                )
        except sqlite3.Error as exc:
            raise AutopostStoreError("Não foi possível salvar o agendamento.") from exc
        return schedule_id

    def get_schedule(self, schedule_id):
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT * FROM autopost_schedules WHERE id = ?",
                    (schedule_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise AutopostStoreError("Não foi possível consultar os agendamentos.") from exc
        return self._schedule_dict(row) if row else None

    def list_schedules(self):
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM autopost_schedules ORDER BY created_at DESC"
                ).fetchall()
        except sqlite3.Error as exc:
            raise AutopostStoreError("Não foi possível consultar os agendamentos.") from exc
        return [self._schedule_dict(row) for row in rows]

    def list_active_schedules(self):
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM autopost_schedules WHERE status = 'active'"
                ).fetchall()
        except sqlite3.Error as exc:
            raise AutopostStoreError("Não foi possível consultar os agendamentos ativos.") from exc
        return [self._schedule_dict(row) for row in rows]

    def list_history(self, limit=50):
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT history.*, schedules.folder_name AS schedule_folder_name
                    FROM autopost_history AS history
                    JOIN autopost_schedules AS schedules ON schedules.id = history.schedule_id
                    ORDER BY history.created_at DESC, history.id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise AutopostStoreError("Não foi possível consultar o histórico de postagens.") from exc
        return [dict(row) for row in rows]

    def update_schedule_status(self, schedule_id, status, updated_at):
        if status not in {"active", "paused", "cancelled"}:
            raise ValueError("Status de agendamento inválido.")
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    UPDATE autopost_schedules
                    SET status = ?, updated_at = ?
                    WHERE id = ? AND status != 'cancelled'
                    """,
                    (status, updated_at, schedule_id),
                )
        except sqlite3.Error as exc:
            raise AutopostStoreError("Não foi possível atualizar o agendamento.") from exc
        return cursor.rowcount > 0

    def claim_slot(self, schedule_id, run_date, run_time, now):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM autopost_schedules WHERE id = ?",
                (schedule_id,),
            ).fetchone()
            if not row or row["status"] != "active":
                connection.rollback()
                return None
            try:
                connection.execute(
                    """
                    INSERT INTO autopost_ticks (
                        schedule_id, run_date, run_time, status, created_at
                    ) VALUES (?, ?, ?, 'processing', ?)
                    """,
                    (schedule_id, run_date, run_time, now),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                return None

            schedule = self._schedule_dict(row)
            videos_today = connection.execute(
                """
                SELECT COUNT(DISTINCT video_id)
                FROM autopost_history
                WHERE schedule_id = ? AND run_date = ?
                """,
                (schedule_id, run_date),
            ).fetchone()[0]
            if videos_today >= schedule["videos_per_day"]:
                message = "O limite de vídeos deste dia já foi atingido."
                self._finish_tick(
                    connection, schedule_id, run_date, run_time, "limit", message, now
                )
                connection.commit()
                return {"schedule": schedule, "limit_reached": True}
            connection.commit()
            return {"schedule": schedule, "limit_reached": False}
        except sqlite3.Error as exc:
            connection.rollback()
            raise AutopostStoreError("Não foi possível reservar o horário do agendamento.") from exc
        finally:
            connection.close()

    @staticmethod
    def _finish_tick(connection, schedule_id, run_date, run_time, status, message, now):
        connection.execute(
            """
            UPDATE autopost_ticks SET status = ?, message = ?
            WHERE schedule_id = ? AND run_date = ? AND run_time = ?
            """,
            (status, message, schedule_id, run_date, run_time),
        )
        connection.execute(
            """
            UPDATE autopost_schedules
            SET last_status = ?, last_message = ?, last_run_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, message, now, now, schedule_id),
        )

    def reserve_next_video(
        self,
        schedule_id,
        run_date,
        run_time,
        candidates,
        posted_video_ids,
        social_posts,
        now,
    ):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            schedule_row = connection.execute(
                "SELECT * FROM autopost_schedules WHERE id = ?",
                (schedule_id,),
            ).fetchone()
            tick = connection.execute(
                """
                SELECT status FROM autopost_ticks
                WHERE schedule_id = ? AND run_date = ? AND run_time = ?
                """,
                (schedule_id, run_date, run_time),
            ).fetchone()
            if (
                not schedule_row
                or schedule_row["status"] != "active"
                or not tick
                or tick["status"] != "processing"
            ):
                connection.rollback()
                return []

            schedule = self._schedule_dict(schedule_row)
            used_video_ids = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT video_id FROM autopost_history WHERE schedule_id = ?",
                    (schedule_id,),
                ).fetchall()
            }
            posted_video_ids = set(posted_video_ids)

            for video in candidates:
                video_id = video.get("id")
                if (
                    not video_id
                    or video_id in used_video_ids
                    or video_id in posted_video_ids
                ):
                    continue
                previous_statuses = social_posts.get(video_id, {})
                platforms = [
                    platform
                    for platform in schedule["destinations"]
                    if previous_statuses.get(platform, {}).get("status")
                    not in {"posted", "inbox", "processing"}
                ]
                if not platforms:
                    continue

                folder_name = video.get("folderName") or schedule["folder_name"]
                video_name = video.get("name") or "Vídeo sem nome"
                for platform in platforms:
                    connection.execute(
                        """
                        INSERT INTO autopost_history (
                            schedule_id, run_date, run_time, video_id, video_name,
                            folder_name, platform, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'agendado', ?, ?)
                        """,
                        (
                            schedule_id,
                            run_date,
                            run_time,
                            video_id,
                            video_name,
                            folder_name,
                            platform,
                            now,
                            now,
                        ),
                    )

                self._finish_tick(
                    connection,
                    schedule_id,
                    run_date,
                    run_time,
                    "scheduled",
                    f"Vídeo reservado: {video_name}",
                    now,
                )
                rows = connection.execute(
                    """
                    SELECT * FROM autopost_history
                    WHERE schedule_id = ? AND run_date = ? AND run_time = ? AND video_id = ?
                    ORDER BY platform
                    """,
                    (schedule_id, run_date, run_time, video_id),
                ).fetchall()
                connection.commit()
                return [dict(row) for row in rows]

            message = "Não há vídeos novos para os destinos escolhidos."
            self._finish_tick(
                connection, schedule_id, run_date, run_time, "empty", message, now
            )
            connection.commit()
            return []
        except sqlite3.Error as exc:
            connection.rollback()
            raise AutopostStoreError("Não foi possível reservar o próximo vídeo.") from exc
        finally:
            connection.close()

    def finish_slot(self, schedule_id, run_date, run_time, status, message, now):
        try:
            with self._connection() as connection:
                self._finish_tick(
                    connection, schedule_id, run_date, run_time, status, message, now
                )
        except sqlite3.Error as exc:
            raise AutopostStoreError("Não foi possível salvar o resultado do horário.") from exc

    def update_history(self, history_id, status, message, remote_id, now):
        if status not in {"postado", "rascunho", "falhou", "analisando", "melhorando", "enviando"}:
            raise ValueError("Status de postagem agendada inválido.")
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    UPDATE autopost_history
                    SET status = ?, message = ?, remote_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, message, remote_id, now, history_id),
                )
                if cursor.rowcount:
                    row = connection.execute(
                        "SELECT schedule_id FROM autopost_history WHERE id = ?",
                        (history_id,),
                    ).fetchone()
                    connection.execute(
                        """
                        UPDATE autopost_schedules
                        SET last_status = ?, last_message = ?, last_run_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (status, message, now, now, row["schedule_id"]),
                    )
        except sqlite3.Error as exc:
            raise AutopostStoreError("Não foi possível atualizar o histórico de postagem.") from exc

