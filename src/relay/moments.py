"""Runs and moments: what happened in each play-through, written to disk as it happens.

A run is one route on one day (`runs/<YYYYMMDD>-<route>/moments.jsonl`). A moment is every look,
judgement or exchange with the spirit. Photos stay where the relay stores them; a moment only
points at the file. Each night the relay keeps the photos the player kept or solved, but only those the
model checked and found no people in; every other photo is deleted. Strangers' faces are only for
judging on the spot, never kept.
"""

import itertools
import json
import os
import threading
import time
from pathlib import Path


_seq = itertools.count()
ORPHAN_AGE_S = 600     # a photo with no moment yet may still be with the model


class Journal:
    def __init__(self, data_dir: Path):
        self.photos = data_dir
        self.runs = data_dir / "runs"
        self.runs.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def _file(self, route: str, day: str | None = None) -> Path:
        d = self.runs / f"{day or time.strftime('%Y%m%d')}-{route}"
        d.mkdir(exist_ok=True)
        return d / "moments.jsonl"

    def _read(self, path: Path) -> list[dict]:
        """Every intact line. A line cut off mid-write (the relay stopped while appending) is skipped; the
        photo it pointed at, if any, then has no moment and the nightly sweep removes it."""
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                if line.strip():
                    out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def _write(self, path: Path, moments: list[dict]):
        """Whole file at once: written beside it, then swapped in, so a crash never leaves half a journal."""
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in moments), encoding="utf-8")
        os.replace(tmp, path)

    def add(self, route: str, kind: str, device: str, **fields) -> dict:
        """kind: look (wander), judge (a station), talk (an exchange with the spirit)."""
        now = time.time()
        moment = {"id": time.strftime("%H%M%S", time.localtime(now)) + f"-{int(now * 1000) % 1000:03d}-{next(_seq) % 10000:04d}",
                  "t": round(now, 3), "route": route, "kind": kind, "device": device, "kept": False, **fields}
        photo = fields.get("photo") or ""
        day = photo[:8] if photo[:8].isdigit() else None    # filed under the day the photo was taken
        with self.lock:
            with self._file(route, day).open("a", encoding="utf-8") as f:
                f.write(json.dumps(moment, ensure_ascii=False) + "\n")
        return moment

    def day(self, day: str | None = None) -> list[dict]:
        """Every moment of a day, all routes, oldest first."""
        day = day or time.strftime("%Y%m%d")
        out = []
        for path in sorted(self.runs.glob(f"{day}-*/moments.jsonl")):
            out += self._read(path)
        return sorted(out, key=lambda m: m["t"])

    def keep(self, device: str, moment_id: str | None = None) -> dict | None:
        """Mark a moment as kept: the given one, or this device's latest look or judgement (any route). Looks
        back to yesterday too: a photo taken just before midnight is filed under the day it was taken."""
        days = (time.strftime("%Y%m%d"), time.strftime("%Y%m%d", time.localtime(time.time() - 86400)))
        with self.lock:
            best = None
            for path in (p for d in days for p in self.runs.glob(f"{d}-*/moments.jsonl")):
                for i, m in enumerate(self._read(path)):
                    hit = m["id"] == moment_id if moment_id else (m["device"] == device and m["kind"] in ("look", "judge"))
                    if hit and (best is None or m["t"] > best[2]["t"]):
                        best = (path, i, m)
            if not best:
                return None
            path, i, _ = best
            moments = self._read(path)
            moments[i]["kept"] = True
            self._write(path, moments)
            return moments[i]

    def judged(self, photo: str, match, said: str | None = None) -> dict | None:
        """A person overruled the model on this photo (the operator page's buttons); `said` replaces the
        model's reason, so the record doesn't contradict itself."""
        day = photo[:8] if photo[:8].isdigit() else time.strftime("%Y%m%d")
        with self.lock:
            for path in self.runs.glob(f"{day}-*/moments.jsonl"):
                moments = self._read(path)
                for m in moments:
                    if m.get("photo") == photo and m["kind"] == "judge":
                        m["match"], m["by"] = match, "manual"
                        if said is not None:
                            m["said"] = said
                        self._write(path, moments)
                        return m
        return None

    def find_taken(self, device: str, taken_at: float) -> dict | None:
        """The moment a late photo already became, if this device sent it before (the answer got lost)."""
        days = {time.strftime("%Y%m%d"), time.strftime("%Y%m%d", time.localtime(time.time() - 86400)),
                time.strftime("%Y%m%d", time.localtime(taken_at))}
        for day in sorted(days):
            for m in self.day(day):
                if m["device"] == device and m.get("taken_at") == taken_at:
                    return m
        return None

    def no_people(self, photo: str) -> bool:
        """True only if this photo belongs to a moment the model said has no people in it."""
        day = photo[:8] if photo[:8].isdigit() else time.strftime("%Y%m%d")
        return any(m.get("photo") == photo and m.get("people") is False for m in self.day(day))

    def digest(self, day: str | None = None) -> dict:
        """The day in short: what the player kept or solved, with places and what the spirit said.
        Photos with people (or not checked) are left out; the words stay."""
        moments = self.day(day)
        picked = [m if m.get("people") is False else {**m, "photo": None}
                  for m in moments if m["kept"] or (m["kind"] == "judge" and m.get("match") is True)]
        return {
            "day": day or time.strftime("%Y%m%d"),
            "routes": sorted({m["route"] for m in moments}),
            "counts": {k: sum(m["kind"] == k for m in moments) for k in ("look", "judge", "talk")},
            "kept": picked,
        }

    @staticmethod
    def worth_keeping(m: dict) -> bool:
        """A photo stays past its day only if it was kept or solved, and the model saw no people in it."""
        return m.get("people") is False and (m["kept"] or (m["kind"] == "judge" and m.get("match") is True))

    def days(self) -> list[str]:
        """Every day that has a run, oldest first."""
        return sorted({p.name[:8] for p in self.runs.glob("*-*") if p.name[:8].isdigit()})

    def changed_at(self, day: str) -> float:
        """When that day's journal was last written (0 if never)."""
        return max((p.stat().st_mtime for p in self.runs.glob(f"{day}-*/moments.jsonl")), default=0.0)

    def pending(self, day: str) -> int:
        """How many photos of that day the nightly tidy-up would still delete (orphans included)."""
        with self.lock:
            moments = [m for path in self.runs.glob(f"{day}-*/moments.jsonl") for m in self._read(path)]
            known = {m.get("photo") for m in moments}
            orphans = [f for f in self.photos.glob(f"{day}-*.jpg")
                       if f.name not in known and time.time() - f.stat().st_mtime > ORPHAN_AGE_S]
            return sum(1 for m in moments if m.get("photo") and not self.worth_keeping(m)) + len(orphans)

    def prune(self, day: str) -> int:
        """Delete every photo not worth keeping (see worth_keeping). Returns how many were deleted."""
        deleted = 0
        with self.lock:
            for path in self.runs.glob(f"{day}-*/moments.jsonl"):
                moments = self._read(path)
                for m in moments:
                    photo = m.get("photo")
                    if photo and not self.worth_keeping(m):
                        (self.photos / photo).unlink(missing_ok=True)
                        m["photo"], m["pruned"] = None, True
                        deleted += 1
                self._write(path, moments)
            # photos no moment points at (the relay stopped mid-judgement): gone too, once they're not
            # still being judged
            known = {m.get("photo") for path in self.runs.glob(f"{day}-*/moments.jsonl") for m in self._read(path)}
            for f in self.photos.glob(f"{day}-*.jpg"):
                if f.name not in known and time.time() - f.stat().st_mtime > ORPHAN_AGE_S:
                    f.unlink(missing_ok=True)
                    deleted += 1
        return deleted
