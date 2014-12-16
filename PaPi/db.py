# -*- coding: utf-8

# Copyright (C) 2014, A. Murat Eren
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation; either version 2 of the License, or (at your option)
# any later version.
#
# Please read the COPYING file.

"""
    Basic DB operations.
"""

import os
import sqlite3

import PaPi.filesnpaths as filesnpaths
from PaPi.utils import ConfigError

class DB:
    def __init__(self, db_path, client_version, new_database=False):
        self.db_path = db_path
        self.version = None

        filesnpaths.is_output_file_writable(db_path)
        if new_database and os.path.exists(self.db_path):
            os.remove(self.db_path)

        self.conn = sqlite3.connect(self.db_path)
        self.cursor = self.conn.cursor()

        if new_database:
            self.set_version(client_version)
        else:
            self.version = self.get_version()
            if self.version != client_version:
                raise ConfigError, "%s was generated when your client was at version %s, however, now it is at\
                                    version %s. Which means this database file cannot be used with this client\
                                    anymore and needs to be re-created :/"\
                                            % (self.db_path, self.version, client_version)


    def get_version(self):
        try:
            row = self._exec('SELECT version from self')
        except:
            raise ConfigError, "%s does not seem to be a database generated by PaPi :/" % self.db_path

        return row.fetchall()[0][0]


    def set_version(self, version):
        self._exec('''CREATE TABLE self (version text)''')
        self._exec('''INSERT INTO self VALUES(?)''', (version,))
        self.commit()


    def commit(self):
        self.conn.commit()


    def disconnect(self):
        self.conn.commit()
        self.conn.close()


    def _exec(self, sql_query, value=None):
        if value:
            return self.cursor.execute(sql_query, value)
        else:
            return self.cursor.execute(sql_query)


    def _exec_many(self, sql_query, values):
        return self.cursor.executemany(sql_query, values)

