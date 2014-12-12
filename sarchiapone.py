#!/usr/bin/env python
# Utility for automatically recording signals from satellite passes
# 2014 Tito Dal Canton

import time
import subprocess
import logging
import urllib2
import itertools
import math
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
        return self.tle_name


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

    def __init__(self, latitude='52.38859', longitude='9.71630', elevation=55.,
                 output_base='./'):
        self.proc = None
        self.frequency = None
        self.observer = ephem.Observer()
        self.observer.lat = ephem.degrees(latitude)
        self.observer.long = ephem.degrees(longitude)
        self.observer.elevation = elevation
        self.output_base = output_base

    def start(self, frequency, prefix):
        self.frequency = frequency
        now = time.strftime('%Y%m%d-%H%M%S')
        args = ['./noaa_apt_rec.py',
                '--frequency', frequency,
                '--output-path', '%s/%s-%s-demod.wav' % (self.output_base, prefix, now),
                '--iq-output-path', '%s/%s-%s-iq.wav' % (self.output_base, prefix, now)]
        self.proc = subprocess.Popen(args)

    def stop(self):
        self.proc.terminate()
        self.proc.wait()
        if self.proc.returncode != 0:
            logging.warning('Receiver process terminated with code %d', self.proc.returncode)
        self.proc = None
        self.frequency = None

    def running(self):
        return not (self.proc is None)


logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s',
                    datefmt='%F %T')

satellites = [
    Satellite('ISS (ZARYA)', 'stations.txt', '145.8', 'iss_sstv'),
    Satellite('NOAA 15', 'weather.txt', '137.62', 'noaa15'),
    Satellite('NOAA 18', 'weather.txt', '137.9125', 'noaa18'),
    Satellite('NOAA 19', 'weather.txt', '137.1', 'noaa19'),
    Satellite('NOAA 9 [P]', 'noaa.txt', '137.505', 'noaa9'),
    Satellite('METEOR-M 2', 'weather.txt', '137.1', 'meteorm2'),
    Satellite('ISIS 1', 'visual.txt', '136.41', 'isis1'),
    #Satellite('OSCAR 7 (AO-7)', 'amateur.txt', '145.9775', 'ao7'),
    #Satellite('HAMSAT (VO-52)', 'amateur.txt', '145.86', 'vo52'),
    #Satellite('ORBCOMM FM32 [+]', 'orbcomm.txt', '137.7', 'orbcomm_fm32')
]
passes = []
receiver = Receiver()

logging.info('Starting monitor')
while True:
    t = ephem.now()

    # calculate new passes if needed
    receiver.observer.date = t
    for sat in satellites:
        if t > sat.next_check_time:
            np = sat.next_pass(receiver.observer)
            if np.interesting:
                logging.info('%s: interesting pass with TCA %s, scheduling',
                             sat, ephem.localtime(np.tca))
                passes.append(np)
            else:
                logging.info('%s: ongoing or low pass with TCA %s, skipping',
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

    time.sleep(3)
