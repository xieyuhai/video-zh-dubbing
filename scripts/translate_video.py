#!/usr/bin/env python3
"""Translate a video into Chinese speech and export a complete MP4."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
load_dotenv(SKILL_DIR / ".env")

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".flv", ".avi", ".m4v", ".ts"}
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
DRAFT_TRANSLATION_PROMPT = (
    "Translate spoken English into natural, concise Simplified Chinese for synchronized narration. "
    "Read neighboring lines as one thought before translating fragments. Use idiomatic spoken Chinese, "
    "not word-for-word syntax or stiff calques. Keep each line short enough for its seconds value. "
    "Use the video topic to resolve likely speech-recognition errors and keep technical terms consistent. "
    "Preserve factual meaning, names, numbers, code identifiers, and the speaker's level of certainty. "
    "Do not invent details or omit a segment. Return JSON only: "
    '{"translations":[{"id":1,"text":"中文"}]}'
)
POLISH_TRANSLATION_PROMPT = (
    "Edit Chinese dubbing lines as a native Mandarin audiovisual editor. "
    "Fix translationese, awkward fragments, inconsistent technical terms, and lines too long for their time slots. "
    "Use neighboring English and the video topic for context. Keep the original meaning, facts, names, "
    "numbers, code identifiers, and tone; do not invent or omit content. "
    "Return one polished line per input id, in exactly the same order, as JSON only: "
    '{"translations":[{"id":1,"text":"中文"}]}'
)


class PipelineError(RuntimeError):
    """An actionable pipeline failure."""


def run_command(command: list[str], *, capture: bool = False, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise PipelineError(f"Required command not found: {command[0]}. Check installation and PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise PipelineError(f"Command timed out: {' '.join(command[:4])} ...") from exc
    except subprocess.CalledProcessError as exc:
        output = "\n".join(part for part in (exc.stdout, exc.stderr) if part)
        if len(output) > 6000:
            output = output[-6000:]
        raise PipelineError(f"Command failed (exit {exc.returncode}): {' '.join(command[:5])}\n{output}") from exc


def require_binary(name: str) -> None:
    if not shutil.which(name):
        raise PipelineError(f"{name} is required but was not found on PATH. Install FFmpeg and ensure {name} is available.")


def probe_media(path: Path) -> dict[str, Any]:
    result = run_command(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture=True,
        timeout=60,
    )
    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"ffprobe returned invalid metadata for {path}") from exc
    streams = metadata.get("streams", [])
    fmt = metadata.get("format", {})
    try:
        duration = float(fmt.get("duration") or max(float(s.get("duration", 0)) for s in streams))
    except (ValueError, TypeError, StopIteration):
        duration = 0.0
    if duration <= 0:
        raise PipelineError(f"Unable to determine a valid duration for {path}")
    return {"duration": duration, "streams": streams, "format": fmt}


def slugify(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", value)
    value = re.sub(r"\s+", "_", value.strip())
    value = re.sub(r"_+", "_", value).strip("._ ")
    return (value or "video")[:100]


def bilibili_part(source: str) -> tuple[str, int] | None:
    parsed = urlparse(source)
    if parsed.hostname not in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}:
        return None
    match = re.fullmatch(r"/video/(BV[0-9A-Za-z]+)(?:/)?", parsed.path)
    if not match:
        return None
    try:
        part = int(parse_qs(parsed.query).get("p", ["1"])[0])
    except ValueError as exc:
        raise PipelineError("Bilibili part parameter must be a positive integer.") from exc
    if part < 1:
        raise PipelineError("Bilibili part parameter must be a positive integer.")
    return match.group(1), part


def bilibili_api_json(path: str, params: dict[str, Any]) -> dict[str, Any]:
    url = f"https://api.bilibili.com{path}?{urlencode(params)}"
    result = run_command([
        "curl", "-sS", "-L", "--fail", "--max-time", "30", "-H", "Referer: https://www.bilibili.com/",
        "-H", "User-Agent: Mozilla/5.0", url,
    ], capture=True, timeout=35)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PipelineError("Bilibili API returned invalid JSON.") from exc
    if payload.get("code") != 0:
        raise PipelineError(f"Bilibili API rejected the request: {payload.get('message', 'unknown error')}")
    return payload["data"]


def download_public_bilibili_part(source: str, work_dir: Path) -> Path:
    target = bilibili_part(source)
    if target is None:
        raise PipelineError("Bilibili fallback requires a standard BV video URL.")
    bvid, part = target
    try:
        data = bilibili_api_json("/x/web-interface/view", {"bvid": bvid})
        if data["rights"].get("download") != 1 or data["rights"].get("pay"):
            raise PipelineError("Bilibili did not mark this video as publicly downloadable.")
        page = next((item for item in data["pages"] if item["page"] == part), None)
        if page is None:
            raise PipelineError(f"Bilibili video has no part {part}.")
        play_data = bilibili_api_json(
            "/x/player/playurl", {"bvid": bvid, "cid": page["cid"], "qn": 64, "fnval": 0}
        )
        urls = play_data["durl"]
    except (KeyError, TypeError, ValueError) as exc:
        raise PipelineError(f"Bilibili public download metadata was unavailable: {exc}") from exc
    if len(urls) != 1:
        raise PipelineError("Bilibili did not provide a single public MP4 for this part.")
    media_url = urls[0]["url"]
    media_host = urlparse(media_url).hostname or ""
    if not media_url.startswith("https://") or not media_host.endswith((".bilivideo.com", ".bilivideo.cn")):
        raise PipelineError("Bilibili returned an unexpected media URL.")
    output = work_dir / f"{bvid}_p{part}_{slugify(page['part'])}.mp4"
    run_command([
        "curl", "-sS", "-L", "--fail", "--max-time", "1200", "-H", "Referer: https://www.bilibili.com/",
        "-H", "User-Agent: Mozilla/5.0", "-o", str(output), media_url,
    ], timeout=1210)
    if not output.is_file() or output.stat().st_size == 0:
        raise PipelineError("Bilibili public MP4 download produced no file.")
    return output


def acquire_video(source: str, work_dir: Path, cookies_browser: str | None) -> tuple[Path, str | None]:
    if re.match(r"^https?://", source.strip(), flags=re.IGNORECASE):
        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "--no-warnings",
            "--no-progress",
            "--merge-output-format",
            "mp4",
            "-o",
            str(work_dir / "%(title).120B.%(ext)s"),
        ]
        if cookies_browser:
            command.extend(["--cookies-from-browser", cookies_browser])
        command.append(source)
        print("[1/8] Downloading source video with yt-dlp...", flush=True)
        try:
            run_command(command, capture=True, timeout=60 * 60 * 4)
        except PipelineError as exc:
            if "HTTP Error 412" not in str(exc) or bilibili_part(source) is None or cookies_browser:
                raise
            print("  Bilibili page returned 412; trying its public download API...", flush=True)
            return download_public_bilibili_part(source, work_dir).resolve(), source
        candidates = [p for p in work_dir.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS and not p.name.endswith(".part")]
        if not candidates:
            raise PipelineError(
                "yt-dlp finished but no video file was found. Update yt-dlp, verify the URL and account access, "
                "or download the video through a permitted method and pass the local file path instead."
            )
        selected = max(candidates, key=lambda p: p.stat().st_mtime)
        return selected.resolve(), source
    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise PipelineError(f"Input is neither an HTTP(S) URL nor an existing local file: {source}")
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise PipelineError(f"Unsupported local file extension {path.suffix!r}. Supported video extensions: {', '.join(sorted(VIDEO_EXTENSIONS))}")
    print("[1/8] Using local source video...", flush=True)
    return path, None


def extract_audio(source_video: Path, work_dir: Path, has_audio: bool) -> tuple[Path, Path]:
    if not has_audio:
        raise PipelineError("The source has no audio stream, so speech recognition and dubbing cannot proceed.")
    full_audio = work_dir / "source_audio_44100.wav"
    asr_audio = work_dir / "source_audio_16000_mono.wav"
    print("[2/8] Extracting audio...", flush=True)
    run_command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source_video), "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(full_audio)], timeout=60 * 60)
    run_command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source_video), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(asr_audio)], timeout=60 * 60)
    return full_audio, asr_audio


def transcribe_audio(audio_path: Path, model_size: str, source_language: str, device: str, compute_type: str) -> list[dict[str, Any]]:
    print(f"[3/8] Transcribing speech locally with faster-whisper ({model_size})...", flush=True)
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise PipelineError("Missing faster-whisper. Install dependencies with: python -m pip install -r requirements.txt") from exc
    try:
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
        language = None if source_language == "auto" else source_language
        segments_iter, info = model.transcribe(
            str(audio_path),
            language=language,
            task="transcribe",
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=True,
        )
        segments: list[dict[str, Any]] = []
        for item in segments_iter:
            text = re.sub(r"\s+", " ", (item.text or "")).strip()
            if not text:
                continue
            start = max(0.0, float(item.start))
            end = max(start + 0.15, float(item.end))
            segments.append({
                "id": len(segments) + 1,
                "start": start,
                "end": end,
                "source_text": text,
                "language": getattr(info, "language", source_language),
                "avg_logprob": getattr(item, "avg_logprob", None),
                "no_speech_prob": getattr(item, "no_speech_prob", None),
            })
    except Exception as exc:
        raise PipelineError(
            f"Speech recognition failed: {exc}\nTry --model small or --source-language auto, and check free disk/memory."
        ) from exc
    if not segments:
        raise PipelineError("No speech was detected. Try --source-language auto, a larger model, or verify the audio.")
    print(f"  Recognized {len(segments)} speech segments; detected language: {getattr(info, 'language', 'unknown')}", flush=True)
    return segments


def parse_json_from_model(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    left, right = text.find("{"), text.rfind("}")
    if left >= 0 and right > left:
        try:
            parsed = json.loads(text[left:right + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    raise PipelineError("Translation model returned invalid JSON. Retry or choose another model/provider.")


def validated_translations(body: str, expected_ids: list[int]) -> list[str]:
    entries = parse_json_from_model(body).get("translations")
    if not isinstance(entries, list):
        raise PipelineError("Translation JSON is missing the 'translations' array.")
    ids = [entry.get("id") for entry in entries if isinstance(entry, dict)]
    if ids != expected_ids or len(entries) != len(expected_ids):
        raise PipelineError("Translation changed, omitted, or reordered segment IDs.")
    texts = [entry.get("text") for entry in entries]
    if any(not isinstance(value, str) or not value.strip() for value in texts):
        raise PipelineError("Translation returned an empty or invalid segment.")
    return [value.strip() for value in texts]


def request_translation_batch(endpoint: str, model: str, api_key: str, system: str,
                              data: dict[str, Any], expected_ids: list[int]) -> list[str]:
    try:
        response = requests.post(
            endpoint,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
                ],
            },
            timeout=180,
        )
        if response.status_code >= 400:
            raise PipelineError(f"Translation API returned HTTP {response.status_code}.")
        body = response.json()["choices"][0]["message"]["content"]
        return validated_translations(body, expected_ids)
    except PipelineError:
        raise
    except (requests.RequestException, ValueError, KeyError, TypeError, IndexError) as exc:
        raise PipelineError(f"Translation API request failed: {exc}. Check credentials, endpoint, and model.") from exc


def translate_openai(segments: list[dict[str, Any]], model: str, base_url: str, api_key: str,
                     video_context: str) -> list[str]:
    endpoint = base_url.rstrip("/") + "/chat/completions"
    translated: list[str] = []
    batch_size = 20
    print(f"[4/8] Translating and polishing Simplified Chinese using OpenAI-compatible API ({model})...", flush=True)
    for offset in range(0, len(segments), batch_size):
        batch = segments[offset:offset + batch_size]
        context = {
            "video_topic": video_context,
            "preceding_english": [s["source_text"] for s in segments[max(0, offset - 3):offset]],
            "following_english": [s["source_text"] for s in segments[offset + len(batch):offset + len(batch) + 3]],
        }
        lines = [{"id": s["id"], "english": s["source_text"], "seconds": round(s["end"] - s["start"], 2)} for s in batch]
        ids = [s["id"] for s in batch]
        draft = request_translation_batch(endpoint, model, api_key, DRAFT_TRANSLATION_PROMPT, {**context, "segments": lines}, ids)
        review_lines = [{**line, "draft_chinese": text} for line, text in zip(lines, draft)]
        polished = request_translation_batch(endpoint, model, api_key, POLISH_TRANSLATION_PROMPT,
                                             {**context, "segments": review_lines}, ids)
        translated.extend(polished)
        print(f"  Polished {len(translated)}/{len(segments)} segments", flush=True)
    return translated


def request_ollama_batch(model: str, instruction: str, data: dict[str, Any], expected_ids: list[int]) -> list[str]:
    prompt = f"{instruction}\n\nInput JSON:\n{json.dumps(data, ensure_ascii=False)}"
    try:
        result = run_command(["ollama", "run", model, "--format", "json", "--think=false", prompt], capture=True, timeout=900)
    except PipelineError as exc:
        raise PipelineError(f"Local Ollama translation failed: {exc}. Check that Ollama is running and the model is installed.") from exc
    return validated_translations(result.stdout, expected_ids)


def translate_ollama(segments: list[dict[str, Any]], model: str, video_context: str) -> list[str]:
    if not shutil.which("ollama"):
        raise PipelineError("The ollama command is missing from PATH. Install Ollama or choose another translation provider.")
    if not model:
        raise PipelineError("Set --ollama-model to an installed local model; no model download is started automatically.")
    translated: list[str] = []
    print(f"[4/8] Translating and polishing Simplified Chinese locally with Ollama ({model})...", flush=True)
    for offset in range(0, len(segments), 20):
        batch = segments[offset:offset + 20]
        context = {
            "video_topic": video_context,
            "preceding_english": [s["source_text"] for s in segments[max(0, offset - 3):offset]],
            "following_english": [s["source_text"] for s in segments[offset + len(batch):offset + len(batch) + 3]],
        }
        lines = [{"id": s["id"], "english": s["source_text"], "seconds": round(s["end"] - s["start"], 2)} for s in batch]
        ids = [s["id"] for s in batch]
        draft = request_ollama_batch(model, DRAFT_TRANSLATION_PROMPT, {**context, "segments": lines}, ids)
        review_lines = [{**line, "draft_chinese": text} for line, text in zip(lines, draft)]
        translated.extend(request_ollama_batch(model, POLISH_TRANSLATION_PROMPT,
                                                {**context, "segments": review_lines}, ids))
        print(f"  Polished {len(translated)}/{len(segments)} segments", flush=True)
    return translated


def translate_google(segments: list[dict[str, Any]]) -> list[str]:
    try:
        from deep_translator import GoogleTranslator
    except ImportError as exc:
        raise PipelineError("Missing deep-translator. Install dependencies with: python -m pip install -r requirements.txt") from exc
    translator = GoogleTranslator(source="auto", target="zh-CN")
    translations: list[str] = []
    print("[4/8] Translating into Simplified Chinese using the no-key Google Translate adapter...", flush=True)
    for index, segment in enumerate(segments, 1):
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                text = translator.translate(segment["source_text"])
                if text and text.strip():
                    translations.append(text.strip())
                    last_error = None
                    break
                last_error = RuntimeError("translator returned empty text")
            except Exception as exc:  # external service can rate-limit
                last_error = exc
            if attempt < 2:
                time.sleep(0.8 * (attempt + 1))
        if last_error is not None:
            raise PipelineError(
                f"Google Translate failed at segment {index}/{len(segments)}: {last_error}. "
                "Retry later or configure OPENAI_API_KEY and use --translation-provider openai."
            )
        if index % 25 == 0 or index == len(segments):
            print(f"  Translated {index}/{len(segments)} segments", flush=True)
    return translations


def translate_segments(segments: list[dict[str, Any]], provider: str, video_context: str = "",
                       ollama_model: str = "") -> str:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    deepseek_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if provider == "auto":
        if not ollama_model:
            raise PipelineError(
                "Auto translation needs an installed local model via --ollama-model or OLLAMA_MODEL. "
                "Choose --translation-provider google or openai explicitly to send transcript text to an external service."
            )
        provider = "ollama"
    if provider == "openai":
        if not api_key and deepseek_key:
            api_key = deepseek_key
            model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip()
            base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
        else:
            model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
            base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
        if not api_key:
            raise PipelineError("Natural translation requires OPENAI_API_KEY or DEEPSEEK_API_KEY.")
        translations = translate_openai(segments, model, base_url, api_key, video_context)
    elif provider == "google":
        translations = translate_google(segments)
    elif provider == "ollama":
        translations = translate_ollama(segments, ollama_model, video_context)
    else:
        raise PipelineError(f"Unsupported translation provider: {provider}")
    for segment, text in zip(segments, translations):
        segment["zh_text"] = text
    return provider


def srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def write_srt(segments: list[dict[str, Any]], path: Path, duration: float) -> None:
    lines: list[str] = []
    for index, segment in enumerate(segments):
        start = min(max(0.0, segment["start"]), duration)
        candidate_end = max(segment["end"], start + 0.5)
        if index + 1 < len(segments):
            next_start = float(segments[index + 1]["start"])
            if next_start > start + 0.15:
                candidate_end = min(candidate_end, next_start)
        end = min(duration, max(start + 0.25, candidate_end))
        if end <= start:
            continue
        lines.extend([str(len(lines) // 4 + 1), f"{srt_timestamp(start)} --> {srt_timestamp(end)}", segment["zh_text"], ""])
    path.write_text("\n".join(lines), encoding="utf-8-sig")
    if not lines:
        raise PipelineError("The translated SRT ended up empty; cannot create a complete subtitle output.")


async def edge_tts_save(text: str, voice: str, output: Path, rate: str) -> None:
    import edge_tts
    communicator = edge_tts.Communicate(text=text, voice=voice, rate=rate)
    await communicator.save(str(output))


def dashscope_tts_save(text: str, voice: str, output: Path) -> None:
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise PipelineError("DASHSCOPE_API_KEY is required for DashScope TTS.")
    endpoint = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": "qwen3-tts-flash", "input": {"text": text, "voice": voice, "language_type": "Chinese"}},
                timeout=120,
            )
            response.raise_for_status()
            audio_url = response.json()["output"]["audio"]["url"]
            if audio_url.startswith("http://"):
                audio_url = "https://" + audio_url[len("http://"):]
            if not audio_url.startswith("https://"):
                raise PipelineError("DashScope returned an invalid audio URL.")
            run_command(["curl", "-sS", "-L", "--fail", "--max-time", "120", "-o", str(output), audio_url], timeout=130)
            if not output.is_file() or output.stat().st_size == 0:
                raise PipelineError("DashScope returned empty audio.")
            return
        except (requests.RequestException, KeyError, ValueError, PipelineError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(attempt + 1)
    raise PipelineError(f"DashScope TTS failed: {last_error}")


def generate_cosyvoice_clips(args: argparse.Namespace, segments_path: Path, work_dir: Path) -> Path:
    output_dir = work_dir / "tts_segments"
    command = [
        args.cosyvoice_python, str(SCRIPT_DIR / "cosyvoice_tts.py"),
        "--segments-file", str(segments_path),
        "--output-dir", str(output_dir),
        "--repo", str(Path(args.cosyvoice_repo).expanduser()),
        "--model-dir", str(Path(args.cosyvoice_model_dir).expanduser()),
        "--mode", args.cosyvoice_mode,
        "--speaker", args.cosyvoice_speaker,
        "--speed", str(args.cosyvoice_speed),
    ]
    if args.cosyvoice_prompt_audio:
        command += ["--prompt-audio", str(Path(args.cosyvoice_prompt_audio).expanduser())]
    if args.cosyvoice_prompt_text_file:
        command += ["--prompt-text-file", str(Path(args.cosyvoice_prompt_text_file).expanduser())]
    run_command(command, timeout=7200)
    return output_dir


def normalize_audio(audio: Any, duration_ms: int) -> Any:
    from pydub import AudioSegment
    audio = audio.set_frame_rate(44100).set_channels(2)
    if len(audio) < duration_ms:
        audio += AudioSegment.silent(duration=duration_ms - len(audio), frame_rate=44100).set_channels(2)
    return audio[:duration_ms]


def make_voice_track(segments: list[dict[str, Any]], duration: float, work_dir: Path, voice: str, tts_rate: str,
                     tts_provider: str, warnings: list[str], reuse_tts_dir: Path | None = None) -> Path:
    from pydub import AudioSegment

    print(f"[5/8] Generating Chinese speech ({voice}) and aligning it to the original timeline...", flush=True)
    track_duration_ms = max(1, int(round(duration * 1000)))
    voice_track = AudioSegment.silent(duration=track_duration_ms, frame_rate=44100).set_channels(2)
    tts_dir = work_dir / "tts_segments"
    tts_dir.mkdir(parents=True, exist_ok=True)
    render_segments = segments
    for i, segment in enumerate(render_segments):
        text = segment["zh_text"].strip()
        if not text:
            raise PipelineError(f"Translation for segment {segment['id']} is empty; refusing to skip speech silently.")
        clip_name = f"segment_{segment['id']:05d}.{'wav' if tts_provider in {'dashscope', 'cosyvoice'} else 'mp3'}"
        tts_clip = (reuse_tts_dir or tts_dir) / clip_name
        fitted_wav = tts_dir / f"segment_{segment['id']:05d}_fit.wav"
        if reuse_tts_dir:
            if not tts_clip.is_file() or tts_clip.stat().st_size == 0:
                raise PipelineError(f"Missing cached speech for segment {segment['id']}: {tts_clip}")
        else:
            try:
                if tts_provider == "dashscope":
                    dashscope_tts_save(text, voice, tts_clip)
                elif tts_provider == "cosyvoice":
                    raise PipelineError("CosyVoice clips must be generated before audio alignment")
                else:
                    asyncio.run(edge_tts_save(text, voice, tts_clip, tts_rate))
            except Exception as exc:
                raise PipelineError(f"{tts_provider} TTS failed at segment {segment['id']}: {exc}") from exc

        clip = AudioSegment.from_file(tts_clip).set_frame_rate(44100).set_channels(2)
        start_ms = max(0, int(round(segment["start"] * 1000)))
        if start_ms >= track_duration_ms:
            warnings.append(f"Segment {segment['id']} starts after the video duration and was not overlaid.")
            continue
        next_start = render_segments[i + 1]["start"] if i + 1 < len(render_segments) else duration
        next_deadline_ms = min(track_duration_ms, int(round(max(segment["start"], next_start) * 1000)))
        natural_end_ms = int(round(max(segment["end"], segment["start"] + 0.35) * 1000)) + 180
        target_end_ms = min(track_duration_ms, next_deadline_ms, max(start_ms + 300, natural_end_ms, start_ms + int(len(clip) / 1.5)))
        budget_ms = max(180, target_end_ms - start_ms)
        if start_ms + budget_ms > track_duration_ms:
            budget_ms = max(1, track_duration_ms - start_ms)

        if len(clip) > budget_ms:
            requested_factor = len(clip) / max(1, budget_ms)
            factor = min(1.65, max(1.0, requested_factor))
            if factor > 1.01:
                run_command([
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(tts_clip),
                    "-filter:a", f"atempo={factor:.4f},aresample=44100", "-ac", "2", str(fitted_wav),
                ], timeout=120)
                clip = AudioSegment.from_file(fitted_wav).set_frame_rate(44100).set_channels(2)
            if len(clip) > budget_ms:
                if len(clip) - budget_ms > 50:
                    warnings.append(
                        f"Segment {segment['id']} did not fit its timing window (voice {len(clip)/1000:.2f}s, "
                        f"budget {budget_ms/1000:.2f}s); the tail was trimmed. Review this line."
                    )
                clip = clip[:budget_ms]
            elif requested_factor > 1.25:
                warnings.append(f"Segment {segment['id']} was accelerated {factor:.2f}x to fit; review speaking pace.")
        if len(clip) < 80:
            warnings.append(f"Segment {segment['id']} has unusually short synthesized speech; review its translation.")
        voice_track = voice_track.overlay(clip, position=start_ms)
        if (i + 1) % 25 == 0 or i + 1 == len(render_segments):
            print(f"  Voiced {i + 1}/{len(render_segments)} segments", flush=True)

    voice_path = work_dir / "chinese_voice_track.wav"
    voice_track[:track_duration_ms].export(voice_path, format="wav")
    return voice_path


def fit_audio_to_duration(audio: Any, duration_ms: int) -> Any:
    from pydub import AudioSegment
    audio = audio.set_frame_rate(44100).set_channels(2)
    if len(audio) < duration_ms:
        audio += AudioSegment.silent(duration=duration_ms - len(audio), frame_rate=44100).set_channels(2)
    return audio[:duration_ms]


def combine_audio(voice_path: Path, original_audio_path: Path, duration: float, work_dir: Path, mode: str,
                  original_volume: float, background_volume: float, warnings: list[str]) -> tuple[Path, str]:
    from pydub import AudioSegment

    track_duration_ms = max(1, int(round(duration * 1000)))
    voice = fit_audio_to_duration(AudioSegment.from_file(voice_path), track_duration_ms)
    used_mode = mode
    background: Any | None = None

    if mode == "mix":
        original = fit_audio_to_duration(AudioSegment.from_file(original_audio_path), track_duration_ms)
        gain = 20 * math.log10(max(0.0001, min(1.0, original_volume)))
        background = original.apply_gain(gain)
    elif mode == "background":
        separated_dir = work_dir / "demucs_separated"
        separated_dir.mkdir(parents=True, exist_ok=True)
        try:
            print("[6/8] Separating original vocals from background with optional Demucs...", flush=True)
            run_command([
                sys.executable, "-m", "demucs", "--two-stems", "vocals", "-n", "htdemucs",
                "-o", str(separated_dir), str(original_audio_path),
            ], timeout=60 * 60 * 4)
            choices = list(separated_dir.rglob("no_vocals.wav"))
            if not choices:
                raise PipelineError("Demucs completed but no no_vocals.wav accompaniment was found.")
            background = fit_audio_to_duration(AudioSegment.from_file(choices[0]), track_duration_ms)
            gain = 20 * math.log10(max(0.0001, min(1.0, background_volume)))
            background = background.apply_gain(gain)
        except Exception as exc:
            used_mode = "replace (background separation fallback)"
            warnings.append(f"Demucs background separation failed; original track was replaced with Chinese narration. Details: {exc}")
            background = None
    else:
        used_mode = "replace"

    if background is not None:
        combined = background.overlay(voice)
    else:
        combined = voice
    combined_path = work_dir / "final_audio.wav"
    combined[:track_duration_ms].export(combined_path, format="wav")
    return combined_path, used_mode


def ffmpeg_filter_path(path: Path) -> str:
    # Escapes common filtergraph special characters; passed as a single argv element, not through a shell.
    value = str(path.resolve()).replace("\\", "\\\\")
    for char in (":", "'", ",", "[", "]"):
        value = value.replace(char, "\\" + char)
    return value


def unique_output_path(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 10000):
        candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise PipelineError(f"Too many existing output versions at {path.parent}")


def render_video(source: Path, audio_path: Path, srt_path: Path, output_path: Path, duration: float,
                 subtitle_mode: str, warnings: list[str]) -> str:
    print("[7/8] Rendering the final H.264/AAC MP4...", flush=True)
    common = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-i", str(audio_path),
    ]

    def encode(mode: str) -> None:
        command = list(common)
        if mode == "burn":
            escaped = ffmpeg_filter_path(srt_path)
            command.extend(["-vf", f"subtitles=filename='{escaped}':force_style='Fontsize=20,Outline=2,Shadow=0,MarginV=28'", "-map", "0:v:0", "-map", "1:a:0"])
        elif mode == "soft":
            command.extend(["-i", str(srt_path), "-map", "0:v:0", "-map", "1:a:0", "-map", "2:0"])
        else:
            command.extend(["-map", "0:v:0", "-map", "1:a:0"])
        command.extend(["-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k"])
        if mode == "soft":
            command.extend(["-c:s", "mov_text", "-metadata:s:s:0", "language=chi", "-metadata:s:s:0", "title=简体中文字幕"])
        command.extend(["-t", f"{duration:.3f}", "-movflags", "+faststart", str(output_path)])
        run_command(command, timeout=60 * 60 * 8)

    try:
        encode(subtitle_mode)
        return subtitle_mode
    except PipelineError as exc:
        if subtitle_mode == "burn":
            warnings.append(f"Burned subtitles were unavailable ({exc}); fell back to selectable soft subtitles.")
            try:
                if output_path.exists():
                    output_path.unlink()
                encode("soft")
                return "soft (burn fallback)"
            except PipelineError:
                if output_path.exists():
                    output_path.unlink()
                raise
        if output_path.exists():
            output_path.unlink()
        raise


def verify_output(path: Path, source_duration: float, subtitle_mode: str) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size < 1024:
        raise PipelineError(f"Output verification failed: final MP4 is missing or unexpectedly small: {path}")
    media = probe_media(path)
    types = [stream.get("codec_type") for stream in media["streams"]]
    if "video" not in types:
        raise PipelineError("Output verification failed: the final file has no video stream.")
    if "audio" not in types:
        raise PipelineError("Output verification failed: the final file has no audio stream.")
    duration_delta = abs(float(media["duration"]) - source_duration)
    if duration_delta > max(1.0, source_duration * 0.01):
        raise PipelineError(
            f"Output duration differs from source by {duration_delta:.2f}s (source {source_duration:.2f}s, "
            f"output {media['duration']:.2f}s). Review the output before use."
        )
    if subtitle_mode.startswith("soft") and not any(stream.get("codec_type") == "subtitle" for stream in media["streams"]):
        raise PipelineError("Output verification failed: soft subtitle mode was requested but no subtitle stream was muxed.")
    return {
        "duration_seconds": round(float(media["duration"]), 3),
        "duration_delta_seconds": round(duration_delta, 3),
        "video_codec": next((s.get("codec_name") for s in media["streams"] if s.get("codec_type") == "video"), None),
        "audio_codec": next((s.get("codec_name") for s in media["streams"] if s.get("codec_type") == "audio"), None),
        "subtitle_streams": sum(1 for s in media["streams"] if s.get("codec_type") == "subtitle"),
        "file_size_bytes": path.stat().st_size,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download or read a video, translate speech to Simplified Chinese, synthesize narration, and export a complete MP4."
    )
    parser.add_argument("source", nargs="?", help="Video URL (YouTube, Bilibili, or yt-dlp-supported site) or local video path")
    parser.add_argument("--output-dir", default="./output", help="Output directory (default: ./output)")
    parser.add_argument("--source-language", default="en", help="ASR source language, e.g. en; use auto to detect (default: en)")
    parser.add_argument("--model", default="small", help="faster-whisper model size: small, medium, large-v3, etc. (default: small)")
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda", "auto"], help="ASR device (default: cpu)")
    parser.add_argument("--compute-type", default="int8", help="faster-whisper compute type (default: int8)")
    parser.add_argument("--translation-provider", default="auto", choices=["auto", "openai", "google", "ollama"], help="Translation backend")
    parser.add_argument("--translation-context", default="", help="Optional video topic or terminology to guide natural translation")
    parser.add_argument("--ollama-model", default=os.getenv("OLLAMA_MODEL", ""), help="Installed local Ollama model for private, no-API translation")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="Edge TTS Chinese voice (default: %(default)s)")
    parser.add_argument("--tts-provider", default="edge", choices=["edge", "dashscope", "cosyvoice"], help="Chinese speech service (default: edge)")
    parser.add_argument("--tts-rate", default="+0%", help="Edge TTS speaking rate, e.g. +0%%, +10%%, -5%% (default: +0%%)")
    parser.add_argument("--cosyvoice-python", default=os.getenv("COSYVOICE_PYTHON", sys.executable), help="Python interpreter with the local CosyVoice dependencies")
    parser.add_argument("--cosyvoice-repo", default=os.getenv("COSYVOICE_REPO"), help="Local CosyVoice repository path")
    parser.add_argument("--cosyvoice-model-dir", default=os.getenv("COSYVOICE_MODEL_DIR"), help="Local CosyVoice model directory")
    parser.add_argument("--cosyvoice-mode", choices=["sft", "zero-shot"], default="sft", help="SFT uses a built-in speaker; zero-shot requires an authorized reference clip")
    parser.add_argument("--cosyvoice-speaker", default="中文女", help="Built-in speaker for CosyVoice SFT mode")
    parser.add_argument("--cosyvoice-prompt-audio", help="Authorized reference audio for CosyVoice zero-shot mode")
    parser.add_argument("--cosyvoice-prompt-text-file", help="Exact spoken transcript of the reference audio")
    parser.add_argument("--cosyvoice-speed", type=float, default=1.0, help="CosyVoice generation speed (default: 1.0)")
    parser.add_argument("--segments-file", help="Existing translated segments JSON, used to resume after transcription and translation")
    parser.add_argument("--reuse-tts-dir", help="Directory of previously synthesized segment audio to reuse for final alignment")
    parser.add_argument("--audio-mode", default="replace", choices=["replace", "mix", "background"], help="Replace original audio, mix it quietly, or attempt source separation")
    parser.add_argument("--original-volume", type=float, default=0.12, help="Original-track linear volume for mix mode, 0.0-1.0 (default: 0.12)")
    parser.add_argument("--background-volume", type=float, default=0.25, help="Separated-background linear volume, 0.0-1.0 (default: 0.25)")
    parser.add_argument("--subtitle-mode", default="soft", choices=["soft", "burn", "none"], help="Selectable MP4 subtitles, burned-in subtitles, or no embedded subtitles")
    parser.add_argument("--cookies-from-browser", choices=["chrome", "chromium", "firefox", "safari", "edge", "brave", "opera", "vivaldi", "whale"], help="Opt in to reading your own local browser cookies for sites requiring sign-in")
    parser.add_argument("--keep-intermediates", action="store_true", help="Keep temporary downloads, audio, and TTS clips for debugging")
    parser.add_argument("--dry-run", action="store_true", help="Validate options and prerequisites without downloading or processing")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not args.source:
        parser.print_help()
        return 0
    if not 0 <= args.original_volume <= 1:
        parser.error("--original-volume must be between 0.0 and 1.0")
    if not 0 <= args.background_volume <= 1:
        parser.error("--background-volume must be between 0.0 and 1.0")
    if args.device == "auto":
        # CPU is the most portable default; CUDA is opt-in because CTranslate2's CUDA runtime
        # requirements differ from the presence of a CUDA-enabled PyTorch installation.
        args.device = "cpu"
    if args.tts_provider == "dashscope" and not os.getenv("DASHSCOPE_API_KEY", "").strip():
        parser.error("--tts-provider dashscope requires DASHSCOPE_API_KEY")
    if args.tts_provider == "cosyvoice" and not args.reuse_tts_dir:
        if not args.cosyvoice_repo or not args.cosyvoice_model_dir:
            parser.error("--tts-provider cosyvoice requires --cosyvoice-repo and --cosyvoice-model-dir")
        if args.cosyvoice_mode == "zero-shot" and not (args.cosyvoice_prompt_audio and args.cosyvoice_prompt_text_file):
            parser.error("CosyVoice zero-shot requires --cosyvoice-prompt-audio and --cosyvoice-prompt-text-file")
    if args.cosyvoice_speed <= 0:
        parser.error("--cosyvoice-speed must be positive")
    if args.translation_provider == "ollama" and not args.ollama_model and not args.segments_file:
        parser.error("--translation-provider ollama requires --ollama-model or OLLAMA_MODEL")

    for binary in ("ffmpeg", "ffprobe"):
        require_binary(binary)
    try:
        import faster_whisper  # noqa: F401
        import edge_tts  # noqa: F401
        import yt_dlp  # noqa: F401
        import pydub  # noqa: F401
    except ImportError as exc:
        raise PipelineError(f"Missing Python dependency: {exc.name}. Run: python -m pip install -r requirements.txt") from exc

    if args.dry_run:
        print("Dry run passed: FFmpeg and required Python dependencies are present.")
        translation = args.translation_provider
        if translation == "auto":
            translation = "ollama" if args.ollama_model else "unconfigured"
        print(f"Input: {args.source}\nASR model: {args.model}\nTranslation: {translation}"
              f"{f' ({args.ollama_model})' if translation == 'ollama' else ''}"
              f"\nSpeech: {args.tts_provider}\nAudio mode: {args.audio_mode}\nSubtitle mode: {args.subtitle_mode}")
        if translation == "ollama" and not shutil.which("ollama"):
            raise PipelineError("The ollama command is missing from PATH.")
        if translation == "unconfigured" and not args.segments_file:
            print("Warning: set --ollama-model for local translation or explicitly select an external provider.")
        if args.translation_provider == "openai" and not (os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY")):
            print("Warning: OPENAI_API_KEY or DEEPSEEK_API_KEY is needed for natural translation.")
        return 0

    warnings: list[str] = []
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix=".video-zh-dubbing-", dir=str(output_dir)))
    succeeded = False
    try:
        source_video, source_url = acquire_video(args.source, work_dir, args.cookies_from_browser)
        video_context = args.translation_context.strip() or source_video.stem
        source_info = probe_media(source_video)
        duration = float(source_info["duration"])
        has_audio = any(stream.get("codec_type") == "audio" for stream in source_info["streams"])
        original_audio, asr_audio = extract_audio(source_video, work_dir, has_audio)
        if args.segments_file:
            segments = json.loads(Path(args.segments_file).expanduser().read_text(encoding="utf-8"))
            if not isinstance(segments, list) or not segments or any(
                not isinstance(segment, dict) or segment.get("id") != index or
                not isinstance(segment.get("zh_text"), str) or not segment["zh_text"].strip() or
                not isinstance(segment.get("start"), (int, float)) or
                not isinstance(segment.get("end"), (int, float)) or
                segment["start"] < 0 or segment["start"] >= duration or segment["end"] <= segment["start"] or
                (index > 1 and segment["start"] < segments[index - 2]["start"])
                for index, segment in enumerate(segments, 1)
            ):
                raise PipelineError("The translated segments file is empty or invalid.")
            provider_used = "pretranslated"
            print(f"[3-4/8] Reusing {len(segments)} translated segments...", flush=True)
        else:
            segments = transcribe_audio(asr_audio, args.model, args.source_language, args.device, args.compute_type)
            provider_used = translate_segments(segments, args.translation_provider, video_context, args.ollama_model)

        source_stem = source_video.stem
        base = slugify(source_stem)
        final_path = unique_output_path(output_dir / f"{base}_zh-dub.mp4")
        # Sidecars use the actual final output stem to avoid collisions.
        sidecar_stem = final_path.with_suffix("")
        srt_path = Path(str(sidecar_stem) + ".srt")
        segments_path = Path(str(sidecar_stem) + ".segments.json")
        report_path = Path(str(sidecar_stem) + ".report.json")
        write_srt(segments, srt_path, duration)
        segments_path.write_text(json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")

        reuse_tts_dir = Path(args.reuse_tts_dir).expanduser().resolve() if args.reuse_tts_dir else None
        if args.tts_provider == "cosyvoice" and reuse_tts_dir is None:
            reuse_tts_dir = generate_cosyvoice_clips(args, segments_path, work_dir)
        voice_used = args.cosyvoice_speaker if args.tts_provider == "cosyvoice" and args.cosyvoice_mode == "sft" else (
            "authorized_reference" if args.tts_provider == "cosyvoice" else args.voice
        )
        voice_path = make_voice_track(segments, duration, work_dir, voice_used, args.tts_rate, args.tts_provider, warnings, reuse_tts_dir)
        print("[6/8] Preparing final audio mix...", flush=True)
        mixed_audio_path, audio_mode_used = combine_audio(
            voice_path, original_audio, duration, work_dir, args.audio_mode,
            args.original_volume, args.background_volume, warnings,
        )
        subtitle_mode_used = render_video(source_video, mixed_audio_path, srt_path, final_path, duration, args.subtitle_mode, warnings)
        try:
            checks = verify_output(final_path, duration, subtitle_mode_used)
        except PipelineError:
            # Do not leave an unverified MP4 looking like a successful deliverable.
            if final_path.exists():
                final_path.unlink()
            raise
        report = {
            "status": "success",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "source": str(source_url or Path(args.source).expanduser().resolve()),
            "source_file": source_video.name,
            "output_file": str(final_path),
            "subtitle_file": str(srt_path),
            "segments_file": str(segments_path),
            "translation_provider": provider_used,
            "translation_model": (
                os.getenv("OPENAI_MODEL", "gpt-4.1-mini") if os.getenv("OPENAI_API_KEY") else
                os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
            ) if provider_used == "openai" else ("Google Translate adapter" if provider_used == "google" else args.ollama_model if provider_used == "ollama" else "pretranslated"),
            "translation_context": video_context,
            "asr_model": args.model,
            "source_language_requested": args.source_language,
            "voice": voice_used,
            "tts_provider": args.tts_provider,
            "tts_rate": args.cosyvoice_speed if args.tts_provider == "cosyvoice" else args.tts_rate,
            "cosyvoice_mode": args.cosyvoice_mode if args.tts_provider == "cosyvoice" else None,
            "cosyvoice_model_dir": str(Path(args.cosyvoice_model_dir).expanduser().resolve()) if args.tts_provider == "cosyvoice" and args.cosyvoice_model_dir else None,
            "audio_mode_requested": args.audio_mode,
            "audio_mode_used": audio_mode_used,
            "subtitle_mode_requested": args.subtitle_mode,
            "subtitle_mode_used": subtitle_mode_used,
            "segment_count": len(segments),
            "warnings": warnings,
            "checks": checks,
            "intermediates_retained": bool(args.keep_intermediates),
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        succeeded = True
        print("[8/8] Quality checks passed.", flush=True)
        print("\nCompleted successfully:")
        print(f"  MP4:      {final_path}")
        print(f"  Subtitles:{srt_path}")
        print(f"  Segments: {segments_path}")
        print(f"  Report:   {report_path}")
        print(f"  Duration: {checks['duration_seconds']:.2f}s | Segments: {len(segments)} | Audio mode: {audio_mode_used} | Subtitles: {subtitle_mode_used}")
        if warnings:
            print(f"\nWarnings ({len(warnings)}):")
            for warning in warnings[:30]:
                print(f"  - {warning}")
            if len(warnings) > 30:
                print(f"  - ... {len(warnings) - 30} additional warning(s) in the report JSON")
        if args.keep_intermediates:
            print(f"  Intermediates: {work_dir}")
        return 0
    except KeyboardInterrupt:
        print("\nCancelled by user. Intermediate files were retained for recovery.", file=sys.stderr)
        print(f"Work directory: {work_dir}", file=sys.stderr)
        return 130
    except PipelineError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        print(f"Intermediate files retained for troubleshooting: {work_dir}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nUNEXPECTED ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"Intermediate files retained for troubleshooting: {work_dir}", file=sys.stderr)
        return 1
    finally:
        if succeeded and not args.keep_intermediates:
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
