"""JSONwriter
Writes events to JSON files, segmenting on max size"""

import os, pathlib
import logging
import time
from . import json_quick
from . import merge_test

TEMP_DIRECTORY = "/tmp/synth_json_writer/"  # We build each file in a temporary directory, then move when it's finished (so that anyone watching the destination directory doesn't ever encounter partially-written files
DEFAULT_DIRECTORY = "../synth_logs/"
DEFAULT_MAX_EVENTS_PER_FILE = 100000    # FYI 100,000 messages is max JSON file size that DP can ingest (if that's where you end-up putting these files)

class Stream():
    """Write properties into JSON files, splitting by max size.
       If you access .files_written property then call close() first"""
    def __init__(self, filename, directory = DEFAULT_DIRECTORY, file_mode="wt", max_events_per_file = DEFAULT_MAX_EVENTS_PER_FILE, merge = False):
        pathlib.Path(TEMP_DIRECTORY).mkdir(exist_ok=True)   # Ensure temp directory exists

        self.target_directory = directory
        self.filename_root = filename
        self.file_mode = file_mode
        self.max_events_per_file = max_events_per_file
        self.merge = merge

        self.file = None
        self.filename = None
        self.files_written = []
        self.file_count = 1
        self.last_event = {}    # Used to merge messages
 
    def _write_event(self, properties):
        self.check_next_file()
        jprops = properties.copy()
        jprops["$ts"] = int(jprops["$ts"] * 1000) # Convert timestamp to ms as that's what DP uses internally in JSON files
        if self.events_in_this_file > 0:
            self.file.write(",\n")
        s = json_quick.dumps(jprops)
        self.file.write(s)

    def write_event(self, properties):
        if not self.merge:
            self._write_event(properties)
            return

        if len(self.last_event) == 0:
            self.last_event = properties
            return

        if merge_test.ok(self.last_event, properties):
            self.last_event.update(properties)
        else:
            self._write_event(self.last_event)
            self.last_event = properties   

    def move_to_next_file(self):
        """Move to next json file"""
        if self.file is not None:
            self._close()

        self.filename = self.filename_root + "%05d" % self.file_count + ".json"
        logging.info("Starting new logfile " + self.filename)
        self.file = open(TEMP_DIRECTORY + self.filename, self.file_mode)  # No-longer unbuffered as Python3 doesn't support that on text files
        self.file.write("[\n")
        self.events_in_this_file = 0

    def check_next_file(self):
        """Check if time to move to next json file"""
        if self.file is None:
            self.move_to_next_file()
            return
        if self.events_in_this_file >= self.max_events_per_file-1:
            self.move_to_next_file()
            return
        self.events_in_this_file += 1

    def _close(self):
        if self.file is not None:
            # logging.info("Closing JSON file")
            self.file.write("\n]\n")
            self.file.close()
            os.rename(TEMP_DIRECTORY + self.filename, DEFAULT_DIRECTORY + self.filename)
            self.files_written.append(DEFAULT_DIRECTORY + self.filename)
            self.file = None
            self.filename = None
            self.file_count += 1

    def close(self):
        if len(self.last_event) != 0:
            self._write_event(self.last_event)
        self._close()

           
