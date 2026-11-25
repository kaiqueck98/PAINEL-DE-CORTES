import ast
import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from urllib.parse import quote
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
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = iter([sample[:5], sample[5:]])
        response.raise_for_status.return_value = None

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

        fake_requests = types.SimpleNamespace(
            get=Mock(return_value=response),
            RequestException=Exception,
            HTTPError=Exception,
        )
        fake_autopost = types.SimpleNamespace(
            _media_tools=Mock(return_value=("ffmpeg", "ffprobe")),
            VideoDownloadError=type("VideoDownloadError", (Exception,), {}),
        )
        fake_subprocess = types.SimpleNamespace(
            Popen=FakeProcess,
            PIPE=subprocess.PIPE,
            CREATE_NO_WINDOW=0,
            TimeoutExpired=subprocess.TimeoutExpired,
        )
        namespace = {
            "os": os,
            "tempfile": tempfile,
            "requests": fake_requests,
            "autopost": fake_autopost,
            "subprocess": fake_subprocess,
            "quote": quote,
            "DRIVE_API_BASE": "https://www.googleapis.com/drive/v3",
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

                fake_requests.get.assert_called_once_with(
                    "https://www.googleapis.com/drive/v3/files/drive-video",
                    params={"alt": "media", "key": "test-drive-key"},
                    headers={"Range": "bytes=0-5242879"},
                    stream=True,
                    timeout=(10, 60),
                )
                self.assertEqual(
                    response.iter_content.call_args.kwargs["chunk_size"],
                    1024 * 1024,
                )
                self.assertEqual(FakeProcess.input_contents, sample)
                self.assertFalse(FakeProcess.input_path.startswith("https://"))
                self.assertFalse(Path(FakeProcess.input_path).exists())
                self.assertTrue(Path(destination).is_file())
                self.assertEqual(progress[-1], 1.0)


if __name__ == "__main__":
    unittest.main()
