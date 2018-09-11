# -*- coding: utf-8 -*-
import os
import fcntl
import yaml
import time
import pytz
import iso8601
import signal
import logging
import threading

TZ = pytz.timezone(os.environ['TZ'] if 'TZ' in os.environ else 'Europe/Kiev')


def long_version(dt, local_timezone=TZ):
    if not dt:
        return 0
    if isinstance(dt, (str, unicode)):
        dt = iso8601.parse_date(dt, default_timezone=None)
    if dt.tzinfo:
        dt = dt.astimezone(local_timezone)
    unixtime = time.mktime(dt.timetuple())
    return int(1e6 * unixtime + dt.microsecond)


def restkit_error(e, client=None):
    out = str(e)
    try:
        response = getattr(e, 'response', None)
        headers = getattr(response, 'headers', None)
        if headers:
            out += " Status:" + str(response.status_int)
            out += " Headers:" + str(headers)
        if client:
            headers = getattr(client, 'headers')
            params = getattr(client, 'params')
            prefix = getattr(client, 'prefix_path')
            uri = getattr(client, 'uri')
            out += " RequestHeaders:" + str(headers)
            out += " RequestParams:" + str(params)
            out += " URI:%s%s" % (uri, prefix)
    except:
        pass
    return out


def decode_bool_values(config):
    for key, value in config.items():
        value = str(value).strip().lower()
        if value in ("1", "true", "yes", "on"):
            config[key] = 1
        elif value in ("0", "false", "no", "off"):
            config[key] = 0
    return config


def chage_process_user_group(config, logger=None):
    from pwd import getpwuid, getpwnam
    from grp import getgrgid, getgrnam
    if config.get('user', ''):
        uid = os.getuid()
        newuid = getpwnam(config['user'])[2]
        if uid != newuid:
            if uid != 0:
                if logger:
                    logger.error("Can't change user not from root")
                return
            if config.get('group', ''):
                newgid = getgrnam(config['group'])[2]
                os.setgid(newgid)
            os.setuid(newuid)
    if not logger:
        return
    uid = os.getuid()
    gid = os.getgid()
    euid = os.geteuid()
    egid = os.getegid()
    logger.info("Process real user/group %d/%d %s/%s", uid, gid, getpwuid(uid)[0], getgrgid(gid)[0])
    logger.info("Process effective user/group %d/%d %s/%s", euid, egid, getpwuid(euid)[0], getgrgid(egid)[0])


class Watchdog:
    counter = 0
    timeout = 0

def watchdog_thread(logger):
    while True:
        Watchdog.counter += 1
        time.sleep(1)
        if Watchdog.counter >= Watchdog.timeout - 5:
            if logger:
                logger.warning("Watchdog counter %d", Watchdog.counter)
        if Watchdog.counter == Watchdog.timeout:
            if logger:
                logger.warning("Watchdog kill pid %d", os.getpid())
            os.kill(os.getpid(), signal.SIGTERM)
        if Watchdog.counter >= Watchdog.timeout + 5:
            if logger:
                logger.warning("Watchdog exit")
            os._exit(1)
            break

def setup_watchdog(timeout, logger=None):
    if not timeout or int(timeout) < 10:
        return
    Watchdog.timeout = int(timeout)
    thread = threading.Thread(target=watchdog_thread, name='Watchdog', args=(logger,))
    thread.daemon = True
    thread.start()

def reset_watchdog():
    if Watchdog.timeout:
        Watchdog.counter = 0


class InfoFilter(logging.Filter):
    def filter(self, rec):
        return rec.levelno < logging.WARNING


class InfoHandler(logging.StreamHandler):
    def __init__(self, *args, **kwargs):
        logging.StreamHandler.__init__(self, *args, **kwargs)
        self.addFilter(InfoFilter())


class SharedFileDict(object):
    """dict shared between processes
    """
    def __init__(self, name, expire=1):
        self.cache = dict()
        self.filename = name + '.yaml'
        self.lastsync = 0
        self.expire = expire

    def __setitem__(self, key, value):
        if self.cache.get(key) == value:
            return
        if value:
            self.cache[key] = value
            self.write()
        else:
            self.cache.pop(key, None)
            self.write(key)

    def __getitem__(self, key):
        if self.is_expired():
            self.read()
        return self.cache[key]

    def get(self, key, default=None):
        if self.is_expired():
            self.read()
        return self.cache.get(key, default)

    def pop(self, key, default=None):
        return self.cache.pop(key, default)

    def update(self, items):
        self.cache = dict(items)
        self.write(reread=False)

    def is_expired(self):
        return time.time() - self.lastsync > self.expire

    def read(self):
        try:
            with open(self.filename) as fp:
                self.cache = yaml.load(fp) or {}
            self.lastsync = time.time()
        except (IOError, ValueError):
            pass

    def write(self, pop_key=None, reread=True):
        tmp_file = self.filename+'.tmp'
        with open(tmp_file, 'w') as fp:
            fcntl.lockf(fp, fcntl.LOCK_EX)
            if reread:
                tmp_cache = self.cache
                self.read()
                self.cache.update(tmp_cache)
            if pop_key:
                self.cache.pop(pop_key)
            yaml.dump(self.cache, fp,
                default_flow_style=False)
            fcntl.lockf(fp, fcntl.LOCK_UN)
        os.rename(tmp_file, self.filename)
        # self.lastsync = time.time()
