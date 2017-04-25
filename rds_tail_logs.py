#!/usr/bin/env python
import boto3
import json
import datetime
import botocore
import collections
import os
import sys
import argparse
import time
import re
import logging
import requests
from elasticsearch import Elasticsearch
from datetime import datetime


class DescribeLogs(object):
  def __init__(self, db_instance, name, last_written, pending_read, marker, last_read):
    self.db_instance = db_instance
    self.name = name
    self.last_written = last_written
    self.pending_read = pending_read
    self.marker = marker
    self.last_read = last_read

  def readThis(self, time_threshold_ms):
    return self.pending_read or ((self.last_written > self.last_read) and
                                 (self.last_written > time_threshold_ms))

# Gather data from the RDS for the streaming of logs
class RDS(object):
  def __init__(self, delay_seconds, region_name, profile_name):
    self.delay_seconds = delay_seconds
    session = boto3.Session(profile_name=profile_name)
    self.client = session.client('rds', region_name=region_name)

  def _aws_api_call(self, func, kwargs):
    time.sleep(self.delay_seconds)
    response = func(**kwargs)
    return response

  def describe_db_log_files(self, instance_id):
    logging.debug('Checking log file descriptions for %s', instance_id)
    return self._aws_api_call(self.client.describe_db_log_files,
                              {'DBInstanceIdentifier': instance_id})

  def download_db_log_file_portion(self, instance_id, logfile_name, marker):
    logging.info('Downloading %s:%s, starting at %s', instance_id, logfile_name, marker)
    return self._aws_api_call(self.client.download_db_log_file_portion,
                              {'DBInstanceIdentifier': instance_id,
                               'LogFileName': logfile_name,
                               'Marker': marker})
# The log streaming starts here

class LogTail(object):
  DATE_REGEX = re.compile(r'(\d\d\d\d-\d\d-\d\d \d\d:\d\d:\d\d UTC)')
  ONE_DAY = 86400000 #milliseconds

  def __init__(self, log_state_file, db_instance_ids,
               minutes_in_the_past_to_start, rds, retention_days, run_once, output_format):
    self.rds = rds
    self.log_state_file = log_state_file
    self.db_instance_ids = db_instance_ids
    self.time_threshold_ms = (int(time.time()) - (minutes_in_the_past_to_start*60)) * 1000
    self.retention_days = retention_days
    self.log_state = {}
    self.run_once = run_once
    self.output_format = output_format
    try:
      with open(self.log_state_file, 'r') as infile:
        self.log_state = json.load(infile)
    except IOError, err:
      logging.warn("Can't open %s: %s. Will create a new file with this name.",
                   self.log_state_file, err)

  def _cleanup_logfile_state(self):
    new_log_state = {}
    for db_instance, logs in self.log_state.iteritems():
      if logs:
        new_log_state.setdefault(db_instance, {})
        for log_name, log_data in logs.iteritems():
          if log_data['time_ms'] > self.time_threshold_ms - (self.ONE_DAY * self.retention_days):
            new_log_state[db_instance][log_name] = log_data
    self.log_state = new_log_state

  def _write_logfile_state(self):
    self._cleanup_logfile_state()
    with open(self.log_state_file, 'w') as outfile:
      json.dump(self.log_state, outfile)

  def _get_logfile_descriptions_indefinitely(self):
    while True:
      for logfile_desc in self._get_logfile_descriptions():
        yield logfile_desc

  def _get_logfile_descriptions(self):
    for instance_id in self.db_instance_ids:
      response = self.rds.describe_db_log_files(instance_id)
      request_time_string = response['ResponseMetadata']['HTTPHeaders']['date']
      request_time = time.strptime(request_time_string, '%a, %d %b %Y %H:%M:%S %Z')
      request_time_ms = int(time.mktime(request_time)) * 1000
      for logfile in response['DescribeDBLogFiles']:
        logfile_name = logfile['LogFileName']
        log_desc = DescribeLogs(instance_id, logfile_name, logfile['LastWritten'],
            self.log_state.get(instance_id, {}).get(logfile_name, {}).get('pending_read', False),
            self.log_state.get(instance_id, {}).get(logfile_name, {}).get('marker', '0'),
            self.log_state.get(instance_id, {}).get(logfile_name, {}).get('time_ms'))
        if log_desc.readThis(self.time_threshold_ms):
          yield log_desc, request_time_ms

  def stream(self):
    if self.run_once:
      logfile_func = self._get_logfile_descriptions
    else:
      logfile_func = self._get_logfile_descriptions_indefinitely
    for log_desc, request_time_ms in logfile_func():
      resp = self.rds.download_db_log_file_portion(log_desc.db_instance, log_desc.name,
                                                   log_desc.marker)
      fields = self.DATE_REGEX.split(resp['LogFileData'])
      if len(fields) > 1:
        if self.DATE_REGEX.match(fields[0]) and (len(fields) % 2 == 0):
          start_index = 0
        elif self.DATE_REGEX.match(fields[1]) and (len(fields) % 2 == 1):
          start_index = 1
        for i in range(start_index, len(fields))[::2]:
          date = fields[i]
          logdata = fields[i+1]
          if self.output_format == 'json':
            line = collections.OrderedDict()
            line['timestamp'] = date
            line['message'] = logdata
            line['host'] = log_desc.db_instance
            line['awsRdsLogFileName'] = log_desc.name
            query = json.dumps(line)
            ES_HOST = {"host" : "IP", "port" : 9200}
            es = Elasticsearch(hosts = [ES_HOST])
            INDEX_NAME = 'database_logs'
            TYPE_NAME = 'db_logs'
            message_arr = logdata.split(':')
            es.indices.create(index=INDEX_NAME, ignore=400)
            es.index(index=INDEX_NAME, doc_type=TYPE_NAME, body={"app_server": message_arr[1], "db_details": message_arr[2], "query_statement": message_arr[6], "time-field": date, "createdAt": datetime.now() })
          elif self.output_format == 'text':
            datalines = [line for line in logdata.split('\n') if line.strip()]
            for line in datalines:
              print('{}:[[ANNOTATED: awsDbInstanceId="{}", awsRdsLogFileName="{}"]]{}'.format(
                    date, log_desc.db_instance, log_desc.name, line))
      self.log_state.setdefault(log_desc.db_instance, {})
      self.log_state[log_desc.db_instance].setdefault(log_desc.name, {})
      self.log_state[log_desc.db_instance][log_desc.name] = {
          'marker': resp['Marker'],
          'pending': resp['AdditionalDataPending'],
          'time_ms': request_time_ms,
      }
      self._write_logfile_state()

# Get the details for the script to work
def main():
  parser = argparse.ArgumentParser(description='Stream logs from rds for a set of db instances.')
  parser.add_argument('--db_instance_ids', '-d', nargs='+', type=str, required=True,
                      help='list of db instance ids')
  parser.add_argument('--minutes_in_the_past_to_start', '-m', type=int, default=0,
                      help=('if logs have not been written to since this many minutes ago, '
                            'ignore them'))
  parser.add_argument('--api_call_delay_seconds', '-a', type=float, default=1.0,
                      help='time to wait before each API call')
  parser.add_argument('--log_state_file', '-s', type=str, default='log_state.json',
                      help='file path for recording the state of log streaming')
  parser.add_argument('--retention_days', '-r', type=int, default=7,
                      help='number of days to retain log metadata')
  parser.add_argument('--log_level', '-l', type=str, default='INFO',
                      choices=['DEBUG', 'INFO', 'WARN', 'ERROR', 'CRITICAL'],
                      help="log level for this script's logs")
  parser.add_argument('--log_filename', '-f', type=str, default='rds_tail_logs.log',
                      help="log filename for this script's logs")
  parser.add_argument('--run_once', '-o', dest='run_once', action='store_true',
                      help="stream all new logs from all db instances and then exit")
  parser.add_argument('--output_format', '-t', choices=['json', 'text'], default='json',
                      help="output format")
  parser.add_argument('--aws_region_name', type=str, help="AWS region name")
  parser.add_argument('--aws_profile_name', default='default', help='AWS credentials profile name')
  args = parser.parse_args()

  os.environ['TZ'] = 'UTC'
  time.tzset()
  logging.basicConfig(filename=args.log_filename, level=logging._levelNames[args.log_level],
                      format='%(asctime)s %(message)s')
  logging.info('Starting rds log streaming with args: %s', args)

  rds = RDS(args.api_call_delay_seconds, args.aws_region_name, args.aws_profile_name)

  rds_tail_logs = LogTail(args.log_state_file, args.db_instance_ids,
                                    args.minutes_in_the_past_to_start, rds,
                                    args.retention_days, args.run_once, args.output_format)
  rds_tail_logs.stream()

if __name__ == '__main__':
  main()
