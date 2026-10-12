"""Tests for large read-only collections (index CSV, search, random picks, chat command names)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.catalog import Collection, Collections

INDEX = """﻿Charakter;Datei;Text (Deutsch);Text (Englisch);Situation;Stimmung;Wwise-ID
Bente Jorgensen;Wem sagst du das.mp3;Wem sagst du das. Zieht sich endlos hin!;Tell me about it.;AT_Bente_Chat1;Positve;1
Bente Jorgensen;AI_DRYBENTE_0HO_00002_3005.mp3;Mein Schiff ist schneller als deins.;My ship is faster.;AT_ai_Threat;Negative;2
Sir Archibald Blake;Kennste nicht Kein Witz.mp3;Kennste nicht? Kein Witz, so sahen wir anno 1602 aus.;;AT_Blake_Joke;;3
Lady Margaret Hunt (KI);Wir suchen.mp3;Wir suchen doch beide nur nach Wahrhaftigkeit.;We are both truth-seekers.;AT_ai10;Positve;4
Lady Margaret Hunt (Kampagne);Hallo.mp3;Hallo Kapitän.;Hello captain.;AT_campaign;;5
_Ohne Zuordnung;1000198965.mp3;;;;;6
Bente Jorgensen;fehlt.mp3;Diese Datei wurde nie exportiert.;;;;7
"""


def make_export(root: Path) -> Path:
    coll = root / "Anno 1800"
    files = [
        "Bente Jorgensen/Wem sagst du das.mp3",
        "Bente Jorgensen/AI_DRYBENTE_0HO_00002_3005.mp3",
        "Bente Jorgensen/Ohne Eintrag im Index.mp3",
        "Sir Archibald Blake/Kennste nicht Kein Witz.mp3",
        "Lady Margaret Hunt (KI)/Wir suchen.mp3",
        "Lady Margaret Hunt (Kampagne)/Hallo.mp3",
        "_Ohne Zuordnung/1000198965.mp3",
        "_Ohne Zuordnung/notiz.txt",
    ]
    for rel in files:
        path = coll / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfb" + rel.encode())
    (coll / "_index.csv").write_text(INDEX, "utf-8")
    return coll


class CollectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        make_export(self.root)
        self.c = Collection(self.root / "Anno 1800")
        self.c.load()

    def tearDown(self):
        self.tmp.cleanup()

    def item(self, text_start: str):
        return next(i for i in self.c.items.values() if i.text.startswith(text_start))

    def test_index_text_wins_over_file_name(self):
        self.assertEqual(len(self.c.items), 7, "audio files only, missing files ignored")
        bente = self.item("Mein Schiff")
        self.assertEqual((bente.group, bente.mood, bente.text_en), ("Bente Jorgensen", "negativ", "My ship is faster."))
        self.assertEqual(self.item("Wem sagst").mood, "positiv", "the export's typo 'Positve' is understood")
        self.assertEqual(self.item("Kennste").text, "Kennste nicht? Kein Witz, so sahen wir anno 1602 aus.")

    def test_readable_file_names_and_code_names(self):
        self.assertEqual(self.item("Ohne Eintrag").text, "Ohne Eintrag im Index")
        unassigned = [i for i in self.c.items.values() if i.group == "Ohne Zuordnung"]
        self.assertEqual(len(unassigned), 1)
        self.assertEqual((unassigned[0].text, unassigned[0].title), ("", "Ohne Text"))

    def test_groups_sorted_with_unassigned_last(self):
        names = [g for g, _ in self.c.groups]
        self.assertEqual(names[-1], "Ohne Zuordnung")
        self.assertEqual(names[0], "Bente Jorgensen")
        self.assertEqual(dict(self.c.groups)["Bente Jorgensen"], 3)

    def test_search_words_group_mood_and_paging(self):
        total, items = self.c.search("schiff")
        self.assertEqual((total, items[0].text), (1, "Mein Schiff ist schneller als deins."))
        self.assertEqual(self.c.search("truth seekers")[0], 1, "English text is searchable")
        self.assertEqual(self.c.search("bente")[0], 3, "speaker names are searchable")
        self.assertEqual(self.c.search("", group="Bente Jorgensen", mood="positiv")[0], 1)
        self.assertEqual(self.c.search("kapitan")[0], 1, "umlauts fold (Kapitän)")
        total, page = self.c.search("", offset=5, limit=10)
        self.assertEqual((total, len(page)), (7, 2))

    def test_pick_prefers_lines_with_text(self):
        for _ in range(30):
            item, total = self.c.pick()
            self.assertTrue(item.text)
            self.assertEqual(total, 7)
        item, total = self.c.pick(group="Ohne Zuordnung")
        self.assertEqual((item.text, total), ("", 1), "a group without text still plays")
        self.assertEqual(self.c.pick("gibtsnicht"), (None, 0))

    def test_find_groups(self):
        self.assertEqual(self.c.find_groups("bente"), ["Bente Jorgensen"])
        self.assertEqual(self.c.find_groups("blake"), ["Sir Archibald Blake"])
        self.assertEqual(len(self.c.find_groups("lady margaret")), 2, "ambiguous names return all candidates")
        self.assertEqual(self.c.find_groups("lady margaret hunt (ki)"), ["Lady Margaret Hunt (KI)"])
        self.assertEqual(self.c.find_groups("niemand"), [])

    def test_board_copy_name_and_transient_sound(self):
        long = self.item("Kennste")
        self.assertEqual(self.c.board_filename(long), "Kennste nicht? Kein Witz, so sahen wir anno 1602 aus.mp3")
        long.text = "Wort " * 40
        name = self.c.board_filename(long)
        self.assertTrue(name.endswith(" ….mp3") and len(name) < 90, name)
        sound = self.c.as_sound(self.item("Mein Schiff"))
        self.assertEqual((sound.category, sound.name), ("Anno 1800/Bente Jorgensen", "Mein Schiff ist schneller als deins."))

    def test_comma_separated_index_and_no_index(self):
        coll = self.root / "Andere"
        (coll / "Gruppe").mkdir(parents=True)
        (coll / "Gruppe" / "a.mp3").write_bytes(b"x")
        Collection(coll).load()  # no index: file names only
        (coll / "_index.csv").write_text("Charakter,Datei,Text (Deutsch)\nGruppe,a.mp3,\"Hallo, Welt\"\n", "utf-8")
        c = Collection(coll)
        c.load()
        self.assertEqual(next(iter(c.items.values())).text, "Hallo, Welt")


CHATWHEEL_INDEX = """\ufeffOrdner;Datei;Sprecher/Team;Text (Original);Übersetzung (Englisch);Spieldatei
Caster TI 2023;Necca - Trampa.mp3;Necca;¡En la trrraampa mortal!;In the death trap!!!;x
Caster TI 2024;tsunami - Chat.mp3;tsunami;Chat is this real?;Chat is this real?;x
Teams Season 2;Team Liquid - Voiceline 3.mp3;Team Liquid;;;x
Voice of God;#dota_chatwheel_source_voice_of_god - Well Played!.mp3;#dota_chatwheel_source_voice_of_god;Well Played!;Well Played!;x
Soundboard;#dota_chatwheel_source_clown - Wow!.mp3;#dota_chatwheel_source_clown;;;x
"""


class ChatWheelIndexTest(unittest.TestCase):
    """Index with an "Ordner" column and a separate speaker (Dota chat wheel export)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        coll = Path(self.tmp.name) / "Dota Chatwheel"
        for line in CHATWHEEL_INDEX.splitlines()[1:]:
            folder, name = line.split(";")[:2]
            (coll / folder).mkdir(parents=True, exist_ok=True)
            (coll / folder / name).write_bytes(b"x")
        (coll / "_index.csv").write_text(CHATWHEEL_INDEX, "utf-8")
        self.c = Collection(coll)
        self.c.load()
        self.by_title = {i.title: i for i in self.c.items.values()}

    def tearDown(self):
        self.tmp.cleanup()

    def test_text_translation_and_speaker(self):
        necca = self.by_title["¡En la trrraampa mortal!"]
        self.assertEqual((necca.group, necca.speaker, necca.text_en), ("Caster TI 2023", "Necca", "In the death trap!!!"))
        self.assertEqual(necca.who, "Caster TI 2023 · Necca")
        self.assertEqual(self.by_title["Chat is this real?"].text_en, "", "no translation identical to the text")
        self.assertEqual(self.c.search("death trap")[0], 1, "the translation is searchable")
        self.assertTrue(self.c.to_json()["translations"], "mixed languages: show translations")

    def test_file_name_fallback_and_localisation_keys(self):
        self.assertEqual(self.by_title["Voiceline 3"].speaker, "Team Liquid")
        god = self.by_title["Well Played!"]
        self.assertEqual((god.speaker, god.who), ("Voice of God", "Voice of God"))
        self.assertEqual(self.by_title["Wow!"].speaker, "Clown")

    def test_speakers_work_like_groups(self):
        self.assertEqual(self.c.find_groups("liquid"), ["Team Liquid"])
        self.assertEqual(self.c.find_groups("tsunami"), ["tsunami"])
        item, total = self.c.pick("", "Team Liquid")
        self.assertEqual((item.title, total), ("Voiceline 3", 1))
        self.assertNotIn("Voice of God", self.c.speakers, "speakers that are also folders are not listed twice")

    def test_command_names_skip_taken_words(self):
        cs = Collections(Path(self.tmp.name))
        cs.assign_commands({"dota", "sb"})
        self.assertEqual(cs.get("dotachatwheel").commands, ("chatwheel", "dotachatwheel"))


class EnglishOnlyIndexTest(unittest.TestCase):
    def test_hero_column_and_english_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            coll = Path(tmp) / "Dota 2"
            (coll / "Abaddon").mkdir(parents=True)
            (coll / "Abaddon" / "A dark thirst slaked.mp3").write_bytes(b"x")
            (coll / "_index.csv").write_text("\ufeffHeld;Datei;Text (Englisch);Situation;Spieldatei\n"
                                             "Abaddon;A dark thirst slaked.mp3;A dark thirst, slaked!;levelup;x\n", "utf-8")
            c = Collection(coll)
            c.load()
            item = next(iter(c.items.values()))
            self.assertEqual((item.text, item.text_en, item.situation), ("A dark thirst, slaked!", "", "levelup"))


class WowIndexTest(unittest.TestCase):
    """WoW export: "Player - Orc Female" folders, "Bezeichnung" as text, "Kategorie" as situation."""

    def test_folders_names_and_several_groups(self):
        index = ("\ufeffOrdner;Datei;Kategorie;Rasse;Geschlecht;Bezeichnung;FileDataID;Spieldatei\n"
                 "Player - Tauren Male;Moo.mp3;Emote;Tauren;Male;Moo;1;x\n"
                 "Player - Tauren Female;Moo.mp3;Emote;Tauren;Female;Moo;2;x\n"
                 "NPC - Tauren Male Brave;Greeting.mp3;NPC;Tauren;Male;Greeting;3;x\n"
                 "Player - Orc Female;Not enough mana.mp3;Error;Orc;Female;Not enough mana;4;x\n")
        with tempfile.TemporaryDirectory() as tmp:
            coll = Path(tmp) / "WoW"
            for line in index.splitlines()[1:]:
                folder, name = line.split(";")[:2]
                (coll / folder).mkdir(parents=True, exist_ok=True)
                (coll / folder / name).write_bytes(b"x")
            (coll / "_index.csv").write_text(index, "utf-8")
            c = Collection(coll)
            c.load()
            mana = next(i for i in c.items.values() if i.group == "Player - Orc Female")
            self.assertEqual((mana.title, mana.situation, mana.who), ("Not enough mana", "Error", "Player - Orc Female"))
            tauren = c.find_groups("tauren")
            self.assertEqual(len(tauren), 3)
            self.assertEqual(c.pick("moo", tauren)[1], 2, "a query across all matching groups")
            self.assertEqual(c.search("emote")[0], 2, "the category is searchable")
            cs = Collections(Path(tmp))
            cs.assign_commands({"sb"})
            self.assertEqual(cs.get("wow").commands, ("wow",))


class NumberedLinesTest(unittest.TestCase):
    """Age of Empires taunts: "!aoe 30" plays taunt 30, the list is in taunt order."""

    def test_number_query_and_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            coll = Path(tmp) / "AoE2"
            coll.mkdir()
            for name in ("1 Yes.mp3", "11 Hahahahahah.mp3", "2 No.mp3", "30 WOLOLO.mp3"):
                (coll / name).write_bytes(b"x")
            (coll / "_index.csv").write_text("\ufeffNummer;Datei;Text\n1;1 Yes.mp3;Yes\n2;2 No.mp3;No\n"
                                             "11;11 Hahahahahah.mp3;Hahahahahah\n30;30 WOLOLO.mp3;WOLOLO\n", "utf-8")
            c = Collection(coll)
            c.load()
            self.assertEqual([i.title for i in c.items.values()], ["Yes", "No", "Hahahahahah", "WOLOLO"])
            self.assertEqual([i.title for i in c.matching("1")], ["Yes"], "the number, not every line with a 1")
            self.assertEqual(c.pick("30")[0].number, "30")
            self.assertEqual(c.example_query() in {"1", "2", "11", "30"}, True)
            cs = Collections(Path(tmp))
            cs.assign_commands({"sb"})
            self.assertEqual(cs.get("aoe2").commands, ("aoe", "aoe2"))


class IncrementalLoadTest(unittest.TestCase):
    """A running export keeps adding files: rescans only read changed folders and skip partial files."""

    def test_rescan_picks_up_new_files_and_skips_partial_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            coll = Path(tmp) / "Dota 2"
            for rel in ("Axe/A Keen worthy to fight.mp3", "Axe/A Keen worthy to fight (2).mp3", "Lina/Fire.mp3"):
                (coll / rel).parent.mkdir(parents=True, exist_ok=True)
                (coll / rel).write_bytes(b"x")
            c = Collection(coll)
            c.load()
            self.assertEqual(len(c.items), 3)
            self.assertEqual(sorted(i.text for i in c.items.values()),
                             ["A Keen worthy to fight", "A Keen worthy to fight", "Fire"], "second takes read cleanly")
            lina_items = [i for i in c.items.values() if i.group == "Lina"]

            (coll / "Axe" / "Halbfertig.mp3.part").write_bytes(b"x")
            (coll / "Axe" / "Auch halb.part.mp3").write_bytes(b"x")
            (coll / "Axe" / "Neu.mp3").write_bytes(b"x")
            (coll / "Pudge").mkdir()
            (coll / "Pudge" / "Fresh meat.mp3").write_bytes(b"x")
            os.utime(coll / "Axe", ns=(1, 2))  # make sure the folder time changed, whatever the clock resolution
            c.load()
            texts = {i.text for i in c.items.values()}
            self.assertIn("Neu", texts)
            self.assertIn("Fresh meat", texts)
            self.assertFalse(any("halb" in t.casefold() for t in texts), "partial files are ignored")
            self.assertEqual(len(c.items), 5)
            self.assertIs([i for i in c.items.values() if i.group == "Lina"][0], lina_items[0],
                          "unchanged folders are taken over, not read again")
            self.assertEqual([g for g, _ in c.groups], ["Axe", "Lina", "Pudge"])

            (coll / "Lina" / "Fire.mp3").unlink()
            c.load(full=True)
            self.assertEqual(len(c.items), 4, "a full rescan also notices removed files")


class CollectionsTest(unittest.TestCase):
    def test_discovery_and_command_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_export(root)
            (root / "Stop").mkdir()  # would shadow /stop
            (root / "_privat").mkdir()
            (root / "datei.mp3").write_bytes(b"x")
            cs = Collections(root)
            self.assertEqual([c.name for c in cs], ["Anno 1800", "Stop"])
            cs.assign_commands({"stop", "sb", "play"})
            self.assertEqual(cs.get("anno1800").commands, ("anno", "anno1800"))
            self.assertEqual(cs.get("stop").commands, (), "existing commands win")
            self.assertIs(cs.by_command("ANNO"), cs.get("anno1800"))
            self.assertEqual(len(Collections(root / "fehlt")), 0, "missing folder is fine")

    def test_discover_new_folders_and_keep_loaded_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cs = Collections(root)
            self.assertEqual(len(cs), 0)
            self.assertFalse(cs.discover(), "nothing changed")
            make_export(root)  # e.g. a volume filled after the bot started
            self.assertTrue(cs.discover())
            anno = cs.get("anno1800")
            anno.load()
            (root / "WoW").mkdir()
            self.assertTrue(cs.discover())
            self.assertIs(cs.get("anno1800"), anno, "already read collections are kept")
            self.assertTrue(cs.get("anno1800").loaded)
            cs.assign_commands({"sb"})
            self.assertEqual(cs.get("wow").commands, ("wow",))
            self.assertFalse(cs.discover())

    def test_first_root_wins_for_equal_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            extern, local = Path(tmp) / "extern", Path(tmp) / "local"
            make_export(extern)
            (local / "Anno 1800" / "Alt").mkdir(parents=True)
            (local / "Anno 1800" / "Alt" / "alt.mp3").write_bytes(b"x")
            (local / "Weitere").mkdir()
            cs = Collections([extern, local, Path(tmp) / "fehlt"])
            self.assertEqual([c.name for c in cs], ["Anno 1800", "Weitere"])
            self.assertEqual(cs.get("anno1800").root, extern / "Anno 1800", "the original folder, not the old copy")


if __name__ == "__main__":
    unittest.main()
