import ast
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


class DriveSampleDownloadTests(unittest.TestCase):
    def test_streams_five_megabyte_range_to_local_file_before_ffmpeg(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "download_video_sample_for_analysis"
        )

        sample = b"video sample"
        downloads = []

        def download_drive_video(video_id, api_key, destination, progress_callback=None, max_bytes=None):
            downloads.append((video_id, api_key, max_bytes))
            Path(destination).write_bytes(sample)
            if progress_callback:
                progress_callback(1.0)

        class FakeProcess:
            command = None
            input_path = None
            input_contents = None

            def __init__(self, command, **kwargs):
                if kwargs.get("stdout") != subprocess.PIPE:
                    raise AssertionError("FFmpeg stdout must be streamed for progress.")
                type(self).command = command
                type(self).input_path = command[command.index("-i") + 1]
                type(self).input_contents = Path(self.input_path).read_bytes()
                Path(command[-1]).write_bytes(b"processed preview")
                self.stdout = iter(["out_time_ms=20000000\n"])

            def wait(self, timeout=None):
                if timeout != 120:
                    raise AssertionError("FFmpeg timeout must remain bounded.")
                return 0

        class FakeAutopost:
            _media_tools = Mock(return_value=("ffmpeg", "ffprobe"))
            VideoDownloadError = type("VideoDownloadError", (Exception,), {})

        class FakeSubprocess:
            Popen = FakeProcess
            PIPE = subprocess.PIPE
            CREATE_NO_WINDOW = 0
            TimeoutExpired = subprocess.TimeoutExpired

        namespace = {
            "os": os,
            "tempfile": tempfile,
            "autopost": FakeAutopost,
            "subprocess": FakeSubprocess,
            "download_drive_video": download_drive_video,
        }

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(tempfile, "gettempdir", return_value=directory):
                exec(
                    compile(ast.Module(body=[function], type_ignores=[]), "<app>", "exec"),
                    namespace,
                )
                destination = str(Path(directory) / "preview.mp4")
                progress = []
                namespace["download_video_sample_for_analysis"](
                    {
                        "id": "drive-video",
                        "size": len(sample),
                        "videoMediaMetadata": {"durationMillis": 60000},
                    },
                    "test-drive-key",
                    destination,
                    progress.append,
                )

                self.assertEqual(downloads, [("drive-video", "test-drive-key", 5 * 1024 * 1024)])
                self.assertEqual(FakeProcess.input_contents, sample)
                self.assertFalse(FakeProcess.input_path.startswith("https://"))
                self.assertFalse(Path(FakeProcess.input_path).exists())
                self.assertTrue(Path(destination).is_file())
                self.assertEqual(progress[-1], 1.0)


if __name__ == "__main__":
    unittest.main()
