import random
import time


def _unit_list(lessons):
    units = []
    for lesson in lessons:
        count = lesson["periods"]
        if lesson["block"] == 2:
            doubles, rest = divmod(count, 2)
            units += [{"lesson": lesson, "size": 2} for _ in range(doubles)]
            units += [{"lesson": lesson, "size": 1} for _ in range(rest)]
        else:
            units += [{"lesson": lesson, "size": 1} for _ in range(count)]
    return units


class _State:
    def __init__(self, classes, teachers, days, grid):
        self.classes = classes
        self.teachers = teachers
        self.days = days
        self.grid = grid
        self.joined = {}
        for i in range(len(grid) - 1):
            self.joined[grid[i]["no"]] = grid[i]["end"] == grid[i + 1]["start"]
        self.class_slot = {}
        self.teacher_slot = {}
        self.teacher_day = {}
        self.subject_day = {}
        self.class_day = {}
        self.placed = []

    def put(self, unit, day, p):
        lesson, size = unit["lesson"], unit["size"]
        ck, tid = lesson["class"], lesson["teacher_id"]
        for q in range(p, p + size):
            self.class_slot[(ck, day, q)] = unit
            self.teacher_slot[(tid, day, q)] = unit
        self.teacher_day[(tid, day)] = self.teacher_day.get((tid, day), 0) + size
        k = (ck, lesson["subject"], day)
        self.subject_day[k] = self.subject_day.get(k, 0) + size
        self.class_day[(ck, day)] = self.class_day.get((ck, day), 0) + size
        unit["at"] = (day, p)
        self.placed.append(unit)

    def remove(self, unit):
        day, p = unit["at"]
        lesson, size = unit["lesson"], unit["size"]
        ck, tid = lesson["class"], lesson["teacher_id"]
        for q in range(p, p + size):
            del self.class_slot[(ck, day, q)]
            del self.teacher_slot[(tid, day, q)]
        self.teacher_day[(tid, day)] -= size
        self.subject_day[(ck, lesson["subject"], day)] -= size
        self.class_day[(ck, day)] -= size
        self.placed.remove(unit)
        unit["at"] = None

    def fits(self, unit, day, p, ignore=None):
        lesson, size = unit["lesson"], unit["size"]
        ck, tid = lesson["class"], lesson["teacher_id"]
        info = self.teachers[tid]
        if day not in info["days"]:
            return False
        if p + size - 1 > self.classes[ck]:
            return False
        load = self.teacher_day.get((tid, day), 0)
        if ignore is not None and ignore["lesson"]["teacher_id"] == tid and ignore["at"][0] == day:
            load -= ignore["size"]
        if load + size > info["max_day"]:
            return False
        for q in range(p, p + size):
            if q > p and not self.joined.get(q - 1, False):
                return False
            a = self.class_slot.get((ck, day, q))
            if a is not None and a is not ignore:
                return False
            b = self.teacher_slot.get((tid, day, q))
            if b is not None and b is not ignore:
                return False
        return True

    def cost(self, unit, day, p, rng):
        lesson, size = unit["lesson"], unit["size"]
        ck, tid = lesson["class"], lesson["teacher_id"]
        c = 0.0
        same = self.subject_day.get((ck, lesson["subject"], day), 0)
        c += 40 * same
        for q in (p - 1, p + size):
            other = self.class_slot.get((ck, day, q))
            if other is not None and other["lesson"]["subject"] == lesson["subject"]:
                c += 25
        n = self.classes[ck]
        filled = [q for q in range(1, n + 1) if (ck, day, q) in self.class_slot]
        if filled:
            dist = min(min(abs(q - f) for f in filled) for q in range(p, p + size))
            c += 3 * max(0, dist - 1)
        else:
            c += 3 * (p - 1)
        c += 0.4 * p
        c += 4 * (self.teacher_day.get((tid, day), 0) + size)
        c += 1.5 * self.class_day.get((ck, day), 0)
        before = 0
        q = p - 1
        while (tid, day, q) in self.teacher_slot:
            before += 1
            q -= 1
        after = 0
        q = p + size
        while (tid, day, q) in self.teacher_slot:
            after += 1
            q += 1
        has_any = self.teacher_day.get((tid, day), 0) > 0
        if has_any and before == 0 and after == 0:
            c += 3
        if before + after + size > 3:
            c += 8 * (before + after + size - 3)
        return c + rng.random()

    def candidates(self, unit, ignore=None):
        ck = unit["lesson"]["class"]
        out = []
        for day in self.days:
            for p in range(1, self.classes[ck] - unit["size"] + 2):
                if self.fits(unit, day, p, ignore):
                    out.append((day, p))
        return out


def _repair(state, unit, rng):
    ck, tid = unit["lesson"]["class"], unit["lesson"]["teacher_id"]
    options = []
    for day in state.days:
        for p in range(1, state.classes[ck] - unit["size"] + 2):
            blockers = {}
            for q in range(p, p + unit["size"]):
                for b in (state.class_slot.get((ck, day, q)),
                          state.teacher_slot.get((tid, day, q))):
                    if b is not None:
                        blockers[id(b)] = b
            if len(blockers) == 1:
                options.append((day, p, next(iter(blockers.values()))))
    rng.shuffle(options)
    for day, p, blocker in options:
        if blocker["size"] != 1 and unit["size"] != blocker["size"]:
            continue
        old = blocker["at"]
        state.remove(blocker)
        if state.fits(unit, day, p):
            state.put(unit, day, p)
            spots = [s for s in state.candidates(blocker) if s != old]
            if spots:
                best = min(spots, key=lambda s: state.cost(blocker, s[0], s[1], rng))
                state.put(blocker, best[0], best[1])
                return True
            state.remove(unit)
        state.put(blocker, old[0], old[1])
    return False


def _attempt(classes, teachers, days, grid, lessons, rng, shake):
    state = _State(classes, teachers, days, grid)
    units = _unit_list(lessons)

    weekly = {}
    for lesson in lessons:
        weekly[lesson["teacher_id"]] = weekly.get(lesson["teacher_id"], 0) + lesson["periods"]

    def difficulty(u):
        t = teachers[u["lesson"]["teacher_id"]]
        room = max(1, t["max_day"] * len([d for d in days if d in t["days"]]))
        pressure = weekly[u["lesson"]["teacher_id"]] / room
        return (-round(pressure, 2) - rng.random() * shake, -u["size"])

    units.sort(key=difficulty)

    leftover = []
    for unit in units:
        spots = state.candidates(unit)
        if spots:
            best = min(spots, key=lambda s: state.cost(unit, s[0], s[1], rng))
            state.put(unit, best[0], best[1])
        else:
            leftover.append(unit)

    still = []
    for unit in leftover:
        if not _repair(state, unit, rng):
            still.append(unit)
    return state, still


def generate(classes, lessons, teachers, days, grid, seconds=6.0, seed=None):
    rng = random.Random(seed)
    started = time.time()
    best = None
    tries = 0
    while True:
        state, still = _attempt(classes, teachers, days, grid, lessons, rng, shake=0.0 if tries == 0 else 0.6)
        tries += 1
        if best is None or len(still) < len(best[1]):
            best = (state, still)
        if not still or time.time() - started > seconds or tries >= 40:
            break

    state, still = best
    placements = []
    for unit in state.placed:
        day, p = unit["at"]
        for q in range(p, p + unit["size"]):
            placements.append({"lesson_id": unit["lesson"]["id"], "class": unit["lesson"]["class"],
                               "day": day, "period": q})
    missing = {}
    for unit in still:
        lid = unit["lesson"]["id"]
        missing[lid] = missing.get(lid, 0) + unit["size"]
    unplaced = [{"lesson_id": lid, "missing": n} for lid, n in missing.items()]
    return {"placements": placements, "unplaced": unplaced, "tries": tries}


def check_plan(classes, lessons, teachers, days, grid):
    problems = []
    need = {}
    for lesson in lessons:
        need[lesson["class"]] = need.get(lesson["class"], 0) + lesson["periods"]
    for ck, total in need.items():
        room = classes.get(ck, 0) * len(days)
        if total > room:
            problems.append(("class", ck, total, room))
    load = {}
    for lesson in lessons:
        load[lesson["teacher_id"]] = load.get(lesson["teacher_id"], 0) + lesson["periods"]
    for tid, total in load.items():
        info = teachers[tid]
        room = info["max_day"] * len([d for d in days if d in info["days"]])
        if total > room:
            problems.append(("teacher", tid, total, room))
    return problems
