#!/usr/bin/env python3
"""
Discord video encoder — re-encodes clips to fit under a target file size.

Accepts one or more input files (or a directory) and encodes each to fit
under --max-mb (default 50MB) using two-pass H.264.

Usage:
    discord_encode.py [file/dir ...] [--output-dir DIR] [--max-mb N] [--ffmpeg PATH]

If no input is given, encodes everything in the current directory.
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def probe_duration(path: Path, ffprobe: str) -> float:
    r = subprocess.run(
        [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default", str(path)],
        capture_output=True, text=True,
    )
    for line in r.stdout.splitlines():
        if "duration=" in line:
            return float(line.split("=")[1])
    return 0.0


def encode_for_discord(
    input_path: Path,
    output_path: Path,
    max_mb: float = 50.0,
    ffmpeg: str = "ffmpeg",
    audio_kbps: int = 128,
) -> bool:
    ffprobe = ffmpeg.replace("ffmpeg", "ffprobe")
    duration = probe_duration(input_path, ffprobe)
    if duration <= 0:
        print(f"  [ERROR] Could not determine duration of {input_path.name}")
        return False

    # Target bitrate in kbps to fit within max_mb
    # Leave a small headroom (95%) to account for container overhead
    target_total_kbps = (max_mb * 1024 * 8 * 0.95) / duration
    video_kbps = max(100, int(target_total_kbps - audio_kbps))

    print(f"  {duration:.1f}s  target video bitrate: {video_kbps} kbps")

    with tempfile.TemporaryDirectory() as tmpdir:
        passlog = str(Path(tmpdir) / "ffmpeg2pass")

        # Pass 1 — analysis only
        r1 = subprocess.run(
            [ffmpeg, "-y",
             "-i", str(input_path),
             "-c:v", "libx264", "-b:v", f"{video_kbps}k",
             "-pass", "1", "-passlogfile", passlog,
             "-an", "-f", "null", os.devnull],
            capture_output=True, text=True,
        )
        if r1.returncode != 0:
            print(f"  [ERROR] Pass 1 failed:\n{r1.stderr[-2000:]}")
            return False

        # Pass 2 — encode
        r2 = subprocess.run(
            [ffmpeg, "-y",
             "-i", str(input_path),
             "-c:v", "libx264", "-b:v", f"{video_kbps}k",
             "-pass", "2", "-passlogfile", passlog,
             "-c:a", "aac", "-b:a", f"{audio_kbps}k",
             str(output_path)],
            capture_output=True, text=True,
        )
        if r2.returncode != 0:
            print(f"  [ERROR] Pass 2 failed:\n{r2.stderr[-2000:]}")
            return False

    if not output_path.exists():
        print(f"  [ERROR] Output missing")
        return False

    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"  OK  {size_mb:.1f} MB  -> {output_path.name}")

    if size_mb > max_mb:
        print(f"  [WARN] Output is {size_mb:.1f} MB, over {max_mb} MB limit")

    return True


def collect_inputs(args_inputs: list[str]) -> list[Path]:
    inputs = []
    for raw in args_inputs:
        p = Path(raw)
        if p.is_dir():
            inputs.extend(sorted(
                f for f in p.iterdir()
                if f.is_file() and f.suffix.lower() in (".mp4", ".mkv", ".mov", ".webm")
            ))
        elif p.is_file():
            inputs.append(p)
        else:
            print(f"  [WARN] Not found: {raw}")
    return inputs


def main():
    parser = argparse.ArgumentParser(
        description="Re-encode clips to fit Discord's file size limit.")
    parser.add_argument("inputs", nargs="*",
        help="Input files or directories. Defaults to current directory.")
    parser.add_argument("--output-dir", default=None,
        help="Where to write encoded files. Defaults to a 'discord' subfolder next to each input.")
    parser.add_argument("--max-mb",  type=float, default=50.0,
        help="Target max file size in MB. Default 50.")
    parser.add_argument("--ffmpeg",  default="ffmpeg")
    parser.add_argument("--audio-kbps", type=int, default=128,
        help="Audio bitrate in kbps. Default 128.")
    args = parser.parse_args()

    raw_inputs = args.inputs if args.inputs else ["."]
    inputs = collect_inputs(raw_inputs)

    if not inputs:
        print("No video files found.")
        sys.exit(0)

    ok = fail = 0
    for inp in inputs:
        print(f"\n{inp.name}")

        if args.output_dir:
            out_dir = Path(args.output_dir)
        else:
            out_dir = inp.parent / "discord"
        out_dir.mkdir(parents=True, exist_ok=True)

        output = out_dir / (inp.stem + "_discord.mp4")

        if encode_for_discord(inp, output,
                               max_mb=args.max_mb,
                               ffmpeg=args.ffmpeg,
                               audio_kbps=args.audio_kbps):
            ok += 1
        else:
            fail += 1

    print(f"\nDone.  {ok} OK, {fail} failed")


if __name__ == "__main__":
    main()
