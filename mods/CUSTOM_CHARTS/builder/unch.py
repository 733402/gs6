"""Reader for UntitledCharts (UnCh) chart exports.

An UnCh export is a folder/zip such as:

    level.json              metadata (title, artists, author, rating, tags, ...)
    NSLevelData.json.gz     the chart, as NextSEKAI / pysekai Sonolus LevelData
    music.mp3  preview.mp3  jacket.png  background_*.png

The sonolus_converters library can *write* this LevelData format but its loader
for it is not implemented (and its Chart Cyanvas loader silently drops every
slide), so this module contains a proper inverse of the library's
`LevelData.next_sekai` exporter, turning the LevelData into a `Score` that can
then be written out as a normal `.sus` file for the builder.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import uuid

DIFFICULTIES = ["easy", "normal", "hard", "expert", "master", "append"]

# Inverse of the lookup tables in sonolus_converters.LevelData.next_sekai.exporter
_DIRECTIONS = {0: "up", 1: "left", 2: "right"}
_EASES = {5: "outin", 3: "out", 1: "linear", 2: "in", 4: "inout"}
_GUIDE_COLORS = {
    101: "neutral",
    102: "red",
    103: "green",
    104: "blue",
    105: "yellow",
    106: "purple",
    107: "cyan",
    108: "black",
}

_SINGLE = re.compile(r"^(Fake)?(Normal|Critical)(Tap|Trace|Flick|TraceFlick)Note$")
_DAMAGE = re.compile(r"^(Fake)?DamageNote$")
_HEAD = re.compile(r"^(Fake)?(Normal|Critical)Head(Tap|Trace)Note$")
_TAIL = re.compile(r"^(Fake)?(Normal|Critical)Tail(Release|Trace|Flick|TraceFlick)Note$")
_TICK = re.compile(r"^(Fake)?(Normal|Critical)TickNote$")
_ANCHOR = re.compile(r"^(Fake)?AnchorNote$")
_HIDDEN_TICK = "TransientHiddenTickNote"


class UnchError(Exception):
    pass


# --------------------------------------------------------------------------- #
# LevelData -> Score
# --------------------------------------------------------------------------- #


class _Ent:
    __slots__ = ("archetype", "name", "d")

    def __init__(self, raw: dict):
        self.archetype: str = raw["archetype"]
        self.name: str | None = raw.get("name")
        self.d: dict = {}
        for item in raw.get("data", []):
            self.d[item["name"]] = item["ref"] if "ref" in item else item.get("value")

    def num(self, key: str, default: float = 0.0) -> float:
        v = self.d.get(key, default)
        return default if isinstance(v, str) or v is None else v


def read_level_data(path: str) -> dict:
    opener = gzip.open if path.lower().endswith(".gz") else open
    with opener(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


def level_data_to_score(level: dict):
    """Convert NextSEKAI/pysekai LevelData (dict) into a sonolus_converters Score."""
    from sonolus_converters.notes import (
        Bpm,
        FeverChance,
        FeverStart,
        Guide,
        GuidePoint,
        MetaData,
        Score,
        Single,
        Skill,
        Slide,
        SlideEndPoint,
        SlideRelayPoint,
        SlideStartPoint,
        TimeScaleGroup,
        TimeScalePoint,
    )

    ents = [_Ent(r) for r in level.get("entities", [])]

    # SUS has one note cell per tick/lane. UnCh can layer a tap and a
    # trace-flick in exactly the same cell; shift only the trace-flick in
    # the generated intermediate Score by one SUS tick. The raw source is untouched.
    sus_tick = 1.0 / 480.0
    cells = {}
    for e in ents:
        m = _SINGLE.match(e.archetype)
        if m:
            key = (round(float(e.num("#BEAT")), 9),
                   round(float(e.num("lane")), 6),
                   round(float(e.num("size", 1.0)), 6))
            cells.setdefault(key, []).append(e)

    shifted = 0
    for key, cell in cells.items():
        if len(cell) < 2 or not any(_SINGLE.match(e.archetype).group(3) == "TraceFlick" for e in cell):
            continue
        occupied = set(cells)
        beat, lane, size = key
        for e in cell:
            if _SINGLE.match(e.archetype).group(3) != "TraceFlick":
                continue
            for sign in (1.0, -1.0, 2.0, -2.0):
                new_key = (round(beat + sign * sus_tick, 9), lane, size)
                if new_key not in occupied:
                    e.d["#BEAT"] = beat + sign * sus_tick
                    occupied.add(new_key)
                    shifted += 1
                    break

    by_name = {e.name: e for e in ents if e.name is not None}

    # ---- time-scale (hi-speed) groups, in file order ----------------------
    group_ents = [e for e in ents if e.archetype == "#TIMESCALE_GROUP"]
    group_index: dict[str, int] = {}
    groups: list[list[tuple[float, float]]] = []
    for i, g in enumerate(group_ents):
        if g.name is not None:
            group_index[g.name] = i
        pts: list[tuple[float, float]] = []
        cur = by_name.get(g.d.get("first"))
        seen: set[str] = set()
        while cur is not None and cur.name not in seen:
            seen.add(cur.name)
            pts.append((cur.num("#BEAT"), cur.num("#TIMESCALE", 1.0)))
            cur = by_name.get(cur.d.get("next"))
        groups.append(pts)

    def tsg(e: _Ent) -> int:
        ref = e.d.get("#TIMESCALE_GROUP")
        return group_index.get(ref, 0) if isinstance(ref, str) else 0

    notes: list = []
    used_groups: set[int] = set()

    # ---- BPM ---------------------------------------------------------------
    for e in ents:
        if e.archetype == "#BPM_CHANGE":
            notes.append(Bpm(beat=e.num("#BEAT"), bpm=e.num("#BPM", 120.0)))

    # ---- events ------------------------------------------------------------
    for e in ents:
        if e.archetype == "Skill":
            notes.append(Skill(beat=e.num("#BEAT")))
        elif e.archetype == "FeverChance":
            notes.append(FeverChance(beat=e.num("#BEAT")))
        elif e.archetype == "FeverStart":
            notes.append(FeverStart(beat=e.num("#BEAT")))

    # ---- single notes ------------------------------------------------------
    for e in ents:
        m = _SINGLE.match(e.archetype)
        if m:
            fake, crit, kind = m.groups()
            direction = (
                _DIRECTIONS.get(int(e.num("direction")), "up")
                if "Flick" in kind
                else None
            )
            used_groups.add(tsg(e))
            notes.append(
                Single(
                    beat=e.num("#BEAT"),
                    critical=(crit == "Critical"),
                    lane=e.num("lane"),
                    size=e.num("size", 1.0),
                    fake=bool(fake),
                    timeScaleGroup=tsg(e),
                    trace=("Trace" in kind),
                    direction=direction,
                    type="single",
                )
            )
        elif _DAMAGE.match(e.archetype):
            used_groups.add(tsg(e))
            notes.append(
                Single(
                    beat=e.num("#BEAT"),
                    critical=False,
                    lane=e.num("lane"),
                    size=e.num("size", 1.0),
                    fake=e.archetype.startswith("Fake"),
                    timeScaleGroup=tsg(e),
                    trace=False,
                    direction=None,
                    type="damage",
                )
            )

    # ---- slides & guides (chains linked through "next") --------------------
    def is_chain_note(e: _Ent) -> bool:
        a = e.archetype
        return bool(_HEAD.match(a) or _TAIL.match(a) or _TICK.match(a) or _ANCHOR.match(a))

    chain_ents = [e for e in ents if is_chain_note(e)]
    has_pred: set[str] = set()
    for e in chain_ents:
        nxt = e.d.get("next")
        if isinstance(nxt, str):
            has_pred.add(nxt)

    for head in chain_ents:
        if head.name in has_pred:
            continue
        chain: list[_Ent] = []
        seen: set[str | None] = set()
        cur: _Ent | None = head
        while cur is not None and cur.name not in seen:
            chain.append(cur)
            seen.add(cur.name)
            cur = by_name.get(cur.d.get("next"))
        if len(chain) < 2:
            continue

        seg_kind = int(head.num("segmentKind", 1))

        if seg_kind >= 100:  # guide
            fade = "none"
            if head.num("segmentAlpha", 1) == 0:
                fade = "in"
            elif chain[-1].num("segmentAlpha", 1) == 0:
                fade = "out"
            pts = []
            for e in chain:
                used_groups.add(tsg(e))
                pts.append(
                    GuidePoint(
                        beat=e.num("#BEAT"),
                        ease=_EASES.get(int(e.num("connectorEase", 1)), "linear"),
                        lane=e.num("lane"),
                        size=e.num("size", 1.0),
                        timeScaleGroup=tsg(e),
                    )
                )
            notes.append(
                Guide(color=_GUIDE_COLORS.get(seg_kind, "neutral"), fade=fade, midpoints=pts)
            )
            continue

        fake = seg_kind >= 50 or any(e.archetype.startswith("Fake") for e in chain)
        slide_crit = (seg_kind % 50) == 2
        conns = []
        last = len(chain) - 1
        for i, e in enumerate(chain):
            a = e.archetype
            used_groups.add(tsg(e))
            common = dict(
                beat=e.num("#BEAT"),
                lane=e.num("lane"),
                size=e.num("size", 1.0),
                timeScaleGroup=tsg(e),
            )
            ease = _EASES.get(int(e.num("connectorEase", 1)), "linear")
            mh, mt, mk, ma = _HEAD.match(a), _TAIL.match(a), _TICK.match(a), _ANCHOR.match(a)
            if i == 0:
                if mh:
                    conns.append(
                        SlideStartPoint(
                            critical=(mh.group(2) == "Critical"),
                            ease=ease,
                            judgeType="trace" if mh.group(3) == "Trace" else "normal",
                            **common,
                        )
                    )
                else:
                    conns.append(
                        SlideStartPoint(
                            critical=slide_crit, ease=ease, judgeType="none", **common
                        )
                    )
            elif i == last:
                if mt:
                    kind = mt.group(3)
                    conns.append(
                        SlideEndPoint(
                            critical=(mt.group(2) == "Critical"),
                            judgeType="trace" if "Trace" in kind else "normal",
                            direction=(
                                _DIRECTIONS.get(int(e.num("direction")), "up")
                                if "Flick" in kind
                                else None
                            ),
                            **common,
                        )
                    )
                else:
                    conns.append(
                        SlideEndPoint(
                            critical=slide_crit, judgeType="none", direction=None, **common
                        )
                    )
            else:
                attached = int(e.num("isAttached")) == 1
                crit = None
                if mk:
                    crit = mk.group(2) == "Critical"
                conns.append(
                    SlideRelayPoint(
                        ease=ease,
                        type="attach" if attached else "tick",
                        critical=crit,
                        **common,
                    )
                )
        notes.append(Slide(critical=slide_crit, fake=fake, connections=conns))

    # ---- time-scale groups into the score ----------------------------------
    nontrivial = {
        i for i, pts in enumerate(groups) if pts and any(p != (0.0, 1.0) for p in pts)
    }
    keep = max(used_groups | nontrivial, default=-1) + 1
    for i in range(keep):
        pts = groups[i] if i < len(groups) and groups[i] else [(0.0, 1.0)]
        notes.append(
            TimeScaleGroup(changes=[TimeScalePoint(beat=b, timeScale=s) for b, s in pts])
        )

    meta = MetaData(
        title="",
        artist="",
        designer="",
        waveoffset=float(level.get("bgmOffset", 0.0) or 0.0),
        requests=["ticks_per_beat 480"],
    )
    score = Score(metadata=meta, notes=notes)
    score._sus_workaround_shifts = shifted
    return score


def expected_counts(level: dict) -> tuple[int, int]:
    """(core, total) straight from the raw entities.
    core  = taps/flicks/traces + slide heads + slide tails (notes that must survive
            conversion exactly).
    total = core + visible slide ticks + the editor's hidden slide ticks (those are
            derived data: never written to the .sus, regenerated when it is read)."""
    core = 0
    for r in level.get("entities", []):
        a = r["archetype"]
        if _SINGLE.match(a) or _HEAD.match(a) or _TAIL.match(a):
            core += 1
    return core, expected_combo(level)


def expected_combo(level: dict) -> int:
    """Combo straight from the raw entities, as an independent cross-check."""
    n = 0
    for r in level.get("entities", []):
        a = r["archetype"]
        if (
            _SINGLE.match(a)
            or _HEAD.match(a)
            or _TAIL.match(a)
            or _TICK.match(a)
            or a == _HIDDEN_TICK
        ):
            n += 1
    return n


# --------------------------------------------------------------------------- #
# UnCh export folder(s) -> builder config
# --------------------------------------------------------------------------- #

_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")
_AUDIO_EXTS = (".mp3", ".wav", ".ogg", ".flac", ".m4a")


def _det_uuid4(seed: str) -> str:
    h = hashlib.sha256(seed.encode("utf-8")).digest()[:16]
    return str(uuid.UUID(bytes=h, version=4))


def _find_file(root: str, stems: tuple[str, ...], exts: tuple[str, ...]) -> str:
    for r, _d, files in os.walk(root):
        for fn in sorted(files):
            stem, ext = os.path.splitext(fn)
            if stem.lower() in stems and ext.lower() in exts:
                return os.path.join(r, fn)
    return ""


def find_all_unch(root: str) -> list[tuple[str, str, str]]:
    """Every UnCh export under `root`, one per folder that holds level.json plus
    NSLevelData.json(.gz): [(export_dir, level.json path, level data path), ...]."""
    out = []
    for r, _d, files in os.walk(root):
        low = {fn.lower(): fn for fn in files}
        lj = low.get("level.json")
        data = (
            low.get("nsleveldata.json.gz")
            or low.get("nsleveldata.json")
            or low.get("chcyleveldata.json.gz")
        )
        if lj and data:
            out.append((r, os.path.join(r, lj), os.path.join(r, data)))
    out.sort(key=lambda t: t[0])
    return out


def find_unch(root: str) -> tuple[str, str] | None:
    """Return (level.json path, level data path) of the first UnCh export under `root`."""
    found = find_all_unch(root)
    return (found[0][1], found[0][2]) if found else None


def _is_chcy(data_path: str) -> bool:
    return os.path.basename(data_path).lower().startswith("chcy")


def _score_from_chcy(sc, data_path: str):
    """Chart Cyanvas LevelData -> (Score, bgm_offset_seconds)."""
    try:
        with open(data_path, "rb") as f:
            score = sc.LevelData.chart_cyanvas.load(f)
    except Exception as ex:
        raise UnchError(f"could not read the Chart Cyanvas data ({ex})")
    return score, float(getattr(score.metadata, "waveoffset", 0.0) or 0.0)


def _difficulty_of(meta: dict) -> str:
    for t in meta.get("tags", []):
        if t.get("icon") == "tag" and str(t.get("title", "")).lower() in DIFFICULTIES:
            return str(t["title"]).lower()
    return "expert"


def _rating_of(meta: dict) -> int:
    try:
        rating = int(meta.get("rating", 1))
    except (TypeError, ValueError):
        rating = 1
    return max(1, min(99, rating))


def _sha1(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _pick_shared(paths: list[str]) -> tuple[str, bool]:
    """Pick the file most exports agree on (by content); ties go to the earliest.
    Returns (path, every_export_has_identical_file)."""
    groups: dict[str, list[str]] = {}
    for p in paths:
        groups.setdefault(_sha1(p), []).append(p)
    best = max(groups.values(), key=len)
    return best[0], len(groups) == 1


def build_cfg(root: str, work_dir: str) -> dict:
    """Convert every UnCh export found under `root` (one folder per difficulty, or a
    single folder) into ONE builder cfg dict (see main.new_cfg) with one chart per
    difficulty. Shared assets (music/preview/jacket) are taken from the exports."""
    import sonolus_converters as sc

    found = find_all_unch(root)
    if not found:
        raise UnchError("not an UnCh export (level.json + NSLevelData.json.gz missing)")

    warnings: list[str] = []

    # ---- read metadata, order by difficulty, drop duplicate difficulties ----
    entries = []
    for export_dir, level_json, data_path in found:
        with open(level_json, "r", encoding="utf-8") as f:
            meta = json.load(f)
        entries.append(
            {
                "dir": export_dir,
                "meta": meta,
                "data_path": data_path,
                "diff": _difficulty_of(meta),
                "rating": _rating_of(meta),
            }
        )
    entries.sort(key=lambda e: DIFFICULTIES.index(e["diff"]))  # stable
    chosen, seen = [], set()
    for e in entries:
        if e["diff"] in seen:
            warnings.append(
                f"skipped {os.path.basename(e['dir'])}: another export already "
                f"provides the {e['diff']} difficulty"
            )
            continue
        seen.add(e["diff"])
        chosen.append(e)

    # ---- convert each chart -------------------------------------------------
    os.makedirs(work_dir, exist_ok=True)
    charts, combos, cores, offsets = [], [], [], []
    for e in chosen:
        if _is_chcy(e["data_path"]):
            score, bgm_offset = _score_from_chcy(sc, e["data_path"])
            want_total = want_core = None  # different raw format: no independent count
        else:
            level = read_level_data(e["data_path"])
            score = level_data_to_score(level)
            bgm_offset = float(level.get("bgmOffset", 0.0) or 0.0)
            want_core, want_total = expected_counts(level)
        score.metadata.waveoffset = 0.0  # applied to the audio via offset_ms instead
        sus_path = os.path.join(work_dir, f"{e['diff']}.sus")
        try:
            sc.sus.export(sus_path, score)
        except Exception:
            try:
                sc.sus.export(sus_path, score, allow_layers=True)
            except Exception as ex:
                raise UnchError(f"could not write the {e['diff']} chart as .sus ({ex})")
        charts.append({"difficulty": e["diff"], "level": e["rating"], "path": sus_path})
        cores.append(want_core)
        combos.append(want_total)
        offsets.append(bgm_offset)

    # ---- one music offset for the whole song --------------------------------
    bgm_offset = max(set(offsets), key=offsets.count)
    if len(set(offsets)) > 1:
        per = ", ".join(
            "{} {:+.3f}s".format(c["difficulty"], o) for c, o in zip(charts, offsets)
        )
        warnings.append(
            f"the charts disagree on the music offset ({per}); "
            f"using {bgm_offset:+.3f}s for all of them"
        )

    # ---- shared assets ------------------------------------------------------
    def shared(label: str, stems: tuple[str, ...], exts: tuple[str, ...]) -> str:
        paths = [p for p in (_find_file(e["dir"], stems, exts) for e in chosen) if p]
        if not paths:
            raise UnchError(f"{label} file not found in the export")
        path, identical = _pick_shared(paths)
        if not identical:
            warnings.append(
                f"{label} differs between the exports; using the version most of "
                f"them share ({os.path.relpath(path, root)})"
            )
        return path

    jacket = shared("jacket", ("jacket", "cover"), _IMG_EXTS)
    track = shared("music", ("music", "bgm"), _AUDIO_EXTS)
    preview = shared("preview", ("preview",), _AUDIO_EXTS)

    # ---- metadata (taken from the first chart; charter = everyone credited) -
    first = chosen[0]["meta"]
    charters: list[str] = []
    for e in chosen:
        m = e["meta"]
        a = m.get("authorUser", {}).get("title") or str(m.get("author", "")).split("#")[0]
        if a and a not in charters:
            charters.append(a)
    artists = str(first.get("artists", "") or "").strip() or "Unknown"
    # Stable per-chart seed for the identifier. Real exports carry a unique "name";
    # if it is missing, don't rely on the folder name alone (a zip's chart folder can be
    # called just "chart", which would give different songs the same identifier).
    names = [
        str(
            e["meta"].get("name")
            or "{}:{}:{}".format(
                os.path.basename(e["dir"]),
                e["meta"].get("title", ""),
                e["meta"].get("artists", ""),
            )
        )
        for e in chosen
    ]
    seed = names[0] if len(names) == 1 else "+".join(sorted(names))

    ignored = sorted(
        {
            os.path.basename(p)
            for e in chosen
            for p in (
                _find_file(e["dir"], ("background_v1",), (".png", ".jpg")),
                _find_file(e["dir"], ("background_v3",), (".png", ".jpg")),
            )
            if p
        }
    )

    return {
        "jacket": jacket,
        "track": track,
        "track_pre": preview,
        "mvs": [],
        "charts": charts,
        "title": str(first.get("title", "") or "Untitled"),
        "charter": ", ".join(charters) or "Unknown",
        "lyricist": "-",
        "composer": "-",
        "arranger": "-",
        "artist": artists,
        "vocals": artists,
        "collab": "",
        "is_full": True,
        # music starts `-bgmOffset` s after chart time 0 -> pad the audio by that much
        "offset_ms": int(round(-bgm_offset * 1000)),
        "mv_offset_ms": 0,
        "original": "",
        "default_vocal": {"name": "", "type": "sekai"},
        "alt_vocals": [],
        "identifier": f"{_det_uuid4('gs6:' + seed + ':a')}_{_det_uuid4('gs6:' + seed + ':b')}",
        "_unch": {
            "source_id": seed,
            "expected_combos": combos,  # aligned with cfg["charts"]
            "expected_cores": cores,  # same order: notes that must match exactly
            "bgm_offset": bgm_offset,
            "ignored": ignored,
            "warnings": warnings,
        },
    }
