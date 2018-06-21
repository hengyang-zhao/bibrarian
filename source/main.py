import argparse
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
import subprocess

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

            self.set_focus_map({k: ('plain' if k is None else str(k)) + '+' for k in [
                'title', 'author', 'delim', 'venue', 'year', 'source',
                'bibkey', 'mark_none', 'mark_selected', 'title_delim',
                'bibtex_ready', 'bibtex_fetching', None]})

        def selectable(self):
            return True

        def keypress(self, size, key):
            if key == ' ':
                self.entry.repo.selected_keys_panel.Toggle(self.entry)
                self.entry.OnSelectionHandler()
            elif key == 'i':
                self.entry.repo.details_panel.original_widget = self.entry.details_widget
            elif key == '@':
                self.entry.OpenInBrowser()
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
    def url(self): return NotImplemented

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

    def OpenInBrowser(self):
        if self.url is None:
            self.repo.message_bar.Post("Could not infer url of this entry.",
                                       "warning", 1)
            return

        status = subprocess.run(["python3", "-m", "webbrowser", "-t", self.url],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        if status.returncode == 0:
            self.repo.message_bar.Post(f"Opened url '{self.url}'.", 'normal', 1)
        else:
            self.repo.message_bar.Post(
                    f"Error occured when opening url '{self.url}' (code {status.returncode})",
                    'error', 1)

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
        self.data = dblp_entry

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
    def url(self):
        try: return self.data['info']['ee']
        except: return None

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
    def url(self):
        try: return self.entry.fields['url']
        except: return None

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

    @staticmethod
    def Create(config, access, event_loop):
        enabled = config.get('enabled', True)
        if 'remote' in config:
            return DblpRepo(event_loop, enabled)

        elif 'glob' in config:
            ctor = {'ro': BibtexRepo, 'rw': OutputBibtexRepo}[access]
            return ctor(config['glob'], event_loop, enabled)
        else:
            raise ValueError(f"Invalid config: {config}")

    class StatusIndicatorWidgetImpl(urwid.AttrMap):
        def __init__(self, repo):
            super().__init__(urwid.SolidFill(), None)
            self.repo = repo

            self._status = None

            self.label = urwid.AttrMap(urwid.Text(f"{repo.source}"), "db_label")
            self.access = urwid.Text("")
            self.status_indicator = urwid.AttrMap(urwid.Text(""), "db_label")
            self.original_widget = urwid.Columns([('pack', self.repo._short_label),
                                                  ('pack', self.repo._enabled_mark),
                                                  ('weight', 1, self.label),
                                                  ('pack', self.status_indicator),
                                                  ('pack', self.access)],
                                                 dividechars=1)
        @property
        def status(self):
            return self._status

        @status.setter
        def status(self, value):
            with self.repo.redraw_lock:
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

    def __init__(self, source, event_loop, enabled):
        self.source = source

        self.redraw_lock = threading.Lock()

        self.event_loop = event_loop
        self._redraw_fd = event_loop.watch_pipe(self._FdWriteHandler)

        self.serial = 0
        self._serial_lock = threading.Lock()

        self.search_results_panel = None
        self.message_bar = None
        self.selected_entries_panel = None
        self.details_panel = None

        self.loading_done = threading.Event()
        self.searching_done = threading.Event()

        self.loading_thread = threading.Thread(name=f"load-{self.source}",
                                               target=self.LoadingThreadWrapper,
                                               daemon=True)

        self.searching_thread = threading.Thread(name=f"search-{self.source}",
                                                 target=self.SearchingThreadWrapper,
                                                 daemon=True)
        self._short_label = urwid.Text("?")
        self._enabled_mark = urwid.Text("")
        self.enabled = enabled

        self._status_indicator_widget = BibRepo.StatusIndicatorWidgetImpl(self)

        self.access_type = 'ro'
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
    def access_type(self):
        return self._access_type

    @access_type.setter
    def access_type(self, value):
        if value == 'ro':
            self._access_type = 'ro'
            self._status_indicator_widget.access.set_text(('db_ro', "ro"))
        elif value == 'rw':
            self._access_type = 'rw'
            self._status_indicator_widget.access.set_text(('db_rw', "rw"))
        else:
            raise ValueError(f"Invalid access info: {value}")

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
        return self._status_indicator_widget.status

    @status.setter
    def status(self, value):
        self._status_indicator_widget.status = value

    @property
    def status_indicator_widget(self):
        return self._status_indicator_widget

    def Search(self, search_text, serial):
        self.search_text = search_text
        with self._serial_lock:
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
            with self._serial_lock:
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

            with self._serial_lock:
                if self.serial == serial:
                    self.searching_done.clear()

    def Redraw(self):
        with self.redraw_lock:
            try:
                os.write(self._redraw_fd, b"?")
            except:
                logging.error(traceback.format_exc())

    def _FdWriteHandler(self, data):
        self.event_loop.draw_screen()

class BibtexRepo(BibRepo):
    def __init__(self, glob_expr, event_loop, enabled):
        super().__init__(os.path.expandvars(os.path.expanduser(glob_expr)),
                         event_loop, enabled)
        self._bib_files = []
        self._bib_entries = []

    @property
    def bib_entries(self):
        self.loading_done.wait()
        return self._bib_entries

    @property
    def bib_files(self):
        self.loading_done.wait()
        return self._bib_files

    def LoadingThreadMain(self):
        glob_expr = self.source
        logging.debug(f"Collecting entries from glob expression '{glob_expr}'")

        self._bib_files = glob.glob(glob_expr, recursive=True)

        if not self._bib_files:
            logging.warning(f"Glob expr '{glob_expr}' matches no target")
            if self.message_bar is not None:
                self.message_bar.Post(f"Glob expr '{glob_expr}' matches no target.",
                                      'warning')
            return 'no file'

        for path in self._bib_files:

            try:
                bib_data = pybtex.database.parse_file(path)
            except Exception as e:
                logging.error(f"Exception raised when parsing file {path}: {e}")
                continue

            for key, entry in bib_data.entries.iteritems():
                self._bib_entries.append(BibtexEntry(key, entry, self, path))

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
    def __init__(self, glob_expr, event_loop, enabled):
        super().__init__(glob_expr, event_loop, enabled)
        self.selected_keys_panel = None

        if len(self.bib_files) > 1:
            raise ValueError(f"Glob expr '{glob_expr}' matches more than one file")

        self.access_type = 'rw'
        self.output_file = self.bib_files[0] if self.bib_files else glob_expr

    def Write(self):
        if self.selected_keys_panel is None:
            return

        self.loading_done.wait()

        entries = {e.bibkey: e.pyb_entry for e in self.bib_entries}
        entries.update({e.bibkey: e.pyb_entry for e in self.selected_keys_panel.entries.values()})

        for key, entry in entries.items():
            if entry is None:
                logging.error(f"Key {key} has empty entry. Not writing to file.")
                return

        pybtex.database.BibliographyData(entries).to_file(self.output_file)
        logging.info(f"Wrote to file '{self.output_file}'")

class DblpRepo(BibRepo):
    def __init__(self, event_loop, enabled):
        super().__init__("https://dblp.org", event_loop, enabled)

    def LoadingThreadMain(self):
        return 'ready'

    def SearchingThreadMain(self, search_text):
        stripped = search_text.strip()
        if not stripped:
            return

        url = f"https://dblp.org/search/publ/api?q={urllib.parse.quote(search_text)}&format=json"
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
        self._serial_lock = threading.Lock()

        self.banner = Banner()

        self._Clear()

    @property
    def serial(self):
        return self._serial

    @serial.setter
    def serial(self, value):
        with self._serial_lock:
            self._serial = value
            self._Clear()

    def _Clear(self):
        self.items = []
        self.SyncDisplay()

    def Add(self, entry, serial):
        with self._serial_lock:
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
        if key in ('ctrl n', 'j'):
            self.original_widget._keypress_down(size)
        elif key in ('ctrl p', 'k'):
            self.original_widget._keypress_up(size)
        else:
            self.original_widget.keypress(size, key)

class SelectedKeysPanel(urwid.Pile):
    def __init__(self, keys_output):
        super().__init__([])
        self.entries = {}
        self.keys_output = keys_output
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

    def Write(self):
        if self.keys_output is None: return

        with open(self.keys_output, 'w') as f:
            print(','.join(map(lambda e: e.bibkey, self.entries.values())),
                  file=f, end='')

            logging.info(f"Wrote selected keys to file '{self.keys_output}'")

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
    def __init__(self, loop):
        super().__init__(urwid.Text("Welcome to bibrarian."), 'msg_normal')

        self.event_loop = loop
        self._redraw_fd = loop.watch_pipe(self._FdWriteHandler)

        self.initial_delay = 1
        self.post_delay = 3
        self.tips_delay = 5
        self.next_message_ready = threading.Event()

        self.next_message_scheduled = 0

        self.messages = [
                "Use ctrl+c to exit the program with all files untouched.",
                "Use ctrl+w to write the selected entries to the target file.",
                "Press @ (shift+2) open the entry using system browser.",
                "Use up (or ctrl+p or k) and down (or ctrl+n or j) to navigate the search results.",
                "Use alt+shift+n to toggle enabled/disabled the n-th bib repo.",
                "This software is powered by Python 3, dblp API, Pybtex, and urwid.",
        ]

        self.msg_lock = threading.Lock()

        self.periodic_trigger_thread = threading.Thread(
                name=f"msg-trigger", target=self._PeriodicTrigger, daemon=True)

        self.message_update_thread = threading.Thread(
                name=f"msg-update", target=self._UpdateMessage, daemon=True)

        self.periodic_trigger_thread.start()
        self.message_update_thread.start()

    def Post(self, message, severity='normal', delay=None):
        if severity == 'normal':
            label = "Message"
            style = 'msg_normal'
        elif severity == 'warning':
            label = "Warning"
            style = 'msg_warning'
        elif severity == 'error':
            label = "Error"
            style = 'msg_error'
        else:
            raise ValueError(f"Invalid severity: {severity}")

        with self.msg_lock:
            self.original_widget = urwid.Text((style, f"{label}: {message}"))
            self.next_message_ready.set()

            if delay is None: delay = self.post_delay
            self.next_message_scheduled = time.time() + delay

    def _FdWriteHandler(self, data):
        self.event_loop.draw_screen()

    def _PeriodicTrigger(self):
        time.sleep(self.initial_delay)
        while True:
            for message in self.messages:
                while True:
                    if time.time() >= self.next_message_scheduled:
                        with self.msg_lock:
                            self.original_widget = urwid.Text(('msg_tips', f"Tip: {message}"))
                            self.next_message_ready.set()
                            self.next_message_scheduled = time.time() + self.tips_delay
                        time.sleep(self.tips_delay)
                        break
                    else:
                        time.sleep(1)
                        continue

    def _UpdateMessage(self):
        while True:
            self.next_message_ready.wait()
            self.next_message_ready.clear()
            os.write(self._redraw_fd, b"?")

    def __del__(self):
        os.close(self._redraw_fd)

class DetailsPanel(urwid.AttrMap):
    def __init__(self):
        super().__init__(urwid.Filler(urwid.Text(
            ('details_hint', 'Hit <i> on highlighted item to update info.')), 'top'), None)

class InputFilter:
    def __init__(self):
        self.widget = None

    def __call__(self, keys, raw):
        if not keys: return keys

        if keys[0] == 'ctrl w':
            try:
                for repo in self.widget.output_repos:
                    repo.Write()
            except:
                logging.error(traceback.format_exc())

            try: self.widget.selected_keys_panel.Write()
            except: logging.error(traceback.format_exc())

            raise urwid.ExitMainLoop()

        elif self.MaskDatabases(keys[0]):
            self.widget.search_results_panel.SyncDisplay()
            return

        return keys

    def MaskDatabases(self, key):
        symbol_number_map = {s: n for s, n in zip(")!@#$%^&*(", range(10))}
        if 'meta ' in key:
            symbol = key[5:]
            if symbol == '~':
                for repo in self.widget.bib_repos:
                    repo.enabled = True
            else:
                number = symbol_number_map.get(symbol)
                if number == 0:
                    for repo in self.widget.bib_repos:
                        repo.enabled = False
                else:
                    try:
                        repo = self.widget.bib_repos[number - 1]
                        repo.enabled = not repo.enabled

                    except: pass
            return True
        elif key == 'enter':
            self.widget.focus_position = 1 - self.widget.focus_position
        else:
            return False

class DatabaseStatusPanel(urwid.Pile):
    def __init__(self, databases, config_source):
        super().__init__([])
        self.contents = [(db, ('pack', None)) for db in databases] \
                      + [(urwid.Text(('cfg_src', f"config: {config_source}")), ('pack', None))]

class TopWidget(urwid.Pile):
    def __init__(self, args, config, event_loop):
        super().__init__([urwid.SolidFill()])

        self.message_bar = MessageBar(event_loop)
        self.search_results_panel = SearchResultsPanel()
        self.details_panel = DetailsPanel()
        self.selected_keys_panel = SelectedKeysPanel(args.keys_output)

        self.output_repos = [BibRepo.Create(cfg, 'rw', event_loop) for cfg in config['rw_repos']]

        self.bib_repos = [BibRepo.Create(cfg, 'ro', event_loop) for cfg in config['ro_repos']] + self.output_repos

        for repo, i in zip(self.bib_repos, itertools.count(1)):
            repo.short_label = f"{i}"
            repo.message_bar = self.message_bar
            repo.search_results_panel = self.search_results_panel
            repo.selected_keys_panel = self.selected_keys_panel
            repo.details_panel = self.details_panel

        self.search_bar = SearchBar()
        self.search_bar.bib_repos = self.bib_repos
        self.search_bar.search_results_panel = self.search_results_panel

        self.db_status_panel = DatabaseStatusPanel(
            [repo.status_indicator_widget for repo in self.bib_repos],
            config.source)

        for repo in self.output_repos:
            repo.selected_keys_panel = self.selected_keys_panel

        self.right_panel = urwid.Pile([
            ('pack', urwid.LineBox(self.db_status_panel, title="Database Info")),
            ('weight', 5, urwid.LineBox(self.details_panel, title="Detailed Info")),
            ('pack', urwid.LineBox(self.selected_keys_panel, title="Selected Entries"))])

        self.main_widget = urwid.Columns([
            ('weight', 2, urwid.LineBox(self.search_results_panel, title="Search Results")),
            ('weight', 1, self.right_panel)])

        self.contents = [(self.search_bar, ('pack', None)),
                         (self.main_widget, ('weight', 1)),
                         (self.message_bar, ('pack', None))]

class DefaultConfig(dict):
    def __init__(self):
        self['ro_repos'] = [
            {
                'remote': "dblp.org",
                'enabled': True
            },
            {
                'glob': "/path/to/lots/of/**/*.bib",
                'enabled': True
            },
            {
                'glob': "/path/to/sample.bib",
                'enabled': False
            },
            {
                'glob': "/path/to/another/sample.bib"
            }
        ]

        self['rw_repos'] = [
            {
                'glob': "reference.bib",
                'enabled': True
            }
        ]

    def Write(self, file):
        with open(file, 'w') as f:
            json.dump(self, f, indent=4)

class Config(dict):
    def __init__(self, file_name):
        prefix = os.getcwd()
        self.source = None

        while True:
            path = os.path.join(prefix, file_name)
            if os.path.isfile(path) and os.access(path, os.R_OK):
                with open(path) as f:
                    self.update(json.load(f))
                    self.source = path
                    break

            if prefix == '/': break
            prefix = os.path.dirname(prefix)

class ArgParser(argparse.ArgumentParser):
    def __init__(self):
        super().__init__(prog="bibrarian")

        self.add_argument("-f", "--config",
                          help="force configuration file path",
                          default=".bibrarian_config.json",
                          action='store'
                          )
        self.add_argument("-g", "--gen-config",
                          help="generate a configuration file",
                          default=False,
                          action='store_true')
        self.add_argument("-l", "--log",
                          help="force log file path",
                          default=f"/tmp/{getpass.getuser()}_babrarian.log",
                          action='store')
        self.add_argument("-k", "--keys-output",
                          help="output bib keys file (truncate mode)",
                          action='store')
        self.add_argument("-v", "--version",
                          action='version',
                          version="%(prog)s 1.0")

class Palette(list):
    def __init__(self):
        self.append(('search_label', 'yellow', 'dark magenta'))
        self.append(('search_content', 'white', 'dark magenta'))
        self.append(('search_hint', 'light cyan', 'dark magenta'))

        self.append(('msg_tips', 'white', 'dark gray'))
        self.append(('msg_normal', 'light green', 'dark gray'))
        self.append(('msg_warning', 'yellow', 'dark gray'))
        self.append(('msg_error', 'light red', 'dark gray'))

        self.append(('details_hint', 'dark green', 'default'))

        self.append(('db_label', 'default', 'default'))
        self.append(('db_enabled', 'light cyan', 'default'))
        self.append(('db_status_ready', 'light green', 'default'))
        self.append(('db_status_loading', 'light cyan', 'default'))
        self.append(('db_status_searching', 'yellow', 'default'))
        self.append(('db_status_error', 'light red', 'default'))
        self.append(('db_rw', 'light magenta', 'default'))
        self.append(('db_ro', 'light green', 'default'))

        self.append(('mark_none', 'default', 'dark gray'))
        self.append(('mark_selected', 'light cyan', 'dark gray'))
        self.append(('title', 'yellow', 'dark gray'))
        self.append(('title_delim', 'default', 'dark gray'))
        self.append(('source', 'dark green', 'default'))
        self.append(('author', 'white', 'default'))
        self.append(('venue', 'underline', 'default'))
        self.append(('year', 'light gray', 'default'))
        self.append(('delim', 'default', 'default'))
        self.append(('bibkey', 'light green', 'default'))
        self.append(('bibtex_ready', 'dark green', 'default'))
        self.append(('bibtex_fetching', 'yellow', 'default'))

        self.append(('plain+', 'default', 'dark magenta'))
        self.append(('mark_none+', 'default', 'light magenta'))
        self.append(('mark_selected+', 'light cyan', 'light magenta'))
        self.append(('title+', 'yellow', 'light magenta'))
        self.append(('title_delim+', 'default', 'light magenta'))
        self.append(('source+', 'light green', 'dark magenta'))
        self.append(('author+', 'white', 'dark magenta'))
        self.append(('venue+', 'white,underline', 'dark magenta'))
        self.append(('year+', 'white', 'dark magenta'))
        self.append(('delim+', 'default', 'dark magenta'))
        self.append(('bibkey+', 'light green', 'dark magenta'))
        self.append(('bibtex_ready+', 'dark green', 'dark magenta'))
        self.append(('bibtex_fetching+', 'yellow', 'dark magenta'))

        self.append(('selected_key', 'light cyan', 'default'))
        self.append(('selected_hint', 'dark cyan', 'default'))

        self.append(('detail_key', 'light green', 'default'))
        self.append(('detail_value', 'default', 'default'))

        self.append(('banner_hi', 'light magenta', 'default'))
        self.append(('banner_lo', 'dark magenta', 'default'))

        self.append(('cfg_src', 'dark gray', 'default'))

if __name__ == '__main__':
    args = ArgParser().parse_args()

    if args.gen_config:
        DefaultConfig().Write(args.config)
        print(f"Wrote default config to file {args.config}")
        sys.exit(0)

    logging.basicConfig(filename=args.log,
                        format="[%(asctime)s %(levelname)7s] %(threadName)s: %(message)s",
                        datefmt="%m-%d-%Y %H:%M:%S",
                        level=logging.DEBUG)

    config = Config(args.config)
    if config.source is None:
        print("Did not find any config file.")
        print("You can generate an example config file using option -g.")
        print("For more information, please use option -h for help.")
        sys.exit(1)

    input_filter = InputFilter()
    main_loop = urwid.MainLoop(urwid.SolidFill(),
                               palette=Palette(),
                               input_filter=input_filter)

    top_widget = TopWidget(args, config, main_loop)

    input_filter.widget = top_widget
    main_loop.widget = top_widget

    try: main_loop.run()
    except KeyboardInterrupt:
        sys.exit(0)
