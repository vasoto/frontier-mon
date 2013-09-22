import re
import collections

from Utils import RecordTable, RecordStatistics, parse_utc_time_usecs, current_utc_time_usecs


"""
 Tomcat's access.log format is:
    *  <servlet_name> <timestamp> id=<id> <payload>
      Ex:
         FrontierPrep 08/05/13 19:34:35.622 CEST +0200 id=293476 <payload>

 where the <payload> is composed of several kinds of messages:
    *  servlet_version:<number> start threads:<number> <query_data> <client_data> <forwarding_data>
      Ex:
       servlet_version:3.30 start threads:1 query /type=frontier_request:1:DEFAULT&encoding=BLOBzip5&p1=<very_long_string_encoding_the_query> raddr 127.0.0.1 frontier-id: CMSSW_5_3_8_patch1 2.8.5 5258 puigh(524) Darren Puigh via: 1.0 vocms213.cern.ch:8000 (squid/frontier-squid-2.7.STABLE9-16.1) x-forwarded-for: 128.146.38.254
    *  DB query finished msecs=<number>
    *  rows=<number>, full size=<number>
    *  DB connection released remaining=<number>
    *  stop threads=<number> msecs=<number>
    *  Error <error message>
    *  Client disconnected while processing payload 0: ClientAbortException ...
    *  SQL <SQL query>
    *  Acquiring DB connection [lock]
    *  Executing DB query
    *  [several others, to be ignored in the meantime]

In any of these cases, the <id> can be appended with a "-ka", which means the connection was attempted to be Kept Alive.

The other kind of entry is that of an exception. An example is:
    java.lang.Exception: X-frontier-id header missing
            at gov.fnal.frontier.Frontier.logClientDesc(Frontier.java:429)
            at gov.fnal.frontier.Frontier.<init>(Frontier.java:261)
            at gov.fnal.frontier.FrontierServlet.service(FrontierServlet.java:123)
            at javax.servlet.http.HttpServlet.service(HttpServlet.java:723)
            <several more of these lines>
    <a blank line>
"""

class TomcatWatcher(object):

    record_variables = {"servlet": str,
                        "version": str,
                        "query": str,
                        "who": str,
                        "error": str,
                        "sql": str,
                        "dbacq": str,
                        "state": str,
                        "fid": str,
                        "forward": str,
                        "via": str,
                        "finish_mode": str,
                        "threads_start": int,
                        "threads_stop": int,
                        "msecs_acq": int,
                        "msecs_finish": int,
                        "msecs_stop": int,
                        "rows": int,
                        "size": int,
                        "active_acq": int,
                        "kaacq": int,
                        "keepalives": int,
                        "time_start": int,
                        "time_stop": int}

    regex_general = re.compile(r'^(?P<servlet>\S+) (?P<timestamp>(?:\S+ ){4})id=(?P<key>\S+) (?P<payload>.*)')
    regex_start = re.compile(r'servlet_version:(?P<version>\S+) start threads:(?P<threads_start>\d+) query (?P<query>\S+) raddr (?P<who>\S+) frontier-id: (?P<complement>.*)')
    regex_dbacq = re.compile(r'DB connection acquired active=(?P<active_acq>\d+) msecs=(?P<msecs_acq>\d+)')
    regex_dbfin = re.compile(r'DB query finished msecs=(?P<msecs_finish>\d+)')
    regex_rowssize = re.compile(r'rows=(?P<rows>\d+), full size=(?P<size>\d+)')
    regex_threads = re.compile(r'stop threads=(?P<threads_stop>\d+) msecs=(?P<msecs_stop>\d+)')
    regex_error = re.compile(r'Error (?P<error>.*)')
    regex_client = re.compile(r'Client (?P<client>.*)')
    regex_sql = re.compile(r'SQL (?P<sql>.*)')
    regex_acq = re.compile(r'Acquiring DB (?P<dbacq>.*)')
    regex_exe = re.compile(r'Executing DB query')
    regex_kaacq = re.compile(r'DB acquire sent keepalive (?P<kaacq>\d+)')

    status_queued = 'queued'
    status_exec = 'executing'
    status_stop = 'finished'

    finish_normal = 'ok'
    finish_timeout = 'timed-out'
    finish_error = 'aborted'

    def __init__ (self, window_length_secs, use_timestamps_in_log=True):

        self.use_timestamps_in_log = use_timestamps_in_log

        self.window_length_V = int (1e6 * window_length_secs)
        self.oldest_start_time = float("inf")
        self.newest_stop_time = 0

        self.history_H = collections.deque()
        initial_rows_estimation = 100 * int(window_length_secs)
        self.data_D = RecordTable(self.record_variables,
                                  initial_rows=initial_rows_estimation,
                                  datatype = int)

        self._last_key = None

        self.stats_list = (
                {'filter': {'servlet':"FrontierProd"},
                 'interest': 'query',
                 'weighter': 'who',
                 'action': 'tally'},
                {'filter': {'servlet':"FrontierProd"},
                 'interest': 'who',
                 'weighter': 'size',
                 'action': 'sum'},
                {'filter': {'servlet':"smallfiles"},
                 'interest': 'who',
                 'weighter': 'size',
                 'action': 'sum'}
                )
        self.statistics = RecordStatistics(self.stats_list)

    def parse_log_line (self, line_in):

        line = line_in.strip()
        if not line: return

        general_match = self.regex_general.match(line)

        if general_match:
            record = general_match.groupdict()

            servlet = record['servlet']
            id_raw = record.pop('key').replace('-ka', '')
            key = servlet + id_raw

            timestamp_log = record.pop('timestamp')
            if self.use_timestamps_in_log:
                timestamp = parse_utc_time_usecs (timestamp_log[:-12])
            else:
                timestamp = current_utc_time_usecs()

            payload = record.pop('payload')

            match = self.regex_start.match(payload)
            if match:
                if self.oldest_start_time > timestamp:
                    self.oldest_start_time = timestamp

                record.update (match.groupdict())
                record['time_start'] = timestamp
                record['threads_start'] = int(record['threads_start'])
                record['state'] = self.status_queued
                record['keepalives'] = 0

                complement = record.pop('complement')
                parts = complement.split(':')
                record['fid'] = parts[0].replace(' x-forwarded-for', '').replace(' via', '')
                if len(parts) > 1:
                    if parts[-2].endswith(' x-forwarded-for'):
                        record['forward'] = parts[-1]
                    record['via'] = ':'.join(parts[1:-1]).replace('x-forwarded-for', '')

                self.data_D[key] = record
                self.history_H.append (key)
                return

            if key in self.data_D:
                self._last_key = key
            else:
                return

            match = self.regex_dbacq.match(payload)
            if match:
                update = match.groupdict()
                update['active_acq'] = int(update['active_acq'])
                update['msecs_acq'] = int(update['msecs_acq'])
                self.data_D.modify (key, update)
                return

            match = self.regex_dbfin.match(payload)
            if match:
                update = match.groupdict()
                update['msecs_finish'] = int(update['msecs_finish'])
                self.data_D.modify (key, update)
                return

            match = self.regex_rowssize.match(payload)
            if match:
                update = match.groupdict()
                update['rows'] = int(update['rows'])
                update['size'] = int(update['size'])
                self.data_D.modify (key, update)
                return

            match = self.regex_threads.match(payload)
            if match:
                update = match.groupdict()
                update['msecs_stop'] = int(update['msecs_stop'])
                update['threads_stop'] = int(update['threads_stop'])
                self.data_D.modify (key, update)
                self.finish_record (key, timestamp, self.finish_normal)
                return

            match = self.regex_sql.match(payload)
            if match:
                update = match.groupdict()
                self.data_D.modify (key, update)
                return

            match = self.regex_acq.match(payload)
            if match:
                update = match.groupdict()
                self.data_D.modify (key, update)
                return

            match = self.regex_exe.match(payload)
            if match:
                update = {'state': self.status_exec}
                self.data_D.modify (key, update)
                return

            match = self.regex_kaacq.match(payload)
            if match:
                record = self.data_D[key]
                update = {'keepalives': record['keepalives'] + int(match.group('kaacq'))}
                self.data_D.modify (key, update)
                return

            match = self.regex_error.match(payload)
            if match:
                update = match.groupdict()
                self.data_D.modify (key, update)
                return

            match = self.regex_client.match(payload)
            if match:
                record = self.data_D[key]
                if 'client' in record:
                    update = match.groupdict()
                    print 'Existing client message for id %s: %s' % (key, record['client'])
                    print 'New error:', match.group('client')
                #self.data_D.modify (key, update)
                return
            #print "No match!", line

        else:
            if 'xception' in line:
                if self._last_key:
                    key = self._last_key
                else:
                    return

                self.finish_record (key, timestamp, self.finish_error)

            elif line.startswith('at '):
                return
            else:
                print "Unforseen line:", line

    def finish_record (self, key, timestamp, finish_mode):

        update = {'time_stop': timestamp,
                  'state': self.status_stop,
                  'finish_mode': finish_mode}
        self.data_D.modify (key, update)

        if self.newest_stop_time < timestamp:
            self.newest_stop_time = timestamp

    def update (self):

        current_timespan_usecs = self.newest_stop_time - self.oldest_start_time
        while current_timespan_usecs > self.window_length_V:
            dropped_key = self.history_H.popleft()
            self.oldest_start_time = self.data_D.render_record (dropped_key, 'time_start')
            del self.data_D[dropped_key]
            current_timespan_usecs = self.newest_stop_time - self.oldest_start_time

    def advance_records (self, line_in):

        self.parse_log_line(line_in)
        self.update()
        #self.statistics.get_statistics(self.data_D)

    def current_window_length_secs (self):

        current_timespan_usecs = self.newest_stop_time - self.oldest_start_time
        return current_timespan_usecs * 1e-6

    def __len__(self):
        return len(self.history_H)

