
from test_queues import QBase

from depot2.stack import Stack
from depot2.tracks import Tracks
from depot2.tstack import TeamStack


class TakeBase(QBase):
    def setUp(self):
        super(TakeBase, self).setUp()
        tr = Tracks(self.t.state, self.t.cfg)
        tr.add("Некритичные баги")
        tr.car_add("tr-0001", "поиск по пустой строке")  # car 0: the next free name is 1

    def tracks(self):
        return Tracks(self.t.state, self.t.cfg)

    def raw(self, name):
        with open(self.t.state.path(name), encoding="utf-8") as f:
            return f.read()

    def ok(self, *argv):
        rc, out, err = self.team(*argv)
        self.assertEqual(rc, 0, err)
        return out


class StackTakeTest(TakeBase):
    def test_an_object_becomes_a_car_and_stays_its_object(self):
        stk = Stack(self.t.state, self.t.cfg)
        stk.add("обложка в карточке, а в списке нет", brief="/tmp/brief-cover.md", size="S")
        stk.add("счётчик пропал")
        stk.add("кнопка экспорта")
        self.ok("stack", "start", "st-0002")
        self.ok("stack", "start", "st-0003")
        self.ok("stack", "set", "st-0003", "--pr", "77")
        self.ok("stack", "post", "st-0003")
        out = self.ok("stack", "take", "st-0001", "--to", "tr-0001")
        self.assertIn("st-0001 → tr-0001:1: новый вагон «обложка в карточке, а в списке нет» (ждёт, эскиз); st-0001 — его "
                      "объект: «tr-0001:1 Некритичные баги — обложка в карточке, а в списке нет»", out)
        t, c = self.tracks().car("tr-0001:1")
        self.assertEqual((c["st"], c["status"], c["stage"], c["size"], c["brief"], c["from"]),
                         ("st-0001", "waiting", "sketch", "S", "/tmp/brief-cover.md", ["st-0001"]))
        self.assertIn("из st-0001 — объект стека (в очереди; добавлен ", c["notes"][0])
        o = Stack(self.t.state, self.t.cfg).get("st-0001")
        self.assertEqual((o["where"], o["title"]), ("queue", "tr-0001:1 Некритичные баги — обложка в карточке, а в списке нет"))
        self.assertIn("tr-0001:1 (объект st-0001)", self.ok("car", "show", "st-0001"))
        # a slot: the car is in work; the post-stack: its PR waits for the owner's merge
        self.ok("stack", "take", "st-0002", "--to", "tr-0001", "--title", "счётчик")
        self.assertEqual(self.tracks().car("tr-0001:2")[1]["status"], "running")
        self.ok("stack", "take", "st-0003", "--to", "tr-0001")
        t, c = self.tracks().car("tr-0001:3")
        self.assertEqual((c["status"], c["pr"]), ("pr", 77))
        self.assertTrue(self.tracks().posted(c))
        # refused: a car's object already, one that left
        rc, _, err = self.team("stack", "take", "st-0001", "--to", "tr-0001")
        self.assertEqual(rc, 1)
        self.assertIn("st-0001 — уже объект вагона tr-0001:1", err)
        rc, _, err = self.team("stack", "take", "st-0001", "--to", "tr-0001:1")
        self.assertIn("--to — трек tr-NNNN (на нём новый вагон)", err)
        # the link works as any car's: done --car merged closes the car
        self.ok("stack", "done", "st-0002", "--reason", "merged #78", "--car", "merged")
        self.assertEqual(self.tracks().car("tr-0001:2")[1]["status"], "merged")
        stk = Stack(self.t.state, self.t.cfg)
        stk.add("ушёл")
        self.ok("stack", "done", "st-0004", "--reason", "снят")
        rc, _, err = self.team("stack", "take", "st-0004", "--to", "tr-0001")
        self.assertEqual(rc, 1)
        self.assertIn("st-0004 вышел из стека", err)

    def test_a_team_item_becomes_a_car_and_leaves(self):
        ts = TeamStack(self.t.state, self.t.cfg)
        ts.add("question", title="спросить команду про take", input_path="/tmp/q.md")
        ts.add("discuss", title="идёт сейчас")
        self.ok("tstack", "start", "tm-0002")
        out = self.ok("tstack", "take", "tm-0001", "--to", "tr-0001", "--size", "S")
        self.assertIn("tm-0001 → tr-0001:1: новый вагон «спросить команду про take» (тележка); tm-0001 вышел из стека "
                      "команды", out)
        c = self.tracks().car("tr-0001:1")[1]
        self.assertEqual((c["from"], c["size"], c["st"], c["stage"]), (["tm-0001"], "S", None, "bogie"))
        self.assertIn("из tm-0001 — стек команды (в очереди, вид: вопрос команде; кто: все; вход: /tmp/q.md; добавлен ",
                      c["notes"][0])
        self.assertNotIn("tm-0001", [o["id"] for o in TeamStack(self.t.state, self.t.cfg).objects()])
        h = TeamStack(self.t.state, self.t.cfg).history()[-1]
        self.assertEqual((h["event"], h["reason"]), ("done", "взят в вагон tr-0001:1"))
        self.assertIn("Пришёл из: tm-0001 — стек команды «спросить команду про take» (вышел из стека).",
                      self.ok("car", "show", "tr-0001:1"))
        rc, _, err = self.team("tstack", "take", "tm-0002", "--to", "tr-0001")
        self.assertEqual(rc, 1)
        self.assertIn("tm-0002 в слоте команды: идёт его раунд", err)

