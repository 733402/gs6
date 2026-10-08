#!/usr/bin/env python3
"""GridlessSekai6 Custom Chart Builder - terminal edition (no GUI / tkinter).

Easiest (no prompts): hand it a chart zip and it does everything
    python main.py mychart.zip           validates + compiles .android/.ios .chart.gs6
    python main.py a.zip b.zip           several at once (output lands next to each zip)
    python main.py UnCh-xxxx.zip         UntitledCharts exports work too (level.json +
                                         NSLevelData.json.gz + music/preview/jacket):
                                         they are converted automatically. A zip holding
                                         several exports (one folder per difficulty) is
                                         merged into ONE multi-difficulty chart.
    python main.py x.zip --set composer="Name" --set offset_ms=300   override fields

Search / download online, then convert straight to .gs6:
    python main.py --search "daisuki"    search UntitledCharts, Next SEKAI, Chart Cyanvas
                                         and official songs, pick a number, done
    python main.py --search "x" --source unch     only one source (unch ns chcy chcy-o pjsk)
    python main.py UnCh-xxxxxxxx...      a chart id or link downloads + converts directly
                                         (also coconut-next-sekai-N, chcy-..., or a sekai.best song id)
    python main.py some_folder           a downloaded/extracted chart folder works too
    (files go to ./out: downloads in out/downloads, .gs6 files next to them; --out DIR to change)

Interactive:
    python main.py                       step-by-step wizard, then a menu
    python main.py --import chart.zip    load a previously built zip, then menu
    python main.py --config chart.json   load a saved config, then menu

Non-interactive (scriptable, good for SSH / headless boxes):
    python main.py --config chart.json --zip out.zip
    python main.py --config chart.json --compile out_base
    python main.py --template > chart.json     print a config template

Relative paths inside a config file are resolved relative to that file.
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile

DIFFICULTIES = ["easy", "normal", "hard", "expert", "master", "append"]
MV_TYPES = ["ogmv", "2dmv"]
VOCAL_TYPES = ["sekai", "original_song", "another_vocal"]
MIN_LEVEL, MAX_LEVEL = 1, 99
MAX_CHARTS = 6
MAX_PREVIEW_SECONDS = 45.0
FORMAT_VERSION = 1

AUDIO_EXTS = ".wav .mp3 .flac .ogg .m4a .aac .opus"


# --------------------------------------------------------------------------- #
# Media / validation helpers (unchanged logic from the GUI version)
# --------------------------------------------------------------------------- #


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    flags = 0x08000000 if os.name == "nt" else 0
    return subprocess.run(cmd, capture_output=True, text=True, creationflags=flags)


def ffprobe_available() -> bool:
    try:
        return _run(["ffprobe", "-version"]).returncode == 0
    except FileNotFoundError:
        return False


def media_duration(path: str) -> float | None:
    cp = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ]
    )
    if cp.returncode != 0:
        return None
    try:
        return float(cp.stdout.strip())
    except ValueError:
        return None


def has_stream(path: str, kind: str) -> bool:
    cp = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            kind[0],
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            path,
        ]
    )
    return cp.returncode == 0 and kind in cp.stdout


def trim_audio(src: str, dst: str, seconds: float, fade: float = 2.0) -> bool:
    """Write the first `seconds` of `src` to `dst` (mp3) with a short fade-out."""
    try:
        cp = _run(
            [
                "ffmpeg", "-y", "-v", "error", "-i", src, "-t", f"{seconds:.3f}",
                "-af", f"afade=t=out:st={max(0.0, seconds - fade):.3f}:d={fade:.3f}",
                "-vn", "-c:a", "libmp3lame", "-q:a", "2", dst,
            ]
        )
    except FileNotFoundError:
        return False
    return cp.returncode == 0 and os.path.isfile(dst)


def validate_image(path: str) -> tuple[bool, str]:
    try:
        from PIL import Image
    except ImportError:
        return False, "Pillow is not installed (pip install Pillow)."
    try:
        with Image.open(path) as im:
            im.verify()
        return True, ""
    except Exception as e:
        return False, f"Not a readable image: {e}"


_TIME_SIG_RE = re.compile(r"^#\d{3}02:", re.IGNORECASE)


def validate_sus_text(text: str) -> tuple[bool, str]:
    lines = [ln.strip() for ln in text.splitlines()]
    sig_lines = [ln for ln in lines if _TIME_SIG_RE.match(ln)]
    if len(sig_lines) != 1:
        return (
            False,
            f"must have exactly one time-signature line, found {len(sig_lines)}",
        )
    if not any(ln.replace(" ", "") == "#00002:4" for ln in lines):
        return False, "missing the 4/4 time signature at measure 0 (#00002: 4)"
    return True, ""


def sus_combo(path: str) -> int:
    import sonolus_converters

    with open(path, "r", encoding="utf-8") as f:
        score = sonolus_converters.sus.load(f)
    return int(score.combo_count)


def sus_core_combo(path: str) -> int | None:
    """Taps/flicks/traces + judged slide heads/tails read back from a .sus file
    (no slide ticks, so no derived/hidden ticks). None if the library objects
    don't look as expected."""
    import sonolus_converters

    with open(path, "r", encoding="utf-8") as f:
        score = sonolus_converters.sus.load(f)
    n = singles = 0
    for note in score.notes:
        kind = type(note).__name__
        if kind == "Single":
            singles += 1
            if getattr(note, "type", "single") != "damage":
                n += 1
        elif kind == "Slide":
            for c in note.connections:
                ck = type(c).__name__
                if ck in ("SlideStartPoint", "SlideEndPoint") and getattr(
                    c, "judgeType", "normal"
                ) != "none":
                    n += 1
    return n if singles else None


def gen_double_uuid() -> str:
    return f"{uuid.uuid4()}_{uuid.uuid4()}"


def validate_double_uuid(s: str) -> bool:
    parts = s.strip().split("_")
    if len(parts) != 2:
        return False
    for p in parts:
        try:
            u = uuid.UUID(p)
        except (ValueError, AttributeError):
            return False
        if u.version != 4 or str(u) != p.lower():
            return False
    return True


def ext_of(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def safe_name(s: str) -> str:
    return re.sub(r"[^\w\-. ]+", "_", s).strip() or "custom_chart"


# --------------------------------------------------------------------------- #
# Config (plain dict) handling
# --------------------------------------------------------------------------- #


def new_cfg() -> dict:
    return {
        "jacket": "",
        "track": "",
        "track_pre": "",
        "mvs": [],  # [{"kind": "ogmv"|"2dmv", "path": "..."}]
        "charts": [],  # [{"difficulty": "expert", "level": 28, "path": "x.sus"}]
        "title": "",
        "charter": "",
        "lyricist": "",
        "composer": "",
        "arranger": "",
        "artist": "",
        "vocals": "",
        "collab": "",
        "is_full": True,
        "offset_ms": 0,
        "mv_offset_ms": 0,
        "original": "",
        "default_vocal": {"name": "", "type": "sekai"},
        "alt_vocals": [],  # [{"name","vocals","type","audio","preview","jacket"}]
        "identifier": gen_double_uuid(),
    }


def template_cfg() -> dict:
    c = new_cfg()
    c.update(
        {
            "jacket": "jacket.png",
            "track": "track.wav",
            "track_pre": "track_pre.wav",
            "mvs": [{"kind": "ogmv", "path": "mv.mp4"}],
            "charts": [{"difficulty": "expert", "level": 28, "path": "expert.sus"}],
            "title": "My Song",
            "charter": "Me",
            "lyricist": "Lyricist",
            "composer": "Composer",
            "arranger": "Arranger",
            "artist": "Artist",
            "vocals": "Vocals",
        }
    )
    return c


PATH_KEYS = ("jacket", "track", "track_pre")


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("config must be a JSON object")
    cfg = new_cfg()
    for k, v in raw.items():
        if k in cfg:
            cfg[k] = v
    base = os.path.dirname(os.path.abspath(path))

    def rp(p):
        if not p:
            return ""
        p = os.path.expanduser(str(p))
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))

    for k in PATH_KEYS:
        cfg[k] = rp(cfg[k])
    cfg["mvs"] = [
        {"kind": m.get("kind", "ogmv"), "path": rp(m.get("path"))}
        for m in cfg.get("mvs") or []
    ]
    cfg["charts"] = [
        {
            "difficulty": c.get("difficulty", "expert"),
            "level": c.get("level", 1),
            "path": rp(c.get("path")),
        }
        for c in cfg.get("charts") or []
    ]
    cfg["alt_vocals"] = [
        {
            "name": a.get("name", ""),
            "vocals": a.get("vocals", ""),
            "type": a.get("type", "original_song"),
            "audio": rp(a.get("audio")),
            "preview": rp(a.get("preview")),
            "jacket": rp(a.get("jacket")),
        }
        for a in cfg.get("alt_vocals") or []
    ]
    dv = cfg.get("default_vocal") or {}
    cfg["default_vocal"] = {"name": dv.get("name", ""), "type": dv.get("type", "sekai")}
    return cfg


def save_config(cfg: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# Prompt helpers
# --------------------------------------------------------------------------- #


class Abort(Exception):
    pass


def _input(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        raise Abort()


def section(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


def ask(label, default="", required=False, validate=None) -> str:
    """Prompt for a string. Enter keeps the default; '-' clears an optional field."""
    while True:
        shown = f" [{default}]" if default not in ("", None) else ""
        raw = _input(f"{label}{shown}: ").strip()
        if raw == "-" and not required:
            val = ""
        elif raw == "":
            val = "" if default is None else str(default)
        else:
            val = raw
        if required and not val:
            print("  ! required.")
            continue
        if validate and val:
            err = validate(val)
            if err:
                print(f"  ! {err}")
                continue
        return val


def ask_yn(label: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = _input(f"{label} [{hint}]: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  ! please answer y or n.")


def ask_int(label, default=0, lo=None, hi=None) -> int:
    rng = ""
    if lo is not None and hi is not None:
        rng = f" ({lo}-{hi})"
    elif lo is not None:
        rng = f" (>= {lo})"
    while True:
        raw = _input(f"{label}{rng} [{default}]: ").strip()
        if not raw:
            return int(default)
        try:
            v = int(raw)
        except ValueError:
            print("  ! must be an integer.")
            continue
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            print(f"  ! out of range{rng}.")
            continue
        return v


def ask_choice(label: str, choices: list[str], default: str) -> str:
    opts = " / ".join(f"{i + 1}={c}" for i, c in enumerate(choices))
    while True:
        raw = _input(f"{label} ({opts}) [{default}]: ").strip().lower()
        if not raw:
            return default
        if raw in choices:
            return raw
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1]
        print("  ! pick one of the listed options.")


def clean_path(raw: str) -> str:
    """Handle drag-and-dropped paths: surrounding quotes, escaped spaces, ~."""
    p = raw.strip()
    if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'":
        p = p[1:-1]
    if os.name != "nt":
        p = p.replace("\\ ", " ")
    return os.path.expanduser(p)


def ask_path(label, default="", required=True, validate=None) -> str:
    while True:
        shown = f" [{default}]" if default else ""
        raw = _input(f"{label}{shown}: ").strip()
        if raw == "-" and not required:
            return ""
        p = clean_path(raw) if raw else (default or "")
        if not p:
            if required:
                print("  ! required.")
                continue
            return ""
        if not os.path.isfile(p):
            print(f"  ! file not found: {p}")
            continue
        if validate:
            err = validate(p)
            if err:
                print(f"  ! {err}")
                continue
        return p


# Per-field validators (return an error string, or "" if fine)


def v_image(p: str) -> str:
    ok, msg = validate_image(p)
    return "" if ok else msg


def v_audio(p: str) -> str:
    if ffprobe_available() and not has_stream(p, "audio"):
        return "no audio stream found."
    return ""


def v_preview(p: str) -> str:
    err = v_audio(p)
    if err:
        return err
    if ffprobe_available():
        d = media_duration(p)
        if d is None:
            return "could not read duration."
        if d > MAX_PREVIEW_SECONDS + 0.05:
            return f"preview is {d:.1f}s (max {MAX_PREVIEW_SECONDS:.0f}s)."
    return ""


def v_video(p: str) -> str:
    if ffprobe_available() and not has_stream(p, "video"):
        return "no video stream found."
    return ""


def v_sus(p: str) -> str:
    try:
        with open(p, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception as e:
        return f"cannot read .sus ({e})."
    ok, msg = validate_sus_text(text)
    if not ok:
        return msg + "."
    try:
        print(f"    combo: {sus_combo(p)}")
    except Exception as e:
        return f"combo derivation failed ({e})."
    return ""


# --------------------------------------------------------------------------- #
# Interactive section editors
# --------------------------------------------------------------------------- #


def edit_files(cfg: dict) -> None:
    section("Files")
    if not ffprobe_available():
        print("  (ffprobe not found on PATH: audio/video/preview checks are skipped)")
    cfg["jacket"] = ask_path("Jacket image", cfg["jacket"], validate=v_image)
    cfg["track"] = ask_path("Audio track", cfg["track"], validate=v_audio)
    cfg["track_pre"] = ask_path(
        f"Preview audio (<= {MAX_PREVIEW_SECONDS:.0f}s)",
        cfg["track_pre"],
        validate=v_preview,
    )
    n = ask_int("Number of music videos", len(cfg["mvs"]), 0, 2)
    old = cfg["mvs"]
    mvs = []
    for i in range(n):
        prev = old[i] if i < len(old) else {}
        kind = ask_choice(
            f"  MV {i + 1} type", MV_TYPES, prev.get("kind", MV_TYPES[i % 2])
        )
        path = ask_path(f"  MV {i + 1} file", prev.get("path", ""), validate=v_video)
        mvs.append({"kind": kind, "path": path})
    cfg["mvs"] = mvs


def edit_charts(cfg: dict) -> None:
    section("Charts (.sus)")
    n = ask_int("Number of charts", max(1, len(cfg["charts"])), 1, MAX_CHARTS)
    old = cfg["charts"]
    charts = []
    for i in range(n):
        prev = old[i] if i < len(old) else {}
        used = {c["difficulty"] for c in charts}
        fallback = next((d for d in DIFFICULTIES if d not in used), DIFFICULTIES[0])
        print(f"- Chart {i + 1}")
        while True:
            diff = ask_choice("  difficulty", DIFFICULTIES, prev.get("difficulty", fallback))
            if diff in used:
                print(f"  ! '{diff}' already used.")
                prev = {}
                continue
            break
        level = ask_int("  level", prev.get("level", 1), MIN_LEVEL, MAX_LEVEL)
        path = ask_path("  .sus file", prev.get("path", ""), validate=v_sus)
        charts.append({"difficulty": diff, "level": level, "path": path})
    cfg["charts"] = charts


def edit_metadata(cfg: dict) -> None:
    section("Metadata  (* = required, '-' clears optional fields)")
    for key, label, req in [
        ("title", "Title*", True),
        ("charter", "Charter*", True),
        ("lyricist", "Lyricist*", True),
        ("composer", "Composer*", True),
        ("arranger", "Arranger*", True),
        ("artist", "Artist*", True),
        ("vocals", "Vocals*", True),
    ]:
        cfg[key] = ask(label, cfg[key], required=req)
    cfg["is_full"] = ask_yn("Full-length (not a game-size cut)?", cfg["is_full"])
    cfg["offset_ms"] = ask_int("offset_ms (chart timing offset)", cfg["offset_ms"])
    cfg["mv_offset_ms"] = ask_int(
        "additional_mv_offset_ms (extra MV offset)", cfg["mv_offset_ms"]
    )
    cfg["original"] = ask("original URL (optional)", cfg["original"])
    cfg["collab"] = ask("Collab (optional)", cfg["collab"])


def edit_vocals(cfg: dict) -> None:
    section("Vocals (alt vocals / covers)")
    print(
        "Leave off for a single-vocal song. If you add alt vocals, name the DEFAULT\n"
        "vocal (it uses the main track + jacket), then add each alternate with its\n"
        "own audio/preview and an optional jacket."
    )
    if not ask_yn("Add alternate vocals?", bool(cfg["alt_vocals"])):
        cfg["alt_vocals"] = []
        cfg["default_vocal"] = {"name": "", "type": "sekai"}
        return
    dv = cfg["default_vocal"]
    dv["name"] = ask("Default vocal name", dv["name"], required=True)
    dv["type"] = ask_choice("Default vocal type", VOCAL_TYPES, dv["type"])
    n = ask_int("Number of alt vocals", max(1, len(cfg["alt_vocals"])), 1)
    old = cfg["alt_vocals"]
    alts = []
    for i in range(n):
        prev = old[i] if i < len(old) else {}
        print(f"- Alt vocal {i + 1}")
        alts.append(
            {
                "name": ask("  name", prev.get("name", ""), required=True),
                "vocals": ask("  vocals", prev.get("vocals", ""), required=True),
                "type": ask_choice(
                    "  type", VOCAL_TYPES, prev.get("type", "original_song")
                ),
                "audio": ask_path("  audio", prev.get("audio", ""), validate=v_audio),
                "preview": ask_path(
                    "  preview", prev.get("preview", ""), validate=v_preview
                ),
                "jacket": ask_path(
                    "  jacket (optional)",
                    prev.get("jacket", ""),
                    required=False,
                    validate=v_image,
                ),
            }
        )
    cfg["alt_vocals"] = alts


def edit_identity(cfg: dict) -> None:
    section("Identity")
    print(
        "This identifier uniquely names the chart. Re-exporting with the same identifier\n"
        "updates that chart in the game instead of adding a duplicate. Keep it after\n"
        "your first export. Custom values must be two valid v4 UUIDs joined by '_'."
    )
    print(f"Current: {cfg['identifier']}")

    def check(v: str) -> str:
        if v.lower() == "new":
            return ""
        return "" if validate_double_uuid(v) else "must be two valid v4 UUIDs joined by '_'."

    val = ask("Identifier (Enter = keep, 'new' = regenerate)", cfg["identifier"], validate=check)
    cfg["identifier"] = gen_double_uuid() if val.lower() == "new" else val.strip()


def show_summary(cfg: dict) -> None:
    section("Summary")
    print(f"Title      : {cfg['title'] or '(unset)'}   Charter: {cfg['charter'] or '(unset)'}")
    print(
        f"Artist     : {cfg['artist'] or '-'}  | Vocals: {cfg['vocals'] or '-'}  "
        f"| Composer: {cfg['composer'] or '-'}"
    )
    print(f"Jacket     : {cfg['jacket'] or '(unset)'}")
    print(f"Track      : {cfg['track'] or '(unset)'}")
    print(f"Preview    : {cfg['track_pre'] or '(unset)'}")
    for m in cfg["mvs"]:
        print(f"MV ({m['kind']:>4}) : {m['path']}")
    for c in cfg["charts"]:
        print(f"Chart      : {c['difficulty']:<7} Lv{c['level']:<3} {c['path']}")
    if cfg["alt_vocals"]:
        print(
            f"Vocals     : default '{cfg['default_vocal']['name']}' + "
            f"{len(cfg['alt_vocals'])} alt"
        )
    print(f"Identifier : {cfg['identifier']}")


# --------------------------------------------------------------------------- #
# Collect / validate (same checks as the GUI version, but returns errors)
# --------------------------------------------------------------------------- #


def collect(cfg: dict) -> tuple[dict | None, list[str]]:
    errs: list[str] = []

    def need(path, label):
        if not path or not os.path.isfile(path):
            errs.append(f"{label} is required (file not found).")
            return False
        return True

    if need(cfg["jacket"], "Jacket"):
        ok, msg = validate_image(cfg["jacket"])
        if not ok:
            errs.append(f"Jacket: {msg}")

    ff = ffprobe_available()
    if need(cfg["track"], "Audio track") and ff and not has_stream(cfg["track"], "audio"):
        errs.append("Audio track has no audio stream.")
    if need(cfg["track_pre"], "Preview audio") and ff:
        dur = media_duration(cfg["track_pre"])
        if dur is None:
            errs.append("Preview audio: could not read duration.")
        elif dur > MAX_PREVIEW_SECONDS + 0.05:
            errs.append(f"Preview audio is {dur:.1f}s (max {MAX_PREVIEW_SECONDS:.0f}s).")

    diffs = []
    seen = set()
    rows = [r for r in cfg["charts"] if (r.get("path") or "").strip()]
    if not rows:
        errs.append("At least one .sus chart is required.")
    if len(rows) > MAX_CHARTS:
        errs.append(f"At most {MAX_CHARTS} charts are allowed.")
    for r in rows:
        d = r.get("difficulty")
        if d not in DIFFICULTIES:
            errs.append(f"Unknown difficulty '{d}'.")
            continue
        if d in seen:
            errs.append(f"Duplicate difficulty '{d}'.")
        seen.add(d)
        if not os.path.isfile(r["path"]):
            errs.append(f"{d}: .sus file not found.")
            continue
        try:
            lvl = int(r.get("level"))
        except (TypeError, ValueError):
            errs.append(f"{d}: level must be a number.")
            continue
        if not (MIN_LEVEL <= lvl <= MAX_LEVEL):
            errs.append(f"{d}: level must be {MIN_LEVEL}-{MAX_LEVEL}.")
        try:
            with open(r["path"], "r", encoding="utf-8") as f:
                text = f.read()
        except Exception as e:
            errs.append(f"{d}: cannot read .sus ({e}).")
            continue
        ok, msg = validate_sus_text(text)
        if not ok:
            errs.append(f"{d}: {msg}.")
        try:
            combo = sus_combo(r["path"])
        except Exception as e:
            errs.append(f"{d}: combo derivation failed ({e}).")
            continue
        diffs.append({"difficulty": d, "level": lvl, "notes": combo, "_path": r["path"]})

    mvs = [m for m in cfg["mvs"] if (m.get("path") or "").strip()]
    og_mv = next((m["path"] for m in mvs if m["kind"] == "ogmv"), None)
    two_d_mv = next((m["path"] for m in mvs if m["kind"] == "2dmv"), None)
    for label, p in (("ogmv", og_mv), ("2dmv", two_d_mv)):
        if p:
            if not os.path.isfile(p):
                errs.append(f"{label}: file not found.")
            elif ff and not has_stream(p, "video"):
                errs.append(f"{label}: no video stream.")
    kinds = [m["kind"] for m in mvs]
    if kinds.count("ogmv") > 1:
        errs.append("Only one 'ogmv' music video is allowed (ogmv/ogmv is unsupported).")
    if kinds.count("2dmv") > 1:
        errs.append("Only one '2dmv' music video is allowed (2dmv/2dmv is unsupported).")

    for key, lab in [
        ("title", "Title"),
        ("charter", "Charter"),
        ("lyricist", "Lyricist"),
        ("composer", "Composer"),
        ("arranger", "Arranger"),
        ("artist", "Artist"),
        ("vocals", "Vocals"),
    ]:
        if not str(cfg[key]).strip():
            errs.append(f"{lab} is required.")

    def as_int(key, label):
        try:
            return int(cfg[key])
        except (TypeError, ValueError):
            errs.append(f"{label} must be an integer.")
            return 0

    offset = as_int("offset_ms", "offset_ms")
    mv_offset = as_int("mv_offset_ms", "additional_mv_offset_ms")

    cover_files = []
    alt_entries = []
    alts = [
        v
        for v in cfg["alt_vocals"]
        if (v.get("name") or "").strip()
        or (v.get("audio") or "").strip()
        or (v.get("vocals") or "").strip()
    ]
    if alts and not cfg["default_vocal"]["name"].strip():
        errs.append("Default vocal name is required when alt vocals are present.")
    for idx, v in enumerate(alts, start=1):
        nm, vo = v["name"].strip(), v["vocals"].strip()
        au, pv, jk = v["audio"].strip(), v["preview"].strip(), v["jacket"].strip()
        if not nm:
            errs.append(f"Alt vocal {idx}: name is required.")
        if not vo:
            errs.append(f"Alt vocal {idx}: vocals are required.")
        if v.get("type") not in VOCAL_TYPES:
            errs.append(f"Alt vocal {idx}: type must be one of {VOCAL_TYPES}.")
        if not au or not os.path.isfile(au):
            errs.append(f"Alt vocal {idx}: audio file is required.")
        elif ff and not has_stream(au, "audio"):
            errs.append(f"Alt vocal {idx}: audio has no audio stream.")
        if not pv or not os.path.isfile(pv):
            errs.append(f"Alt vocal {idx}: preview is required.")
        elif ff:
            d = media_duration(pv)
            if d is not None and d > MAX_PREVIEW_SECONDS + 0.05:
                errs.append(
                    f"Alt vocal {idx}: preview is {d:.1f}s (max {MAX_PREVIEW_SECONDS:.0f}s)."
                )
        has_jacket = bool(jk)
        if has_jacket:
            if not os.path.isfile(jk):
                errs.append(f"Alt vocal {idx}: jacket not found.")
            else:
                ok, msg = validate_image(jk)
                if not ok:
                    errs.append(f"Alt vocal {idx}: jacket {msg}")
        alt_entries.append(
            {
                "id": idx,
                "name": nm,
                "vocals": vo,
                "vocal_type": v.get("type", "original_song"),
                "has_jacket": has_jacket,
            }
        )
        if au:
            cover_files.append((au, f"covers/cover_{idx}" + ext_of(au)))
        if pv:
            cover_files.append((pv, f"covers/cover_pre_{idx}" + ext_of(pv)))
        if jk:
            cover_files.append((jk, f"covers/cover_jacket_{idx}" + ext_of(jk)))

    song_duration = None
    if cfg["track"].strip() and ff:
        song_duration = media_duration(cfg["track"])
        if song_duration is not None:
            song_duration = round(song_duration, 3)

    if not validate_double_uuid(str(cfg["identifier"])):
        errs.append("Identifier must be two valid v4 UUIDs joined by '_'.")

    if errs:
        return None, errs

    def tr(v):
        v = str(v).strip()
        return {"jp": v, "en": v}

    info = {
        "identifier": cfg["identifier"].strip(),
        "format_version": FORMAT_VERSION,
        "title": tr(cfg["title"]),
        "charter": cfg["charter"].strip(),
        "difficulties": [
            {"difficulty": d["difficulty"], "level": d["level"], "notes": d["notes"]}
            for d in diffs
        ],
        "lyricist": tr(cfg["lyricist"]),
        "composer": tr(cfg["composer"]),
        "arranger": tr(cfg["arranger"]),
        "artist": tr(cfg["artist"]),
        "vocals": tr(cfg["vocals"]),
        "isFullLength": bool(cfg["is_full"]),
        "vocaloid_or_other": "other",
        "offset_ms": offset,
        "additional_mv_offset_ms": mv_offset,
        "song_duration": song_duration,
        "original": str(cfg["original"]).strip() or None,
        "original_music_video": og_mv is not None,
        "2d_music_video": two_d_mv is not None,
    }
    if str(cfg["collab"]).strip():
        info["collab"] = tr(cfg["collab"])

    if alt_entries:

        def d2(s):
            return {"jp": s, "en": s}

        covers = [
            {
                "default": True,
                "name": d2(cfg["default_vocal"]["name"].strip()),
                "vocal_type": cfg["default_vocal"]["type"],
            }
        ]
        for a in alt_entries:
            covers.append(
                {
                    "default": False,
                    "id": a["id"],
                    "name": d2(a["name"]),
                    "vocals": d2(a["vocals"]),
                    "vocal_type": a["vocal_type"],
                    "has_jacket": a["has_jacket"],
                }
            )
        info["covers"] = covers

    return {
        "info": info,
        "diffs": diffs,
        "jacket": cfg["jacket"],
        "track": cfg["track"],
        "track_pre": cfg["track_pre"],
        "ogmv": og_mv,
        "2dmv": two_d_mv,
        "cover_files": cover_files,
    }, []


def print_errors(errs: list[str]) -> None:
    print("\nCannot build:")
    for e in errs:
        print(f"  * {e}")


# --------------------------------------------------------------------------- #
# Build / compile / import
# --------------------------------------------------------------------------- #


def write_zip(out: str, data: dict) -> None:
    info = data["info"]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("ChartInfo.json", json.dumps(info, ensure_ascii=False, indent=2))
        z.write(data["jacket"], "jacket" + ext_of(data["jacket"]))
        z.write(data["track"], "track" + ext_of(data["track"]))
        z.write(data["track_pre"], "track_pre" + ext_of(data["track_pre"]))
        for d in data["diffs"]:
            z.write(d["_path"], f"{d['difficulty']}.sus")
        if data["ogmv"]:
            z.write(data["ogmv"], "mv" + ext_of(data["ogmv"]))
        if data["2dmv"]:
            z.write(data["2dmv"], "2dmv" + ext_of(data["2dmv"]))
        for src, zip_name in data.get("cover_files", []):
            z.write(src, zip_name)


def default_base(data: dict) -> str:
    t = data["info"]["title"]
    return safe_name(t["en"] or t["jp"] or "custom_chart")


def confirm_overwrite(path: str, assume_yes: bool) -> bool:
    if not os.path.exists(path) or assume_yes:
        return True
    return ask_yn(f"{path} exists. Overwrite?", False)


def do_build(data: dict, out: str, assume_yes: bool = False) -> bool:
    if not out.lower().endswith(".zip"):
        out += ".zip"
    if not confirm_overwrite(out, assume_yes):
        return False
    try:
        write_zip(out, data)
    except Exception as e:
        print(f"Build failed: {e}")
        return False
    print(f"Built: {out}\nKeep the ZIP for when you want to update the chart.")
    return True


def strip_compile_suffixes(base: str) -> str:
    changed = True
    while changed:
        changed = False
        for suf in (".chart.gs6", ".gs6", ".zip", ".android", ".ios"):
            if base.lower().endswith(suf):
                base = base[: -len(suf)]
                changed = True
    return base


def do_compile(data: dict, out_base: str, assume_yes: bool = False) -> bool:
    try:
        import encode
    except Exception as e:
        print(
            f"Compiler unavailable: could not load encode.py:\n  {e}\n"
            "The .chart.gs6 compiler needs cricodecs, UnityPy, Pillow and ffmpeg "
            "(see requirements.txt) plus the base bundles in the bases folder."
        )
        return False
    bases = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bases")
    missing = encode.missing_bases(bases, want_mv=(data["ogmv"] or data["2dmv"]))
    if missing:
        print("Missing base bundles in the bases folder:")
        for m in missing:
            print(f"  * {m}")
        return False
    base = strip_compile_suffixes(out_base)
    targets = [f"{base}.android.chart.gs6", f"{base}.ios.chart.gs6"]
    for t in targets:
        if not confirm_overwrite(t, assume_yes):
            return False
    print("Compiling .chart.gs6, this can take a minute...")
    try:
        outputs = encode.compile_chart(data, bases, base, log=lambda m: print(f"  {m}"))
    except Exception:
        import traceback

        print("Compile failed:\n" + traceback.format_exc())
        return False
    print("Compiled (one encrypted bundle set per platform):")
    for o in outputs:
        print(f"  {o}")
    print(f"\nIdentifier (save this to update the chart later):\n  {data['info']['identifier']}")
    return True


_IMPORT_DIRS: list[str] = []


def _cleanup_imports() -> None:
    for d in _IMPORT_DIRS:
        shutil.rmtree(d, ignore_errors=True)


atexit.register(_cleanup_imports)


def load_from_zip(path: str) -> dict:
    tmp = tempfile.mkdtemp(prefix="gs6chart_")
    _IMPORT_DIRS.append(tmp)
    root = os.path.realpath(tmp)
    with zipfile.ZipFile(path) as z:
        for member in z.namelist():  # refuse path traversal
            dest = os.path.realpath(os.path.join(root, member))
            if dest != root and not dest.startswith(root + os.sep):
                raise ValueError(f"unsafe path in zip: {member}")
        z.extractall(root)
    return load_from_dir(root)


def load_from_dir(root: str) -> dict:
    """Load a chart from a folder: a built chart (ChartInfo.json), an UntitledCharts /
    Next SEKAI / Chart Cyanvas export, or an official-chart download."""
    root = os.path.realpath(root)
    info_path = ""
    for r, _d, files in sorted(os.walk(root), key=lambda t: t[0].count(os.sep)):
        if "ChartInfo.json" in files:
            info_path = os.path.join(r, "ChartInfo.json")
            break
    if not info_path:
        return _load_pjsk(root) if _find_pjsk_meta(root) else _load_unch(root)
    with open(info_path, "r", encoding="utf-8") as f:
        info = json.load(f)

    def find(stem: str) -> str:
        for r, _d, files in os.walk(root):
            for fn in files:
                if os.path.splitext(fn)[0] == stem:
                    return os.path.join(r, fn)
        return ""

    def one(v) -> str:
        if isinstance(v, dict):
            return v.get("en") or v.get("jp") or ""
        return v or ""

    cfg = new_cfg()
    cfg["jacket"] = find("jacket")
    cfg["track"] = find("track")
    cfg["track_pre"] = find("track_pre")
    if find("mv"):
        cfg["mvs"].append({"kind": "ogmv", "path": find("mv")})
    if find("2dmv"):
        cfg["mvs"].append({"kind": "2dmv", "path": find("2dmv")})
    for d in info.get("difficulties", []):
        diff = d.get("difficulty", "expert")
        cfg["charts"].append(
            {"difficulty": diff, "level": int(d.get("level", 1)), "path": find(diff)}
        )
    cfg["title"] = one(info.get("title"))
    cfg["charter"] = info.get("charter", "") or ""
    for k in ("lyricist", "composer", "arranger", "artist", "vocals", "collab"):
        cfg[k] = one(info.get(k))
    cfg["is_full"] = bool(info.get("isFullLength", True))
    cfg["offset_ms"] = int(info.get("offset_ms", 0))
    cfg["mv_offset_ms"] = int(info.get("additional_mv_offset_ms", 0))
    cfg["original"] = info.get("original") or ""
    if info.get("identifier"):
        cfg["identifier"] = info["identifier"]
    for c in info.get("covers", []):
        if c.get("default"):
            cfg["default_vocal"] = {
                "name": one(c.get("name")),
                "type": c.get("vocal_type", "sekai"),
            }
        else:
            cid = c.get("id")
            cfg["alt_vocals"].append(
                {
                    "name": one(c.get("name")),
                    "vocals": one(c.get("vocals")),
                    "type": c.get("vocal_type", "original_song"),
                    "audio": find(f"cover_{cid}"),
                    "preview": find(f"cover_pre_{cid}"),
                    "jacket": find(f"cover_jacket_{cid}"),
                }
            )
    return cfg


def _fit_preview(cfg: dict, warnings: list[str]) -> None:
    """Trim the preview to the allowed length (first 44.5s + fade-out) when needed."""
    if not (cfg.get("track_pre") and ffprobe_available()):
        return
    d = media_duration(cfg["track_pre"])
    if d is None or d <= MAX_PREVIEW_SECONDS + 0.05:
        return
    out = os.path.join(os.path.dirname(cfg["track_pre"]), "preview_trimmed.mp3")
    if trim_audio(cfg["track_pre"], out, MAX_PREVIEW_SECONDS - 0.5):
        warnings.append(
            f"preview was {d:.1f}s (max {MAX_PREVIEW_SECONDS:.0f}s): used its "
            f"first {MAX_PREVIEW_SECONDS - 0.5:.1f}s with a short fade-out"
        )
        cfg["track_pre"] = out
    else:
        warnings.append(
            f"preview is {d:.1f}s (max {MAX_PREVIEW_SECONDS:.0f}s) and could not "
            "be trimmed automatically (is ffmpeg on PATH?)"
        )


def _find_pjsk_meta(root: str) -> tuple[str, dict] | None:
    """(folder, level.json) of an official-chart download made by fetch.py."""
    for r, _d, files in os.walk(root):
        if "level.json" not in files:
            continue
        try:
            with open(os.path.join(r, "level.json"), "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        g = meta.get("_gs6") if isinstance(meta, dict) else None
        if isinstance(g, dict) and g.get("source") == "pjsk":
            return r, meta
    return None


def _load_pjsk(root: str) -> dict:
    """An official game chart downloaded from sekai.best (one .sus per difficulty)."""
    found = _find_pjsk_meta(root)
    if not found:
        raise ValueError("not an official-chart download (level.json with a _gs6 block missing).")
    r, meta = found
    g = meta["_gs6"]
    order = {d: i for i, d in enumerate(DIFFICULTIES)}
    cfg = new_cfg()
    warnings: list[str] = []
    combos: list = []
    for d in sorted(g.get("difficulties", []), key=lambda d: order.get(d.get("musicDifficulty"), 99)):
        name = d.get("musicDifficulty")
        path = os.path.join(r, f"{name}.sus")
        if name not in order or not os.path.isfile(path):
            continue
        lvl = max(MIN_LEVEL, min(MAX_LEVEL, int(d.get("playLevel") or 1)))
        cfg["charts"].append({"difficulty": name, "level": lvl, "path": path})
        want = d.get("totalNoteCount")
        try:
            got = sus_combo(path)
        except Exception:
            got = None
        combos.append(got)
        if want and got is not None and got != want:
            warnings.append(f"{name}: combo {got} here vs {want} in the game data")
    if not cfg["charts"]:
        raise ValueError("no chart files found in the download.")
    for key, fname in (("jacket", "jacket.png"), ("track", "music.mp3"), ("track_pre", "preview.mp3")):
        p = os.path.join(r, fname)
        if not os.path.isfile(p):
            raise ValueError(f"{fname} is missing from the download.")
        cfg[key] = p

    def txt(k: str) -> str:
        v = str(meta.get(k) or "").strip()
        return v if v else "-"

    cfg["title"] = txt("title")
    cfg["composer"], cfg["lyricist"], cfg["arranger"] = txt("composer"), txt("lyricist"), txt("arranger")
    cfg["artist"] = cfg["composer"]
    cfg["vocals"] = str(g.get("vocals") or "").strip() or "-"
    cfg["charter"] = "SEGA"
    cfg["is_full"] = True
    cfg["identifier"] = _stable_identifier(f"pjsk:{g.get('region')}:{meta.get('id')}:{g.get('vocal_id')}")
    _fit_preview(cfg, warnings)

    n = len(cfg["charts"])
    print(
        f"Detected official chart: {cfg['title']} - {cfg['artist']}  "
        f"[{g.get('region')}, {g.get('vocal_caption') or 'vocal version'}]\n"
        f"  {n} chart{'s' if n != 1 else ''}:"
    )
    for c, got in zip(cfg["charts"], combos):
        print(f"    {c['difficulty']:<7} Lv{c['level']:<3} combo {got}")
    for w in warnings:
        print(f"  ! {w}")
    print(
        "  music offset 0 ms (official audio); charter is set to SEGA. Change anything "
        "with --set, e.g. --set offset_ms=300"
    )
    return cfg


def _stable_identifier(seed: str) -> str:
    import hashlib

    def one(tag: str) -> str:
        h = hashlib.sha256(f"gs6:{seed}:{tag}".encode("utf-8")).digest()[:16]
        return str(uuid.UUID(bytes=h, version=4))

    return f"{one('a')}_{one('b')}"


def _load_unch(root: str) -> dict:
    """Fallback: the zip is an UntitledCharts export (level.json + NSLevelData.json.gz)."""
    try:
        import unch
    except ImportError as e:
        raise ValueError(f"ChartInfo.json not found, and unch.py could not be loaded ({e}).")
    if not unch.find_unch(root):
        raise ValueError(
            "no ChartInfo.json found, and it is not an UntitledCharts export "
            "(level.json + NSLevelData.json.gz) either."
        )
    try:
        cfg = unch.build_cfg(root, os.path.join(root, "_converted"))
    except unch.UnchError as e:
        raise ValueError(str(e))
    meta = cfg.pop("_unch")
    notes_info = []
    for c, want, want_core in zip(
        cfg["charts"], meta["expected_combos"], meta["expected_cores"]
    ):
        # independent check of the round trip (.sus written, then read back)
        got = sus_combo(c["path"])
        if want is None:  # e.g. Chart Cyanvas: no raw count to compare against
            notes_info.append((got, None, False))
            continue
        try:
            got_core = sus_core_combo(c["path"])
        except Exception:
            got_core = None
        if got_core is not None and got_core != want_core:
            raise ValueError(
                f"conversion check failed for {c['difficulty']}: {got_core} real "
                f"notes after conversion, the source data has {want_core}. "
                "Refusing to build."
            )
        diff = got - want
        if got_core is None and abs(diff) > 3:
            raise ValueError(
                f"conversion check failed for {c['difficulty']}: converted chart has "
                f"combo {got}, the source data has {want}. Refusing to build."
            )
        notes_info.append((got, diff, got_core is not None))
    _fit_preview(cfg, meta["warnings"])
    n = len(cfg["charts"])
    print(
        f"Detected chart export: {cfg['title']} - {cfg['artist']}\n"
        f"  {n} chart{'s' if n != 1 else ''} found:"
    )
    for c, want, (got, diff, core_ok) in zip(
        cfg["charts"], meta["expected_combos"], notes_info
    ):
        line = f"    {c['difficulty']:<7} Lv{c['level']:<3} combo {got}"
        if diff is None:
            line += " (converted; this source format has no independent note count)"
        elif diff == 0:
            line += " (verified)"
        else:
            line += f" (verified; {diff:+d} vs editor's {want}, see below)"
        print(line)
        if diff:
            meta["warnings"].append(
                f"{c['difficulty']}: slide tick count differs by {diff:+d} from the "
                "editor's. Ticks are regenerated by the converter library and are "
                "not stored in the .sus; every tap, flick, trace and slide "
                "head/tail matched exactly."
                if core_ok
                else f"{c['difficulty']}: combo differs by {diff:+d} (small, accepted)."
            )
    print(f"  music offset {meta['bgm_offset']:+.3f}s -> offset_ms={cfg['offset_ms']}")
    for w in meta["warnings"]:
        print(f"  ! {w}")
    if meta["ignored"]:
        print(f"  not used by the game mod: {', '.join(meta['ignored'])}")
    print(
        "  lyricist/composer/arranger are not in UnCh data; set them with "
        "--set composer=... if you want them filled in."
    )
    return cfg


_STR_KEYS = {
    "title", "charter", "lyricist", "composer", "arranger",
    "artist", "vocals", "collab", "original", "identifier",
}
_INT_KEYS = {"offset_ms", "mv_offset_ms"}


def apply_overrides(cfg: dict, sets: list[str]) -> None:
    for item in sets or []:
        if "=" not in item:
            raise ValueError(f"--set expects KEY=VALUE, got '{item}'")
        k, v = item.split("=", 1)
        k, v = k.strip().lower().replace("-", "_"), v.strip()
        if k in _STR_KEYS:
            cfg[k] = v
        elif k in _INT_KEYS:
            try:
                cfg[k] = int(v)
            except ValueError:
                raise ValueError(f"{k} must be an integer")
        elif k == "is_full":
            cfg[k] = v.lower() in ("1", "true", "yes", "y")
        elif k in ("level", "difficulty"):
            if len(cfg["charts"]) != 1:
                raise ValueError(f"--set {k} only works when there is exactly one chart")
            if k == "level":
                lvl = int(v)
                if not (MIN_LEVEL <= lvl <= MAX_LEVEL):
                    raise ValueError(f"level must be {MIN_LEVEL}-{MAX_LEVEL}")
                cfg["charts"][0]["level"] = lvl
            else:
                if v.lower() not in DIFFICULTIES:
                    raise ValueError(f"difficulty must be one of {DIFFICULTIES}")
                cfg["charts"][0]["difficulty"] = v.lower()
        else:
            raise ValueError(
                f"unknown key '{k}' (try: {', '.join(sorted(_STR_KEYS | _INT_KEYS | {'is_full', 'level', 'difficulty'}))})"
            )


# --------------------------------------------------------------------------- #
# Interactive driver
# --------------------------------------------------------------------------- #


def menu(cfg: dict, assume_yes: bool) -> None:
    while True:
        print(
            "\n--- Menu ---------------------------------------------------\n"
            "  b  Build .zip                 1  Edit files\n"
            "  c  Compile .chart.gs6         2  Edit charts\n"
            "  j  Save config as JSON        3  Edit metadata\n"
            "  v  View summary               4  Edit vocals\n"
            "  q  Quit                       5  Edit identity"
        )
        choice = _input("> ").strip().lower()
        if choice in ("q", "quit", "exit"):
            return
        if choice == "1":
            edit_files(cfg)
        elif choice == "2":
            edit_charts(cfg)
        elif choice == "3":
            edit_metadata(cfg)
        elif choice == "4":
            edit_vocals(cfg)
        elif choice == "5":
            edit_identity(cfg)
        elif choice == "v":
            show_summary(cfg)
        elif choice == "j":
            default = (safe_name(cfg["title"]) if cfg["title"] else "chart") + ".json"
            out = ask("Save config to", default)
            if confirm_overwrite(out, assume_yes):
                save_config(cfg, out)
                print(f"Saved {out}")
                if any(
                    d and any(str(v).startswith(d) for v in (cfg["jacket"], cfg["track"]))
                    for d in _IMPORT_DIRS
                ):
                    print(
                        "  note: paths point at a temporary folder from --import; "
                        "they vanish when this program exits."
                    )
        elif choice in ("b", "c"):
            data, errs = collect(cfg)
            if errs:
                print_errors(errs)
                continue
            base = default_base(data)
            if choice == "b":
                out = ask("Output .zip path", os.path.join(os.getcwd(), base + ".zip"))
                do_build(data, out, assume_yes)
            else:
                out = ask(
                    "Output base name (writes .android.chart.gs6 and .ios.chart.gs6)",
                    os.path.join(os.getcwd(), base),
                )
                do_compile(data, out, assume_yes)
        else:
            print("  ! unknown option.")


def wizard(cfg: dict) -> None:
    edit_files(cfg)
    edit_charts(cfg)
    edit_metadata(cfg)
    edit_vocals(cfg)
    edit_identity(cfg)
    show_summary(cfg)


def _choose_vocal(descs: list[str]) -> int:
    print("\nThis song has several vocal versions:")
    for i, d in enumerate(descs, 1):
        print(f"  {i}. {d}")
    return ask_int("Which one", default=1, lo=1, hi=len(descs)) - 1


def _out_name(cfg: dict) -> str:
    name = safe_name(cfg["title"])
    if len(cfg["charts"]) == 1:
        name += f" ({cfg['charts'][0]['difficulty']})"
    return name


def build_from_folder(folder: str, args, out_dir: str | None = None) -> bool:
    """Load a chart folder, apply --set overrides, validate and compile .gs6 files."""
    try:
        cfg = load_from_dir(folder)
        apply_overrides(cfg, args.set)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"Load failed: {e}", file=sys.stderr)
        return False
    data, errs = collect(cfg)
    if errs:
        print_errors(errs)
        return False
    out_dir = out_dir or args.out
    os.makedirs(out_dir, exist_ok=True)
    return do_compile(data, os.path.join(out_dir, _out_name(cfg)), assume_yes=args.yes)


def get_and_build(source: str, ident: str, args) -> bool:
    """Download one chart from an online source and convert it to .gs6."""
    import fetch

    region = "auto" if args.region == "all" else args.region
    print(f"\nDownloading from {fetch.LABELS[source]}: {ident}")
    try:
        folder = fetch.download(
            source,
            ident,
            os.path.join(args.out, "downloads"),
            region=region,
            cover=args.cover,
            choose=_choose_vocal if sys.stdin.isatty() else None,
            log=print,
        )
    except fetch.NotFound:
        print(f"Not found: {ident} (check the id, or try --source).", file=sys.stderr)
        return False
    except fetch.FetchError as e:
        print(f"Download failed: {e}", file=sys.stderr)
        return False
    print(f"Saved to {folder}")
    return build_from_folder(folder, args)


def cmd_search(args) -> int:
    import fetch

    sources = list(fetch.SOURCES) if args.source == "all" else [args.source]
    regions = tuple(fetch.REGIONS) if args.region == "all" else (
        ("jp", "en") if args.region == "auto" else (args.region,)
    )
    query = args.search or ""
    page = max(0, args.page - 1)
    interactive = sys.stdin.isatty()
    while True:
        results: list[dict] = []
        more = False
        for s in sources:
            print(f"Searching {fetch.LABELS[s]}...")
            try:
                rs, nxt = fetch.search(s, query, page, regions)
            except fetch.FetchError as e:
                print(f"  ! {fetch.LABELS[s]}: {e}")
                continue
            results += rs
            more = more or nxt
        print()
        if results:
            print(f"Results for '{query}' (page {page + 1}):" if query else f"Newest charts (page {page + 1}):")
            for i, r in enumerate(results, 1):
                print(fetch.format_result(i, r))
        else:
            print("No results." if query else "Nothing listed.")
        if not interactive:
            return 0 if results else 1
        hints = ["number = download + convert"]
        if more:
            hints.append("n = next page")
        if page:
            hints.append("p = previous page")
        hints += ["s = new search", "q = quit"]
        while True:
            try:
                raw = _input(f"\n[{'; '.join(hints)}]\n> ").strip()
            except (Abort, KeyboardInterrupt):
                print()
                return 130
            low = raw.lower()
            if low in ("q", "quit", "exit"):
                return 0
            if low == "n" and more:
                page += 1
                break
            if low == "p" and page:
                page -= 1
                break
            if low == "s":
                try:
                    query = _input("Search for: ").strip()
                except (Abort, KeyboardInterrupt):
                    return 130
                page = 0
                break
            if raw.isdigit() and 1 <= int(raw) <= len(results):
                r = results[int(raw) - 1]
                if get_and_build(r["source"], r["id"], args):
                    return 0
                continue
            ident = fetch.identify(raw)
            if ident and not raw.isdigit():
                if get_and_build(ident[0], ident[1], args):
                    return 0
                continue
            print("  ! not a valid choice.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="GridlessSekai6 Custom Chart Builder (terminal edition)"
    )
    ap.add_argument(
        "zips",
        nargs="*",
        metavar="CHART.zip",
        help="chart zip(s) containing ChartInfo.json: auto-validate and compile, no prompts",
    )
    ap.add_argument("--config", metavar="FILE", help="load a JSON config")
    ap.add_argument("--import", dest="import_zip", metavar="ZIP", help="load a built chart .zip")
    ap.add_argument("--zip", metavar="OUT.zip", help="build a chart zip and exit")
    ap.add_argument(
        "--compile",
        metavar="OUT_BASE",
        help="compile .android/.ios .chart.gs6 files and exit",
    )
    ap.add_argument("--save-config", metavar="FILE", help="write the loaded config to FILE")
    ap.add_argument("--template", action="store_true", help="print a config template and exit")
    ap.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override a field, e.g. --set composer=Name --set offset_ms=300 (repeatable)",
    )
    ap.add_argument(
        "-s",
        "--search",
        nargs="?",
        const="",
        default=None,
        metavar="QUERY",
        help="search online charts by title/artist/author (empty = newest), pick one, convert to .gs6",
    )
    ap.add_argument(
        "--source",
        default="all",
        choices=["all", "unch", "ns", "chcy", "chcy-o", "pjsk"],
        help="where to search / download from (default: all). unch=UntitledCharts, "
        "ns=Next SEKAI, chcy/chcy-o=Chart Cyanvas, pjsk=official game charts",
    )
    ap.add_argument("--page", type=int, default=1, help="search result page (default 1)")
    ap.add_argument(
        "--region",
        default="auto",
        choices=["auto", "all", "jp", "en", "cn", "kr", "tw"],
        help="official charts: game server region (auto detects; 'all' searches every region)",
    )
    ap.add_argument("--cover", type=int, default=None, metavar="N", help="official charts: vocal version number")
    ap.add_argument("--out", default="out", metavar="DIR", help="folder for downloads and .gs6 output (default: out)")
    ap.add_argument("-y", "--yes", action="store_true", help="overwrite files without asking")
    args = ap.parse_args(argv)

    if args.template:
        print(json.dumps(template_cfg(), ensure_ascii=False, indent=2))
        return 0

    if args.search is not None:
        if args.zips or args.config or args.import_zip or args.zip or args.compile:
            ap.error("--search can't be combined with other inputs")
        try:
            return cmd_search(args)
        except ImportError as e:
            print(f"fetch.py (or the 'requests' package) is unavailable: {e}", file=sys.stderr)
            return 1

    if args.zips:
        if args.config or args.import_zip or args.zip or args.compile:
            ap.error("a bare CHART.zip can't be combined with --config/--import/--zip/--compile")
        failed = 0
        for zp in args.zips:
            zp = clean_path(zp)
            print(f"\n##### {zp}")
            if not os.path.isfile(zp):
                if os.path.isdir(zp):
                    if not build_from_folder(zp, args):
                        failed += 1
                    continue
                try:
                    import fetch

                    ident = fetch.identify(zp)
                except ImportError:
                    ident = None
                if ident:
                    src = args.source if args.source != "all" else ident[0]
                    if not get_and_build(src, ident[1], args):
                        failed += 1
                    continue
            try:
                cfg = load_from_zip(zp)
            except (OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as e:
                print(f"Load failed: {e}", file=sys.stderr)
                failed += 1
                continue
            try:
                apply_overrides(cfg, args.set)
            except ValueError as e:
                print(f"--set failed: {e}", file=sys.stderr)
                return 2
            data, errs = collect(cfg)
            if errs:
                print_errors(errs)
                failed += 1
                continue
            out_base = os.path.splitext(os.path.abspath(zp))[0]
            if not do_compile(data, out_base, assume_yes=True):
                failed += 1
        print(f"\nDone: {len(args.zips) - failed} ok, {failed} failed.")
        return 1 if failed else 0
    if args.config and args.import_zip:
        ap.error("use either --config or --import, not both")

    try:
        if args.config:
            cfg = load_config(args.config)
        elif args.import_zip:
            cfg = load_from_zip(args.import_zip)
            print(f"Imported {os.path.basename(args.import_zip)}.")
        else:
            cfg = None
    except (OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as e:
        print(f"Load failed: {e}", file=sys.stderr)
        return 1

    if cfg is not None and args.set:
        try:
            apply_overrides(cfg, args.set)
        except ValueError as e:
            print(f"--set failed: {e}", file=sys.stderr)
            return 2

    batch = bool(args.zip or args.compile or args.save_config)

    if batch:
        if cfg is None:
            print("--zip/--compile/--save-config need --config or --import.", file=sys.stderr)
            return 2
        ok = True
        if args.save_config:
            save_config(cfg, args.save_config)
            print(f"Saved {args.save_config}")
        if args.zip or args.compile:
            data, errs = collect(cfg)
            if errs:
                print_errors(errs)
                return 1
            if args.zip:
                ok = do_build(data, args.zip, assume_yes=True) and ok
            if args.compile:
                ok = do_compile(data, args.compile, assume_yes=True) and ok
        return 0 if ok else 1

    print("GridlessSekai6 Custom Chart Builder (terminal)")
    print("Enter keeps the [default]; '-' clears an optional field; Ctrl+C quits.")
    if not ffprobe_available():
        print("WARNING: ffprobe not found on PATH. Audio/preview checks disabled.")
    try:
        if cfg is None:
            cfg = new_cfg()
            wizard(cfg)
        else:
            show_summary(cfg)
        menu(cfg, args.yes)
    except (Abort, KeyboardInterrupt):
        print("\nAborted.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
