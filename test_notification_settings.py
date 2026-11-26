import io
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.orm import sessionmaker

import app


class NotificationSettingsTests(unittest.TestCase):
    def setUp(self):
        self.engine = app.create_token_engine("sqlite://")
        app.TokenBase.metadata.create_all(bind=self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )
        self.client = app.app.test_client()
        with self.client.session_transaction() as browser_session:
            browser_session["authenticated"] = True
        self.patches = [
            patch.object(app, "TOKEN_ENGINE", self.engine),
            patch.object(app, "TokenSessionLocal", self.session_factory),
            patch.object(app, "_read_legacy_notification_settings", return_value={}),
            patch.dict(
                os.environ,
                {
                    "DATABASE_URL": "",
                    "TELEGRAM_BOT_TOKEN": "",
                    "TELEGRAM_CHAT_ID": "",
                },
            ),
        ]
        for active_patch in self.patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)
        self.addCleanup(self.engine.dispose)

    def csrf_token(self):
        self.client.get("/agendamento")
        with self.client.session_transaction() as browser_session:
            return browser_session["csrf_token"]

    def test_saving_notification_settings_commits_encrypted_values(self):
        token = "test-bot-token"
        csrf = self.csrf_token()
        form = {key: "" for key in app.NOTIFICATION_FIELDS}
        form.update(
            {
                "csrf_token": csrf,
                "TELEGRAM_BOT_TOKEN": token,
                "TELEGRAM_CHAT_ID": "12345",
            }
        )
        response = self.client.post(
            "/agendamento/notificacoes", data=form, follow_redirects=True
        )

        self.assertIn("Salvo no banco ✅", response.get_data(as_text=True))
        self.assertIn("Telegram configurado ✅", response.get_data(as_text=True))
        saved = app.notification_settings()
        self.assertEqual(saved["TELEGRAM_BOT_TOKEN"], token)
        self.assertEqual(saved["TELEGRAM_CHAT_ID"], "12345")

        with self.session_factory() as session_db:
            row = session_db.get(app.NotificationSetting, 1)
            self.assertIsNotNone(row)
            self.assertNotIn(token, row.value)
            self.assertEqual(
                json.loads(app.social_publishing.decrypt_token_value(row.value))[
                    "TELEGRAM_BOT_TOKEN"
                ],
                token,
            )

    def test_existing_sqlite_notification_settings_are_migrated(self):
        legacy = {key: "" for key in app.NOTIFICATION_FIELDS}
        legacy.update(
            {"TELEGRAM_BOT_TOKEN": "legacy-token", "TELEGRAM_CHAT_ID": "54321"}
        )
        with patch.object(
            app, "_read_legacy_notification_settings", return_value=legacy
        ):
            settings = app.notification_settings()

        self.assertEqual(settings["TELEGRAM_BOT_TOKEN"], "legacy-token")
        with self.session_factory() as session_db:
            row = session_db.get(app.NotificationSetting, 1)
            self.assertIsNotNone(row)
            migrated = json.loads(app.social_publishing.decrypt_token_value(row.value))
        self.assertEqual(migrated["TELEGRAM_CHAT_ID"], "54321")

    def test_environment_credentials_are_shown_as_configured_without_exposing_token(self):
        token = "test-secret-token"
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": "12345"},
        ):
            response = self.client.get("/configuracao")

        page = response.get_data(as_text=True)
        self.assertIn("Telegram configurado ✅", page)
        self.assertIn("TESTAR TELEGRAM", page)
        self.assertNotIn(token, page)

    def test_render_does_not_report_sqlite_as_persistent_configuration_storage(self):
        with patch.dict(os.environ, {"RENDER": "true", "DATABASE_URL": ""}):
            settings, database_error = app.notification_settings_with_status()
            with self.assertRaisesRegex(
                app.NotificationSettingsError, "DATABASE_URL"
            ):
                app.save_notification_settings(settings)

        self.assertIn("DATABASE_URL", database_error)

    def test_analysis_sample_uses_short_low_resolution_even_dimensions(self):
        class SampleProcess:
            def __init__(self):
                self.stdout = iter(["out_time_ms=10000000\n"])

            def wait(self, timeout=None):
                return 0

        class SampleResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size):
                self.chunk_size = chunk_size
                yield b"source sample"

        with tempfile.TemporaryDirectory() as temp_dir:
            destination = os.path.join(temp_dir, "sample.mp4")

            def start_process(command, **kwargs):
                with open(destination, "wb") as sample:
                    sample.write(b"small sample")
                start_process.command = command
                return SampleProcess()

            progress = []
            with patch.object(app.autopost, "_media_tools", return_value=("ffmpeg", "ffprobe")), \
                    patch.object(app.subprocess, "Popen", side_effect=start_process), \
                    patch.object(app.requests, "get", return_value=SampleResponse()) as get:
                app.download_video_sample_for_analysis(
                    {
                        "id": "drive-video",
                        "videoMediaMetadata": {"durationMillis": "90000"},
                    },
                    "drive-key",
                    destination,
                    progress.append,
                )

        command = start_process.command
        self.assertIn("20", command)
        self.assertIn(
            "scale=640:640:force_original_aspect_ratio=decrease,"
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            command,
        )
        self.assertEqual(command[command.index("-threads") + 1], "1")
        self.assertEqual(progress[-1], 1.0)
        self.assertEqual(
            get.call_args.kwargs["headers"]["Range"], "bytes=0-5242879"
        )

    def test_automatic_schedule_still_creates_a_preview(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = app.autopost.AutopostStore(
                os.path.join(temp_dir, "schedules.sqlite3")
            )
            store.initialize()
            with patch.object(app, "AUTPOST_STORE", store), \
                    patch.object(app.autopost, "validar_analise_conteudo"), \
                    patch.object(
                        app.social_publishing,
                        "load_social_tokens",
                        return_value={
                            "youtube": {"refresh_token": "mock"},
                            "tiktok": {"refresh_token": "mock"},
                        },
                    ), \
                    patch.object(
                        app,
                        "load_drive_folder_link",
                        return_value="https://drive.google.com/drive/folders/folder-123",
                    ), \
                    patch.object(app, "get_folder_name", return_value="Vídeos de teste"), \
                    patch.object(app.threading, "Thread"), \
                    patch.object(app, "automatic_warnings", return_value=[]):
                csrf = self.csrf_token()
                post_response = self.client.post(
                    "/agendamento",
                    data={
                        "action": "automatico",
                        "csrf_token": csrf,
                    },
                )
                self.assertEqual(post_response.status_code, 302)
                self.assertEqual(post_response.headers["Location"], "/agendamento")
                response = self.client.get(post_response.headers["Location"])

            page = response.get_data(as_text=True)
            schedules = store.list_schedules()
            self.assertEqual(response.status_code, 200)
            self.assertIn("Análise iniciada", page)
            self.assertEqual(len(schedules), 1)
            self.assertEqual(schedules[0]["status"], "draft")
            self.assertEqual(
                schedules[0]["times"], ["08:00", "12:30", "18:00", "21:00"]
            )
            self.assertNotIn(
                "https://drive.google.com/drive/folders/folder-123", page
            )
            self.assertNotIn('<input name="folder_link"', page)
            self.assertIn("fetch('/agendamento/progresso'", page)
            self.assertIn("setTimeout(refreshContent,5000)", page)
            self.assertIn("Progresso da análise", page)

    def test_postgres_saving_requires_a_persistent_encryption_key(self):
        postgres_engine = SimpleNamespace(
            dialect=SimpleNamespace(name="postgresql")
        )
        with patch.object(app, "TOKEN_ENGINE", postgres_engine), patch.object(
            app, "SOCIAL_TOKEN_ENCRYPTION_KEY_CONFIGURED", False
        ):
            with self.assertRaisesRegex(
                app.NotificationSettingsError, "SOCIAL_TOKEN_ENCRYPTION_KEY"
            ):
                app.save_notification_settings({})

    def test_test_button_sends_saved_credentials(self):
        settings = {key: "" for key in app.NOTIFICATION_FIELDS}
        settings.update(
            {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "12345"}
        )
        app.save_notification_settings(settings)
        csrf = self.csrf_token()

        with patch.object(app, "send_telegram_message") as send_message:
            response = self.client.post(
                "/agendamento/notificacoes/testar",
                data={"csrf_token": csrf},
                follow_redirects=True,
            )

        send_message.assert_called_once_with(
            "test-token",
            "12345",
            "✅ Mensagem de teste do Painel de Cortes.",
        )
        self.assertIn(
            "Telegram: mensagem de teste enviada ✅",
            response.get_data(as_text=True),
        )

    def test_save_failure_is_shown_instead_of_success(self):
        csrf = self.csrf_token()
        form = {key: "" for key in app.NOTIFICATION_FIELDS}
        form["csrf_token"] = csrf
        with patch.object(
            app,
            "save_notification_settings",
            side_effect=app.NotificationSettingsError("Banco indisponível"),
        ):
            with self.assertLogs(app.app.logger.name, level="ERROR"):
                response = self.client.post(
                    "/agendamento/notificacoes", data=form, follow_redirects=True
                )

        page = response.get_data(as_text=True)
        self.assertIn("Erro ao salvar: Banco indisponível", page)
        self.assertNotIn("Salvo no banco ✅", page)

    def test_telegram_sender_posts_message_and_reports_invalid_response(self):
        response = io.BytesIO(b'{"ok": true}')
        with patch.object(app, "urlopen", return_value=response) as open_url:
            app.send_telegram_message("test-token", "12345", "Teste")

        request = open_url.call_args.args[0]
        self.assertEqual(
            json.loads(request.data),
            {"chat_id": "12345", "text": "Teste"},
        )
        self.assertEqual(open_url.call_args.kwargs["timeout"], 12)

        with patch.object(app, "urlopen", return_value=io.BytesIO(b'{"ok": false}')):
            with self.assertRaisesRegex(app.TelegramNotificationError, "não confirmou"):
                app.send_telegram_message("test-token", "12345", "Teste")


if __name__ == "__main__":
    unittest.main()
