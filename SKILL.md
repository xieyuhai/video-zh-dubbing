---
name: video-zh-dubbing
description: Convert a specified English video URL or local video into a Chinese-dubbed MP4 with natural Mandarin translation and synchronized Chinese subtitles. Use for YouTube, Bilibili multi-part videos, and other yt-dlp-supported URLs.
---

# Video to Chinese Dub

Use the bundled Python pipeline to produce a complete Chinese-dubbed video. Do not merely provide a suggested workflow when the user has supplied a URL and asked for the finished artifact: run the pipeline if terminal execution is available and the user has authorized the required network/download actions.

Requires Python 3.10+, FFmpeg/ffprobe, network access for URL downloads, and a local faster-whisper model. Online translation and speech are optional; use the local CosyVoice backend when natural Mandarin is needed without a paid speech API.

Resolve `<skill-root>` as the directory containing this `SKILL.md`; do not assume the caller's current working directory is the skill directory. Prefer `<skill-root>/.venv/bin/python` on macOS/Linux or `<skill-root>/.venv\Scripts\python.exe` on Windows when that virtual environment exists; otherwise use the active Python interpreter. Commands below assume the current directory has been changed to `<skill-root>`.

## Quick start

1. Check prerequisites:

   ```bash
   python3 scripts/check_environment.py
   ```

2. Install FFmpeg and Python dependencies. See `README.md` for macOS, Windows, and Linux commands.
3. For natural translation without a paid API, use an already installed Ollama model with `--translation-provider ollama --ollama-model MODEL`. The pipeline runs it locally in two passes and validates every segment ID; review technical terms and timing before delivery. If no local model is available, use `--translation-provider google` only with permission as an online draft and manually polish the timestamped segments, or pass reviewed JSON via `--segments-file`. Use `OPENAI_API_KEY` or `DEEPSEEK_API_KEY` only when explicitly permitted; never switch to a disallowed API.
4. Convert a video URL (Bilibili, YouTube, or any site supported by yt-dlp):

   ```bash
   python3 scripts/translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID" \
     --translation-provider ollama --ollama-model MODEL --output-dir ./output
   ```

   Bilibili URLs can include a part parameter such as `?p=2`; pass the full target URL.
5. Or process a local video:

   ```bash
   python3 scripts/translate_video.py ./input.mp4 \
     --translation-provider ollama --ollama-model MODEL --output-dir ./output
   ```

## Defaults and options

- Source language defaults to English (`--source-language en`); use `auto` to detect it.
- Chinese voice defaults to `zh-CN-XiaoxiaoNeural`; use `--voice zh-CN-YunxiNeural` for a younger male voice or `--voice zh-CN-YunyangNeural` for a male narration voice.
- For natural no-per-call-fee speech, prefer a locally installed `Fun-CosyVoice3-0.5B` in `zero-shot` mode when an explicitly authorized reference clip and exact transcript are available. Otherwise, use `CosyVoice-300M-SFT` with a built-in speaker (for example `中文女`). Never clone a voice without permission. The model loads once for all segments, writes PCM16 WAV clips, and checkpoints progress. See `README.md` for the flags.
- If Edge TTS is unreachable, set `DASHSCOPE_API_KEY` and use `--tts-provider dashscope --voice Cherry` for Qwen3 TTS. The translated transcript text is sent to DashScope.
- Translation defaults to `auto`, which uses only an explicitly configured local `OLLAMA_MODEL` and otherwise stops before translation. Select `--translation-provider openai` or `google` explicitly before any transcript is sent to an external translator. Ollama requires its local service and an installed model; the skill never downloads one automatically. Use `--translation-context "video topic and key terms"` when the filename does not clearly identify the subject; the title or filename is used otherwise.
- `--audio-mode replace` replaces the source audio with Chinese speech (default, dependable). `--audio-mode mix` keeps the original track at low volume under the Chinese voice. `--audio-mode background` attempts to separate original vocals with optional Demucs, then mixes the Chinese voice with the remaining background. If Demucs is unavailable or fails, the script reports the issue and safely falls back to replacement audio.
- `--subtitle-mode soft` embeds selectable Chinese subtitles in MP4 and also writes an external SRT (default). Inspect the source frames first: if captions are already burned into the picture, keep `soft` to avoid overlapping duplicate text. `burn` burns subtitles into the picture and requires an FFmpeg build with the `subtitles`/libass filter and a usable Chinese font. `none` omits subtitles from the MP4 but still writes an SRT sidecar for review.
- Tune ASR with `--model small` (balanced default), `--model medium`, or `--model large-v3`. Larger models need more memory and take longer. The first run downloads the selected model.
- Use `--cookies-from-browser chrome` only if you are signed in and the site requires your own browser session. This reads local browser cookies; it is not enabled by default.
- Use `--keep-intermediates` to retain downloaded source audio, WAV files, and generated speech clips for troubleshooting.
- Use `--segments-file path/to/translated.segments.json` to reuse reviewed translations after a TTS failure without repeating recognition and translation.
- Use `--reuse-tts-dir path/to/tts_segments` with an existing set of generated clips to adjust timing and rerender without calling the speech service again.

## Required output checks

The script must verify that the MP4 exists, is non-empty, has a video stream and an audio stream, and has a duration close to the input. It emits an SRT, translated segment JSON, and a processing report next to the final MP4. Any failed stage must stop the pipeline and provide a useful error instead of pretending the output is complete.

Before delivery, inspect the translated JSON/SRT for mistranscribed names and terms, unnatural word-for-word phrasing, and segments that were sped up or trimmed. Inspect a rendered frame too: reject missing-glyph boxes, off-screen captions, and duplicate burned subtitles. For CosyVoice, audition a short sample before rendering a long video; check the actual pronunciation of decimals, code names, and English terms, not just their written text. A reviewed segment may add `spoken_text` to spell out a number or filename naturally while `zh_text` remains the exact subtitle. Their meaning must match. Avoid shipping clipped speech or conspicuous speed-up.

## Workflow

1. Resolve URL vs local file. For URLs, download only the requested video using yt-dlp (`--no-playlist`). If a Bilibili page returns 412, the script may use Bilibili's public API only when the video is explicitly marked downloadable; preserve the requested `?p=` part. Never bypass DRM, paywalls, or account restrictions.
2. Use ffprobe to inspect streams and duration. Extract mono 16-kHz PCM audio.
3. Transcribe with faster-whisper, retaining source timestamps. Fail clearly if no speech is detected.
4. Translate with topic and neighboring speech context, then polish into concise, idiomatic Mandarin. Preserve IDs, order, meaning, numbers, names, and technical terms. Check source-recognition errors against the video topic.
5. Generate each Chinese voice segment through the selected speech service. Fit it to its timestamp window conservatively; warn in the report when a segment needs aggressive speed-up or truncation.
6. Build a full-length Chinese voice track. Apply the requested audio mode; optional Demucs separation is an enhancement, not a hard dependency.
7. Write Chinese SRT and translated JSON. Mux or burn subtitles as requested, export a broadly compatible H.264/AAC MP4, and verify the result.
8. Report exact output paths, selected settings, warnings, and any remaining review needs.

## Examples

```bash
# YouTube URL; API translation; Chinese female voice; voice-over on extracted background
OPENAI_API_KEY="$OPENAI_API_KEY" python3 scripts/translate_video.py "https://youtu.be/VIDEO_ID" \
  --translation-provider openai --audio-mode background --subtitle-mode soft --output-dir ./output

# Bilibili part 2; use a configured LLM key for natural translation
python3 scripts/translate_video.py "https://www.bilibili.com/video/BV1NAuq6uE8t/?p=2" \
  --translation-provider openai --translation-context "规范驱动开发 SDD 与智能体编程" \
  --voice zh-CN-YunyangNeural --audio-mode mix --output-dir ./output

# Burn Chinese subtitles into the picture
python3 scripts/translate_video.py ./input.mp4 --subtitle-mode burn --output-dir ./output

# Fully local speech using CosyVoice3 and an authorized reference; no paid APIs
python3 scripts/translate_video.py ./input.mp4 --segments-file ./reviewed.segments.json \
  --tts-provider cosyvoice --cosyvoice-python /path/to/cosyvoice-python \
  --cosyvoice-repo /path/to/CosyVoice --cosyvoice-model-dir /path/to/Fun-CosyVoice3-0.5B \
  --cosyvoice-mode zero-shot --cosyvoice-prompt-audio ./reference.wav \
  --cosyvoice-prompt-text-file ./reference.txt --subtitle-mode burn --output-dir ./output

# Local transcription, Ollama translation, and CosyVoice narration
python3 scripts/translate_video.py ./input.mp4 --translation-provider ollama --ollama-model MODEL \
  --tts-provider cosyvoice --cosyvoice-python /path/to/cosyvoice-python \
  --cosyvoice-repo /path/to/CosyVoice --cosyvoice-model-dir /path/to/CosyVoice-300M-SFT \
  --cosyvoice-mode sft --output-dir ./output
```

## Safety and rights

Only process videos you own, are licensed to adapt, or are otherwise permitted to download and transform. Respect the source platform's terms and copyright. Never bypass DRM or technical access controls. `--cookies-from-browser` is opt-in and should only use the user's own authenticated session. Do not publish a dubbed derivative without confirming applicable rights.

The project is for learning, research, and technical exchange. Do not use it for unlawful activities, unauthorized voice cloning, fraud, impersonation, or misleading content. See the disclaimer in `README.md`; users are responsible for their use and distribution of generated content under applicable law.
