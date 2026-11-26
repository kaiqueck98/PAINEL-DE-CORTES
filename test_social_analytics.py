import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from cryptography.fernet import Fernet

import app
import social_analytics


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300

    def json(self):
        return self.payload


class SocialAnalyticsTests(unittest.TestCase):
    def test_daily_refresh_is_scheduled_server_side(self):
        scheduler = unittest.mock.Mock()
        with patch.object(app, "AUTPOST_SCHEDULER", scheduler), patch.object(
            app, "AUTPOST_SCHEDULER_STARTED", False
        ), patch.object(app.AUTPOST_STORE, "list_active_schedules", return_value=[]):
            app.ensure_autopost_scheduler()

        scheduler.start.assert_called_once_with()
        job = next(call for call in scheduler.add_job.call_args_list
                   if call.kwargs.get("id") == "social-analytics-daily")
        self.assertEqual(job.args[0], app.start_social_analytics_refresh)
        self.assertEqual(job.kwargs["args"], ("daily",))
        self.assertEqual(str(job.kwargs["trigger"].fields[5].expressions[0]), "6")
        self.assertEqual(str(job.kwargs["trigger"].fields[6].expressions[0]), "15")

    def test_unexpected_provider_error_finishes_job_without_exposing_exception_text(self):
        initial_job = {
            "running": True, "message": "", "errors": {},
            "started_at": None, "completed_at": None,
        }
        with patch.object(app, "SOCIAL_ANALYTICS_JOB", initial_job), patch.object(
            app, "_social_analytics_tokens", return_value={"tiktok": {}}
        ), patch.object(
            app.social_publishing, "get_access_token",
            side_effect=RuntimeError("access_token=secret-value"),
        ):
            app._refresh_social_analytics_worker()

        self.assertFalse(initial_job["running"])
        self.assertIn("completed_at", initial_job)
        self.assertNotIn("secret-value", str(initial_job["errors"]))

    def test_instagram_token_and_account_metadata_are_encrypted_in_server_database(self):
        key = Fernet.generate_key().decode("ascii")
        with tempfile.TemporaryDirectory() as directory:
            engine = create_engine(f"sqlite:///{os.path.join(directory, 'tokens.sqlite3')}")
            app.TokenBase.metadata.create_all(engine)
            session_local = sessionmaker(bind=engine, expire_on_commit=False)
            token_data = {
                "access_token": "private-instagram-access-token",
                "refresh_token": "private-instagram-refresh-token",
                "expires_at": None,
                "user_email": None,
                "profile_data": {
                    "account_id": "account-private-id",
                    "username": "diverso_clips",
                    "page_id": "page-private-id",
                },
            }
            with patch.dict(os.environ, {"SOCIAL_TOKEN_ENCRYPTION_KEY": key}), patch.object(
                app, "TokenSessionLocal", session_local
            ):
                app.save_tokens_to_db({"instagram": token_data})
                session_db = session_local()
                row = session_db.query(app.Token).filter_by(provider="instagram").one()
                self.assertNotIn("private-instagram-access-token", row.access_token)
                self.assertNotIn("private-instagram-refresh-token", row.refresh_token)
                self.assertNotIn("account-private-id", row.profile_data)
                session_db.close()
                loaded = app.load_tokens_from_db()["instagram"]
            engine.dispose()

        self.assertEqual(loaded, token_data)

    def test_tiktok_display_api_reports_profile_and_video_counters(self):
        responses = [
            FakeResponse({
                "data": {"user": {
                    "open_id": "account-1", "username": "diverso_clips",
                    "follower_count": 42, "likes_count": 600,
                }},
                "error": {"code": "ok", "message": ""},
            }),
            FakeResponse({
                "data": {"videos": [
                    {"id": "one", "create_time": 1764158400, "title": "Frutas",
                     "view_count": 1000, "like_count": 100, "comment_count": 10,
                     "share_count": 5, "share_url": "https://tiktok.example/one"},
                    {"id": "two", "create_time": 1764158400, "title": "Filmes",
                     "view_count": 500, "like_count": 50, "comment_count": 4,
                     "share_count": 2, "share_url": "https://tiktok.example/two"},
                ], "cursor": 0, "has_more": False},
                "error": {"code": "ok", "message": ""},
            }),
        ]
        with patch.object(social_analytics.requests, "request", side_effect=responses) as request:
            report = social_analytics.fetch_tiktok_analytics("server-token")

        self.assertEqual(report["account_id"], "account-1")
        self.assertEqual(report["username"], "diverso_clips")
        self.assertEqual(report["followers"], 42)
        self.assertEqual(report["views"], 1500)
        self.assertEqual(report["likes"], 600)
        self.assertEqual(report["comments"], 14)
        self.assertEqual(report["shares"], 7)
        self.assertEqual(len(report["media"]), 2)
        self.assertTrue(request.call_args_list[1].kwargs["params"]["fields"].endswith("view_count"))

    def test_instagram_graph_api_reports_profile_media_and_daily_views(self):
        responses = [
            FakeResponse({"id": "ig-1", "username": "diverso_clips", "followers_count": 80}),
            FakeResponse({"data": [{
                "id": "ig-media-1", "caption": "Memes", "media_type": "VIDEO",
                "media_product_type": "REELS", "timestamp": "2026-11-25T12:00:00+0000",
                "total_views_count": 1200, "total_like_count": 130,
                "total_comments_count": 11, "shares_count": 8,
                "permalink": "https://instagram.example/reel/1",
            }]}),
            FakeResponse({"data": [{"name": "views", "values": [
                {"value": 20, "end_time": "2026-11-24T00:00:00+0000"},
                {"value": 35, "end_time": "2026-11-25T00:00:00+0000"},
            ]}]}),
        ]
        with patch.object(social_analytics.requests, "request", side_effect=responses):
            report = social_analytics.fetch_instagram_analytics("server-token", "ig-1")

        self.assertEqual(report["username"], "diverso_clips")
        self.assertEqual(report["followers"], 80)
        self.assertEqual(report["views"], 1200)
        self.assertEqual(report["likes"], 130)
        self.assertEqual(report["comments"], 11)
        self.assertEqual(report["shares"], 8)
        self.assertEqual(report["daily_views"]["2026-11-25"], 35)

    def test_server_snapshots_compute_daily_tiktok_view_delta_and_top_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = create_engine(f"sqlite:///{os.path.join(directory, 'analytics.sqlite3')}")
            app.TokenBase.metadata.create_all(engine)
            session_local = sessionmaker(bind=engine, expire_on_commit=False)
            first_day = datetime(2026, 11, 24, 12, tzinfo=app.AUTPOST_TIMEZONE)
            second_day = datetime(2026, 11, 25, 12, tzinfo=app.AUTPOST_TIMEZONE)
            first_report = {
                "platform": "tiktok", "account_id": "account-1", "username": "diverso_clips",
                "followers": 40, "views": 1000, "likes": 100, "comments": 10, "shares": 5,
                "media": [{"id": "remote-1", "title": "Frutas", "posted_at": "2026-11-20",
                           "views": 1000, "likes": 100, "comments": 10, "shares": 5,
                           "permalink": "https://tiktok.example/1"}],
                "daily_views": {},
            }
            second_report = json.loads(json.dumps(first_report))
            second_report["followers"] = 42
            second_report["views"] = 1150
            second_report["media"][0]["views"] = 1150
            second_report["media"][0]["likes"] = 120
            with patch.object(app, "TokenSessionLocal", session_local):
                with patch.object(app, "schedule_now", return_value=first_day):
                    app.save_social_analytics_snapshot(first_report, {"remote-1": "FRUTAS"})
                with patch.object(app, "schedule_now", return_value=second_day):
                    app.save_social_analytics_snapshot(second_report, {"remote-1": "FRUTAS"})
                    summary = app.social_analytics_summary({
                        "tiktok": {"profile_data": {"account_id": "account-1"}}
                    })
            engine.dispose()

        self.assertEqual(summary["totals"]["views"], "1.150")
        self.assertEqual(summary["totals"]["followers"], "42")
        self.assertEqual(summary["top_videos"][0]["folder"], "FRUTAS")
        self.assertEqual(summary["chart_days"][-1]["tiktok"], 150)
        self.assertIsNone(summary["chart_days"][-2]["tiktok"])


if __name__ == "__main__":
    unittest.main()
