
import datetime
import json

from test_tracks import PAST, TracksBase

from depot2.stack import Stack, StackError, age_ru
from depot2.tracks import reads_dropped

THEN = "2015-11-08T13:00:00+02:00"
LATER = datetime.datetime.fromisoformat("2015-11-08T15:05:00+02:00")


class PostStackBase(TracksBase):
    def stk(self):
        return Stack(self.t.state, self.t.cfg)

    def in_slots(self, *titles, **kw):
        s = self.stk()
        for i, title in enumerate(titles):
            o = s.add(title, branch="b%d" % (i + 1), pr=301 + i, brief="/tmp/brief-%d.md" % (i + 1), **kw)  # noqa: UP031
            s.start(o["id"])
            s.set(o["id"], status="PR #%d, CI зелёный" % (301 + i))  # noqa: UP031
        return s


class PostStackTest(PostStackBase):
    def test_post_and_refuse_when_full(self):
        self.in_slots("Импорт", "Обложки", "Закладки", "Рассылка")
        rc, out, err = self.team("stack", "post", "st-0001", "--reason", "перед мерджем подтянуть main")
        self.assertEqual(rc, 0, err)
        self.assertIn("st-0001 → the post-stack (1/4): PR #301 waits for the owner's merge; the slot is free (3/4)", out)
        o = self.stk().get("st-0001")
        self.assertEqual((o["where"], o["pr"], o["branch"], o["brief"], o["status"]),
                         ("post", 301, "b1", "/tmp/brief-1.md", "PR #301, CI зелёный"))  # all kept
        self.assertEqual((o["posted"]["reason"], o["posted"]["from"], o["ci"]["state"]),
                         ("перед мерджем подтянуть main", "slot", "зелёный"))
        h = self.stk().history()[-1]
        self.assertEqual((h["event"], h["object"], h["was"], h["pr"]), ("post", "st-0001", "slot", 301))
        for oid in ("st-0002", "st-0003", "st-0004"):
            self.assertEqual(self.team("stack", "post", oid)[0], 0)
        self.assertEqual(len(self.stk().in_slots()), 0)  # the post-stack holds no slot
        self.assertEqual([o["id"] for o in self.stk().posted()], ["st-0001", "st-0002", "st-0003", "st-0004"])
        s = self.stk()
        s.add("пятый", pr=305)
        s.start("st-0005")
        rc, _, err = self.team("stack", "post", "st-0005")
        self.assertEqual(rc, 1)
        self.assertIn("пост-стек полон (4/4): st-0005 stays in its slot", err)
        self.assertEqual(self.stk().get("st-0005")["where"], "slot")
        rc, _, err = self.team("stack", "post", "st-0001")
        self.assertIn("in the post-stack already", err)
        # the capacity is config: stack.post_max
        self.assertEqual(self.team("--set", "stack.post_max=5", "stack", "post", "st-0005")[0], 0)

    def test_post_without_a_pr_or_with_a_lock_is_refused(self):
        s = self.stk()
        s.add("без PR")
        s.start("st-0001")
        rc, _, err = self.team("stack", "post", "st-0001")
        self.assertEqual(rc, 1)
        self.assertIn("st-0001 has no PR: only a PR that waits for the owner's merge goes to the post-stack", err)
        self.assertEqual(self.stk().get("st-0001")["where"], "slot")
        self.team("stack", "set", "st-0001", "--pr", "310")
        self.stk().lock("st-0001", "keeper", "замер выдачи", "дорого", "r", "tx-000001", 10)
        rc, _, err = self.team("stack", "post", "st-0001")
        self.assertIn("is locked (lk-0001, Копилка: замер выдачи): a locked object does not merge", err)
        self.assertEqual(self.stk().get("st-0001")["where"], "slot")

    def test_a_cars_object_posted_stays_linked_and_its_car_stays_pr(self):
        tr = self.tracks()
        a = tr.add("Читалка")
        tr.car_add(a["id"], "макет", n="R0", size="M", branch="reader-mockup")
        tr.car_add(a["id"], "профиль", n="P", size="S")
        tr.car_add(a["id"], "после макета", n="R1", after=["R0"], size="S")
        self.ride("tr-0001", "reader", slots="max 1")
        self.tracks().car_stack("tr-0001:R0", start=True)
        self.team("stack", "set", "st-0001", "--pr", "206", "--ci", "green")
        self.assertEqual(self.trains().next(PAST)["picks"], [])      # max 1: R0 holds the train's slot
        rc, out, err = self.team("stack", "post", "st-0001")
        self.assertEqual(rc, 0, err)
        self.assertIn("tr-0001:R0 → PR #206 открыт, в пост-стеке: ждёт мерджа владельца (st-0001, слот свободен)", out)
        self.assertIn("Свободный слот берёт (trains next): tr-0001:P (поезд reader P5, вагон P5) — `depot2 tracks car stack tr-0001:P "
                      "--start`", out)
        tr = self.tracks()
        _t, c = tr.car("tr-0001:R0")
        self.assertEqual((c["status"], c["st"], c["pr"], c["branch"]), ("pr", "st-0001", 206, "reader-mockup"))
        self.assertTrue(tr.posted(c))
        self.assertFalse(tr.parked(c))                                # not parked: it waits only for the merge
        self.assertEqual(self.trains().busy(self.trains().get("reader")), [])
        self.assertEqual([p["car"]["n"] for p in self.trains().next(PAST)["picks"]], ["P"])
        self.assertIn("| R0 | P5 | макет | — | M: ≈ 1%UW (12 MTok) / — | reader-mockup / PR #206 | st-0001 | reader | PR открыт, "
                      "в пост-стеке: ждёт мерджа владельца |", tr.car_md("tr-0001", now=PAST))
        self.assertIn("Пост-стек — ждут мерджа владельца (1/4, слот не держат): st-0001 (tr-0001:R0) PR #206",
                      "\n".join(self.trains().next_lines(PAST, train=self.trains().get("reader"))))
        self.assertNotIn("Запаркованы", "\n".join(self.trains().next_lines(PAST)))
        rc, _, err = self.team("trains", "done", "reader")
        self.assertIn("cars — tr-0001:R0 (PR открыт)", err)
        rc, _, err = self.team("tracks", "car", "stack", "tr-0001:R0")
        self.assertIn("is in the post-stack (st-0001, PR #206): `depot2 stack start st-0001` brings it back", err)
        # a car whose `after` is not met: its PR is blocked, not ready to merge: `stack park`
        self.trains().set("reader", slots="max 2")
        tr.car_set("tr-0001:R1", no_after=True)
        self.tracks().car_stack("tr-0001:R1", start=True)
        self.tracks().car_set("tr-0001:R1", after=["R0"])
        self.team("stack", "set", "st-0002", "--pr", "207")
        rc, _, err = self.team("stack", "post", "st-0002")
        self.assertEqual(rc, 1)
        self.assertIn("st-0002 is the object of tr-0001:R1, which waits for R0 (PR открыт): its PR is blocked, not "
                      "ready to merge; stack park st-0002", err)

    def test_done_after_post(self):
        self.in_slots("Импорт", "Обложки")
        self.team("stack", "post", "st-0001")
        rc, out, err = self.team("stack", "done", "st-0001", "--reason", "merged #301")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("st-0001", [o["id"] for o in self.stk().objects()])
        self.assertEqual(self.tracks().ref_state("#301"), (True, "PR #301 (слит)"))
        self.assertNotIn("Свободный слот", out)  # it held no slot
        # a car's object: `--car merged`, as for any car's object
        tr = self.tracks()
        a = tr.add("Т")
        tr.car_add(a["id"], "x", size="S")
        tr.car_stack("tr-0001:0", start=True)
        self.team("stack", "set", "st-0003", "--pr", "320")
        self.team("stack", "post", "st-0003")
        self.assertEqual(self.team("stack", "done", "st-0003", "--reason", "merged #320")[0], 1)  # no --car
        self.assertEqual(self.tracks().car("tr-0001:0")[1]["status"], "pr")
        rc, out, err = self.team("stack", "done", "st-0003", "--reason", "merged #320", "--car", "merged")
        self.assertEqual(rc, 0, err)
        self.assertIn("tr-0001:0 → слит (merged #320)", out)
        self.assertTrue(self.tracks().ref_state("#320")[0])

    def test_start_back_from_the_post_stack(self):
        tr = self.tracks()
        a = tr.add("Т")
        tr.car_add(a["id"], "вагон", size="S", branch="car-b")
        self.ride("tr-0001", "t", slots="all-free")
        self.tracks().car_stack("tr-0001:0", start=True)
        self.team("stack", "set", "st-0001", "--pr", "330")
        self.team("stack", "post", "st-0001")
        # a free slot: back into it, the agent works; the car is running
        rc, out, err = self.team("stack", "start", "st-0001", "--reason", "владелец: доработать подсказку")
        self.assertEqual(rc, 0, err)
        self.assertIn("st-0001 → slot (1/4), back from the post-stack for work", out)
        self.assertIn("tr-0001:0 → в работе (st-0001 в слоте)", out)
        o = self.stk().get("st-0001")
        self.assertEqual((o["where"], o["pr"], o["branch"], o["status"]), ("slot", 330, "car-b", "агент работает"))
        self.assertNotIn("posted", o)
        h = self.stk().history()[-1]
        self.assertEqual((h["event"], h["unposted"], h["reason"]), ("start", True, "владелец: доработать подсказку"))
        # no free slot: the head of the queue; the car is ready
        self.team("stack", "post", "st-0001")
        s = self.stk()
        for i in range(4):
            s.add("занят %d" % i)  # noqa: UP031
        s.add("ждёт")
        for oid in ("st-0002", "st-0003", "st-0004", "st-0005"):
            s.start(oid)
        rc, out, err = self.team("stack", "start", "st-0001")
        self.assertEqual(rc, 0, err)
        self.assertIn("st-0001 ← the post-stack, back for work: no free slot (4/4), the head of the queue", out)
        self.assertIn("tr-0001:0 → готов (st-0001 в голове очереди стека, на доработку; PR #330)", out)
        self.assertEqual([o["id"] for o in self.stk().queue()], ["st-0001", "st-0006"])
        self.assertEqual(self.tracks().car("tr-0001:0")[1]["status"], "ready")
        # no free slot and a full queue: it stays in the post-stack
        self.assertEqual(self.team("stack", "post", "st-0001")[0], 0)  # from the queue too
        self.assertEqual(self.tracks().car("tr-0001:0")[1]["status"], "pr")
        rc, _, err = self.team("--set", "stack.queue_max=1", "stack", "start", "st-0001")
        self.assertEqual(rc, 1)
        self.assertIn("no free slot (4/4) and the queue is full (1): st-0001 stays in the post-stack", err)
        self.assertEqual(self.stk().get("st-0001")["where"], "post")

    def test_three_sections_in_md_and_json(self):
        s = self.in_slots("Импорт", "Обложки")
        s.add("в очереди")
        md = s.md()
        self.assertIn("| **Приёмка — пост-стек, ждут мерджа владельца (0/4)** | | | | | | | | | | | | |\n| — | пост | (пусто) |", md)
        self.team("stack", "post", "st-0002", "--reason", "подтянуть main с #205")
        s = self.stk()
        o = s.get("st-0002")
        o["posted"]["ts"], o["ci"] = THEN, {"state": "зелёный", "ts": "2015-11-08T12:58:00+02:00"}
        s._save("test", "st-0002")
        md = self.stk().md(now=LATER)
        self.assertLess(md.index("**Сборка — в слотах (1/4)**"), md.index("**Приёмка — пост-стек, ждут мерджа владельца (1/4)**"))
        self.assertLess(md.index("**Приёмка — пост-стек, ждут мерджа владельца (1/4)**"), md.index("**Очередь — ждёт слота (1/10)**"))
        self.assertIn("| П1 | пост | st-0002 Обложки | P5 | b2 / PR #302 | CI зелёный (2015-11-08 12:58) · ждёт 2 ч 05 мин · "
                      "PR #302, CI зелёный · подтянуть main с #205 | нет | — | — | — |", md)
        self.assertIn("| С2 | слот | свободен |", md)  # the slot it held is free
        self.assertIn("Пост-стек: PR готов (CI зелёный) и ждёт только «мердж» владельца; слота не держит", md)
        self.assertIn("**Приёмка — пост-стек", self.team("stack", "--md")[1])
        data = json.loads(self.team("stack", "--json")[1])
        self.assertEqual((data["post_max"], [o["id"] for o in data["post"]], [o["id"] for o in data["in_slots"]]),
                         (4, ["st-0002"], ["st-0001"]))
        # `stack set --ci` records the CI as last seen, stamped
        self.team("stack", "set", "st-0002", "--ci", "red")
        self.assertEqual(self.stk().get("st-0002")["ci"]["state"], "красный")
        self.assertIn("CI красный (", self.stk().md())
        # `trains next`: the free slots without the post-stack, and the post-stack on one line
        lines = "\n".join(self.trains().next_lines(LATER))
        self.assertIn("Свободных слотов в стеке: 3/4.", lines)
        self.assertIn("Пост-стек — ждут мерджа владельца (1/4, слот не держат): st-0002 PR #302 (CI красный, ждёт ", lines)
        nx = json.loads(self.team("trains", "next", "--json")[1])
        self.assertEqual((nx["free"], nx["post_max"], [(p["id"], p["pr"], p["car"]) for p in nx["post"]]),
                         (3, 4, [("st-0002", 302, None)]))
        self.assertIn("пост-стек 1/4", self.team("queues")[1])
        # the team stack has no post-stack
        self.assertNotIn("Пост-стек", self.team("tstack")[1])
        self.assertEqual(age_ru(THEN, LATER + datetime.timedelta(days=1)), "1 д 2 ч")
        self.assertEqual(age_ru(THEN, datetime.datetime.fromisoformat("2015-11-08T13:07:00+02:00")), "7 мин")

    def test_the_ref_state_of_a_posted_pr(self):
        s = self.stk()
        s.add("раньше", pr=340)
        s.done("st-0001", "merged #340")          # an older row with the same PR says merged
        self.assertTrue(self.tracks().ref_state("#340")[0])
        self.in_slots("доработка #340")
        self.stk().set("st-0002", pr=340)
        self.team("stack", "post", "st-0002")
        tr = self.tracks()
        self.assertEqual(tr.ref_state("#340"),
                         (False, "PR #340 (не слит; в пост-стеке st-0002, ждёт мерджа владельца)"))
        self.assertEqual(tr.ref_state("st-0002"), (False, "st-0002 (в пост-стеке, ждёт мерджа владельца)"))
        s = self.stk()
        s.add("после доработки", depends=["st-0002"])
        with self.assertRaises(StackError):
            s.start("st-0003")                    # a dependency in the post-stack is not done

    def test_a_slot_freed_by_post_takes_the_next_autostart_object(self):
        self.in_slots("Импорт", "Обложки", "Закладки", "Рассылка")
        s = self.stk()
        s.add("сам", autostart=True)
        s.add("слово владельца")
        rc, out, err = self.team("stack", "post", "st-0003")
        self.assertEqual(rc, 0, err)
        self.assertIn("Свободный слот берёт (trains next): st-0005 (P5) — `depot2 stack start st-0005`", out)
        self.assertEqual([o["id"] for o in self.trains().next(PAST)["stack"]], ["st-0005"])
        self.team("stack", "start", "st-0005")
        rc, out, err = self.team("stack", "post", "st-0004")
        self.assertIn("Свободный слот (trains next): с автозапуском брать нечего; ждут слова владельца: st-0006", out)


class CarBackTest(PostStackBase):

    def setUp(self):
        super().setUp()
        tr = self.tracks()
        a = tr.add("Поиск")
        tr.car_add(a["id"], "иконки", n="3", size="S", branch="format-icons", brief="/tmp/format-icons.md")
        tr.car_add(a["id"], "History", n="7", size="S", branch="search-history")
        tr.car_set("tr-0001:3", status="ready")
        self.ride("tr-0001", "poisk", slots="all-free")
        tr = self.tracks()
        tr.car_stack("tr-0001:3")
        tr.car_stack("tr-0001:7", start=True)
        Stack(self.t.state, self.t.cfg).add("подсказки поиска", depends=["st-0001"])

    def test_done_without_car_is_refused_for_a_cars_object(self):
        for oid, ref, ru in (("st-0001", "tr-0001:3", "готов"), ("st-0002", "tr-0001:7", "в работе")):
            rc, _, err = self.team("stack", "done", oid, "--reason", "вернулся в поезд")
            self.assertEqual(rc, 1)
            self.assertIn(f"{oid} is the object of {ref} ({ru}): say what the car becomes: --car merged (its PR landed), "
                          "--car dropped (the car is off), or --car back", err)
        self.assertEqual([c["status"] for c in self.tracks().get("tr-0001")["cars"]], ["ready", "running"])
        self.assertEqual([o["id"] for o in self.stk().objects()], ["st-0002", "st-0001", "st-0003"])

    def test_car_back_returns_it_to_its_train_as_it_was(self):
        rc, out, err = self.team("stack", "done", "st-0002", "--car", "back", "--reason", "владелец: в поезд")
        self.assertEqual(rc, 0, err)
        self.assertIn("tr-0001:7 → ждёт, назад на трек как был: бриф и ветка при нём, связь со стеком снята "
                      "(st-0002 вышел); снова в стек: depot2 tracks car stack tr-0001:7", out)
        rc, out, err = self.team("stack", "done", "st-0001", "--car", "back", "--reason", "владелец: в поезд")
        self.assertIn("tr-0001:3 → готов, назад на трек как был", out)
        tr = self.tracks()
        c3, c7 = tr.car("tr-0001:3")[1], tr.car("tr-0001:7")[1]
        self.assertEqual((c3["status"], c3["st"], c3["branch"], c3["brief"], c3["finished"]),
                         ("ready", None, "format-icons", "/tmp/format-icons.md", None))
        self.assertEqual((c7["status"], c7["st"], c7["returned"]["st"]), ("waiting", None, "st-0002"))  # as stacked
        self.assertEqual(self.stk().history()[-1]["outcome"], "back")
        self.assertEqual(tr.ref_state("st-0001"), (False, "st-0001 (вне стека; вагон tr-0001:3 готов)"))
        self.assertFalse(tr.ref_state("tr-0001:3")[0])
        self.assertEqual(self.team("stack", "start", "st-0003")[0], 1)
        # `trains car stack` puts it in again: a new object; the old one still waits for the car
        rc, out, err = self.team("tracks", "car", "stack", "tr-0001:3", "--start")
        self.assertEqual(rc, 0, err)
        self.assertIn("tr-0001:3 → st-0004 in the stack queue", out)
        self.assertFalse(self.tracks().ref_state("st-0001")[0])
        self.assertEqual(self.team("stack", "start", "st-0003")[0], 1)
        self.team("stack", "set", "st-0004", "--pr", "350")
        self.team("stack", "done", "st-0004", "--car", "merged", "--reason", "merged #350")
        self.assertEqual(self.tracks().ref_state("st-0001"), (True, "st-0001 (вышел из стека)"))
        self.assertEqual(self.team("stack", "start", "st-0003")[0], 0)

    def test_car_back_refusals_and_a_non_car_outcome(self):
        self.team("stack", "set", "st-0002", "--pr", "360")
        rc, _, err = self.team("stack", "done", "st-0002", "--car", "back", "--reason", "x")
        self.assertIn("st-0002 has PR #360: a car with a PR goes back to its track with `stack park st-0002`", err)
        self.assertEqual(self.stk().get("st-0002")["where"], "slot")
        rc, _, err = self.team("stack", "done", "st-0003", "--car", "back", "--reason", "x")  # noqa: RUF059
        self.assertIn("st-0003 is no track's car's object: --car back sends a car back to its track", err)
        # a car with no PR is not parked: the refusal says how it goes back
        _rc, _, err = self.team("stack", "park", "st-0001", "--reason", "x")
        self.assertIn("a car with no PR goes back to its track with stack done st-0001 --car back", err)
        # an object that is no car's: --car merged|dropped is its row's outcome, and a PR ref reads it first
        s = self.stk()
        s.add("просто PR", pr=361)
        s.add("ещё PR", pr=362)
        self.team("stack", "done", "st-0004", "--car", "dropped", "--reason", "merged into #300 instead")
        self.team("stack", "done", "st-0005", "--car", "merged", "--reason", "closed")
        tr = self.tracks()
        self.assertEqual((tr.ref_state("#361")[0], tr.ref_state("#362")[0]), (False, True))


class PostStackRowsTest(PostStackBase):
    def test_closed_reads_dropped_only_when_the_reason_does_not_say_merged(self):
        self.assertFalse(reads_dropped("merged #201, branch closed"))
        self.assertTrue(reads_dropped("closed: superseded"))
        self.assertTrue(reads_dropped("closed, not merged"))
        self.assertTrue(reads_dropped("dropped: merged into #300 instead"))
        self.assertFalse(reads_dropped("слит #201, ветка закрыта"))
        s = self.stk()
        s.add("a", pr=401)
        s.add("b", pr=402)
        s.done("st-0001", "merged #401, branch closed")
        s.done("st-0002", "closed: superseded")
        tr = self.tracks()
        self.assertEqual((tr.ref_state("#401")[0], tr.ref_state("#402")[0]), (True, False))

    def test_a_parked_car_comes_back_for_work_before_its_after(self):
        tr = self.tracks()
        a = tr.add("Аккаунты")
        tr.car_add(a["id"], "док v2", n="0b", size="doc")
        tr.car_add(a["id"], "пароли", n="M", size="M", branch="passwords-0.1")
        tr.car_set("tr-0001:0b", status="ready")
        self.ride("tr-0001", "accounts", slots="max 1")
        tr = self.tracks()
        tr.car_stack("tr-0001:M", start=True)
        tr.car_set("tr-0001:M", after=["0b"])
        self.team("stack", "set", "st-0001", "--pr", "202")
        self.team("stack", "park", "st-0001", "--reason", "вливать после 0b")
        rc, out, err = self.team("tracks", "car", "stack", "tr-0001:M")  # no --no-after needed
        self.assertEqual(rc, 0, err)
        self.assertIn("tr-0001:M → st-0001 back in the stack queue (PR #202)", out)
        self.assertIn("tr-0001:M: its after (0b (готов)) is not met; it gates the merge, not the work", out)
        tr = self.tracks()
        self.assertEqual(tr.car("tr-0001:M")[1]["status"], "ready")
        self.assertEqual([p["car"]["n"] for p in self.trains().next(PAST)["picks"]], ["0b"])  # a freed slot does not take M
        self.assertEqual(self.team("stack", "start", "st-0001")[0], 0)              # the owner's word does

    def test_park_refuses_a_full_queue(self):
        self.t.cfg["stack"]["queue_max"] = 1
        s = self.stk()
        s.add("в слоте", pr=410)
        s.start("st-0001")
        s.add("в очереди")
        with self.assertRaises(StackError) as e:
            s.park("st-0001", "ждёт замера")
        self.assertIn("очередь полна (1): a parked object goes to the head of the queue; st-0001 stays in its slot", str(e.exception))
        self.assertIn("or stack post st-0001 if its PR waits only for the merge", str(e.exception))
        self.assertEqual(self.stk().get("st-0001")["where"], "slot")

    def test_no_none_in_known_ids(self):
        s = self.stk()
        s.add("старый")
        del s.get("st-0001")["autostart"]
        s._save("test", "st-0001")
        self.assertEqual(self.team("queues", "migrate")[0], 0)
        s = self.stk()
        self.assertIn(None, [h.get("object") for h in s.history()])  # the migration's row names no object
        self.assertNotIn(None, s.known_ids())
