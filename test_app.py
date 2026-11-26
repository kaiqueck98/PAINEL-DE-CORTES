import base64
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
from copy import deepcopy
from unittest.mock import patch

import app


class FakeDriveResponse(io.BytesIO):
    def __init__(self, content, content_type="video/mp4", status=200, headers=None):
        super().__init__(content)
        self.status = status
        self.headers = {
            "Content-Length": str(len(content)),
            "Content-Type": content_type,
        }
        self.headers.update(headers or {})


class DriveFolderTests(unittest.TestCase):
    def test_extracts_folder_id_from_drive_links(self):
        self.assertEqual(
            app.extract_folder_id("https://drive.google.com/drive/folders/abc_123?usp=sharing"),
            "abc_123",
        )
        self.assertEqual(
            app.extract_folder_id("https://drive.google.com/open?id=abc-123"),
            "abc-123",
        )

    def test_rejects_non_drive_links(self):
        with self.assertRaises(ValueError):
            app.extract_folder_id("https://example.com/folders/abc123")


class PanelRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        with self.client.session_transaction() as browser_session:
            browser_session["authenticated"] = True
        self.drive_link_patch = patch.object(
            app,
            "load_drive_folder_link",
            return_value="https://drive.google.com/drive/folders/test-folder",
        )
        self.drive_link_patch.start()
        self.addCleanup(self.drive_link_patch.stop)

    def test_root_shows_only_the_login_form(self):
        client = app.app.test_client()
        response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="user"', response.data)
        self.assertIn(b'name="password"', response.data)
        self.assertIn(b">Entrar no painel \xe2\x86\x92</button>", response.data)
        self.assertIn(b'action="/login"', response.data)
        self.assertIn("O futuro dos cortes começa aqui.".encode(), response.data)
        self.assertNotIn(b"Interface demonstrativa", response.data)
        self.assertNotIn(b"Dashboard", response.data)
        self.assertNotIn(b"Link da pasta do Google Drive", response.data)

        with client.session_transaction() as browser_session:
            csrf_token = browser_session["csrf_token"]
        response = client.post(
            "/login",
            data={"csrf_token": csrf_token, "user": "admin", "password": "incorreta"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b' id="status" role="status">Usu\xc3\xa1rio ou senha inv\xc3\xa1lidos.</p>', response.data)

        with patch.dict(os.environ, {"LOGIN_USER": "admin", "LOGIN_PASS": "diverso123"}):
            response = client.post(
                "/login",
                data={"csrf_token": csrf_token, "user": "admin", "password": "diverso123"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/dashboard")
        with client.session_transaction() as browser_session:
            self.assertTrue(browser_session["authenticated"])

    def test_dashboard_shows_folders_without_rendering_all_videos(self):
        view = {
            "folder_name": "Principal",
            "folders": [{"id": "child123", "name": "Cortes", "video_count": 2507}],
            "videos": [{"id": "video123", "name": "Não mostrar na raiz.mp4"}],
            "video_count": 2508,
            "warnings": [],
        }
        with patch.dict(os.environ, {"DRIVE_API_KEY": "test-key"}), patch.object(
            app, "get_folder_view", return_value=view
        ), patch.object(
            app, "load_drive_folder_link",
            return_value="https://drive.google.com/drive/folders/folder123",
        ):
            response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Cortes", response.data)
        self.assertIn(b"2507 v", response.data)
        self.assertIn(b"page=clips", response.data)
        self.assertNotIn(b"N\xc3\xa3o mostrar na raiz.mp4", response.data)

    def test_home_folder_opens_automatic_cuts_for_that_folder(self):
        view = {
            "folder_name": "Cortes",
            "folders": [{"id": "nested123", "name": "Subcortes", "video_count": 3}],
            "videos": [{
                "id": "video123",
                "name": "Corte pronto.mp4",
                "webViewLink": "https://drive.google.com/file/d/video123/view",
                "videoMediaMetadata": {"durationMillis": "90500", "width": 640, "height": 360},
            }, {
                "id": "video720",
                "name": "Qualidade boa.mp4",
                "videoMediaMetadata": {"durationMillis": "60000", "width": 1280, "height": 720},
            }, {
                "id": "videoReady",
                "name": "Pronto.mp4",
                "videoMediaMetadata": {"durationMillis": "45000"},
            }],
            "video_count": 4,
            "warnings": [],
        }
        with patch.dict(os.environ, {"DRIVE_API_KEY": "test-key"}), patch.object(
            app, "get_folder_view", return_value=view
        ), patch.object(
            app,
            "load_social_posts",
            return_value={
                "video123": {"tiktok": {"status": "posted"}},
                "video720": {"tiktok": {"status": "inbox"}},
            },
        ):
            response = self.client.get("/dashboard?page=home&path=root123,child123")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Subcortes", response.data)
        self.assertIn(b"page=clips&amp;path=root123,child123,nested123", response.data)
        self.assertNotIn(b"Corte pronto.mp4", response.data)

    def test_statistics_use_real_drive_and_posted_counts(self):
        view = {
            "folder_name": "Cortes",
            "folders": [],
            "videos": [
                {"id": "low123", "name": "Teste.mp4", "videoMediaMetadata": {"durationMillis": "5000"}},
                {
                    "id": "best123",
                    "name": "Corte final pronto.mp4",
                    "thumbnailLink": "https://example.test/cover.jpg",
                    "videoMediaMetadata": {"durationMillis": "60000"},
                },
            ],
            "video_count": 2,
            "warnings": [],
        }
        with patch.dict(os.environ, {"DRIVE_API_KEY": "test-key"}), patch.object(
            app, "get_folder_view", side_effect=lambda *_: deepcopy(view)
        ), patch.object(app, "load_good_video_ids", return_value={"best123"}), patch.object(
            app, "load_posted_video_ids", return_value={"best123"}
        ), patch.object(app, "load_social_posts", return_value={}):
            response = self.client.get(
                "/dashboard?page=stats&path=root123,child123"
            )

        self.assertIn(b"Estat\xc3\xadsticas reais", response.data)
        self.assertIn(b"Total de cortes</small><div class=\"metric\">2", response.data)
        self.assertIn(b"Pastas encontradas</small><div class=\"metric\">0", response.data)
        self.assertIn(b"Marcados como postados</small><div class=\"metric\">1", response.data)

    def test_mark_good_persists_video_id_to_json_file(self):
        view = {
            "folder_name": "Cortes",
            "folders": [],
            "videos": [],
            "video_count": 0,
            "warnings": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            bons_file = os.path.join(directory, "bons.json")
            with patch.object(app, "GOOD_VIDEOS_FILE", bons_file), patch.dict(
                os.environ, {"DRIVE_API_KEY": "test-key"}
            ), patch.object(app, "get_folder_view", return_value=view):
                response = self.client.post(
                    "/dashboard",
                    data={
                        "action": "mark_good",
                        "path": "root123,child123",
                        "video_id": "video123",
                        "sort_order": "score",
                        "good_filter": "good",
                    },
                )

            self.assertEqual(response.status_code, 302)
            self.assertIn("good_filter=good", response.location)
            with open(bons_file, "r", encoding="utf-8") as file:
                self.assertEqual(json.load(file), ["video123"])

    def test_saving_folder_model_persists_and_applies_file_number(self):
        view = {
            "folder_name": "Cortes",
            "folders": [],
            "videos": [{
                "id": "video123",
                "name": "corte_08.mp4",
                "folderName": "Cortes",
                "videoMediaMetadata": {"durationMillis": "60000"},
            }],
            "video_count": 1,
            "warnings": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            models_file = os.path.join(directory, "modelos.json")
            with patch.object(app, "FOLDER_MODELS_FILE", models_file), patch.dict(
                os.environ, {"DRIVE_API_KEY": "test-key"}
            ), patch.object(app, "get_folder_view", return_value=view), patch.object(
                app, "load_good_video_ids", return_value=set()
            ):
                saved = self.client.post(
                    "/dashboard",
                    data={
                        "action": "save_model",
                        "path": "root123,folder123",
                        "title_template": "Parte {numero} | Melhor momento 😂",
                        "hashtags_template": "#memes #viral #engraçado",
                    },
                )
                rendered = self.client.get(saved.location)

            self.assertEqual(saved.status_code, 302)
            self.assertIn("value=\"Parte 08 | Melhor momento 😂\"", rendered.get_data(as_text=True))
            self.assertIn("value=\"#memes #viral #engraçado\"", rendered.get_data(as_text=True))
            with open(models_file, "r", encoding="utf-8") as file:
                self.assertEqual(
                    json.load(file)["folder123"],
                    {
                        "title_template": "Parte {numero} | Melhor momento 😂",
                        "hashtags_template": "#memes #viral #engraçado",
                    },
                )

    def test_mark_posted_persists_filters_and_sorts_posted_card_last(self):
        view = {
            "folder_name": "Cortes",
            "folders": [],
            "videos": [
                {"id": "posted123", "name": "Postado.mp4", "videoMediaMetadata": {"durationMillis": "60000"}},
                {"id": "pending123", "name": "Pendente.mp4", "videoMediaMetadata": {"durationMillis": "60000"}},
            ],
            "video_count": 2,
            "warnings": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            posted_file = os.path.join(directory, "postados.json")
            with patch.object(app, "POSTED_VIDEOS_FILE", posted_file), patch.dict(
                os.environ, {"DRIVE_API_KEY": "test-key"}
            ), patch.object(app, "get_folder_view", side_effect=lambda *_: deepcopy(view)), patch.object(
                app, "load_good_video_ids", return_value=set()
            ), patch.object(app, "load_folder_models", return_value={}):
                marked = self.client.post(
                    "/dashboard",
                    data={
                        "action": "mark_posted",
                        "path": "root123,folder123",
                        "video_id": "posted123",
                        "posted_filter": "all",
                    },
                )
                all_videos = self.client.get(marked.location)
                posted_only = self.client.get(
                    "/dashboard?path=root123,folder123&posted_filter=posted"
                )
                pending_only = self.client.get(
                    "/dashboard?path=root123,folder123&posted_filter=pending"
                )

            self.assertEqual(marked.status_code, 302)
            self.assertLess(
                all_videos.data.index(b'<h3 class="video-name">Pendente.mp4'),
                all_videos.data.index(b'<h3 class="video-name">Postado.mp4'),
            )
            self.assertIn(b'class="video posted"', all_videos.data)
            self.assertIn(b"Postado.mp4", posted_only.data)
            self.assertNotIn(b"Pendente.mp4", posted_only.data)
            self.assertIn(b"Pendente.mp4", pending_only.data)
            self.assertNotIn(b"Postado.mp4", pending_only.data)
            with open(posted_file, "r", encoding="utf-8") as file:
                self.assertEqual(json.load(file), ["posted123"])

    def test_download_streams_drive_content_as_attachment(self):
        content = b"video bytes"
        with patch.dict(os.environ, {"DRIVE_API_KEY": "test-key"}), patch.object(
            app, "urlopen", return_value=FakeDriveResponse(content)
        ) as open_url:
            response = self.client.get("/download/video123?filename=meu%20corte.mp4")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, content)
        self.assertIn('attachment; filename="meu_corte.mp4"', response.headers["Content-Disposition"])
        self.assertIn("alt=media", open_url.call_args.args[0].full_url)

    def test_original_preview_streams_range_requests(self):
        response_content = b"234"
        drive_response = FakeDriveResponse(
            response_content,
            status=206,
            headers={"Content-Range": "bytes 2-4/10"},
        )
        with patch.dict(os.environ, {"DRIVE_API_KEY": "test-key"}), patch.object(
            app, "urlopen", return_value=drive_response
        ) as open_url:
            response = self.client.get(
                "/stream/video123?filename=corte.mp4",
                headers={"Range": "bytes=2-4"},
            )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, response_content)
        self.assertEqual(response.headers["Content-Range"], "bytes 2-4/10")
        self.assertIn("inline;", response.headers["Content-Disposition"])
        self.assertEqual(open_url.call_args.args[0].get_header("Range"), "bytes=2-4")

    def test_improved_output_download_keeps_preview_file_available(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = os.path.join(directory, "corte_melhorado.mp4")
            with open(output_path, "wb") as file:
                file.write(b"improved video")
            job_id = "a" * 32
            job = {
                "status": "complete",
                "progress": 100,
                "message": "Vídeo pronto",
                "updated_at": time.time(),
                "output_path": output_path,
                "output_name": "corte_melhorado.mp4",
                "workdir": directory,
            }
            with patch.dict(app.IMPROVEMENT_JOBS, {job_id: job}):
                response = self.client.get(f"/improved/{job_id}")
                self.assertEqual(response.data, b"improved video")
                self.assertIn("corte_melhorado.mp4", response.headers["Content-Disposition"])
                response.close()
                self.assertEqual(job["status"], "complete")
                self.assertTrue(os.path.exists(output_path))

                preview = self.client.get(f"/improved-preview/{job_id}", headers={"Range": "bytes=0-6"})
                try:
                    self.assertEqual(preview.status_code, 206)
                    self.assertEqual(preview.data, b"improve")
                    self.assertTrue(os.path.exists(output_path))
                finally:
                    preview.close()

    def test_improvement_reports_missing_ffmpeg(self):
        with patch.dict(os.environ, {"DRIVE_API_KEY": "test-key"}), patch.object(
            app.shutil, "which", return_value=None
        ):
            response = self.client.post(
                "/improve/video123",
                data={"filename": "corte.mp4", "duration_ms": "60000"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("FFmpeg não encontrado", response.get_json()["error"])

    def test_reports_missing_api_key(self):
        with patch.dict(os.environ, {"DRIVE_API_KEY": ""}), patch.object(
            app, "load_drive_folder_link",
            return_value="https://drive.google.com/drive/folders/folder123",
        ):
            response = self.client.get("/dashboard")

        self.assertIn(b"Configure a vari", response.data)

    def test_social_connection_buttons_are_shown_at_the_top(self):
        response = self.client.get("/configuracao")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"TikTok", response.data)
        self.assertIn(b"YouTube", response.data)
        self.assertNotIn(b"Configure SOCIAL_TOKEN_ENCRYPTION_KEY", response.data)

    def test_tiktok_site_verification_returns_configured_plain_text(self):
        verification_code = "tiktok-site-verification-123"
        with patch.dict(
            os.environ,
            {"TIKTOK_DEVELOPERS_SITE_VERIFICATION_CODE": verification_code},
        ):
            response = self.client.get("/tiktok-developers-site-verification.txt")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/plain")
        self.assertEqual(response.get_data(as_text=True), verification_code)

    def test_tiktok_site_verification_reports_missing_code(self):
        with patch.dict(os.environ, {"TIKTOK_DEVELOPERS_SITE_VERIFICATION_CODE": ""}):
            response = self.client.get("/tiktok-developers-site-verification.txt")

        self.assertEqual(response.status_code, 503)
        self.assertIn(
            b"Configure TIKTOK_DEVELOPERS_SITE_VERIFICATION_CODE",
            response.data,
        )

    def test_privacy_and_terms_pages_render_and_are_linked_from_panel(self):
        privacy_response = self.client.get("/privacy")
        terms_response = self.client.get("/terms")
        home_response = self.client.get("/")
        dashboard_response = self.client.get("/dashboard")

        self.assertEqual(privacy_response.status_code, 200)
        self.assertIn(b"Pol\xc3\xadtica de Privacidade", privacy_response.data)
        self.assertIn(b"tokens sociais", privacy_response.data)
        self.assertEqual(terms_response.status_code, 200)
        self.assertIn(b"Termos de Uso", terms_response.data)
        self.assertIn(b"direitos", terms_response.data)
        self.assertEqual(home_response.status_code, 302)
        self.assertEqual(home_response.location, "/dashboard")
        self.assertEqual(dashboard_response.status_code, 200)
        self.assertIn(b"Home", dashboard_response.data)

    def test_connected_social_accounts_show_status(self):
        with patch.object(
            app.social_publishing,
            "load_social_tokens",
            return_value={
                "tiktok": {"refresh_token": "tiktok-refresh"},
                "youtube": {"refresh_token": "youtube-refresh"},
            },
        ):
            response = self.client.get("/configuracao")

        self.assertIn(b"TikTok conectado \xe2\x9c\x85", response.data)
        self.assertIn(b"YouTube conectado \xe2\x9c\x85", response.data)

    def test_connected_tiktok_shows_inbox_draft_instructions(self):
        with patch.object(
            app.social_publishing,
            "load_social_tokens",
            return_value={"tiktok": {"refresh_token": "refresh"}},
        ):
            response = self.client.get("/configuracao")

        page = response.get_data(as_text=True)
        self.assertIn("caixa de entrada do TikTok", page)
        self.assertIn("Abra a notificação no TikTok para adicionar texto, editar e publicar.", page)
        self.assertNotIn("Privacidade no TikTok", page)

    def test_tiktok_connect_redirects_to_official_oauth_with_state(self):
        from cryptography.fernet import Fernet
        from urllib.parse import parse_qs, urlparse

        with patch.dict(
            os.environ,
            {
                "TIKTOK_CLIENT_KEY": "client-key",
                "TIKTOK_CLIENT_SECRET": "client-secret",
                "TIKTOK_REDIRECT_URI": "http://localhost:5000/oauth/tiktok/callback",
               "SOCIAL_TOKEN_ENCRYPTION_KEY": os.environ.get("SOCIAL_TOKEN_ENCRYPTION_KEY", "")
            },
        ):
            response = self.client.get("/oauth/tiktok")
            with self.client.session_transaction() as session_data:
                state = session_data["oauth_state_tiktok"]
                code_verifier = session_data["oauth_code_verifier_tiktok"]

        self.assertEqual(response.status_code, 302)
        params = parse_qs(urlparse(response.location).query)
        expected_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        self.assertEqual(params["state"], [state])
        self.assertEqual(params["code_challenge"], [expected_challenge])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertEqual(
            params["redirect_uri"],
            ["http://localhost:5000/oauth/tiktok/callback"],
        )
        self.assertGreaterEqual(len(code_verifier), 43)
        self.assertLessEqual(len(code_verifier), 128)

    def test_tiktok_callback_uses_and_clears_session_code_verifier(self):
        with self.client.session_transaction() as session_data:
            session_data["oauth_state_tiktok"] = "state-value"
            session_data["oauth_code_verifier_tiktok"] = "session-verifier"
        with patch.object(
            app.social_publishing, "exchange_authorization_code"
        ) as exchange:
            response = self.client.get(
                "/oauth/tiktok/callback",
                query_string={"state": "state-value", "code": "authorization-code"},
            )
            with self.client.session_transaction() as session_data:
                self.assertNotIn("oauth_state_tiktok", session_data)
                self.assertNotIn("oauth_code_verifier_tiktok", session_data)

        self.assertEqual(response.status_code, 302)
        exchange.assert_called_once_with(
            "tiktok",
            "authorization-code",
            code_verifier="session-verifier",
        )

    def test_tiktok_inbox_upload_requires_csrf_but_not_a_caption(self):
        self.client.get("/dashboard")
        with self.client.session_transaction() as session_data:
            csrf_token = session_data["csrf_token"]
        worker = unittest.mock.Mock()
        with patch.dict(os.environ, {"DRIVE_API_KEY": "test-key"}), patch.object(
            app.social_publishing,
            "load_social_tokens",
            return_value={"tiktok": {"refresh_token": "refresh-token"}},
        ), patch.object(app, "load_social_posts", return_value={}), patch.object(
            app.threading, "Thread", return_value=worker
        ) as thread:
            rejected = self.client.post(
                "/social-post/tiktok/video123",
                data={"filename": "corte.mp4"},
            )
            accepted = self.client.post(
                "/social-post/tiktok/video123",
                data={"filename": "corte.mp4"},
                headers={"X-CSRF-Token": csrf_token},
            )

        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(thread.call_args.kwargs["args"][1:7], (
            "tiktok",
            "video123",
            "corte.mp4",
            "",
            "",
            "test-key",
        ))
        worker.start.assert_called_once()
        with app.SOCIAL_POST_JOBS_LOCK:
            app.SOCIAL_POST_JOBS.pop(accepted.get_json()["job_id"], None)

    def test_tiktok_inbox_upload_cannot_be_started_twice(self):
        self.client.get("/dashboard")
        with self.client.session_transaction() as session_data:
            csrf_token = session_data["csrf_token"]
        worker = unittest.mock.Mock()
        with patch.dict(os.environ, {"DRIVE_API_KEY": "test-key"}), patch.object(
            app.social_publishing,
            "load_social_tokens",
            return_value={"tiktok": {"refresh_token": "refresh-token"}},
        ), patch.object(
            app,
            "load_social_posts",
            return_value={"video123": {"tiktok": {"status": "inbox"}}},
        ), patch.object(app.threading, "Thread", return_value=worker):
            response = self.client.post(
                "/social-post/tiktok/video123",
                data={"filename": "corte.mp4"},
                headers={"X-CSRF-Token": csrf_token},
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("caixa de entrada do TikTok", response.get_json()["error"])
        worker.start.assert_not_called()

    def test_social_post_uses_completed_improvement_and_marks_video_posted(self):
        with tempfile.TemporaryDirectory() as directory:
            improved_path = os.path.join(directory, "corte_melhorado.mp4")
            with open(improved_path, "wb") as file:
                file.write(b"improved")
            workdir = os.path.join(directory, "social-job")
            os.mkdir(workdir)
            job_id = "b" * 32
            with app.SOCIAL_POST_JOBS_LOCK:
                app.SOCIAL_POST_JOBS[job_id] = {
                    "status": "queued",
                    "progress": 0,
                    "message": "",
                    "updated_at": time.time(),
                }
            with patch.object(app.tempfile, "mkdtemp", return_value=workdir), patch.object(
                app, "copy_completed_improvement", return_value=(improved_path, "corte_melhorado.mp4")
            ), patch.object(
                app.social_publishing, "get_access_token", return_value="access-token"
            ), patch.object(
                app.social_publishing, "publish_youtube", return_value="youtube-id"
            ) as publish, patch.object(app, "record_social_post") as record, patch.object(
                app, "mark_video_posted"
            ) as mark_posted:
                app.process_social_post_job(
                    job_id,
                    "youtube",
                    "video123",
                    "corte.mp4",
                    "Título do card",
                    "#hashtags",
                    "drive-key",
                )

            self.assertEqual(publish.call_args.args[:4], (
                improved_path,
                "Título do card",
                "#hashtags",
                "access-token",
            ))
            record.assert_called_once_with("video123", "youtube", "youtube-id")
            mark_posted.assert_called_once_with("video123")
            self.assertEqual(app.SOCIAL_POST_JOBS[job_id]["status"], "complete")
            self.assertEqual(app.SOCIAL_POST_JOBS[job_id]["message"], "Postado no YouTube.")
            with app.SOCIAL_POST_JOBS_LOCK:
                app.SOCIAL_POST_JOBS.pop(job_id, None)

    def test_social_platform_post_status_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            social_posts_file = os.path.join(directory, "publicados.json")
            with patch.object(app, "SOCIAL_POSTS_FILE", social_posts_file):
                app.record_social_post("video123", "tiktok", "publish-id")
                posts = app.load_social_posts()

            self.assertEqual(posts["video123"]["tiktok"]["id"], "publish-id")
            self.assertEqual(posts["video123"]["tiktok"]["status"], "posted")

    def test_tiktok_processing_job_remains_blocked_after_unconfirmed_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = os.path.join(directory, "social-job")
            os.mkdir(workdir)
            job_id = "c" * 32

            def pending_upload(_path, _token, progress, publish_id_callback):
                publish_id_callback("pending-publish-id")
                progress("processing", 90, "Processando")
                raise app.social_publishing.SocialPublishingPending(
                    "TikTok ainda está preparando o rascunho."
                )

            with app.SOCIAL_POST_JOBS_LOCK:
                app.SOCIAL_POST_JOBS[job_id] = {
                    "status": "queued",
                    "progress": 0,
                    "message": "",
                    "updated_at": time.time(),
                }
            with patch.object(app.tempfile, "mkdtemp", return_value=workdir), patch.object(
                app, "copy_completed_improvement", return_value=None
            ), patch.object(
                app, "download_video_for_social_post"
            ), patch.object(
                app.social_publishing, "get_access_token", return_value="access-token"
            ), patch.object(
                app.social_publishing, "upload_tiktok_inbox", side_effect=pending_upload
            ), patch.object(
                app, "record_social_post"
            ) as record, patch.object(app, "mark_video_posted") as mark_posted:
                app.process_social_post_job(
                    job_id,
                    "tiktok",
                    "video123",
                    "corte.mp4",
                    "Título",
                    "#viral",
                    "drive-key",
                )

            record.assert_called_once_with(
                "video123",
                "tiktok",
                "pending-publish-id",
                status="processing",
            )
            mark_posted.assert_not_called()
            self.assertEqual(app.SOCIAL_POST_JOBS[job_id]["status"], "pending")
            with app.SOCIAL_POST_JOBS_LOCK:
                app.SOCIAL_POST_JOBS.pop(job_id, None)

    def test_tiktok_inbox_upload_is_not_marked_as_posted(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = os.path.join(directory, "social-job")
            os.mkdir(workdir)
            job_id = "d" * 32
            with app.SOCIAL_POST_JOBS_LOCK:
                app.SOCIAL_POST_JOBS[job_id] = {
                    "status": "queued",
                    "progress": 0,
                    "message": "",
                    "updated_at": time.time(),
                }
            with patch.object(app.tempfile, "mkdtemp", return_value=workdir), patch.object(
                app, "copy_completed_improvement", return_value=None
            ), patch.object(
                app, "download_video_for_social_post"
            ), patch.object(
                app.social_publishing, "get_access_token", return_value="access-token"
            ), patch.object(
                app.social_publishing,
                "upload_tiktok_inbox",
                return_value=("inbox-publish-id", "inbox"),
            ) as upload, patch.object(
                app, "record_social_post"
            ) as record, patch.object(app, "mark_video_posted") as mark_posted:
                app.process_social_post_job(
                    job_id,
                    "tiktok",
                    "video123",
                    "corte.mp4",
                    "",
                    "",
                    "drive-key",
                )

            upload.assert_called_once()
            self.assertEqual(
                record.call_args_list[-1].args,
                ("video123", "tiktok", "inbox-publish-id"),
            )
            self.assertEqual(record.call_args_list[-1].kwargs, {"status": "inbox"})
            mark_posted.assert_not_called()
            self.assertEqual(app.SOCIAL_POST_JOBS[job_id]["status"], "complete")
            self.assertEqual(app.SOCIAL_POST_JOBS[job_id]["result_status"], "inbox")
            self.assertIn("caixa de entrada do TikTok", app.SOCIAL_POST_JOBS[job_id]["message"])
            with app.SOCIAL_POST_JOBS_LOCK:
                app.SOCIAL_POST_JOBS.pop(job_id, None)


class RecursiveVideoTests(unittest.TestCase):
    def test_builds_folder_counts_recursively_and_continues_after_folder_error(self):
        contents = {
            "root": [
                {"id": "empty", "name": "Vazia", "mimeType": "application/vnd.google-apps.folder"},
                {"id": "clips", "name": "Cortes", "mimeType": "application/vnd.google-apps.folder"},
                {"id": "blocked", "name": "Sem acesso", "mimeType": "application/vnd.google-apps.folder"},
                {"id": "ignored", "name": "documento.pdf", "mimeType": "application/pdf"},
            ],
            "empty": [],
            "clips": [
                {"id": "video1", "name": "corte.WEBM", "mimeType": "video/webm"},
                {"id": "more", "name": "Mais cortes", "mimeType": "application/vnd.google-apps.folder"},
            ],
            "more": [{"id": "video2", "name": "final.mkv", "mimeType": "application/octet-stream"}],
        }
        visited = []

        def contents_for(folder_id, _api_key):
            visited.append(folder_id)
            if folder_id == "blocked":
                raise app.DriveError("Acesso negado")
            return contents[folder_id]

        with patch.object(app, "get_folder_name", return_value="Principal"), patch.object(
            app, "list_folder_contents", side_effect=contents_for
        ):
            view = app.get_folder_view("root", "test-key")

        self.assertEqual(view["video_count"], 2)
        self.assertEqual(
            {folder["name"]: folder["video_count"] for folder in view["folders"]},
            {"Vazia": 0, "Cortes": 2, "Sem acesso": 0},
        )
        self.assertEqual(view["videos"], [])
        self.assertEqual(set(visited), {"root", "empty", "clips", "more", "blocked"})
        self.assertEqual(len(view["warnings"]), 1)
        self.assertIn("Sem acesso", view["warnings"][0])

        with patch.object(app, "get_folder_name", return_value="Cortes"), patch.object(
            app, "list_folder_contents", side_effect=contents_for
        ):
            clips_view = app.get_folder_view("clips", "test-key")
        self.assertEqual([video["id"] for video in clips_view["videos"]], ["video1"])
        self.assertEqual(clips_view["folders"][0]["video_count"], 1)


class VideoSuggestionTests(unittest.TestCase):
    def test_score_varies_with_title_duration_and_good_mark(self):
        baseline = {
            "name": "video.mp4",
            "videoMediaMetadata": {"durationMillis": "5000"},
        }
        specific = {
            "name": "Corte final entrevista.mp4",
            "videoMediaMetadata": {"durationMillis": "60000"},
        }

        self.assertGreater(app.score_video(specific), app.score_video(baseline))
        self.assertGreater(app.score_video(baseline, marked_good=True), app.score_video(baseline))
        self.assertGreater(
            app.score_video({**baseline, "videoMediaMetadata": {"durationMillis": "60000"}}),
            app.score_video(baseline),
        )

    def test_social_suggestions_use_three_folder_based_titles_not_filename(self):
        titles = app.gerar_titulo("Cortes Esportivos (6) - VIDEOS 2", "Final_Golaço.mp4")

        self.assertEqual(len(titles), 3)
        self.assertTrue(all(len(title) <= 70 for title in titles))
        combined = " ".join(titles).casefold()
        self.assertIn("cortes esportivos", combined)
        self.assertNotIn("final golaço", combined)
        self.assertNotIn("golaço", combined)
        self.assertNotIn("mp4", combined)

        title, hashtags = app.suggest_social_copy("Cortes Esportivos", "Final_Golaço.mp4")

        self.assertIn("Cortes Esportivos", title)
        self.assertNotIn("Final Golaço", title)
        self.assertIn("#cortes", hashtags)
        self.assertIn("#esportivos", hashtags)
        self.assertNotIn("#golaco", hashtags)

    def test_file_number_uses_name_number_or_stable_fallback(self):
        self.assertEqual(app.file_number("corte_08.mp4", 2), "08")
        self.assertEqual(app.file_number("corte-final.mp4", 2), "2")

    def test_quality_uses_short_edge_and_720p_threshold(self):
        self.assertEqual(
            app.video_quality({"videoMediaMetadata": {"width": 480, "height": 854}}),
            ("480p", True),
        )
        self.assertEqual(
            app.video_quality({"videoMediaMetadata": {"width": 1280, "height": 720}}),
            ("720p", False),
        )
        self.assertEqual(app.video_quality({}), ("Indisponível", False))


if __name__ == "__main__":
    unittest.main()
