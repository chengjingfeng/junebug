from subprocess import Popen, PIPE
import httplib
import json
import os
import time
import re
import math
import sys
import argparse


class Process(object):
    def get_command(self):
        '''Subclasses implement. Returns a list representing start command.'''
        return []

    def start(self, stdout=PIPE, extra_commands=[]):
        env = os.environ.copy()
        env['JUNEBUG_DISABLE_LOGGING'] = 'true'
        command = self.get_command()
        command.extend(extra_commands)
        self.process = Popen(
            command, env=env, stdout=stdout)
        self.post_start()

    def get_rss(self):
        if not sys.platform.startswith('linux'):
            return 0
        pid = self.process.pid
        with open("/proc/%d/status" % pid) as f:
            d = f.read()
            start = d.find('RSS')
            end = d.find('\n', start)
            m = re.search("(\d+)\s+([kKmM])B", d[start:end])
            if m.group(2) in 'kK':
                coef = 0.001
            else:
                coef = 1.0
            return int(math.ceil((float(m.group(1))) * coef))

    def post_start(self):
        '''Subclasses implement. What to do after starting process.'''
        pass

    def stop(self):
        self.process.terminate()


class Junebug(Process):
    def __init__(self, config):
        self.config = config
        self.conn = httplib.HTTPConnection('localhost', port=8080)

    def get_command(self):
        return ['jb']

    def post_start(self):
        # This is horrible
        time.sleep(2)

    def create_channel(self):
        if self.config.channel_type == 'ussd':
            self.conn.request(
                "POST", '/channels/',
                json.dumps({
                    'type': 'dmark',
                    'config': {
                        'web_path': 'api',
                        'web_port': 8001,
                        "redis_manager": {
                          "key_prefix": "my_ussd",
                          "host": "redis",
                          "db": 2,
                          "port": 6379
                        }
                    },
                    'mo_url': 'http://localhost:8002',
                }),
                {'Content-Type': 'application/json'})
        elif self.config.channel_type == 'smpp':
            self.conn.request(
                "POST", '/channels/',
                json.dumps({
                    "type": "smpp",
                    "mo_url": "http://localhost:8002",
                    "config": {
                        "system_id": "smppclient1",
                        "password": "password",
                        "twisted_endpoint": "tcp:localhost:2775",
                        "redis_manager": {
                          "key_prefix": "my_smpp",
                          "host": "redis",
                          "db": 2,
                          "port": 6379
                        }
                    }
                }),
                {'Content-Type': 'application/json'})
        else:
            raise RuntimeError(
                'Invalid channel type %r' % self.config.channel_type)
        r = self.conn.getresponse()
        assert r.status == 201
        channel = json.loads(r.read())['result']['id']
        self._wait_for_channel_start()
        return channel

    def _wait_for_channel_start(self):
        # This is horrible
        print 'Waiting for channel to start'
        time.sleep(5)

    def delete_ussd_channel(self, channelid):
        self.conn.request(
            "DELETE", '/channels/%s' % channelid)
        r = self.conn.getresponse()
        assert r.status == 200


class FakeApplicationServer(Process):
    def get_command(self):
        return ['python', 'application_server.py']


class BenchmarkRunner(Process):
    def __init__(self, config):
        self.config = config

    def get_command(self):
        if self.config.channel_type == 'ussd':
            command = ['python', 'submit_message.py']
        elif self.config.channel_type == 'smpp':
            command = ['python', 'submit_message_smpp.py']
        command.extend([
            '--end-id', str(self.config.test_length),
            '--warmup', str(self.config.warmup)])
        return command

    def print_results(self):
        for line in iter(self.process.stdout.readline, ''):
            print line.rstrip('\n')


def parse_arguments(args):
    parser = argparse.ArgumentParser(
        description=(
            'Runs the Junebug benchmarks and print out the results.'))
    parser.add_argument(
        '--channel-type', dest='channel_type', type=str, default='ussd',
        help='The type of channel to benchmark. Either ussd or smpp')
    parser.add_argument(
        '--test-length', dest='test_length', type=int, default=10000,
        help='The number of messages to send per benchmark.')
    parser.add_argument(
        '--warmup', dest='warmup', default=3000,
        help='Number of iterations to discard for statistics')
    parser.add_argument(
        '--concurrency', dest='concurrency', type=int, default=[2, 5, 10, 20],
        nargs='+', help='The list of concurrencies to test')
    return parser.parse_args(args)


def main():
    config = parse_arguments(sys.argv[1:])
    try:
        print 'Starting Junebug benchmark...'
        jb = Junebug(config)
        jb.start()

        app = FakeApplicationServer()
        app.start()

        ch = jb.create_channel()

        for concurrency in config.concurrency:
            print 'Running benchmark with concurrency %d' % concurrency
            benchmark = BenchmarkRunner(config)
            max_rss = 0
            benchmark.start(
                stdout=None, extra_commands=[
                    '--concurrency=%d' % concurrency])
            while benchmark.process.poll() is None:
                try:
                    max_rss = max(max_rss, benchmark.get_rss())
                except (OSError, IOError):
                    pass  # possible race condition?
                time.sleep(0.2)
            if sys.platform.startswith('linux'):
                print "Max memory: %d" % max_rss

        jb.delete_ussd_channel(ch)
    finally:
        jb.stop()
        app.stop()
        try:
            benchmark.stop()
        except:
            pass

if __name__ == '__main__':
    main()