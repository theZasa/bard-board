"""Tests for large read-only collections (index CSV, search, random picks, chat command names)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from app.catalog import Collection, Collections

INDEX = """﻿Charakter;Datei;Text (Deutsch);Text (Englisch);Situation;Stimmung;Wwise-ID
Paula Berger;Wem sagst du das.mp3;Wem sagst du das. Zieht sich endlos hin!;Tell me about it.;AT_Paula_Chat1;Positve;1
Paula Berger;AI_DRYPAULA_0HO_00002_3005.mp3;Mein Schiff ist schneller als deins.;My ship is faster.;AT_ai_Threat;Negative;2
Professor Otto Blum;Kennste nicht Kein Witz.mp3;Kennste nicht? Kein Witz, so sahen wir 1602 aus.;;AT_Blum_Witz;;3
Rosa Margarete Hartmann (KI);Wir suchen.mp3;Wir suchen doch beide nur nach Wahrhaftigkeit.;We are both truth-seekers.;AT_ai10;Positve;4
Rosa Margarete Hartmann (Kampagne);Hallo.mp3;Hallo Kapitän.;Hello captain.;AT_campaign;;5
_Ohne Zuordnung;1000198965.mp3;;;;;6
Paula Berger;fehlt.mp3;Diese Datei wurde nie exportiert.;;;;7
"""


def make_export(root: Path) -> Path:
    coll = root / "Archiv 1990"
    files = [
        "Paula Berger/Wem sagst du das.mp3",
        "Paula Berger/AI_DRYPAULA_0HO_00002_3005.mp3",
        "Paula Berger/Ohne Eintrag im Index.mp3",
        "Professor Otto Blum/Kennste nicht Kein Witz.mp3",
        "Rosa Margarete Hartmann (KI)/Wir suchen.mp3",
        "Rosa Margarete Hartmann (Kampagne)/Hallo.mp3",
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
        self.c = Collection(self.root / "Archiv 1990")
        self.c.load()

    def tearDown(self):
        self.tmp.cleanup()

    def item(self, text_start: str):
        return next(i for i in self.c.items.values() if i.text.startswith(text_start))

    def test_index_text_wins_over_file_name(self):
        self.assertEqual(len(self.c.items), 7, "audio files only, missing files ignored")
        paula = self.item("Mein Schiff")
        self.assertEqual((paula.group, paula.mood, paula.text_en), ("Paula Berger", "negativ", "My ship is faster."))
        self.assertEqual(self.item("Wem sagst").mood, "positiv", "the export's typo 'Positve' is understood")
        self.assertEqual(self.item("Kennste").text, "Kennste nicht? Kein Witz, so sahen wir 1602 aus.")

    def test_readable_file_names_and_code_names(self):
        self.assertEqual(self.item("Ohne Eintrag").text, "Ohne Eintrag im Index")
        unassigned = [i for i in self.c.items.values() if i.group == "Ohne Zuordnung"]
        self.assertEqual(len(unassigned), 1)
        self.assertEqual((unassigned[0].text, unassigned[0].title), ("", "Ohne Text"))

    def test_groups_sorted_with_unassigned_last(self):
        names = [g for g, _ in self.c.groups]
        self.assertEqual(names[-1], "Ohne Zuordnung")
        self.assertEqual(names[0], "Paula Berger")
        self.assertEqual(dict(self.c.groups)["Paula Berger"], 3)

    def test_search_words_group_mood_and_paging(self):
        total, items = self.c.search("schiff")
        self.assertEqual((total, items[0].text), (1, "Mein Schiff ist schneller als deins."))
        self.assertEqual(self.c.search("truth seekers")[0], 1, "English text is searchable")
        self.assertEqual(self.c.search("paula")[0], 3, "speaker names are searchable")
        self.assertEqual(self.c.search("", group="Paula Berger", mood="positiv")[0], 1)
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
        self.assertEqual(self.c.find_groups("paula"), ["Paula Berger"])
        self.assertEqual(self.c.find_groups("blum"), ["Professor Otto Blum"])
        self.assertEqual(len(self.c.find_groups("rosa margarete")), 2, "ambiguous names return all candidates")
        self.assertEqual(self.c.find_groups("rosa margarete hartmann (ki)"), ["Rosa Margarete Hartmann (KI)"])
        self.assertEqual(self.c.find_groups("niemand"), [])

    def test_board_copy_name_and_transient_sound(self):
        long = self.item("Kennste")
        self.assertEqual(self.c.board_filename(long), "Kennste nicht? Kein Witz, so sahen wir 1602 aus.mp3")
        long.text = "Wort " * 40
        name = self.c.board_filename(long)
        self.assertTrue(name.endswith(" ….mp3") and len(name) < 90, name)
        sound = self.c.as_sound(self.item("Mein Schiff"))
        self.assertEqual((sound.category, sound.name), ("Archiv 1990/Paula Berger", "Mein Schiff ist schneller als deins."))

    def test_comma_separated_index_and_no_index(self):
        coll = self.root / "Andere"
        (coll / "Gruppe").mkdir(parents=True)
        (coll / "Gruppe" / "a.mp3").write_bytes(b"x")
        Collection(coll).load()  # no index: file names only
        (coll / "_index.csv").write_text("Charakter,Datei,Text (Deutsch)\nGruppe,a.mp3,\"Hallo, Welt\"\n", "utf-8")
        c = Collection(coll)
        c.load()
        self.assertEqual(next(iter(c.items.values())).text, "Hallo, Welt")


EXTRAS_INDEX = """\ufeffOrdner;Datei;Sprecher/Team;Text (Original);Übersetzung (Englisch);Spieldatei
Moderation 2023;Rita - Jugada.mp3;Rita;¡Qué jugada increíble!;What an incredible play!;x
Moderation 2024;nova - Chat.mp3;nova;Chat is this real?;Chat is this real?;x
Teams Staffel 2;Team Komet - Voiceline 3.mp3;Team Komet;;;x
Voice of God;#podcast_extras_source_voice_of_god - Well Played!.mp3;#podcast_extras_source_voice_of_god;Well Played!;Well Played!;x
Soundboard;#podcast_extras_source_clown - Super!.mp3;#podcast_extras_source_clown;;;x
"""


class SpeakerIndexTest(unittest.TestCase):
    """Index with an "Ordner" column and a separate speaker (e.g. a voice line export with casters and teams)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        coll = Path(self.tmp.name) / "Podcast Extras"
        for line in EXTRAS_INDEX.splitlines()[1:]:
            folder, name = line.split(";")[:2]
            (coll / folder).mkdir(parents=True, exist_ok=True)
            (coll / folder / name).write_bytes(b"x")
        (coll / "_index.csv").write_text(EXTRAS_INDEX, "utf-8")
        self.c = Collection(coll)
        self.c.load()
        self.by_title = {i.title: i for i in self.c.items.values()}

    def tearDown(self):
        self.tmp.cleanup()

    def test_text_translation_and_speaker(self):
        rita = self.by_title["¡Qué jugada increíble!"]
        self.assertEqual((rita.group, rita.speaker, rita.text_en), ("Moderation 2023", "Rita", "What an incredible play!"))
        self.assertEqual(rita.who, "Moderation 2023 · Rita")
        self.assertEqual(self.by_title["Chat is this real?"].text_en, "", "no translation identical to the text")
        self.assertEqual(self.c.search("incredible play")[0], 1, "the translation is searchable")
        self.assertTrue(self.c.to_json()["translations"], "mixed languages: show translations")

    def test_file_name_fallback_and_localisation_keys(self):
        self.assertEqual(self.by_title["Voiceline 3"].speaker, "Team Komet")
        god = self.by_title["Well Played!"]
        self.assertEqual((god.speaker, god.who), ("Voice of God", "Voice of God"))
        self.assertEqual(self.by_title["Super!"].speaker, "Clown")

    def test_speakers_work_like_groups(self):
        self.assertEqual(self.c.find_groups("komet"), ["Team Komet"])
        self.assertEqual(self.c.find_groups("nova"), ["nova"])
        item, total = self.c.pick("", "Team Komet")
        self.assertEqual((item.title, total), ("Voiceline 3", 1))
        self.assertNotIn("Voice of God", self.c.speakers, "speakers that are also folders are not listed twice")

    def test_command_names_skip_taken_words(self):
        cs = Collections(Path(self.tmp.name))
        cs.assign_commands({"podcast", "sb"})
        self.assertEqual(cs.get("podcastextras").commands, ("extras", "podcastextras"))


class EnglishOnlyIndexTest(unittest.TestCase):
    def test_hero_column_and_english_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            coll = Path(tmp) / "Podcast 2"
            (coll / "Abaddon").mkdir(parents=True)
            (coll / "Abaddon" / "A dark thirst slaked.mp3").write_bytes(b"x")
            (coll / "_index.csv").write_text("\ufeffHeld;Datei;Text (Englisch);Situation;Spieldatei\n"
                                             "Abaddon;A dark thirst slaked.mp3;A dark thirst, slaked!;levelup;x\n", "utf-8")
            c = Collection(coll)
            c.load()
            item = next(iter(c.items.values()))
            self.assertEqual((item.text, item.text_en, item.situation), ("A dark thirst, slaked!", "", "levelup"))


class FolderIndexTest(unittest.TestCase):
    """Export with "Spieler - Zwerg Frau" folders, "Bezeichnung" as text, "Kategorie" as situation."""

    def test_folders_names_and_several_groups(self):
        index = ("\ufeffOrdner;Datei;Kategorie;Rasse;Geschlecht;Bezeichnung;FileDataID;Spieldatei\n"
                 "Spieler - Riese Mann;Muh.mp3;Geste;Riese;Mann;Muh;1;x\n"
                 "Spieler - Riese Frau;Muh.mp3;Geste;Riese;Frau;Muh;2;x\n"
                 "NPC - Riese Mann Wache;Gruss.mp3;NPC;Riese;Mann;Gruss;3;x\n"
                 "Spieler - Zwerg Frau;Nicht genug Kraft.mp3;Fehler;Zwerg;Frau;Nicht genug Kraft;4;x\n")
        with tempfile.TemporaryDirectory() as tmp:
            coll = Path(tmp) / "Quiz"
            for line in index.splitlines()[1:]:
                folder, name = line.split(";")[:2]
                (coll / folder).mkdir(parents=True, exist_ok=True)
                (coll / folder / name).write_bytes(b"x")
            (coll / "_index.csv").write_text(index, "utf-8")
            c = Collection(coll)
            c.load()
            mana = next(i for i in c.items.values() if i.group == "Spieler - Zwerg Frau")
            self.assertEqual((mana.title, mana.situation, mana.who), ("Nicht genug Kraft", "Fehler", "Spieler - Zwerg Frau"))
            riesen = c.find_groups("riese")
            self.assertEqual(len(riesen), 3)
            self.assertEqual(c.pick("muh", riesen)[1], 2, "a query across all matching groups")
            self.assertEqual(c.search("geste")[0], 2, "the category is searchable")
            cs = Collections(Path(tmp))
            cs.assign_commands({"sb"})
            self.assertEqual(cs.get("quiz").commands, ("quiz",))


class NumberedLinesTest(unittest.TestCase):
    """Numbered lines: "!rufe 30" plays line 30, the list is in number order."""

    def test_number_query_and_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            coll = Path(tmp) / "Rufe2"
            coll.mkdir()
            for name in ("1 Yes.mp3", "11 Hahahahahah.mp3", "2 No.mp3", "30 Hallo.mp3"):
                (coll / name).write_bytes(b"x")
            (coll / "_index.csv").write_text("\ufeffNummer;Datei;Text\n1;1 Yes.mp3;Yes\n2;2 No.mp3;No\n"
                                             "11;11 Hahahahahah.mp3;Hahahahahah\n30;30 Hallo.mp3;Hallo\n", "utf-8")
            c = Collection(coll)
            c.load()
            self.assertEqual([i.title for i in c.items.values()], ["Yes", "No", "Hahahahahah", "Hallo"])
            self.assertEqual([i.title for i in c.matching("1")], ["Yes"], "the number, not every line with a 1")
            self.assertEqual(c.pick("30")[0].number, "30")
            self.assertEqual(c.example_query() in {"1", "2", "11", "30"}, True)
            cs = Collections(Path(tmp))
            cs.assign_commands({"sb"})
            self.assertEqual(cs.get("rufe2").commands, ("rufe", "rufe2"))


class IncrementalLoadTest(unittest.TestCase):
    """A running export keeps adding files: rescans only read changed folders and skip partial files."""

    # On Windows hosts unchanged folders get read again; the bot itself always runs on Linux.
    @unittest.skipIf(sys.platform == "win32", "rescan detection is Linux-only; covered in CI and in the container")
    def test_rescan_picks_up_new_files_and_skips_partial_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            coll = Path(tmp) / "Podcast 2"
            for rel in ("Max/Ein guter Tag.mp3", "Max/Ein guter Tag (2).mp3", "Nora/Feuer.mp3"):
                (coll / rel).parent.mkdir(parents=True, exist_ok=True)
                (coll / rel).write_bytes(b"x")
            c = Collection(coll)
            c.load()
            self.assertEqual(len(c.items), 3)
            self.assertEqual(sorted(i.text for i in c.items.values()),
                             ["Ein guter Tag", "Ein guter Tag", "Feuer"], "second takes read cleanly")
            nora_items = [i for i in c.items.values() if i.group == "Nora"]

            (coll / "Max" / "Halbfertig.mp3.part").write_bytes(b"x")
            (coll / "Max" / "Auch halb.part.mp3").write_bytes(b"x")
            (coll / "Max" / "Neu.mp3").write_bytes(b"x")
            (coll / "Otto").mkdir()
            (coll / "Otto" / "Frisches Brot.mp3").write_bytes(b"x")
            os.utime(coll / "Max", ns=(1, 2))  # make sure the folder time changed, whatever the clock resolution
            c.load()
            texts = {i.text for i in c.items.values()}
            self.assertIn("Neu", texts)
            self.assertIn("Frisches Brot", texts)
            self.assertFalse(any("halb" in t.casefold() for t in texts), "partial files are ignored")
            self.assertEqual(len(c.items), 5)
            self.assertIs([i for i in c.items.values() if i.group == "Nora"][0], nora_items[0],
                          "unchanged folders are taken over, not read again")
            self.assertEqual([g for g, _ in c.groups], ["Max", "Nora", "Otto"])

            (coll / "Nora" / "Feuer.mp3").unlink()
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
            self.assertEqual([c.name for c in cs], ["Archiv 1990", "Stop"])
            cs.assign_commands({"stop", "sb", "play"})
            self.assertEqual(cs.get("archiv1990").commands, ("archiv", "archiv1990"))
            self.assertEqual(cs.get("stop").commands, (), "existing commands win")
            self.assertIs(cs.by_command("ARCHIV"), cs.get("archiv1990"))
            self.assertEqual(len(Collections(root / "fehlt")), 0, "missing folder is fine")

    def test_discover_new_folders_and_keep_loaded_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cs = Collections(root)
            self.assertEqual(len(cs), 0)
            self.assertFalse(cs.discover(), "nothing changed")
            make_export(root)  # e.g. a volume filled after the bot started
            self.assertTrue(cs.discover())
            archiv = cs.get("archiv1990")
            archiv.load()
            (root / "Quiz").mkdir()
            self.assertTrue(cs.discover())
            self.assertIs(cs.get("archiv1990"), archiv, "already read collections are kept")
            self.assertTrue(cs.get("archiv1990").loaded)
            cs.assign_commands({"sb"})
            self.assertEqual(cs.get("quiz").commands, ("quiz",))
            self.assertFalse(cs.discover())

    def test_first_root_wins_for_equal_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            extern, local = Path(tmp) / "extern", Path(tmp) / "local"
            make_export(extern)
            (local / "Archiv 1990" / "Alt").mkdir(parents=True)
            (local / "Archiv 1990" / "Alt" / "alt.mp3").write_bytes(b"x")
            (local / "Weitere").mkdir()
            cs = Collections([extern, local, Path(tmp) / "fehlt"])
            self.assertEqual([c.name for c in cs], ["Archiv 1990", "Weitere"])
            self.assertEqual(cs.get("archiv1990").root, extern / "Archiv 1990", "the original folder, not the old copy")


if __name__ == "__main__":
    unittest.main()
