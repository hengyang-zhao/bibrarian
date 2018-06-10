import os
import glob
import json
import sys
import time
import logging
import threading

import urllib
import urllib.request
import urllib.parse

import urwid

import pybtex
import pybtex.database

class EntryWrapper:
    def __init__(self, raw_entry, source, old_key=None):
        self.source = source
        self.raw_entry = raw_entry

    def Authors(self):
        if type(self.raw_entry) is pybtex.database.Entry:
            return "Author from local bib"
        elif type(self.raw_entry) is dict:
            return self.raw_entry['info']['authors']['author'][0] + ' et al'
        else:
            return f"(Author unknown from type {type(self.raw_entry)})"

    def Title(self):
        if type(self.raw_entry) is pybtex.database.Entry:
            try:
                return self.raw_entry.fields['title']
            except:
                return str(self.raw_entry)
        elif type(self.raw_entry) is dict:
            return str(self.raw_entry['info']['title'])
        else:
            return f"(Title unknown from type {type(self.raw_entry)})"

    def Year(self):
        return "1950"

    def Venue(self):
        return "MIND"

    def Source(self):
        return self.source

    def Match(self, keywords):
        for keyword in keywords:
            if keyword.upper() in self.Title().upper():
                continue
            return False
        else:
            return True

    def MakeSelectableEntry(self):
        return urwid.AttrMap(urwid.Text(self.Title()), None)

    def __str__(self):
        return f"{self.Authors()}, {self.Title()}, {self.Venue()}, {self.Year()}, ({self.Source})"

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

            for item in self.SearchingThreadMain(self.search_text):
                for sink in self.search_result_sinks:
                    sink.Add(item, serial)

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
                self.bib_entries.append(EntryWrapper(entry, path, old_key=key))

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
                #logging.debug(raw_entry)
                yield EntryWrapper(raw_entry, "dblp")

class SearchResultSink:
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
        self.parent.original_widget = urwid.ListBox(urwid.SimpleListWalker(self.items))

def SwitchFocus(key):
    pass

logging.basicConfig(filename="log.txt",
                    format="[%(asctime)s %(levelname)7s] %(threadName)s: %(message)s",
                    datefmt="%m-%d-%Y %H:%M:%S",
                    level=logging.DEBUG)

palette = [('search_label', 'yellow,bold', 'dark gray'),
           ('search_content', 'white', 'dark gray'),
           ('message_bar', 'white', 'dark gray'),
           ('red', 'default', 'dark red'),
           ('green', 'default', 'dark green'),
           ('yellow', 'default', 'yellow'),
           ('db_label', 'default', 'default'),
           ('db_status_ready', 'light green', 'default'),
           ('db_status_loading', 'light red', 'default'),
           ('db_status_searching', 'yellow', 'default'),
           ('blank', 'default', 'default')]
main_loop = urwid.MainLoop(urwid.SolidFill(), palette, unhandled_input=SwitchFocus)

with open("config.json") as config_file:
    config = json.load(config_file)

bib_repos = [DblpBibRepo(main_loop)] + [LocalBibRepo(bib, main_loop) for bib in config['bib_files']]

search_bar = urwid.Edit(('search_label', "Search: "))
message_bar = urwid.Text(('message_bar', "Message"))

result_panel = urwid.AttrMap(urwid.SolidFill(), None)
search_result_sink = SearchResultSink(result_panel)

db_panel = urwid.Pile([repo.MakeStatusIndicator() for repo in bib_repos])

details_panel = urwid.AttrMap(urwid.SolidFill(), 'red')
picked_panel = urwid.AttrMap(urwid.SolidFill(), 'yellow')

left_panel = result_panel
right_panel = urwid.Pile([('pack', db_panel),
                          ('weight', 5, details_panel),
                          ('weight', 1, picked_panel)])

main_widget = urwid.Columns([('weight', 2, left_panel),
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

