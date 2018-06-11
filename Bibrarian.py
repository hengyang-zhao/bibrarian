import os
import glob
import json
import sys
import time
import logging
import threading
import traceback

import urllib
import urllib.request
import urllib.parse

import urwid

import pybtex
import pybtex.database

class BibEntry:
    def __init__(self, source):
        self.source = source

    def Authors(self): return NotImplemented
    def Title(self): return NotImplemented
    def Year(self): return NotImplemented
    def Venue(self): return NotImplemented
    def BibKey(self): return NotImplemented
    def MakeDetailPage(self): return NotImplemented

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

    def MakeSelectableEntry(self):
        class SearchCandidate(urwid.Pile):
            def __init__(self, entry, *args, **argv):
                super().__init__(*args, **argv)
                self.entry = entry

            def selectable(self):
                return True

            def keypress(self, size, key):
                if key != ' ': return key
                details_panel.original_widget = self.entry.MakeDetailPage()

        title = urwid.AttrMap(urwid.Text(self.Title()), 'title')
        info = urwid.Text([('author', f"{self.AbbrevAuthors()}"),
                           ('delim', ". "),
                           ('venue', f"{self.Venue()}"),
                           ('delim', ", "),
                           ('year', f"{self.Year()}"),
                           ('delim', ".")])
        source = urwid.Text([('source', f"{self.Source()}"),
                             ('delim', "::"),
                             ('bibkey', f"{self.BibKey()}")])

        pile = SearchCandidate(self, [title, info, source])

        return urwid.AttrMap(pile, None, {palette_key: str(palette_key) + '+'
                             for palette_key in ['title', 'author', 'delim',
                                                 'venue', 'year', 'source',
                                                 'bibkey', None]})

class DblpBibEntry(BibEntry):
    def __init__(self, dblp_entry):
        super().__init__('dblp.org')
        self.data = dblp_entry

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
        return self.data['info']['key'].split('/')[-1]

    def MakeDetailPage(self):
        key_item = urwid.Columns([('pack', urwid.Text(('detail_key', "citation key: "))),
                                  ('weight', 1, urwid.Text(('detail_value', self.BibKey())))])
        source_item = urwid.Columns([('pack', urwid.Text(('detail_key', "source: "))),
                                     ('weight', 1, urwid.Text(('detail_value', self.Source())))])
        person_items = urwid.Pile([
            urwid.Columns([('pack', urwid.Text(('detail_key', f"{k.lower()}: "))),
                           ('weight', 1, urwid.Text(('detail_value', '\n'.join(self.data['info']['authors'][k]))))])
            for k in self.data['info']['authors'].keys()
            ])
        info_items = urwid.Pile([
            urwid.Columns([('pack', urwid.Text(('detail_key', f"{k.lower()}: "))),
                           ('weight', 1, urwid.Text(('detail_value', f"{self.data['info'][k]}")))])
            for k in self.data['info'].keys() if k != 'authors'
            ])

        return urwid.Pile([('pack', key_item),
                           ('pack', source_item),
                           ('pack', person_items),
                           ('pack', info_items),
                           ('weight', 1, urwid.SolidFill())])

class BibtexEntry(BibEntry):
    def __init__(self, key, entry, source):
        super().__init__(source)
        self.key = key
        self.entry = entry

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

    def MakeDetailPage(self):
        key_item = urwid.Columns([('pack', urwid.Text(('detail_key', "citation key: "))),
                                  ('weight', 1, urwid.Text(('detail_value', self.BibKey())))])
        source_item = urwid.Columns([('pack', urwid.Text(('detail_key', "source: "))),
                                     ('weight', 1, urwid.Text(('detail_value', self.Source())))])
        type_item = urwid.Columns([('pack', urwid.Text(('detail_key', "type: "))),
                                   ('weight', 1, urwid.Text(('detail_value', self.entry.type)))])
        person_items = urwid.Pile([
            urwid.Columns([('pack', urwid.Text(('detail_key', f"{k.lower()}: "))),
                           ('weight', 1, urwid.Text(('detail_value', '\n'.join([str(p) for p in self.entry.persons[k]]))))])
            for k in self.entry.persons.keys()
            ])
        misc_items = urwid.Pile([
            urwid.Columns([('pack', urwid.Text(('detail_key', f"{k.lower()}: "))),
                           ('weight', 1, urwid.Text(('detail_value', f"{self.entry.fields[k]}")))])
            for k in self.entry.fields.keys() if self.entry.fields[k]
            ])

        return urwid.Pile([('pack', key_item),
                           ('pack', source_item),
                           ('pack', type_item),
                           ('pack', person_items),
                           ('pack', misc_items),
                           ('weight', 1, urwid.SolidFill())])

class BibRepo:

    REDRAW_LOCK = threading.Lock()

    def __init__(self, source, event_loop):
        self.source = source
        self.event_loop = event_loop

        self.serial = 0
        self.serial_lock = threading.Lock()

        self.search_result_sinks = []

        self.status_indicator_left = urwid.AttrMap(urwid.Text(f"{self.source}"), "db_label")
        self.status_indicator_right = urwid.AttrMap(urwid.Text(""), "db_label")

        self.loading_done = threading.Event()
        self.loading_done.clear()

        self.searching_done = threading.Event()
        self.searching_done.clear()

        self.loading_thread = threading.Thread(name=f"load-{self.source}",
                                               target=self.LoadingThreadWrapper,
                                               daemon=True)

        self.searching_thread = threading.Thread(name=f"search-{self.source}",
                                                 target=self.SearchingThreadWrapper,
                                                 daemon=True)

        self.SetStatus("initialized")
        self.loading_thread.start()
        self.searching_thread.start()

    def Search(self, search_text, serial):
        self.search_text = search_text
        with self.serial_lock:
            self.serial = serial
        self.searching_done.set()

    def ConnectSink(self, sink):
        self.search_result_sinks.append(sink)

    def MakeStatusIndicator(self):

        return urwid.Columns([self.status_indicator_left,
                              ('pack', self.status_indicator_right)],
                             dividechars=1)

    def SetStatus(self, status):
        with BibRepo.REDRAW_LOCK:
            if status == 'initialized':
                self.status_indicator_right.original_widget.set_text("initialized")
            elif status == 'loading':
                self.status_indicator_right.set_attr_map({None: "db_status_loading"})
                self.status_indicator_right.original_widget.set_text("loading")
            elif status == 'searching':
                self.status_indicator_right.set_attr_map({None: "db_status_searching"})
                self.status_indicator_right.original_widget.set_text("searching")
            elif status == 'ready':
                self.status_indicator_right.set_attr_map({None: "db_status_ready"})
                self.status_indicator_right.original_widget.set_text("ready")
                logging.debug(self.status_indicator_right)
            else:
                raise LookupError(f"Invalid status: {status}")

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
            try: self.event_loop.draw_screen()
            except: pass

class LocalBibRepo(BibRepo):
    def __init__(self, glob_expr, event_loop):
        super().__init__(glob_expr, event_loop)

    def LoadingThreadMain(self):
        has_match = False
        glob_expr = self.source
        logging.debug(f"Collecting entries from glob expression '{glob_expr}'")

        self.bib_entries = []
        for path in glob.glob(glob_expr, recursive=True):
            has_match = True

            try:
                bib_data = pybtex.database.parse_file(path)
            except Exception as e:
                logging.error(f"Exception raised when parsing file {path}: {e}")
                continue

            for key, entry in bib_data.entries.iteritems():
                self.bib_entries.append(BibtexEntry(key, entry, path))

            logging.debug(f"Parsed {len(bib_data.entries)} entries from file {path}")

        if not has_match:
            logging.warning(f"Glob expr '{glob_expr}' matches no target")

    def SearchingThreadMain(self, search_text):
        stripped = search_text.strip()
        if not stripped:
            return

        keywords = search_text.split()
        for entry in self.bib_entries:
            if entry.Match(keywords):
                yield entry

class DblpBibRepo(BibRepo):
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

            for raw_entry in bib_data['result']['hits']['hit']:
                yield DblpBibEntry(raw_entry)

class SearchResultSink:
    class ListWalkerModifiedHandler:
        def __init__(self, widget):
            self.widget = widget
            self.counter = 0

        def __call__(self):
            message_bar.set_text(f"Modified: {self.counter}.")
            self.counter += 1

    def __init__(self, box_widget):
        self.parent = box_widget
        self.serial = 0
        self.serial_lock = threading.Lock()
        self.Clear()

    def Clear(self):
        self.items = []
        self.Push()

    def SetSerial(self, serial):
        with self.serial_lock:
            self.serial = serial
            self.Clear()

    def Add(self, entry, serial):
        with self.serial_lock:
            if self.serial == serial:
                self.items.append(entry.MakeSelectableEntry())
                self.Push()

    def Push(self):
        new_list_walker = urwid.SimpleListWalker(self.items)
        urwid.connect_signal(new_list_walker, 'modified', SearchResultSink.ListWalkerModifiedHandler(new_list_walker))
        self.parent.original_widget = urwid.ListBox(new_list_walker)

def SwitchFocus(key):
    pass

logging.basicConfig(filename="log.txt",
                    format="[%(asctime)s %(levelname)7s] %(threadName)s: %(message)s",
                    datefmt="%m-%d-%Y %H:%M:%S",
                    level=logging.DEBUG)

palette = [('search_label', 'yellow,bold', 'dark cyan'),
           ('search_content', 'white', 'dark cyan'),
           ('message_bar', 'white', 'dark gray'),
           ('db_label', 'default', 'default'),
           ('db_status_ready', 'light green', 'default'),
           ('db_status_loading', 'light red', 'default'),
           ('db_status_searching', 'yellow', 'default'),

           ('title', 'yellow', 'dark gray'),
           ('source', 'dark green', 'default'),
           ('author', 'white', 'default'),
           ('venue', 'underline', 'default'),
           ('year', 'light gray', 'default'),
           ('delim', 'default', 'default'),
           ('bibkey', 'light green', 'default'),

           ('None+', 'default', 'dark magenta'),
           ('title+', 'yellow', 'light magenta'),
           ('source+', 'light green', 'dark magenta'),
           ('author+', 'white', 'dark magenta'),
           ('venue+', 'white,underline', 'dark magenta'),
           ('year+', 'white', 'dark magenta'),
           ('delim+', 'default', 'dark magenta'),
           ('bibkey+', 'light green', 'dark magenta'),

           ('detail_key', 'light green', 'default'),
           ('detail_value', 'default', 'default'),
           ]
main_loop = urwid.MainLoop(urwid.SolidFill(), palette, unhandled_input=SwitchFocus)

with open("config.json") as config_file:
    config = json.load(config_file)

bib_repos = [DblpBibRepo(main_loop)] + [LocalBibRepo(bib, main_loop) for bib in config['bib_files']]

search_bar = urwid.Edit(('search_label', "Search: "))
message_bar = urwid.Text(('message_bar', "Message"))

result_panel = urwid.AttrMap(urwid.SolidFill(), None)
search_result_sink = SearchResultSink(result_panel)

db_panel = urwid.Pile([repo.MakeStatusIndicator() for repo in bib_repos])

details_panel = urwid.AttrMap(urwid.SolidFill(), 'details')
picked_panel = urwid.AttrMap(urwid.Text("Selected:"), 'selected')

left_panel = result_panel
right_panel = urwid.Pile([('pack', urwid.LineBox(db_panel)),
                          ('weight', 5, urwid.LineBox(details_panel)),
                          ('pack', urwid.LineBox(picked_panel))])

main_widget = urwid.Columns([('weight', 2, urwid.LineBox(left_panel)),
                             ('weight', 1, right_panel)])

top_widget = urwid.Pile([('pack', urwid.AttrMap(search_bar, 'search_content')),
                         ('weight', 1, main_widget),
                         ('pack', urwid.AttrMap(message_bar, 'message_bar'))])

class UpdateSearchPanel:
    def __init__(self):
        self.serial = 0

    def __call__(self, edit, text):
        message_bar.set_text(f"Got '{text}'.")
        search_result_sink.SetSerial(self.serial)
        for repo in bib_repos:
            repo.Search(text, self.serial)

        self.serial += 1

urwid.connect_signal(search_bar, 'change', UpdateSearchPanel())

for repo in bib_repos:
    repo.ConnectSink(search_result_sink)

main_loop.widget = top_widget
main_loop.run()

