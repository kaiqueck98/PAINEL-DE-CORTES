import json
import os
import sqlite3
import uuid
from contextlib import contextmanager


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
        except sqlite3.Error as exc:
            raise AutopostStoreError("Não foi possível inicializar o banco de agendamentos.") from exc

    @staticmethod
    def _schedule_dict(row):
        schedule = dict(row)
        schedule["times"] = json.loads(schedule.pop("times_json"))
        schedule["destinations"] = json.loads(schedule.pop("destinations_json"))
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
    ):
        schedule_id = uuid.uuid4().hex
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO autopost_schedules (
                        id, folder_id, folder_name, videos_per_day, times_json,
                        destinations_json, timezone, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        schedule_id,
                        folder_id,
                        folder_name,
                        videos_per_day,
                        json.dumps(times),
                        json.dumps(destinations),
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
        if status not in {"postado", "rascunho", "falhou"}:
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

