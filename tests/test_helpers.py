"""Offline smoke tests for deterministic helpers; does not call external services."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "translate_video.py"
SPEC = importlib.util.spec_from_file_location("translate_video", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
COSYVOICE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cosyvoice_tts.py"
COSYVOICE_SPEC = importlib.util.spec_from_file_location("cosyvoice_tts", COSYVOICE_SCRIPT)
assert COSYVOICE_SPEC and COSYVOICE_SPEC.loader
COSYVOICE = importlib.util.module_from_spec(COSYVOICE_SPEC)
COSYVOICE_SPEC.loader.exec_module(COSYVOICE)


class HelperTests(unittest.TestCase):
    def test_slugify_removes_path_and_control_characters(self):
        value = MODULE.slugify("  demo/movie: <hello>  ")
        self.assertNotIn("/", value)
        self.assertNotIn(":", value)
        self.assertNotIn("<", value)
        self.assertTrue(value)

    def test_srt_timestamp_formats_milliseconds(self):
        self.assertEqual(MODULE.srt_timestamp(3661.234), "01:01:01,234")
        self.assertEqual(MODULE.srt_timestamp(-1), "00:00:00,000")

    def test_model_json_parser_accepts_plain_json_and_fenced_content(self):
        self.assertEqual(MODULE.parse_json_from_model('{"translations": []}'), {"translations": []})
        self.assertEqual(MODULE.parse_json_from_model('Result: {"translations": []} thanks'), {"translations": []})

    def test_write_srt_preserves_text_and_timing(self):
        segments = [
            {"id": 1, "start": 0.2, "end": 1.1, "zh_text": "你好。"},
            {"id": 2, "start": 1.4, "end": 2.0, "zh_text": "世界。"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.srt"
            MODULE.write_srt(segments, path, duration=3.0)
            data = path.read_text(encoding="utf-8-sig")
        self.assertIn("00:00:00,200 --> 00:00:01,100", data)
        self.assertIn("你好。", data)
        self.assertIn("世界。", data)

    def test_path_with_dots_keeps_full_output_basename(self):
        with tempfile.TemporaryDirectory() as tmp:
            stem = Path(tmp) / "tutorial.v2_zh-dub"
            srt = Path(str(stem) + ".srt")
            self.assertEqual(srt.name, "tutorial.v2_zh-dub.srt")

    def test_parser_accepts_supported_modes(self):
        parser = MODULE.build_parser()
        args = parser.parse_args(["https://example.com/video", "--audio-mode", "background", "--subtitle-mode", "soft", "--tts-provider", "cosyvoice"])
        self.assertEqual(args.audio_mode, "background")
        self.assertEqual(args.subtitle_mode, "soft")
        self.assertEqual(args.tts_provider, "cosyvoice")

    def test_cosyvoice_command_uses_local_model_once(self):
        parser = MODULE.build_parser()
        args = parser.parse_args([
            "input.mp4", "--tts-provider", "cosyvoice", "--cosyvoice-python", "/tmp/python",
            "--cosyvoice-repo", "/tmp/CosyVoice", "--cosyvoice-model-dir", "/tmp/model",
            "--cosyvoice-mode", "zero-shot", "--cosyvoice-prompt-audio", "/tmp/reference.wav",
            "--cosyvoice-prompt-text-file", "/tmp/reference.txt",
        ])
        with tempfile.TemporaryDirectory() as tmp, patch.object(MODULE, "run_command") as run:
            output = MODULE.generate_cosyvoice_clips(args, Path(tmp) / "segments.json", Path(tmp))
        command = run.call_args.args[0]
        self.assertEqual(command[0], "/tmp/python")
        self.assertIn("--prompt-audio", command)
        self.assertTrue(str(output).endswith("tts_segments"))

    def test_cosyvoice_cache_changes_with_translation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            model = root / "model"
            repo.mkdir()
            model.mkdir()
            args = COSYVOICE.build_parser().parse_args([
                "--segments-file", str(root / "segments.json"), "--output-dir", str(root / "audio"),
                "--repo", str(repo), "--model-dir", str(model), "--mode", "sft",
            ])
            self.assertIsNone(COSYVOICE.validate_options(args))
            first = COSYVOICE.config_digest(args, [{"id": 1, "zh_text": "第一句"}], None)
            second = COSYVOICE.config_digest(args, [{"id": 1, "zh_text": "改过的第一句"}], None)
            spoken = COSYVOICE.config_digest(args, [{"id": 1, "zh_text": "第一句", "spoken_text": "口播第一句"}], None)
            self.assertNotEqual(first, second)
            self.assertNotEqual(first, spoken)

    def test_cosyvoice_zero_shot_requires_authorized_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            model = root / "model"
            repo.mkdir()
            model.mkdir()
            args = COSYVOICE.build_parser().parse_args([
                "--segments-file", str(root / "segments.json"), "--output-dir", str(root / "audio"),
                "--repo", str(repo), "--model-dir", str(model), "--mode", "zero-shot",
            ])
            with self.assertRaisesRegex(ValueError, "requires --prompt-audio"):
                COSYVOICE.validate_options(args)

    def test_bilibili_part_url_keeps_requested_part(self):
        self.assertEqual(
            MODULE.bilibili_part("https://www.bilibili.com/video/BV1NAuq6uE8t/?spm_id_from=333&p=2"),
            ("BV1NAuq6uE8t", 2),
        )
        with self.assertRaises(MODULE.PipelineError):
            MODULE.bilibili_part("https://www.bilibili.com/video/BV1NAuq6uE8t?p=0")

    def test_public_bilibili_fallback_selects_part_two(self):
        view = {"rights": {"download": 1, "pay": 0}, "pages": [
            {"page": 1, "cid": 11, "part": "First"}, {"page": 2, "cid": 22, "part": "Second"},
        ]}
        play = {"durl": [{"url": "https://upos.bilivideo.com/second.mp4"}]}
        with tempfile.TemporaryDirectory() as tmp:
            def download(command, **_kwargs):
                Path(command[command.index("-o") + 1]).write_bytes(b"video")

            with patch.object(MODULE, "bilibili_api_json", side_effect=[view, play]) as get, \
                 patch.object(MODULE, "run_command", side_effect=download):
                result = MODULE.download_public_bilibili_part(
                    "https://www.bilibili.com/video/BV1NAuq6uE8t?p=2", Path(tmp)
                )
            self.assertIn("p2_Second", result.name)
            self.assertEqual(get.call_args_list[1].args[1]["cid"], 22)

    def test_bilibili_412_uses_public_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = Path(tmp) / "part2.mp4"
            expected.write_bytes(b"video")
            with patch.object(MODULE, "run_command", side_effect=MODULE.PipelineError("HTTP Error 412")), \
                 patch.object(MODULE, "download_public_bilibili_part", return_value=expected) as fallback:
                actual, source = MODULE.acquire_video(
                    "https://www.bilibili.com/video/BV1NAuq6uE8t?p=2", Path(tmp), None
                )
            self.assertEqual(actual, expected.resolve())
            self.assertIn("?p=2", source)
            fallback.assert_called_once()

    def test_translation_polish_keeps_ids_and_context(self):
        calls = []

        class Response:
            status_code = 200

            def __init__(self, text):
                self.text = text

            def json(self):
                return {"choices": [{"message": {"content": self.text}}]}

        def post(_url, **kwargs):
            calls.append(kwargs["json"])
            result = (["直译一句", "直译二句"] if len(calls) == 1 else ["自然的一句。", "顺口的第二句。"])
            return Response(json.dumps({"translations": [
                {"id": index, "text": text} for index, text in enumerate(result, 1)
            ]}, ensure_ascii=False))

        segments = [
            {"id": 1, "start": 0.0, "end": 2.0, "source_text": "First fragment"},
            {"id": 2, "start": 2.0, "end": 4.0, "source_text": "finishes the thought"},
        ]
        with patch.object(MODULE.requests, "post", side_effect=post):
            result = MODULE.translate_openai(segments, "model", "https://example.com/v1", "key", "软件开发")
        self.assertEqual(result, ["自然的一句。", "顺口的第二句。"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(json.loads(calls[0]["messages"][1]["content"])["video_topic"], "软件开发")
        self.assertEqual(json.loads(calls[1]["messages"][1]["content"])["segments"][0]["draft_chinese"], "直译一句")

    def test_translation_rejects_missing_segment(self):
        class Response:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": '{"translations":[{"id":2,"text":"漏了一句"}]}'}}]}

        with patch.object(MODULE.requests, "post", return_value=Response()):
            with self.assertRaisesRegex(MODULE.PipelineError, "segment IDs"):
                MODULE.request_translation_batch("https://example.com", "model", "key", "system", {}, [1, 2])

    def test_local_ollama_translation_uses_two_passes_without_api(self):
        segments = [
            {"id": 1, "start": 0.0, "end": 2.0, "source_text": "First fragment"},
            {"id": 2, "start": 2.0, "end": 4.0, "source_text": "finishes the thought"},
        ]
        outputs = [
            '{"translations":[{"id":1,"text":"初稿一"},{"id":2,"text":"初稿二"}]}',
            '{"translations":[{"id":1,"text":"自然的一句。"},{"id":2,"text":"顺口的第二句。"}]}',
        ]
        from subprocess import CompletedProcess
        with patch.object(MODULE.shutil, "which", return_value="/usr/local/bin/ollama"), \
             patch.object(MODULE, "run_command", side_effect=[CompletedProcess([], 0, item, "") for item in outputs]) as run, \
             patch.dict(MODULE.os.environ, {"OPENAI_API_KEY": "unused-test-key", "DEEPSEEK_API_KEY": "unused-test-key"}):
            provider = MODULE.translate_segments(segments, "auto", "软件开发", "local-model")
        self.assertEqual(provider, "ollama")
        self.assertEqual([item["zh_text"] for item in segments], ["自然的一句。", "顺口的第二句。"])
        self.assertEqual(run.call_count, 2)
        self.assertIn("初稿一", run.call_args.args[0][-1])
        self.assertEqual(run.call_args.args[0][:3], ["ollama", "run", "local-model"])

    def test_local_ollama_rejects_reordered_ids(self):
        from subprocess import CompletedProcess
        payload = '{"translations":[{"id":2,"text":"第二句"},{"id":1,"text":"第一句"}]}'
        with patch.object(MODULE, "run_command", return_value=CompletedProcess([], 0, payload, "")):
            with self.assertRaisesRegex(MODULE.PipelineError, "segment IDs"):
                MODULE.request_ollama_batch("local-model", "translate", {}, [1, 2])

    def test_auto_translation_never_uses_external_service_implicitly(self):
        segments = [{"id": 1, "start": 0.0, "end": 2.0, "source_text": "Hello"}]
        with patch.dict(MODULE.os.environ, {"OPENAI_API_KEY": "unused-test-key", "DEEPSEEK_API_KEY": "unused-test-key"}), \
             patch.object(MODULE.requests, "post") as post:
            with self.assertRaisesRegex(MODULE.PipelineError, "Choose --translation-provider"):
                MODULE.translate_segments(segments, "auto")
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
