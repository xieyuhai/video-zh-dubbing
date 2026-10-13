#!/usr/bin/env python3
"""Generate timestamped-segment audio with a local CosyVoice installation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
from pathlib import Path


def digest_file(path: Path | None) -> str | None:
    if path is None:
        return None
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_prompt_text(args: argparse.Namespace) -> str | None:
    if args.prompt_text_file:
        return args.prompt_text_file.read_text(encoding="utf-8").strip()
    return args.prompt_text


def validate_options(args: argparse.Namespace) -> str | None:
    if not args.repo.is_dir() or not args.model_dir.is_dir():
        raise ValueError("CosyVoice repository and model directory must exist locally")
    if args.preview_count is not None and args.preview_count < 1:
        raise ValueError("--preview-count must be positive")
    if args.mode == "zero-shot":
        prompt = resolve_prompt_text(args)
        if not args.prompt_audio or not args.prompt_audio.is_file() or not prompt:
            raise ValueError("zero-shot requires --prompt-audio and --prompt-text-file or --prompt-text")
        return prompt
    if not args.speaker:
        raise ValueError("sft requires --speaker")
    return None


def config_digest(args: argparse.Namespace, segments: list[dict], prompt_text: str | None) -> str:
    payload = {
        "repo": str(args.repo.resolve()),
        "model_dir": str(args.model_dir.resolve()),
        "mode": args.mode,
        "speaker": args.speaker,
        "prompt_audio": digest_file(args.prompt_audio),
        "prompt_text": prompt_text,
        "speed": args.speed,
        "seed": args.seed,
        "segments": [
            {"id": item["id"], "zh_text": item["zh_text"], "spoken_text": item.get("spoken_text")}
            for item in segments
        ],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def generate(args: argparse.Namespace) -> None:
    prompt_text = validate_options(args)
    segments = json.loads(args.segments_file.read_text(encoding="utf-8"))
    if not isinstance(segments, list) or not segments or any(
        item.get("id") != index or not isinstance(item.get("zh_text"), str) or not item["zh_text"].strip()
        or ("spoken_text" in item and (not isinstance(item["spoken_text"], str) or not item["spoken_text"].strip()))
        for index, item in enumerate(segments, 1)
    ):
        raise ValueError("segments JSON must contain sequential ids and non-empty zh_text")
    selected_ids = {int(part) for part in args.only_ids.split(",")} if args.only_ids else None
    if selected_ids and selected_ids - set(range(1, len(segments) + 1)):
        raise ValueError("--only-ids contains an out-of-range segment id")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    fingerprint = config_digest(args, segments, prompt_text)
    checkpoint = args.output_dir / "cosyvoice-checkpoint.json"
    previous = json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.exists() else {}
    # An explicit repair list preserves other clips even when the reviewed text changed.
    completed = set(previous.get("completed", [])) if previous.get("fingerprint") == fingerprint or selected_ids else set()
    if selected_ids:
        completed.difference_update(selected_ids)
    if not selected_ids and len(completed) == len(segments) and all(
        (args.output_dir / f"segment_{item['id']:05d}.wav").is_file()
        and (args.output_dir / f"segment_{item['id']:05d}.wav").stat().st_size > 1024
        for item in segments
    ):
        print(f"Reusing {len(segments)} CosyVoice clips", flush=True)
        return

    cache_root = args.output_dir / ".runtime-cache"
    for key, directory in (
        ("NUMBA_CACHE_DIR", cache_root / "numba"),
        ("MPLCONFIGDIR", cache_root / "matplotlib"),
        ("XDG_CACHE_HOME", cache_root / "xdg"),
    ):
        directory.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault(key, str(directory))
    sys.path.insert(0, str(args.repo.resolve()))
    matcha = args.repo / "third_party" / "Matcha-TTS"
    if matcha.is_dir():
        sys.path.insert(0, str(matcha.resolve()))

    import torch
    import torchaudio
    from cosyvoice.cli.cosyvoice import AutoModel

    load_options = (
        {"load_jit": False, "load_trt": False, "fp16": False}
        if (args.model_dir / "cosyvoice.yaml").exists()
        else {"load_trt": False, "load_vllm": False, "fp16": False}
    )
    model = AutoModel(model_dir=str(args.model_dir), **load_options)
    if args.mode == "zero-shot" and (args.model_dir / "cosyvoice3.yaml").exists():
        prompt_text = f"You are a helpful assistant.<|endofprompt|>{prompt_text}"

    selected = ([item for item in segments if item["id"] in selected_ids] if selected_ids else
                segments[:args.preview_count] if args.preview_count else segments)
    for item in selected:
        segment_id = item["id"]
        destination = args.output_dir / f"segment_{segment_id:05d}.wav"
        if segment_id in completed and destination.is_file() and destination.stat().st_size > 1024:
            continue
        random.seed(args.seed + segment_id)
        torch.manual_seed(args.seed + segment_id)
        if args.mode == "zero-shot":
            chunks = model.inference_zero_shot(
                item.get("spoken_text", item["zh_text"]), prompt_text, str(args.prompt_audio), stream=False, speed=args.speed
            )
        else:
            chunks = model.inference_sft(item.get("spoken_text", item["zh_text"]), args.speaker, stream=False, speed=args.speed)
        speech = [chunk["tts_speech"].detach().cpu() for chunk in chunks if chunk.get("tts_speech") is not None]
        if not speech:
            raise RuntimeError(f"CosyVoice returned no audio for segment {segment_id}")
        audio = torch.cat(speech, dim=1)
        peak = float(torch.max(torch.abs(audio)).item())
        if peak > 0:
            audio = torch.clamp(audio * min(0.92 / peak, 4.0), -0.999, 0.999)
        temporary = destination.with_suffix(".tmp.wav")
        torchaudio.save(str(temporary), audio, model.sample_rate, encoding="PCM_S", bits_per_sample=16)
        temporary.replace(destination)
        completed.add(segment_id)
        checkpoint.write_text(json.dumps({"fingerprint": fingerprint, "completed": sorted(completed)}), encoding="utf-8")
        if segment_id % 20 == 0 or segment_id == selected[-1]["id"]:
            print(f"CosyVoice synthesized {segment_id}/{len(segments)}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--segments-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("sft", "zero-shot"), default="sft")
    parser.add_argument("--speaker", default="中文女")
    parser.add_argument("--prompt-audio", type=Path)
    parser.add_argument("--prompt-text")
    parser.add_argument("--prompt-text-file", type=Path)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--preview-count", type=int, help="Generate only the first N clips for a voice check; resume later")
    parser.add_argument("--only-ids", help="Comma-separated ids to regenerate after a targeted pronunciation edit")
    return parser


if __name__ == "__main__":
    generate(build_parser().parse_args())
