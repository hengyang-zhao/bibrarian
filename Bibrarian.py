import getpass
import glob
import hashlib
import itertools
import json
import logging
import os
import sys
import threading
import time
import traceback

import urllib
import urllib.request
import urllib.parse

import urwid

import pybtex
import pybtex.database

class BibEntry:
    class SearchPanelWidgetImpl(urwid.AttrMap):
        def __init__(self, entry):
            super().__init__(urwid.SolidFill(), None)
            self.entry = entry

            self.title = urwid.AttrMap(urwid.Text(entry.Title()), 'title')
            self.info = urwid.Text([('author', f"{entry.AbbrevAuthors()}"),
                                    ('delim', ". "),
                                    ('venue', f"{entry.Venue()}"),
                                    ('delim', ", "),
                                    ('year', f"{entry.Year()}"),
                                    ('delim', ".")])
            self.mark = urwid.AttrMap(urwid.Text(('mark_none', "[M]"), align='right'), None)
            self.source = urwid.Text([('source', f"{entry.Source()}"),
                                      ('delim', "::"),
                                      ('bibkey', f"{entry.BibKey()}")])

            self.original_widget = urwid.Pile([
                urwid.AttrMap(urwid.Columns([('weight', 1, self.title),
                                             ('pack', self.mark)],
                                            dividechars=1),
                              'title'),
                self.info, self.source])

            self.set_focus_map({palette_key: str(palette_key) + '+' for palette_key in [
                'title', 'author', 'delim', 'venue', 'year', 'source',
                'bibkey', 'mark_none', 'mark_selected', 'title_delim',
                'bibtex_ready', 'bibtex_fetching', None]})

        def selectable(self):
            return True

        def keypress(self, size, key):
            if key == ' ':
                selected_keys_panel.Toggle(self.entry)
                self.entry.OnSelection()
            elif key == 'i':
                details_panel.original_widget = self.entry.DetailsWidget()
            else:
                return key

    def __init__(self, source, repo):
        self.repo = repo
        self.source = source
        self.search_panel_widget = None
        self.mark = None

    def Authors(self): return NotImplemented
    def Title(self): return NotImplemented
    def Year(self): return NotImplemented
    def Venue(self): return NotImplemented
    def BibKey(self): return NotImplemented

    def ToPybEntry(self): return NotImplemented
    def DetailsWidget(self): return NotImplemented

    def OnSelection(self): pass

    def AbbrevAuthors(self):
        authors = self.Authors()
        if len(authors) == 1:
            return f"{authors[0]}"
        else:
            return f"{authors[0]} et al"

    def Source(self):
        return self.source

    def Match(self, keywords):
        trivial = True
        for keyword in filter(lambda k: len(k) >= 3, keywords):
            trivial = False

            if keyword.upper() in self.Title().upper():
                continue

            matched = False
            for author in self.Authors():
                if keyword.upper() in author.upper():
                    matched = True
                    break

            if not matched: return False

        return not trivial

    def SearchPanelWidget(self):
        self.InitializeSearchPanelWidget()
        return self.search_panel_widget

    def Mark(self, mark):
        self.InitializeSearchPanelWidget()
        if mark is None:
            self.search_panel_widget.mark.original_widget.set_text(
                    [('title_delim', "["), ('mark_none', " "), ('title_delim', "]")])
        elif mark == 'selected':
            self.search_panel_widget.mark.original_widget.set_text(
                    [('title_delim', "["), ('mark_selected', "X"), ('title_delim', "]")])
        else:
            raise ValueError(f"Invalid mark: {mark}")

    def InitializeSearchPanelWidget(self):
        if self.search_panel_widget is None:
            self.search_panel_widget = BibEntry.SearchPanelWidgetImpl(self)

    def UniqueKey(self):
        return f"{self.Source()}::{self.BibKey()}"

    def UniqueKeyItem(self):
        return urwid.Text([('selected_key', self.BibKey()), ('selected_hint', f"({self.Source()})")])

class DblpEntry(BibEntry):

    class DetailsWidgetImpl(urwid.Pile):

        def __init__(self, entry):
            super().__init__([])

            self.entry = entry

            self.key_item = urwid.Columns([('pack', urwid.Text(('detail_key', "citation key: "))),
                                           ('weight', 1, urwid.Text(('detail_value', entry.BibKey())))])
            self.source_item = urwid.Columns([('pack', urwid.Text(('detail_key', "source: "))),
                                              ('weight', 1, urwid.Text(('detail_value', entry.Source())))])
            self.person_items = urwid.Pile([
                urwid.Columns([('pack', urwid.Text(('detail_key', f"{k.lower()}: "))),
                               ('weight', 1, urwid.Text(('detail_value', '\n'.join(entry.data['info']['authors'][k]))))])
                for k in entry.data['info']['authors'].keys()
                ])

            self.info_items = urwid.Pile([
                urwid.Columns([('pack', urwid.Text(('detail_key', f"{k.lower()}: "))),
                               ('weight', 1, urwid.Text(('detail_value', f"{entry.data['info'][k]}")))])
                for k in entry.data['info'].keys() if k != 'authors'
                ])

            self.contents = [(self.key_item, ('pack', None)),
                             (self.source_item, ('pack', None)),
                             (self.person_items, ('pack', None)),
                             (self.info_items, ('pack', None)),
                             (urwid.SolidFill(), ('weight', 1))]

    class FdWriteHandler:
        def __init__(self, loop):
            self.loop = loop

        def __call__(self, data):
            self.loop.draw_screen()

    def __init__(self, dblp_entry, repo):
        super().__init__('dblp.org', repo)
        self.data = dblp_entry
        self.details_widget = None
        self.bib_key = None

        self.redraw_fd = None

        self.pybtex_entry = None
        self.bibtex_loading_done = threading.Event()

        self.bibtex_loading_thread = threading.Thread(
                name=f"bibtex-{self.BibKey()}",
                target=self.LoadPybtexEntry,
                daemon=False)

    def OnSelection(self):
        if self.redraw_fd is None:
            event_loop = self.repo.event_loop
            self.redraw_fd = event_loop.watch_pipe(DblpEntry.FdWriteHandler(event_loop))
            self.bibtex_loading_thread.start()

    def LoadPybtexEntry(self):
        bib_url = f"https://dblp.org/rec/bib2/{self.data['info']['key']}.bib"
        try:
            if self.search_panel_widget is not None:
                self.search_panel_widget.source.set_text([
                    ('source', f"{self.Source()}"),
                    ('delim', "::"),
                    ('bibkey', f"{self.BibKey()}"),
                    ('bibtex_fetching', " (fetching bibtex)")])
                os.write(self.redraw_fd, b"?")

            with urllib.request.urlopen(bib_url) as remote:
                bib_text = remote.read().decode('utf-8')

            pyb_db = pybtex.database.parse_string(bib_text, 'bibtex')
            self.pybtex_entry = pyb_db.entries[f"DBLP:{self.data['info']['key']}"]

            if self.search_panel_widget is not None:
                self.search_panel_widget.source.set_text([
                    ('source', f"{self.Source()}"),
                    ('delim', "::"),
                    ('bibkey', f"{self.BibKey()}"),
                    ('bibtex_ready', " (bibtex ready)")])
                os.write(self.redraw_fd, b"?")

        except Exception as e:
            logging.error(f"Error when fetching bibtex entry from DBLP: Entry: {self.data} {traceback.format_exc()}")

        self.bibtex_loading_done.set()

    def ToPybEntry(self):
        self.bibtex_loading_done.wait()
        return self.pybtex_entry

    def Authors(self):
        try:
            authors = self.data['info']['authors']['author']
            if authors: return authors
            else: return ["Unknown"]
        except: return ["Unknown"]

    def Title(self):
        try: return str(self.data['info']['title'])
        except: return "Unknown"

    def Year(self):
        try: return str(self.data['info']['year'])
        except: return "Unknown"

    def Venue(self):
        try: return self.data['info']['venue']
        except: return "Unknown"

    def BibKey(self):
        if self.bib_key is None:
            flat_key = self.data['info']['key']
            base = flat_key.split('/')[-1]
            sha1 = hashlib.sha1(flat_key.encode('utf-8')).hexdigest()
            self.bib_key = f"{base}:{sha1[:4].upper()}"

        return self.bib_key

    def InitializeDetailsWidget(self):
        if self.details_widget is None:
            self.details_widget = DblpEntry.DetailsWidgetImpl(self)

    def DetailsWidget(self):
        self.InitializeDetailsWidget()
        return self.details_widget

    def __del__(self):
        if self.redraw_fd is not None:
            os.close(self.redraw_fd)

class BibtexEntry(BibEntry):

    class DetailsWidgetImpl(urwid.Pile):

        def __init__(self, entry):
            super().__init__([])

            self.entry = entry
            self.key = urwid.Columns([
                ('pack', urwid.Text(('detail_key', "citation key: "))),
                ('weight', 1, urwid.Text(('detail_value', entry.BibKey())))])

            self.source = urwid.Columns([
                ('pack', urwid.Text(('detail_key', "source: "))),
                ('weight', 1, urwid.Text(('detail_value', entry.Source())))])

            self.item_type = urwid.Columns([
                ('pack', urwid.Text(('detail_key', "type: "))),
                ('weight', 1, urwid.Text(('detail_value', entry.entry.type)))])

            self.persons = urwid.Pile([
                urwid.Columns([('pack', urwid.Text(('detail_key', f"{k.lower()}: "))),
                               ('weight', 1, urwid.Text(('detail_value', '\n'.join([str(p) for p in entry.entry.persons[k]]))))])
                for k in entry.entry.persons.keys()
                ])

            self.info = urwid.Pile([
                urwid.Columns([('pack', urwid.Text(('detail_key', f"{k.lower()}: "))),
                               ('weight', 1, urwid.Text(('detail_value', f"{entry.entry.fields[k]}")))])
                for k in entry.entry.fields.keys() if entry.entry.fields[k]
                ])

            self.contents = [(self.key, ('pack', None)),
                             (self.source, ('pack', None)),
                             (self.item_type, ('pack', None)),
                             (self.persons, ('pack', None)),
                             (self.info, ('pack', None)),
                             (urwid.SolidFill(), ('weight', 1))]

    def __init__(self, key, entry, repo, source):
        super().__init__(source, repo)
        self.key = key
        self.entry = entry
        self.repo = repo
        self.details_widget = None

    def Authors(self):
        try: return [str(au) for au in self.entry.persons['author']]
        except: return ["Unknown"]

    def Title(self):
        try: return self.entry.fields['title']
        except: return "Unknown"

    def Year(self):
        try: return self.entry.fields['year']
        except: return "Unknown"

    def Venue(self):
        try:
            if 'booktitle' in self.entry.fields:
                return self.entry.fields['booktitle']
            elif 'journal' in self.entry.fields:
                return self.entry.fields['journal']
            elif 'publisher' in self.entry.fields:
                return f"Publisher: {self.entry.fields['publisher']}"
        except: return "Unknown"

    def BibKey(self):
        return self.key

    def ToPybEntry(self):
        return self.entry

    def InitializeDetailsWidget(self):
        if self.details_widget is None:
            self.details_widget = BibtexEntry.DetailsWidgetImpl(self)

    def DetailsWidget(self):
        self.InitializeDetailsWidget()
        return self.details_widget

class BibRepo:

    REDRAW_LOCK = threading.Lock()

    class StatusIndicatorWidgetImpl(urwid.AttrMap):
        def __init__(self, repo):
            super().__init__(urwid.SolidFill(), None)
            self.repo = repo

            self.label = urwid.AttrMap(urwid.Text(f"{repo.source}"), "db_label")
            self.status = urwid.AttrMap(urwid.Text(""), "db_label")

            self.original_widget = urwid.Columns([('pack', self.repo._short_label),
                                                  ('pack', self.repo.enabled_mark),
                                                  ('weight', 1, self.label),
                                                  ('pack', self.status),
                                                  ('pack', self.repo.extra_info)],
                                                 dividechars=1)
        def UpdateStatus(self, status):
            with BibRepo.REDRAW_LOCK:
                if self.repo.status == 'initialized':
                    self.status.original_widget.set_text("initialized")
                elif self.repo.status == 'loading':
                    self.status.set_attr_map({None: "db_status_loading"})
                    self.status.original_widget.set_text("loading")
                elif self.repo.status == 'searching':
                    self.status.set_attr_map({None: "db_status_searching"})
                    self.status.original_widget.set_text("searching")
                elif self.repo.status == 'ready':
                    self.status.set_attr_map({None: "db_status_ready"})
                    self.status.original_widget.set_text("ready")
                    logging.debug(self.status)
                else:
                    raise LookupError(f"Invalid status: {status}")

    class FdWriteHandler:
        def __init__(self, loop):
            self.loop = loop

        def __call__(self, data):
            self.loop.draw_screen()

    def __init__(self, source, event_loop):
        self.source = source

        self.event_loop = event_loop
        self.redraw_fd = event_loop.watch_pipe(BibRepo.FdWriteHandler(event_loop))

        self.serial = 0
        self.serial_lock = threading.Lock()

        self.search_result_sinks = []

        self.loading_done = threading.Event()
        self.searching_done = threading.Event()

        self.loading_thread = threading.Thread(name=f"load-{self.source}",
                                               target=self.LoadingThreadWrapper,
                                               daemon=True)

        self.searching_thread = threading.Thread(name=f"search-{self.source}",
                                                 target=self.SearchingThreadWrapper,
                                                 daemon=True)
        self._short_label = urwid.Text("?")

        self.selected_entries_panel = None
        self.status_indicator_widget = None

        self.enabled = True
        self.enabled_mark = urwid.Text("")
        self.SetEnabled(True)

        self.extra_info = urwid.Text(('db_ro', "ro"))

        self.SetStatus("initialized")
        self.loading_thread.start()
        self.searching_thread.start()

    @property
    def short_label(self):
        return self._short_label.get_text()

    @short_label.setter
    def short_label(self, value):
        self._short_label.set_text(value)

    def Enabled(self):
        return self.enabled

    def SetEnabled(self, enabled):
        self.enabled = enabled
        if enabled:
            self.enabled_mark.set_text(["[", ('db_enabled', "X"), "]"])
        else:
            self.enabled_mark.set_text("[ ]")

    def ToggleEnabled(self):
        self.SetEnabled(not self.Enabled())

    def Search(self, search_text, serial):
        self.search_text = search_text
        with self.serial_lock:
            self.serial = serial
        self.searching_done.set()

    def AttachSink(self, sink):
        self.search_result_sinks.append(sink)

    def SetStatus(self, status):
        self.status = status
        self.InitializeStatusIndicatorWidget()
        self.status_indicator_widget.UpdateStatus(status)

    def InitializeStatusIndicatorWidget(self):
        if self.status_indicator_widget is None:
            self.status_indicator_widget = BibRepo.StatusIndicatorWidgetImpl(self)

    def StatusIndicatorWidget(self):
        self.InitializeStatusIndicatorWidget()
        return self.status_indicator_widget

    def LoadingThreadWrapper(self):

        self.SetStatus("loading")
        self.Redraw()

        self.LoadingThreadMain()

        self.SetStatus("ready")
        self.Redraw()

        self.loading_done.set()

    def LoadingThreadMain(self):
        return NotImplemented

    def SearchingThreadWrapper(self):

        self.loading_done.wait()
        logging.debug("Acknowledge loading completion.")
        while True:
            self.searching_done.wait()
            with self.serial_lock:
                serial = self.serial

            logging.debug(f"Start searching: {self.search_text}")
            self.SetStatus("searching")
            self.Redraw()

            try:
                for item in self.SearchingThreadMain(self.search_text):

                    if item.BibKey() in self.selected_entries_panel.entries.keys():
                        item.Mark('selected')
                    else:
                        item.Mark(None)

                    for sink in self.search_result_sinks:
                        sink.Add(item, serial)
            except Exception as e:
                logging.error(traceback.format_exc())

            self.SetStatus("ready")
            self.Redraw()
            logging.debug(f"Done searching: {self.search_text}")

            with self.serial_lock:
                if self.serial == serial:
                    self.searching_done.clear()

    def Redraw(self):
        with BibRepo.REDRAW_LOCK:
            try:
                os.write(self.redraw_fd, b"?")
                logging.info(f"Wrote '?' to fd {self.redraw_fd}")
            except:
                logging.error(traceback.format_exc())

    def AttachPickedEntries(self, selected_entries_panel):
        self.selected_entries_panel = selected_entries_panel

    def __del__(self):
        os.close(self.redraw_fd)

class BibtexRepo(BibRepo):
    def __init__(self, glob_expr, event_loop):
        super().__init__(glob_expr, event_loop)
        self.bib_files = []

    def LoadingThreadMain(self):
        glob_expr = self.source
        logging.debug(f"Collecting entries from glob expression '{glob_expr}'")

        self.bib_entries = []
        self.bib_files = glob.glob(glob_expr, recursive=True)

        if not self.bib_entries:
            logging.warning(f"Glob expr '{glob_expr}' matches no target")

        for path in self.bib_files:

            try:
                bib_data = pybtex.database.parse_file(path)
            except Exception as e:
                logging.error(f"Exception raised when parsing file {path}: {e}")
                continue

            for key, entry in bib_data.entries.iteritems():
                self.bib_entries.append(BibtexEntry(key, entry, self, path))

            logging.debug(f"Parsed {len(bib_data.entries)} entries from file {path}")

    def SearchingThreadMain(self, search_text):
        stripped = search_text.strip()
        if not stripped:
            return

        keywords = search_text.split()
        for entry in self.bib_entries:
            if entry.Match(keywords):
                yield entry

class OutputBibtexRepo(BibtexRepo):
    def __init__(self, glob_expr, event_loop):
        super().__init__(glob_expr, event_loop)
        self.selected_keys_panel = None

        if len(self.bib_files) > 1:
            raise ValueError(f"Glob expr '{glob_expr}' matches more than one file")

        self.extra_info.set_text(('db_rw', "rw"))
        self.output_file = self.bib_files[0] if self.bib_files else glob_expr

    def AttachSelectedKeys(self, selected_keys_panel):
        self.selected_keys_panel = selected_keys_panel

    def Write(self):
        if self.selected_keys_panel is None:
            return

        self.loading_done.wait()

        entries = {e.BibKey(): e.ToPybEntry() for e in self.bib_entries}
        entries.update({e.BibKey(): e.ToPybEntry() for e in selected_keys_panel.entries.values()})

        for key, entry in entries.items():
            if entry is None:
                logging.error(f"Key {key} has empty entry. Not writing to file.")
                return

        pybtex.database.BibliographyData(entries).to_file(self.output_file)
        logging.info(f"Wrote to file '{self.output_file}'")

class DblpRepo(BibRepo):
    def __init__(self, event_loop):
        super().__init__("http://dblp.org", event_loop)

    def LoadingThreadMain(self):
        pass

    def SearchingThreadMain(self, search_text):
        stripped = search_text.strip()
        if not stripped:
            return

        url = f"http://dblp.org/search/publ/api?q={urllib.parse.quote(search_text)}&format=json"
        logging.debug(f"search_text: '{search_text}'")
        logging.debug(f"url: '{url}'")
        with urllib.request.urlopen(url) as response:
            bib_data = json.load(response)

            if 'hit' not in bib_data['result']['hits']:
                return []

            for entry in bib_data['result']['hits']['hit']:
                yield DblpEntry(entry, self)

class Banner(urwid.AttrMap):
    def __init__(self):
        super().__init__(urwid.SolidFill(), None)
        self.big_text = urwid.BigText([('banner_hi', "bib"),
                                       ('banner_lo', "rarian")],
                                      urwid.font.HalfBlock7x7Font())

        self.big_text_clipped = urwid.Padding(self.big_text, 'center', width='clip')

        self.subtitle = urwid.Text(('banner_hi', "A BibTeX Management Tool Powered By D.B.L.P"), align='center')
        self.version = urwid.Text(('banner_lo', "version 1.0"), align='center')

        self.original_widget = urwid.Filler(
                urwid.Pile([self.big_text_clipped, self.subtitle, self.version]),
                'middle')

class SearchResultsPanel(urwid.AttrMap):
    def __init__(self):
        super().__init__(urwid.SolidFill(), None)
        self.serial = 0
        self.serial_lock = threading.Lock()

        self.banner = Banner()

        self.Clear()

    def Clear(self):
        self.items = []
        self.SyncDisplay()

    def SetSerial(self, serial):
        with self.serial_lock:
            self.serial = serial
            self.Clear()

    def Add(self, entry, serial):
        with self.serial_lock:
            if self.serial == serial:
                self.items.append(entry.SearchPanelWidget())
                self.SyncDisplay()

    def SyncDisplay(self):

        enabled_items = [item for item in self.items if item.entry.repo.Enabled()]
        if enabled_items:
            self.list_walker = urwid.SimpleListWalker(enabled_items)
            self.original_widget = urwid.ListBox(self.list_walker)

        else:
            self.original_widget = self.banner

    def keypress(self, size, key):
        if key == 'ctrl n':
            self.original_widget._keypress_down(size)
        elif key == 'ctrl p':
            self.original_widget._keypress_up(size)
        else:
            self.original_widget.keypress(size, key)

class SelectedKeysPanel(urwid.Pile):
    def __init__(self, *args, **kwargs):
        super().__init__([], **kwargs)
        self.entries = {}
        self.SyncDisplay()

    def Toggle(self, entry):
        key = entry.UniqueKey()
        if key in self.entries:
            del self.entries[key]
            entry.Mark(None)
        else:
            self.entries[key] = entry
            entry.Mark('selected')

        self.SyncDisplay()

    def Add(self, entry):
        self.entries[entry.UniqueKey()] = entry
        self.SyncDisplay()

    def SyncDisplay(self):
        new_contents = [(ent.UniqueKeyItem(), ('pack', None)) for ent in self.entries.values()]
        if not new_contents:
            new_contents = [(urwid.Text(('selected_hint', "Hit <SPACE> on highlighted item to select.")), ('pack', None))]

        self.contents = new_contents

logging.basicConfig(filename=f"/tmp/{getpass.getuser()}_babrarian.log",
                    format="[%(asctime)s %(levelname)7s] %(threadName)s: %(message)s",
                    datefmt="%m-%d-%Y %H:%M:%S",
                    level=logging.DEBUG)

palette = [('search_label', 'yellow', 'dark magenta'),
           ('search_content', 'white', 'dark magenta'),
           ('search_hint', 'light cyan', 'dark magenta'),

           ('message_bar', 'white', 'dark gray'),

           ('details_hint', 'dark green', 'default'),

           ('db_label', 'default', 'default'),
           ('db_enabled', 'light cyan', 'default'),
           ('db_status_ready', 'light green', 'default'),
           ('db_status_loading', 'light red', 'default'),
           ('db_status_searching', 'yellow', 'default'),
           ('db_rw', 'light red', 'default'),
           ('db_ro', 'light green', 'default'),

           ('mark_none', 'default', 'dark gray'),
           ('mark_selected', 'light cyan', 'dark gray'),
           ('title', 'yellow', 'dark gray'),
           ('title_delim', 'default', 'dark gray'),
           ('source', 'dark green', 'default'),
           ('author', 'white', 'default'),
           ('venue', 'underline', 'default'),
           ('year', 'light gray', 'default'),
           ('delim', 'default', 'default'),
           ('bibkey', 'light green', 'default'),
           ('bibtex_ready', 'dark green', 'default'),
           ('bibtex_fetching', 'yellow', 'default'),

           ('None+', 'default', 'dark magenta'),
           ('mark_none+', 'default', 'light magenta'),
           ('mark_selected+', 'light cyan', 'light magenta'),
           ('title+', 'yellow', 'light magenta'),
           ('title_delim+', 'default', 'light magenta'),
           ('source+', 'light green', 'dark magenta'),
           ('author+', 'white', 'dark magenta'),
           ('venue+', 'white,underline', 'dark magenta'),
           ('year+', 'white', 'dark magenta'),
           ('delim+', 'default', 'dark magenta'),
           ('bibkey+', 'light green', 'dark magenta'),
           ('bibtex_ready+', 'dark green', 'dark magenta'),
           ('bibtex_fetching+', 'yellow', 'dark magenta'),

           ('selected_key', 'light cyan', 'default'),
           ('selected_hint', 'dark cyan', 'default'),

           ('detail_key', 'light green', 'default'),
           ('detail_value', 'default', 'default'),

           ('banner_hi', 'light magenta', 'default'),
           ('banner_lo', 'dark magenta', 'default'),
           ]

class SearchBar(urwid.AttrMap):
    def __init__(self):
        super().__init__(urwid.SolidFill(), 'search_content')

        self.search = urwid.Edit(('search_label', "Search: "))

        self.original_widget = self.search

        self.search_panel = None
        self.search_serial = 0
        self.bib_repos = []

    def TextChangeHandler(self, edit, text):
        search_results_panel.SetSerial(self.search_serial)
        for repo in self.bib_repos:
            repo.Search(text, self.search_serial)

        self.search_serial += 1

    def SetActiveRepos(self, repos):
        self.bib_repos = repos

    def AttachSearchPanel(self, search_panel):
        self.search_panel = search_panel
        urwid.connect_signal(self.search, 'change', self.TextChangeHandler)

class InputFilter:
    def __call__(self, keys, raw):
        if not keys: return keys

        if keys[0] == 'ctrl w':
            try: output_repo.Write()
            except:
                logging.error(traceback.format_exc())
            raise urwid.ExitMainLoop()

        if self.MaskDatabases(keys[0]):
            search_results_panel.SyncDisplay()
            return

        return keys

    def MaskDatabases(self, key):
        symbol_number_map = {s: n for s, n in zip(")!@#$%^&*(", range(10))}
        if 'meta ' in key:
            symbol = key[5:]
            if symbol == '~':
                for repo in bib_repos:
                    repo.SetEnabled(True)
            else:
                number = symbol_number_map.get(symbol)
                if number == 0:
                    for repo in bib_repos:
                        repo.SetEnabled(False)

                else:
                    try:
                        repo = bib_repos[number - 1]
                        repo.ToggleEnabled()

                    except: pass
            return True
        elif key == 'enter':
            top_widget.focus_position = 1 - top_widget.focus_position
        else:
            return False

main_loop = urwid.MainLoop(urwid.SolidFill(),
                           palette=palette,
                           input_filter=InputFilter())

with open("config.json") as config_file:
    config = json.load(config_file)

output_repo = OutputBibtexRepo(config['bib_output'], main_loop)

bib_repos = [DblpRepo(main_loop)] \
          + [BibtexRepo(bib, main_loop) for bib in config['bib_files']] \
          + [output_repo]

for repo, i in zip(bib_repos, itertools.count(1)):
    repo.short_label = f"{i}"

message_bar = urwid.Text(('message_bar', "Message"))

search_results_panel = SearchResultsPanel()

search_bar = SearchBar()
search_bar.SetActiveRepos(bib_repos)
search_bar.AttachSearchPanel(search_results_panel)

db_status_panel = urwid.Pile([repo.StatusIndicatorWidget() for repo in bib_repos])

details_panel = urwid.AttrMap(urwid.Filler(urwid.Text(
    ('details_hint', 'Hit <i> on highlighted item to update info.')), 'top'), None)

selected_keys_panel = SelectedKeysPanel()

output_repo.AttachSelectedKeys(selected_keys_panel)

right_panel = urwid.Pile([('pack', urwid.LineBox(db_status_panel, title="Database Info")),
                          ('weight', 5, urwid.LineBox(details_panel, title="Detailed Info")),
                          ('pack', urwid.LineBox(selected_keys_panel, title="Selected Entries"))])

main_widget = urwid.Columns([('weight', 2, urwid.LineBox(search_results_panel, title="Search Results")),
                             ('weight', 1, right_panel)])

top_widget = urwid.Pile([('pack', search_bar),
                         ('weight', 1, main_widget),
                         ('pack', urwid.AttrMap(message_bar, 'message_bar'))])

for repo in bib_repos:
    repo.AttachSink(search_results_panel)
    repo.AttachPickedEntries(selected_keys_panel)

main_loop.widget = top_widget
main_loop.run()

