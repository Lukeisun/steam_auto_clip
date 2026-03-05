#!/usr/bin/env python3
"""
Steam Game Recording Clip Extractor

Automatically finds all Steam game recordings on this machine and extracts
kill/death highlight clips from every session.

Usage:
    python steam_clips.py [--output-dir DIR]
                          [--before S] [--after S]
                          [--death-before S] [--death-after S]
                          [--death-respawn-lag S]
                          [--cluster-gap S]
                          [--events kill,death]
                          [--crf N] [--preset PRESET]
                          [--ffmpeg PATH]
                          [--debug-intermediate]
"""

import datetime
import json
import os
import re
import subprocess
import argparse
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET


# ---------------------------------------------------------------------------
# Steam discovery
# ---------------------------------------------------------------------------

def find_steam_root() -> Path | None:
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", ""), "Steam"),
        Path(os.environ.get("PROGRAMFILES", ""), "Steam"),
        Path(os.environ.get("HOME", ""), ".steam", "steam"),
        Path(os.environ.get("HOME", ""), "Library", "Application Support", "Steam"),
    ]
    for p in candidates:
        if (p / "userdata").is_dir():
            return p
    return None


def find_all_recording_dirs(steam_root: Path) -> list[Path]:
    """Return every gamerecordings dir found under userdata."""
    dirs = []
    for user_dir in sorted((steam_root / "userdata").iterdir()):
        rec = user_dir / "gamerecordings"
        if rec.is_dir():
            dirs.append(rec)
    return dirs


def get_persona_name(user_dir: Path) -> str:
    vdf = user_dir / "config" / "localconfig.vdf"
    if vdf.exists():
        m = re.search(r'"PersonaName"\s+"([^"]+)"',
                      vdf.read_text(encoding="utf-8", errors="replace"))
        if m:
            return m.group(1)
    return user_dir.name


def select_account(steam_root: Path) -> Path:
    """
    If there's only one account with recordings, use it automatically.
    Otherwise prompt the user to pick one.
    Returns the chosen gamerecordings directory.
    """
    candidates = []
    for user_dir in sorted((steam_root / "userdata").iterdir()):
        rec = user_dir / "gamerecordings"
        if rec.is_dir() and (rec / "video").is_dir():
            candidates.append((user_dir, rec))

    if not candidates:
        print("No Steam accounts with game recordings found.")
        sys.exit(1)

    if len(candidates) == 1:
        user_dir, rec = candidates[0]
        print(f"Using account: {get_persona_name(user_dir)}  (ID {user_dir.name})")
        return rec

    print("\nSelect Steam account:")
    for i, (user_dir, _) in enumerate(candidates, 1):
        print(f"  {i}. {get_persona_name(user_dir)}  (ID {user_dir.name})")
    while True:
        try:
            idx = int(input("Enter number: ").strip()) - 1
            if 0 <= idx < len(candidates):
                return candidates[idx][1]
        except (ValueError, EOFError):
            pass
        print(f"  Please enter a number between 1 and {len(candidates)}.")


# ---------------------------------------------------------------------------
# MPD / timeline parsing
# ---------------------------------------------------------------------------

def parse_mpd(mpd_path: Path) -> dict:
    ns        = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}
    root      = ET.parse(mpd_path).getroot()
    period    = root.find("mpd:Period", ns)
    video_seg = period.find(
        ".//mpd:AdaptationSet[@contentType='video']//mpd:SegmentTemplate", ns)
    timescale      = int(video_seg.get("timescale", 1000000))
    chunk_duration = int(video_seg.get("duration",  3000000))
    start_number   = int(video_seg.get("startNumber", 0))
    vid_id = period.find(
        ".//mpd:AdaptationSet[@contentType='video']//mpd:Representation", ns
    ).get("id", "0")
    aud_id = period.find(
        ".//mpd:AdaptationSet[@contentType='audio']//mpd:Representation", ns
    ).get("id", "1")
    return {
        "start_number":     start_number,
        "chunk_duration_s": chunk_duration / timescale,
        "video_rep_id":     vid_id,
        "audio_rep_id":     aud_id,
    }


def load_timeline(timeline_path: Path, event_types: set) -> tuple[list, int]:
    """Returns (events, daterecorded_unix) where daterecorded is session start."""
    with open(timeline_path, encoding="utf-8") as f:
        data = json.load(f)
    daterecorded = int(data.get("daterecorded", 0))
    events = []
    for entry in data.get("entries", []):
        if entry.get("type") != "event":
            continue
        if not any(t in entry.get("title", "").lower() for t in event_types):
            continue
        events.append({
            "time_ms":     int(entry["time"]),
            "title":       entry.get("title", ""),
            "description": entry.get("description", ""),
        })
    return sorted(events, key=lambda e: e["time_ms"]), daterecorded


# ---------------------------------------------------------------------------
# Session pairing
# ---------------------------------------------------------------------------

def pair_sessions(rec_dir: Path) -> list[tuple[Path, Path, float]]:
    """
    Returns [(timeline_json, video_dir, offset_s), ...] sorted by session date.

    offset_s: subtract from timeline time_ms/1000 to get video position.
    The timeline starts logging before the video folder is created, so the
    video is offset_s shorter than the timeline at the start.

    Pairing picks the closest unmatched video dir by wall-clock timestamp
    within a 600s window. 600s is generous enough to handle cases where the
    video dir is created several minutes after the timeline starts, while
    rejecting timelines whose video was never recorded / has been deleted.
    """
    timelines  = sorted((rec_dir / "timelines").glob("timeline_*.json"))
    video_dirs = sorted(
        d for d in (rec_dir / "video").iterdir()
        if d.is_dir() and (d / "session.mpd").exists()
    )

    def ts_seconds(name: str) -> int | None:
        m = re.search(r'(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})', name)
        if not m:
            return None
        import datetime as dt
        try:
            d = dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            int(m.group(4)), int(m.group(5)), int(m.group(6)))
            return int(d.timestamp())
        except ValueError:
            return None

    unmatched_vds = list(video_dirs)
    pairs = []
    for tl in timelines:
        tl_s = ts_seconds(tl.name)
        if tl_s is None:
            continue
        best, best_diff = None, 600
        for vd in unmatched_vds:
            vd_s = ts_seconds(vd.name)
            if vd_s is None:
                continue
            diff = abs(vd_s - tl_s)
            if diff < best_diff:
                best, best_diff = vd, diff
        if best is not None:
            unmatched_vds.remove(best)
            vd_s  = ts_seconds(best.name) or 0
            offset = float(vd_s - tl_s)   # positive = video starts this many seconds after timeline
            pairs.append((tl, best, offset))
        else:
            print(f"  WARNING: no video dir matched for {tl.name}, skipping.")

    if unmatched_vds:
        for vd in unmatched_vds:
            print(f"  WARNING: no timeline matched for {vd.name}, skipping.")

    return sorted(pairs, key=lambda t: t[1].name)


# ---------------------------------------------------------------------------
# Chunk helpers
# ---------------------------------------------------------------------------

def chunks_for_range(start_s: float, end_s: float, mpd: dict) -> list:
    cs = mpd["chunk_duration_s"]
    sn = mpd["start_number"]
    return list(range(sn + max(0, int(start_s / cs)),
                      sn + int(end_s / cs) + 1))


def first_chunk_start_s(chunk_num: int, mpd: dict) -> float:
    return (chunk_num - mpd["start_number"]) * mpd["chunk_duration_s"]


def concat_url(video_dir: Path, rep_id: str, chunk_nums: list) -> str:
    init  = video_dir / f"init-stream{rep_id}.m4s"
    parts = [str(init).replace("\\", "/")]
    for n in chunk_nums:
        p = video_dir / f"chunk-stream{rep_id}-{n:05d}.m4s"
        if p.exists():
            parts.append(str(p).replace("\\", "/"))
    return "concat:" + "|".join(parts)


# ---------------------------------------------------------------------------
# Clip extraction (two-pass)
# ---------------------------------------------------------------------------

def make_clip(
    video_dir: Path,
    mpd: dict,
    start_s: float,
    end_s: float,
    output_path: Path,
    ffmpeg: str = "ffmpeg",
) -> bool:
    """Two-pass clip extraction. Pass 1 resets timestamps, pass 2 stream-copies the trim."""
    vid_id     = mpd["video_rep_id"]
    aud_id     = mpd["audio_rep_id"]
    chunk_nums = chunks_for_range(start_s, end_s, mpd)
    existing   = [n for n in chunk_nums
                  if (video_dir / f"chunk-stream{vid_id}-{n:05d}.m4s").exists()]
    if not existing:
        print(f"  [SKIP] No chunks on disk for {start_s:.1f}s – {end_s:.1f}s")
        return False

    actual_start_s = first_chunk_start_s(existing[0],  mpd)
    actual_end_s   = first_chunk_start_s(existing[-1], mpd) + mpd["chunk_duration_s"]
    clip_start     = max(start_s, actual_start_s)
    clip_end       = min(end_s,   actual_end_s)
    if clip_end <= clip_start:
        print(f"  [SKIP] Clip window outside available chunks")
        return False

    trim_start = clip_start - actual_start_s
    trim_end   = clip_end   - actual_start_s
    vid_url    = concat_url(video_dir, vid_id, existing)
    aud_url    = concat_url(video_dir, aud_id, existing)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_mp4 = Path(tmpdir) / "intermediate.mp4"

        # Pass 1 — concat chunks -> zero-based intermediate
        r1 = subprocess.run(
            [ffmpeg, "-y", "-i", vid_url, "-i", aud_url,
             "-map", "0:v", "-map", "1:a", "-c", "copy",
             str(tmp_mp4).replace("\\", "/")],
            capture_output=True, text=True,
        )
        if r1.returncode != 0 or not tmp_mp4.exists() or tmp_mp4.stat().st_size < 1000:
            print(f"  [ERROR] Pass 1 failed:\n{r1.stderr[-2000:]}")
            return False

        # Pass 2 — stream-copy trim (cuts on nearest keyframe, instant)
        r2 = subprocess.run(
            [ffmpeg, "-y",
             "-ss", f"{trim_start:.6f}",
             "-to", f"{trim_end:.6f}",
             "-i", str(tmp_mp4).replace("\\", "/"),
             "-c", "copy",
             str(output_path).replace("\\", "/")],
            capture_output=True, text=True,
        )
        if r2.returncode != 0:
            print(f"  [ERROR] Pass 2 failed:\n{r2.stderr[-2000:]}")
            return False

    if not output_path.exists() or output_path.stat().st_size < 1000:
        print(f"  [ERROR] Output missing or empty")
        return False

    size_kb = output_path.stat().st_size // 1024
    ffprobe = ffmpeg.replace("ffmpeg", "ffprobe")
    probe   = subprocess.run(
        [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default", str(output_path)],
        capture_output=True, text=True,
    )
    dur_line = next((l for l in probe.stdout.splitlines() if "duration" in l), "")
    dur      = float(dur_line.split("=")[1]) if "=" in dur_line else 0.0
    print(f"  OK  {size_kb:,} KB  {dur:.1f}s")
    return True


# ---------------------------------------------------------------------------
# Full-session debug dump
# ---------------------------------------------------------------------------

def dump_session(
    video_dir: Path,
    mpd: dict,
    output_path: Path,
    event_markers: list,   # [(session_time_s, title), ...]
    ffmpeg: str = "ffmpeg",
) -> bool:
    """
    Binary-concatenate ALL chunks for a session and mux into a single MP4.
    Stream-copy only — no re-encode, no overlay. Fast.
    Prints the event timestamp list to stdout.
    """
    vid_id  = mpd["video_rep_id"]
    aud_id  = mpd["audio_rep_id"]
    all_vid = sorted(video_dir.glob(f"chunk-stream{vid_id}-?????.m4s"))
    all_aud = sorted(video_dir.glob(f"chunk-stream{aud_id}-?????.m4s"))
    if not all_vid:
        print("  [SKIP] No video chunks found")
        return False

    print(f"\n  Event timestamps:")
    for ev_s, title in sorted(event_markers):
        h  = int(ev_s) // 3600
        m  = (int(ev_s) % 3600) // 60
        s  = int(ev_s) % 60
        print(f"    {h:02d}:{m:02d}:{s:02d}  {title}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_dir = Path(tmpdir)
        tmp_vid = tmp_dir / "session_vid.mp4"
        tmp_aud = tmp_dir / "session_aud.mp4"

        vid_parts = [video_dir / f"init-stream{vid_id}.m4s"] + list(all_vid)
        aud_parts = [video_dir / f"init-stream{aud_id}.m4s"] + list(all_aud)

        with open(tmp_vid, "wb") as out:
            for p in vid_parts:
                out.write(p.read_bytes())
        with open(tmp_aud, "wb") as out:
            for p in aud_parts:
                out.write(p.read_bytes())

        r = subprocess.run(
            [ffmpeg, "-y",
             "-i", str(tmp_vid).replace("\\", "/"),
             "-i", str(tmp_aud).replace("\\", "/"),
             "-map", "0:v", "-map", "1:a", "-c", "copy",
             str(output_path).replace("\\", "/")],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f"  [ERROR] Session mux failed:\n{r.stderr[-2000:]}")
            return False

    if not output_path.exists() or output_path.stat().st_size < 1000:
        print(f"  [ERROR] Session output missing or empty")
        return False

    size_mb = output_path.stat().st_size // (1024 * 1024)
    print(f"  Session OK  {size_mb:,} MB  -> {output_path.name}")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Extract highlight clips from all Steam game recordings.")
    parser.add_argument("--output-dir",        default=None)
    parser.add_argument("--before",            type=float, default=20.0)
    parser.add_argument("--after",             type=float, default=10.0)
    parser.add_argument("--cluster-gap",       type=float, default=15.0,
        help="Merge events within this many seconds into one clip. Default 15. Set 0 to disable.")
    parser.add_argument("--events",            default="kill,death")
    parser.add_argument("--ffmpeg",            default="ffmpeg")
    parser.add_argument("--debug-intermediate", action="store_true",
        help="Dump full session MP4 with event list overlay instead of extracting clips.")
    args = parser.parse_args()

    steam_root = find_steam_root()
    if not steam_root:
        print("Could not find Steam installation.")
        sys.exit(1)

    rec_dir = select_account(steam_root)
    pairs   = pair_sessions(rec_dir)
    if not pairs:
        print("No sessions found.")
        sys.exit(0)

    out_dir = Path(args.output_dir) if args.output_dir else \
              Path.home() / "Videos" / "steam_clips"
    out_dir.mkdir(parents=True, exist_ok=True)

    event_types = {e.strip().lower() for e in args.events.split(",")}

    print(f"Output -> {out_dir}")
    ok = fail = 0

    for timeline_path, video_dir, tl_offset in pairs:
        print(f"\n=== {video_dir.name} ===  (timeline offset: {tl_offset:.0f}s)")

        mpd                   = parse_mpd(video_dir / "session.mpd")
        events, daterecorded  = load_timeline(timeline_path, event_types)

        if not events:
            print("  no matching events, skipping")
            continue

        def safe(s: str) -> str:
            return "".join(c if (c.isalnum() or c in "_-") else "_"
                           for c in s.replace(" ", "_"))

        def wall_clock(time_ms: int) -> str:
            """Convert timeline time_ms to a wall-clock datetime string."""
            ts = daterecorded + time_ms / 1000.0
            return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d_%H-%M-%S")

        # Build per-event clip windows.
        # Subtract tl_offset: the video starts tl_offset seconds after the
        # timeline starts, so video_pos = time_ms/1000 - tl_offset.
        windows = []
        for ev in events:
            event_s    = ev["time_ms"] / 1000.0 - tl_offset
            clip_start = max(0.0, event_s - args.before)
            clip_end   = event_s + args.after
            wc         = wall_clock(ev["time_ms"])
            label      = f"{wc}_{safe(ev['title'])}_{safe(ev['description'])[:30]}"
            windows.append((clip_start, clip_end, label, event_s, ev["title"]))

        # Cluster merging
        windows.sort(key=lambda w: w[0])
        clusters = []
        for start, end, label, event_s, title in windows:
            if clusters and start <= clusters[-1][1] + args.cluster_gap:
                cs, ce, labels, markers = clusters[-1]
                clusters[-1] = (cs, max(ce, end), labels + [label], markers + [(event_s, title)])
            else:
                clusters.append((start, end, [label], [(event_s, title)]))

        print(f"  {len(events)} events -> {len(clusters)} clip(s) after clustering")

        # Debug: dump full session, skip individual clips
        if args.debug_intermediate:
            all_markers = [(event_s, title) for _, _, _, event_s, title in windows]
            session_out = out_dir / f"{video_dir.name}_FULL_SESSION.mp4"
            print(f"\n  Dumping full session -> {session_out.name}")
            dump_session(video_dir, mpd, session_out, all_markers,
                         ffmpeg=args.ffmpeg)
            continue

        for i, (clip_start, clip_end, labels, markers) in enumerate(clusters, start=1):
            if len(labels) > 1:
                # Use the first event's wall-clock time for the cluster name
                first_label = labels[0]
                clip_name = f"{first_label}_CLUSTER_{len(labels)}events.mp4"
                desc      = f"cluster of {len(labels)}: " + ", ".join(labels)
            else:
                clip_name = f"{labels[0]}.mp4"
                desc      = labels[0]

            output_path = out_dir / clip_name
            duration    = clip_end - clip_start
            print(f"\n  [{i}/{len(clusters)}] {desc}")
            print(f"    {clip_start:.1f}s – {clip_end:.1f}s  ({duration:.0f}s)")

            if make_clip(video_dir, mpd, clip_start, clip_end, output_path,
                         ffmpeg=args.ffmpeg):
                ok += 1
            else:
                fail += 1

    print(f"\nDone.  {ok} clips OK, {fail} failed  ->  {out_dir}")


if __name__ == "__main__":
    main()
