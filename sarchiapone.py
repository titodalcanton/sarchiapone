#!/usr/bin/env python

# Automatically record signals from satellite transits
# Copyright (C) 2014 Tito Dal Canton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import sys
import time
import subprocess
import logging
import urllib2
import itertools
import math
import ConfigParser
import ephem


class Satellite:
    tle_url_base = 'http://celestrak.com/NORAD/elements/'

    def __init__(self, tle_name, tle_url, frequency, output_prefix):
        self.tle_name = tle_name
        self.output_prefix = output_prefix
        self.frequency = frequency
        self.next_check_time = ephem.now()

        # download TLE list
        tle_fd = urllib2.urlopen(self.tle_url_base + tle_url)
        tle_list = []
        for line, i in zip(tle_fd, itertools.cycle(xrange(3))):
            if i == 0:
                tle_list.append([])
            tle_list[-1].append(line.strip())
        
        # scan TLEs for this satellite
        self.tle = None
        for tle in tle_list:
            if tle[0] == tle_name:
                self.tle = tle[1:]
                break
        if self.tle is None:
            raise RuntimeError('TLE "%s" not found' % tle_name)
        
        # init ephem body object
        self.body = ephem.readtle(tle_name, self.tle[0], self.tle[1])

    def next_pass(self, observer):
        np = observer.next_pass(self.body)
        return Pass(np, self)

    def __str__(self):
        return self.output_prefix


class Pass:
    "Models a satellite pass."

    def __init__(self, ephem_pass, sat):
        self.interesting = True
        self.begin = ephem_pass[0]
        self.tca = ephem_pass[2]
        self.end = ephem_pass[4]
        self.max_elevation = float(ephem_pass[3]) * 180. / math.pi
        if self.begin <= ephem.now():
            self.interesting = False
        if self.max_elevation < 35.:
            self.interesting = False
        if self.end <= self.begin:
            # happens when pass is ongoing
            self.interesting = False
        self.sat = sat
        self.status = 'future'
    
    def set_status(self, status):
        self.status = status


class Receiver:
    "Models a program that can record a frequency band to a file."

    def __init__(self, latitude, longitude, elevation, horizon, output_base):
        self.proc = None
        self.frequency = None
        self.observer = ephem.Observer()
        self.observer.lat = ephem.degrees(latitude)
        self.observer.long = ephem.degrees(longitude)
        self.observer.elevation = float(elevation)
        self.observer.horizon = ephem.degrees(horizon)
        self.output_base = output_base

    def start(self, frequency, prefix):
        self.frequency = frequency
        now = time.strftime('%Y%m%d-%H%M%S')
        self.iq_file_path = '%s/%s-%s-iq.wav' % (self.output_base, prefix, now)
        self.demod_file_path = '%s/%s-%s-demod.wav' % (self.output_base, prefix, now)
        args = ['./noaa_apt_rec.py',
                '--frequency', frequency,
                '--output-path', self.demod_file_path,
                '--iq-output-path', self.iq_file_path]
        self.proc = subprocess.Popen(args)

    def stop(self):
        self.proc.terminate()
        self.proc.wait()
        if not self.proc.returncode in [0, -15]:
            # for unknown reasons, normal termination returns -15
            logging.warning('Receiver process terminated with code %d',
                            self.proc.returncode)
        self.proc = None
        self.frequency = None

    def running(self):
        return not (self.proc is None)


logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s',
                    datefmt='%F %T')

logging.info('Reading configuration')
config = ConfigParser.ConfigParser()
config.read(sys.argv[1])

# load satellites to monitor
satellites = []
for section in config.sections():
    if section.startswith('sat:'):
        sat = Satellite(config.get(section, 'tle_label'),
                        config.get(section, 'tle_file'),
                        config.get(section, 'frequency'),
                        section.replace('sat:', ''))
        satellites.append(sat)

# load receiver definition
receiver = Receiver(config.get('receiver', 'latitude'),
                    config.get('receiver', 'longitude'),
                    config.get('receiver', 'elevation'),
                    config.get('receiver', 'horizon'),
                    config.get('receiver', 'output_path'))

passes = []

logging.info('Starting monitor')
while True:
    t = ephem.now()

    # calculate new passes if needed
    receiver.observer.date = t
    for sat in satellites:
        if t > sat.next_check_time:
            np = sat.next_pass(receiver.observer)
            if np.interesting:
                logging.info('%s: interesting pass with TCA %s and max altitude %f, scheduling',
                             sat, ephem.localtime(np.tca), np.max_elevation)
                passes.append(np)
            else:
                logging.debug('%s: ongoing or low pass with TCA %s, skipping',
                              sat, ephem.localtime(np.tca))
            sat.next_check_time = ephem.Date(np.end + ephem.minute)

    # handle scheduled or active passes
    for p in passes:
        if p.status == 'future':
            if t > p.begin:
                if receiver.running():
                    logging.info('%s: raising, receiver busy, deferring reception', p.sat)
                    p.set_status('deferred')
                else:
                    logging.info('%s: raising, starting reception', p.sat)
                    receiver.start(p.sat.frequency, p.sat.output_prefix)
                    p.set_status('receiving')
        elif p.status == 'deferred':
            if not receiver.running():
                logging.info('%s: receiver now free, starting reception', p.sat)
                receiver.start(p.sat.frequency, p.sat.output_prefix)
                p.set_status('receiving')
        elif p.status == 'receiving':
            if t > p.end:
                if receiver.running():
                    if receiver.frequency == p.sat.frequency:
                        logging.info('%s: setting, stopping reception', p.sat)
                        receiver.stop()
                    else:
                        logging.info('%s: setting, pass missed', p.sat)
                else:
                    raise RuntimeError('This should not happen!')
                passes.remove(p)

    try:
        time.sleep(1)
    except KeyboardInterrupt:
        break
