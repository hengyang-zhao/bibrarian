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

            self.title = urwid.AttrMap(urwid.Text(entry.title), 'title')
            self.info = urwid.Text([('author', f"{entry.abbrev_authors}"),
                                    ('delim', ". "),
                                    ('venue', f"{entry.venue}"),
                                    ('delim', ", "),
                                    ('year', f"{entry.year}"),
                                    ('delim', ".")])
            self.mark = urwid.AttrMap(urwid.Text(('mark_none', "[M]"), align='right'), None)
            self.source = urwid.Text([('source', f"{entry.source}"),
                                      ('delim', "::"),
                                      ('bibkey', f"{entry.bibkey}")])

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
                self.entry.OnSelectionHandler()
            elif key == 'i':
                details_panel.original_widget = self.entry.details_widget
            else:
                return key

    def __init__(self, source, repo):
        self.repo = repo
        self._source = source
        self._search_panel_widget = None
        self._mark = None

    @property
    def authors(self): return NotImplemented

    @property
    def title(self): return NotImplemented

    @property
    def year(self): return NotImplemented

    @property
    def venue(self): return NotImplemented

    @property
    def bibkey(self): return NotImplemented

    @property
    def abbrev_authors(self):
        authors = self.authors
        if len(authors) == 1:
            return f"{authors[0]}"
        else:
            return f"{authors[0]} et al"

    @property
    def pyb_entry(self): return NotImplemented

    @property
    def details_widget(self): return NotImplemented

    @property
    def source(self):
        return self._source

    def Match(self, keywords):
        trivial = True
        for keyword in filter(lambda k: len(k) >= 3, keywords):
            trivial = False

            if keyword.upper() in self.title.upper():
                continue

            matched = False
            for author in self.authors:
                if keyword.upper() in author.upper():
                    matched = True
                    break

            if not matched: return False

        return not trivial

    @property
    def search_panel_widget(self):
        self._InitializeSearchPanelWidget()
        return self._search_panel_widget

    @property
    def mark(self):
        return self._mark

    @mark.setter
    def mark(self, value):
        self._InitializeSearchPanelWidget()
        self._mark = value
        if value is None:
            self._search_panel_widget.mark.original_widget.set_text(
                    [('title_delim', "["), ('mark_none', " "), ('title_delim', "]")])
        elif value == 'selected':
            self._search_panel_widget.mark.original_widget.set_text(
                    [('title_delim', "["), ('mark_selected', "X"), ('title_delim', "]")])
        else:
            raise ValueError(f"Invalid mark: {mark}")

    @property
    def unique_key(self):
        return f"{self.source}::{self.bibkey}"

    @property
    def unique_key_item(self):
        return urwid.Text([('selected_key', self.bibkey), ('selected_hint', f"({self.source})")])

    def OnSelectionHandler(self): pass

    def _InitializeSearchPanelWidget(self):
        if self._search_panel_widget is None:
            self._search_panel_widget = BibEntry.SearchPanelWidgetImpl(self)

class DblpEntry(BibEntry):

    class DetailsWidgetImpl(urwid.Pile):

        def __init__(self, entry):
            super().__init__([])

            self._entry = entry

            self.key_item = urwid.Columns([('pack', urwid.Text(('detail_key', "bibtex key: "))),
                                           ('weight', 1, urwid.Text(('detail_value', entry.bibkey)))])
            self.source_item = urwid.Columns([('pack', urwid.Text(('detail_key', "source: "))),
                                              ('weight', 1, urwid.Text(('detail_value', entry.source)))])
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

            @property
            def entry(self):
                return self._entry

    def __init__(self, dblp_entry, repo):
        super().__init__('dblp.org', repo)
        self._data = dblp_entry

        self._details_widget = None
        self._bibkey = None
        self._redraw_fd = None

        self.pybtex_entry = None
        self.bibtex_loading_done = threading.Event()

        self.bibtex_loading_thread = threading.Thread(
                name=f"bibtex-{self.bibkey}",
                target=self._LoadPybtexEntry,
                daemon=False)

    def __del__(self):
        if self._redraw_fd is not None:
            os.close(self._redraw_fd)
    @property
    def data(self):
        return self._data

    @property
    def pyb_entry(self):
        self.bibtex_loading_done.wait()
        return self.pybtex_entry

    @property
    def authors(self):
        try:
            authors = self.data['info']['authors']['author']
            if authors: return authors
            else: return ["Unknown"]
        except: return ["Unknown"]

    @property
    def title(self):
        try: return str(self.data['info']['title'])
        except: return "Unknown"

    @property
    def year(self):
        try: return str(self.data['info']['year'])
        except: return "Unknown"

    @property
    def venue(self):
        try: return self.data['info']['venue']
        except: return "Unknown"

    @property
    def bibkey(self):
        if self._bibkey is None:
            flat_key = self.data['info']['key']
            base = flat_key.split('/')[-1]
            sha1 = hashlib.sha1(flat_key.encode('utf-8')).hexdigest()
            self._bibkey = f"{base}:{sha1[:4].upper()}"

        return self._bibkey

    @property
    def details_widget(self):
        self._InitializeDetailsWidget()
        return self._details_widget

    def OnSelectionHandler(self):
        if self._redraw_fd is None:
            event_loop = self.repo.event_loop
            self._redraw_fd = event_loop.watch_pipe(self._FdWriteHandler)
            self.bibtex_loading_thread.start()

    def _FdWriteHandler(self, data):
        self.repo.event_loop.draw_screen()

    def _InitializeDetailsWidget(self):
        if self._details_widget is None:
            self._details_widget = DblpEntry.DetailsWidgetImpl(self)

    def _LoadPybtexEntry(self):
        bib_url = f"https://dblp.org/rec/bib2/{self.data['info']['key']}.bib"
        try:
            if self.search_panel_widget is not None:
                self.search_panel_widget.source.set_text([
                    ('source', f"{self.source}"),
                    ('delim', "::"),
                    ('bibkey', f"{self.bibkey}"),
                    ('bibtex_fetching', " (fetching bibtex)")])
                os.write(self._redraw_fd, b"?")

            with urllib.request.urlopen(bib_url) as remote:
                bib_text = remote.read().decode('utf-8')

            pyb_db = pybtex.database.parse_string(bib_text, 'bibtex')
            self.pybtex_entry = pyb_db.entries[f"DBLP:{self.data['info']['key']}"]

            if self.search_panel_widget is not None:
                self.search_panel_widget.source.set_text([
                    ('source', f"{self.source}"),
                    ('delim', "::"),
                    ('bibkey', f"{self.bibkey}"),
                    ('bibtex_ready', " (bibtex ready)")])
                os.write(self._redraw_fd, b"?")

        except Exception as e:
            logging.error(f"Error when fetching bibtex entry from DBLP: Entry: {self.data} {traceback.format_exc()}")

        self.bibtex_loading_done.set()


class BibtexEntry(BibEntry):

    class DetailsWidgetImpl(urwid.Pile):

        def __init__(self, entry):
            super().__init__([])

            self.entry = entry
            self.key = urwid.Columns([
                ('pack', urwid.Text(('detail_key', "citation key: "))),
                ('weight', 1, urwid.Text(('detail_value', entry.bibkey)))])

            self.source = urwid.Columns([
                ('pack', urwid.Text(('detail_key', "source: "))),
                ('weight', 1, urwid.Text(('detail_value', entry.source)))])

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
        self._bibkey = key
        self.entry = entry
        self._details_widget = None

    @property
    def authors(self):
        try: return [str(au) for au in self.entry.persons['author']]
        except: return ["Unknown"]

    @property
    def title(self):
        try: return self.entry.fields['title']
        except: return "Unknown"

    @property
    def year(self):
        try: return self.entry.fields['year']
        except: return "Unknown"

    @property
    def venue(self):
        try:
            if 'booktitle' in self.entry.fields:
                return self.entry.fields['booktitle']
            elif 'journal' in self.entry.fields:
                return self.entry.fields['journal']
            elif 'publisher' in self.entry.fields:
                return f"Publisher: {self.entry.fields['publisher']}"
        except: return "Unknown"

    @property
    def bibkey(self):
        return self._bibkey

    @property
    def pyb_entry(self):
        return self.entry

    @property
    def details_widget(self):
        self._InitializeDetailsWidget()
        return self._details_widget

    def _InitializeDetailsWidget(self):
        if self._details_widget is None:
            self._details_widget = BibtexEntry.DetailsWidgetImpl(self)

class BibRepo:

    REDRAW_LOCK = threading.Lock()

    class StatusIndicatorWidgetImpl(urwid.AttrMap):
        def __init__(self, repo):
            super().__init__(urwid.SolidFill(), None)
            self.repo = repo

            self._status = None

            self.label = urwid.AttrMap(urwid.Text(f"{repo.source}"), "db_label")
            self.status_indicator = urwid.AttrMap(urwid.Text(""), "db_label")
            self.original_widget = urwid.Columns([('pack', self.repo._short_label),
                                                  ('pack', self.repo._enabled_mark),
                                                  ('weight', 1, self.label),
                                                  ('pack', self.status_indicator),
                                                  ('pack', self.repo.extra_info)],
                                                 dividechars=1)
        @property
        def status(self):
            return self._status

        @status.setter
        def status(self, value):
            with BibRepo.REDRAW_LOCK:
                self._status = value
                if value == 'initialized':
                    self.status_indicator.original_widget.set_text("initialized")
                elif value == 'loading':
                    self.status_indicator.set_attr_map({None: "db_status_loading"})
                    self.status_indicator.original_widget.set_text("loading")
                elif value == 'searching':
                    self.status_indicator.set_attr_map({None: "db_status_searching"})
                    self.status_indicator.original_widget.set_text("searching")
                elif value == 'ready':
                    self.status_indicator.set_attr_map({None: "db_status_ready"})
                    self.status_indicator.original_widget.set_text("ready")
                elif value == 'no file':
                    self.status_indicator.set_attr_map({None: "db_status_error"})
                    self.status_indicator.original_widget.set_text("no file")
                else:
                    raise LookupError(f"Invalid status: {status}")

    def __init__(self, source, event_loop):
        self.source = source

        self.event_loop = event_loop
        self._redraw_fd = event_loop.watch_pipe(self._FdWriteHandler)

        self.serial = 0
        self.serial_lock = threading.Lock()

        self.search_results_panel = None

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
        self._status_indicator_widget = None

        self._enabled_mark = urwid.Text("")
        self.enabled = True

        self.extra_info = urwid.Text(('db_ro', "ro"))

        self.status = "initialized"
        self.loading_thread.start()
        self.searching_thread.start()

    def __del__(self):
        os.close(self._redraw_fd)

    @property
    def short_label(self):
        return self._short_label.get_text()

    @short_label.setter
    def short_label(self, value):
        self._short_label.set_text(value)

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        self._enabled = value
        if self._enabled:
            self._enabled_mark.set_text(["[", ('db_enabled', "X"), "]"])
        else:
            self._enabled_mark.set_text("[ ]")

    @property
    def status(self):
        self._InitializeStatusIndicatorWidget()
        return self._status_indicator_widget.status

    @status.setter
    def status(self, value):
        self._InitializeStatusIndicatorWidget()
        self._status_indicator_widget.status = value

    @property
    def status_indicator_widget(self):
        self._InitializeStatusIndicatorWidget()
        return self._status_indicator_widget

    def Search(self, search_text, serial):
        self.search_text = search_text
        with self.serial_lock:
            self.serial = serial
        self.searching_done.set()

    def LoadingThreadWrapper(self):

        self.status = "loading"
        self.Redraw()

        status = self.LoadingThreadMain()

        self.status = status
        self.Redraw()

        self.loading_done.set()

    def LoadingThreadMain(self):
        return NotImplemented

    def SearchingThreadWrapper(self):

        self.loading_done.wait()
        if self.status == 'no file':
            return

        while True:
            self.searching_done.wait()
            with self.serial_lock:
                serial = self.serial

            self.status = "searching"
            self.Redraw()

            try:
                for item in self.SearchingThreadMain(self.search_text):

                    if self.selected_entries_panel is not None and \
                       item.bibkey in self.selected_entries_panel.entries.keys():
                        item.mark = 'selected'
                    else:
                        item.mark = None

                    if self.search_results_panel is not None:
                        self.search_results_panel.Add(item, serial)
            except Exception as e:
                logging.error(traceback.format_exc())

            self.status = "ready"
            self.Redraw()

            with self.serial_lock:
                if self.serial == serial:
                    self.searching_done.clear()

    def Redraw(self):
        with BibRepo.REDRAW_LOCK:
            try:
                os.write(self._redraw_fd, b"?")
            except:
                logging.error(traceback.format_exc())

    def _InitializeStatusIndicatorWidget(self):
        if self._status_indicator_widget is None:
            self._status_indicator_widget = BibRepo.StatusIndicatorWidgetImpl(self)

    def _FdWriteHandler(self, data):
        self.event_loop.draw_screen()

class BibtexRepo(BibRepo):
    def __init__(self, glob_expr, event_loop):
        super().__init__(glob_expr, event_loop)
        self.bib_files = []

    def LoadingThreadMain(self):
        glob_expr = self.source
        logging.debug(f"Collecting entries from glob expression '{glob_expr}'")

        self.bib_files = glob.glob(glob_expr, recursive=True)

        if not self.bib_files:
            logging.warning(f"Glob expr '{glob_expr}' matches no target")
            return 'no file'

        self.bib_entries = []
        for path in self.bib_files:

            try:
                bib_data = pybtex.database.parse_file(path)
            except Exception as e:
                logging.error(f"Exception raised when parsing file {path}: {e}")
                continue

            for key, entry in bib_data.entries.iteritems():
                self.bib_entries.append(BibtexEntry(key, entry, self, path))

            logging.debug(f"Parsed {len(bib_data.entries)} entries from file {path}")

        return 'ready'

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

    def Write(self):
        if self.selected_keys_panel is None:
            return

        self.loading_done.wait()

        entries = {e.bibkey: e.pyb_entry for e in self.bib_entries}
        entries.update({e.bibkey: e.pyb_entry for e in selected_keys_panel.entries.values()})

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
        return 'ready'

    def SearchingThreadMain(self, search_text):
        stripped = search_text.strip()
        if not stripped:
            return

        url = f"http://dblp.org/search/publ/api?q={urllib.parse.quote(search_text)}&format=json"
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
        self._serial = 0
        self.serial_lock = threading.Lock()

        self.banner = Banner()

        self._Clear()

    @property
    def serial(self):
        return self._serial

    @serial.setter
    def serial(self, value):
        with self.serial_lock:
            self._serial = value
            self._Clear()

    def _Clear(self):
        self.items = []
        self.SyncDisplay()

    def Add(self, entry, serial):
        with self.serial_lock:
            if self._serial == serial:
                self.items.append(entry.search_panel_widget)
                self.SyncDisplay()

    def SyncDisplay(self):

        enabled_items = [item for item in self.items if item.entry.repo.enabled]
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
        key = entry.unique_key
        if key in self.entries:
            del self.entries[key]
            entry.mark = None
        else:
            self.entries[key] = entry
            entry.mark = 'selected'

        self.SyncDisplay()

    def Add(self, entry):
        self.entries[entry.unique_key] = entry
        self.SyncDisplay()

    def SyncDisplay(self):
        new_contents = [(ent.unique_key_item, ('pack', None)) for ent in self.entries.values()]
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
           ('db_status_loading', 'light cyan', 'default'),
           ('db_status_searching', 'yellow', 'default'),
           ('db_status_error', 'light red', 'default'),
           ('db_rw', 'light magenta', 'default'),
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

        self._search = urwid.Edit(('search_label', "Search: "))

        self.original_widget = self._search

        self.search_results_panel = None
        self._search_serial = 0
        self.bib_repos = []

        urwid.connect_signal(self._search, 'change', self.TextChangeHandler)

    def TextChangeHandler(self, edit, text):
        if self.search_results_panel is None:
            return

        self.search_results_panel.serial = self._search_serial
        for repo in self.bib_repos:
            repo.Search(text, self._search_serial)

        self._search_serial += 1

class MessageBar(urwid.AttrMap):
    def __init__(self):
        super().__init__(urwid.Text("Message"), 'message_bar')

class DetailsPanel(urwid.AttrMap):
    def __init__(self):
        super().__init__(urwid.Filler(urwid.Text(
            ('details_hint', 'Hit <i> on highlighted item to update info.')), 'top'), None)

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
                    repo.enabled = True
            else:
                number = symbol_number_map.get(symbol)
                if number == 0:
                    for repo in bib_repos:
                        repo.enabled = False

                else:
                    try:
                        repo = bib_repos[number - 1]
                        repo.enabled = not repo.enabled

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

message_bar = MessageBar()
search_results_panel = SearchResultsPanel()
details_panel = DetailsPanel()
selected_keys_panel = SelectedKeysPanel()

search_bar = SearchBar()
search_bar.bib_repos = bib_repos
search_bar.search_results_panel = search_results_panel

db_status_panel = urwid.Pile([repo.status_indicator_widget for repo in bib_repos])
output_repo.selected_keys_panel = selected_keys_panel

right_panel = urwid.Pile([('pack', urwid.LineBox(db_status_panel, title="Database Info")),
                          ('weight', 5, urwid.LineBox(details_panel, title="Detailed Info")),
                          ('pack', urwid.LineBox(selected_keys_panel, title="Selected Entries"))])

main_widget = urwid.Columns([('weight', 2, urwid.LineBox(search_results_panel, title="Search Results")),
                             ('weight', 1, right_panel)])

top_widget = urwid.Pile([('pack', search_bar),
                         ('weight', 1, main_widget),
                         ('pack', message_bar)])

for repo in bib_repos:
    repo.search_results_panel = search_results_panel
    repo.selected_keys_panel = selected_keys_panel

main_loop.widget = top_widget
main_loop.run()

