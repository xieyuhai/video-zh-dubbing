# Implementation notes

## Stage invariants

1. Never claim success until the final MP4 has passed ffprobe checks.
2. Preserve ASR timestamps and segment order in JSON/SRT.
3. Do not silently discard translation failures. Fail clearly if a segment cannot be translated or voiced.
4. Local Ollama or explicitly authorized API translation uses adjacent source lines, topic context, and a separate Mandarin editing pass. Verify that the returned IDs exactly match the batch before using any text for TTS or subtitles.
5. Optional Demucs separation must never be a hard prerequisite for basic dubbing.
6. When segment narration exceeds its time budget, time-stretch conservatively and log every aggressive fit/truncation for human review.
7. The final MP4 has H.264 video and AAC audio. Selectable subtitle mode uses `mov_text` in the MP4 and also writes SRT.

## Known limits

- Automatic ASR timing is approximate. Human review is recommended for technical, legal, medical, or heavily accented speech.
- Edge TTS is online and can change availability or voice catalog.
- The no-key translation backend may be throttled.
- Google Translate output is a draft; review it for idiomatic Chinese and technical terms before delivering the dubbed MP4.
- Background separation quality depends on source material and Demucs model.
- Supported sites are limited to sites currently handled by yt-dlp. The script does not bypass DRM or authentication controls.
