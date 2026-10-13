# Video Chinese Dubbing Skill

A portable Agent Skill for Codex and other agents that support `SKILL.md`. It turns a user-provided video URL (YouTube, Bilibili, or another yt-dlp-supported site) or local video into a Chinese-voiced MP4 with subtitle and report sidecars.

## Install the Skill into Codex or another compatible agent

Extract the ZIP so the skill folder is named `video-zh-dubbing` and contains `SKILL.md`. A common cross-client location is `~/.agents/skills/video-zh-dubbing/`; project-local installs can use `.agents/skills/video-zh-dubbing/`. Restart or reload the agent session if the skill catalog is cached.

The skill instructions teach the agent how to call the scripts; the Python dependencies and FFmpeg still need to be installed on the machine that executes them.

## What it does

- Downloads the requested URL with `yt-dlp` (including Bilibili part URLs such as `?p=2`) or reads a local file. A Bilibili 412 response can fall back to its public API only for videos marked downloadable.
- Transcribes speech locally with `faster-whisper` and preserves segment timestamps.
- Translates with neighboring speech and video-topic context, then polishes the draft into natural spoken Mandarin using an installed local Ollama model or an explicitly configured OpenAI-compatible API. Google Translate remains an unpolished fallback.
- Generates Chinese narration using Edge TTS or a local CosyVoice model. CosyVoice runs without per-call API fees and produces resumable PCM16 clips.
- Supports Qwen3 TTS through DashScope when Edge TTS is unavailable (`DASHSCOPE_API_KEY`, `--tts-provider dashscope --voice Cherry`).
- Replaces original speech, mixes it quietly under Chinese narration, or optionally separates background audio with Demucs.
- Exports H.264/AAC MP4, a Simplified-Chinese SRT, a translated segment JSON, and a JSON processing report.

## Requirements

- Python 3.10 or newer.
- FFmpeg and ffprobe available on `PATH`.
- Network access for web video download and Edge TTS. The selected Whisper model downloads locally on first run. Ollama and CosyVoice can run locally after their models are installed.
- Enough free disk space for the source video, temporary WAV files, and final MP4.
- Optional CosyVoice repository, downloaded model, and a separate Python environment with its dependencies for local neural speech. The main skill environment does not install PyTorch automatically.

### macOS

```bash
brew install ffmpeg
cd video-zh-dubbing
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python scripts/check_environment.py
```

If Homebrew is not installed, install FFmpeg from a trusted package source and ensure both `ffmpeg` and `ffprobe` are on `PATH`.

### Windows (PowerShell)

Install Python 3.10+ and FFmpeg, and add FFmpeg's `bin` directory to `PATH`. Then run:

```powershell
cd video-zh-dubbing
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python scripts/check_environment.py
```

### Linux (Debian/Ubuntu)

```bash
sudo apt-get update && sudo apt-get install -y ffmpeg python3-venv
cd video-zh-dubbing
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python scripts/check_environment.py
```

## Translation backends

### Local translation with Ollama

Install Ollama and a model suited to English-to-Chinese translation separately, then start its local service. Set `OLLAMA_MODEL` or pass `--ollama-model`. The script uses the `ollama` CLI, requests JSON output, and runs a draft and a Mandarin-polish pass. It does not download a model or send transcript text to a paid translation API. Model licenses and resource needs vary; review your chosen model's terms.

```bash
python scripts/translate_video.py ./input.mp4 \
  --translation-provider ollama --ollama-model YOUR_INSTALLED_MODEL \
  --translation-context "video topic and technical terms" --output-dir ./output
```

`auto` uses `OLLAMA_MODEL` when configured; otherwise it stops before translation. External translation requires an explicit `--translation-provider google` or `openai`, even if API keys exist in the environment. Check names, numbers, code identifiers, and speaking pace in the resulting segment JSON before publishing.

### Optional API translation and polish

Set `OPENAI_API_KEY` (optionally `OPENAI_MODEL` and `OPENAI_BASE_URL`) or `DEEPSEEK_API_KEY` (optionally `DEEPSEEK_MODEL` and `DEEPSEEK_BASE_URL`). The script sends transcript text and topic context to the selected provider, not the video file. Each batch gets neighboring English lines and a second Chinese editing pass. Use `--translation-context` to supply the topic and exact terminology when a URL or filename is unclear.

### No local model or API key: Google Translate draft

With explicit `--translation-provider google`, the script uses `deep-translator`'s Google Translate adapter. It may be rate-limited and its literal output needs line-by-line review. Correct the generated `*.segments.json` and rerun with `--segments-file` so subtitles and speech use the same approved wording.

When Google is throttled, transcribe locally, translate and polish the transcript in the agent, then supply a reviewed `--segments-file`. Do not silently switch to a paid provider when the user excluded it.

## Local CosyVoice speech

CosyVoice is optional and uses the [official project](https://github.com/QwenAudio/CosyVoice) and [model weights](https://huggingface.co/FunAudioLLM/Fun-CosyVoice3-0.5B-2512) under Apache-2.0. It requires no speech API key, but local inference uses CPU/GPU time. Review the license and any reference-voice rights before publishing.

Use `CosyVoice-300M-SFT` with built-in `中文女` when no authorized reference audio is available. Use `Fun-CosyVoice3-0.5B` in zero-shot mode when you have an authorized reference WAV and its exact spoken transcript. A reference generated from the licensed built-in SFT voice is also suitable for a consistent synthetic voice. Set `COSYVOICE_PYTHON`, `COSYVOICE_REPO`, and `COSYVOICE_MODEL_DIR` or pass their corresponding flags:

```bash
python scripts/translate_video.py ./input.mp4 --segments-file ./reviewed.segments.json \
  --tts-provider cosyvoice --cosyvoice-python /path/to/cosyvoice-python \
  --cosyvoice-repo /path/to/CosyVoice \
  --cosyvoice-model-dir /path/to/CosyVoice-300M-SFT \
  --cosyvoice-mode sft --cosyvoice-speaker 中文女 --output-dir ./output

# For CosyVoice3, replace the model and mode and provide an authorized reference:
python scripts/translate_video.py ./input.mp4 --segments-file ./reviewed.segments.json \
  --tts-provider cosyvoice --cosyvoice-python /path/to/cosyvoice-python \
  --cosyvoice-repo /path/to/CosyVoice --cosyvoice-model-dir /path/to/Fun-CosyVoice3-0.5B \
  --cosyvoice-mode zero-shot --cosyvoice-prompt-audio ./reference.wav \
  --cosyvoice-prompt-text-file ./reference.txt --output-dir ./output
```

## Commands

```bash
# URL input
python scripts/translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID" --ollama-model YOUR_INSTALLED_MODEL --output-dir ./output
python scripts/translate_video.py "https://www.bilibili.com/video/BV1NAuq6uE8t/?p=2" --ollama-model YOUR_INSTALLED_MODEL --output-dir ./output
python scripts/translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID" --ollama-model YOUR_INSTALLED_MODEL --translation-context "Software architecture; domain terminology" --output-dir ./output

# Local input
python scripts/translate_video.py ./input.mp4 --ollama-model YOUR_INSTALLED_MODEL --output-dir ./output

# Resume from a reviewed translated segment file after a speech-service failure
python scripts/translate_video.py ./input.mp4 --segments-file ./output/input_zh-dub.segments.json \
  --tts-provider dashscope --voice Cherry --output-dir ./output

# Choose source language, ASR model, voice and audio policy
python scripts/translate_video.py URL --source-language auto --model medium \
  --voice zh-CN-YunyangNeural --audio-mode background --subtitle-mode soft

# Embed subtitles visually instead of as a selectable track
python scripts/translate_video.py URL --subtitle-mode burn

# Keep work files after processing
python scripts/translate_video.py URL --keep-intermediates

# Browser cookies are opt-in and read from your own local browser profile
python scripts/translate_video.py URL --cookies-from-browser chrome

# Show all options
python scripts/translate_video.py --help
```

## Audio modes

- `replace`: Chinese narration replaces the source audio. Most reliable; original music and ambience are removed.
- `mix`: Chinese narration is mixed with the source audio lowered to `--original-volume` (default `0.12`). Original English may remain audible.
- `background`: attempts voice/background separation through optional `demucs`. If unavailable or unsuccessful, the program warns and falls back to `replace` rather than failing the whole video job. Install with `python -m pip install demucs` if your environment supports it. First use downloads model weights and can be slow.

## Output files

For `example.mp4` the output directory contains:

- `example_zh-dub.mp4` — complete dubbed video.
- `example_zh-dub.srt` — Simplified Chinese subtitles.
- `example_zh-dub.segments.json` — timestamped source and translated text.
- `example_zh-dub.report.json` — settings, checks, and warnings.

The first run downloads the requested faster-whisper model. `small` is a practical starting point; use `medium` or `large-v3` when higher recognition accuracy is worth more time and memory. Edge TTS is an online service wrapper, not an offline voice model, so network availability is required.

## Troubleshooting

- **Site unsupported / download denied:** update yt-dlp (`python -m pip install -U yt-dlp`), check whether the site supports downloading, and use an authenticated session only when you are authorized. Do not attempt DRM circumvention.
- **No speech detected:** use `--source-language auto`, try a larger model, or ensure the source audio is audible and not music-only.
- **Translation throttled:** use an installed Ollama model, retry later, or process a shorter section. Use an external API only when permitted.
- **Ollama unavailable:** start the local Ollama service, verify the model with `ollama list`, and pass its exact name through `--ollama-model`.
- **Stiff Chinese or wrong technical term:** review the segment JSON, correct the wording, and rerun with `--segments-file`. Supplying `--translation-context` reduces ambiguous term errors.
- **TTS connection error:** check network access and retry; Edge TTS depends on the online service.
- **Local CosyVoice dependency error:** run its own Python environment with `--cosyvoice-python`; keep `COSYVOICE_REPO` and `COSYVOICE_MODEL_DIR` pointed at a complete local installation. The helper uses the model's actual sample rate instead of assuming 24 kHz.
- **Burned subtitle filter unavailable:** use `--subtitle-mode soft`; the SRT is always written for review.
- **Background separation failure:** the script documents the fallback and continues with replacement audio. Install Demucs separately to enable separation.
- **Audio too fast or a decimal is mispronounced:** inspect the segment audio and report warnings; shorten the Chinese line or add `spoken_text` (for example, `零点四`) alongside the unchanged numeric `zh_text` subtitle. Use `scripts/cosyvoice_tts.py --only-ids 54,62` to regenerate only edited clips, then rerender with `--reuse-tts-dir`. Check code names and English terms by listening as well.

## Rights and privacy

Download and transform only content you own, have permission to process, or are otherwise legally allowed to use. Respect source platform rules. The skill does not bypass DRM. When using an API translation backend, transcript text is sent to that configured provider. Edge TTS sends text to Microsoft's online speech service via the `edge-tts` wrapper. Review the applicable privacy terms before using confidential material.
