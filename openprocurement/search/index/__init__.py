# -*- coding: utf-8 -*-
import os, sys
from time import time, sleep
from logging import getLogger
from multiprocessing import Process

logger = getLogger(__name__)


class BaseIndex(object):
    """Search Index Interface
    """
    config = {
        'async_reindex': 0,
        'ignore_errors': 0,
        'index_speed': 100,
        'error_wait': 10,
    }

    allow_async_reindex = False
    magic_exit_code = 84

    def __init__(self, engine, source, config={}):
        assert(self.__index_name__)
        if config:
            self.config.update(config)
            self.config['index_speed'] = float(self.config['index_speed'])
        rename_index = 'rename_' + self.__index_name__
        if rename_index in self.config:
            self.__index_name__ = self.config[rename_index]
        self.source = source
        self.engine = engine
        self.engine.add_index(self)
        self.reindex_process = None
        self.after_init()

    def __del__(self):
        self.stop_childs()

    def __str__(self):
        return self.__index_name__

    def __repr__(self):
        return self.__index_name__

    @property
    def current_index(self):
        key = self.__index_name__
        return self.engine.get_index(key)

    def index_age(self, name=None):
        if not name:
            name = self.current_index
        if not name:
            return time()
        prefix, suffix = name.rsplit('_', 1)
        return int(time() - int(suffix))

    def after_init(self):
        pass

    def need_reindex(self):
        return not self.current_index

    def create_index(self, name):
        return

    def finish_index(self, name):
        return

    def new_index(self, is_async=False):
        index_key = self.__index_name__
        index_key_next = "{}.next".format(index_key)
        # try restore last index (in case of crash)
        name = self.engine.get_index(index_key_next)
        current_index = self.current_index
        if current_index and name <= current_index:
            name = None
        if name and self.index_age(name) > 2*86400:
            name = None
        if not name:
            name = "{}_{}".format(index_key, int(time()))
            self.create_index(name)
            self.engine.set_index(index_key_next, name)
        else:
            logger.info("Use already created index %s", name)
        # check current not same to new in async
        if is_async and name == current_index:
            raise RuntimeError("same index in async mode")
        # also set current if empty (don't in async mode)
        # current = self.engine.get_index(index_key)
        # if not current and not is_async:
        #     self.engine.set_index(index_key, name)
        return name

    def delete_index(self, name):
        index_key = self.__index_name__
        index_key_prev = "{}.prev".format(index_key)
        self.engine.set_index(index_key_prev, name)

    def set_current(self, name):
        index_key = self.__index_name__
        old_index = self.current_index
        if name != old_index:
            logger.info("Change current index %s -> %s",
                        old_index, name)
            self.engine.set_index(index_key, name)
            assert(self.current_index == name)
            if old_index:
                self.delete_index(old_index)
        # remove index.next key
        index_key_next = "{}.next".format(index_key)
        if self.engine.get_index(index_key_next) == name:
            self.engine.set_index(index_key_next, '')

    def test_exists(self, index_name, info):
        return self.engine.test_exists(index_name, info)

    def test_noindex(self, item):
        return False

    def before_index_item(self, item):
        return True

    def indexing_stat(self, index_name, fetched, indexed,
                      iter_count, last_date):
        last_date = last_date or ""
        pause = 1.0 * iter_count / self.config['index_speed']
        logger.info(
            "[%s] Fetched %d indexed %d last %s wait %1.1fs",
            index_name, fetched, indexed, last_date, pause)
        if pause > 0.01:
            sleep(pause)

    def index_item(self, index_name, item):
        if self.test_noindex(item):
            if self.engine.debug:
                logger.debug("[%s] Noindex %s %s", index_name,
                             item['data']['id'], 
                             item['data'].get('tenderID', ''))
            return None

        self.before_index_item(item)

        for retry in range(5):
            try:
                return self.engine.index_item(index_name, item)
            except Exception as e:
                if retry > 3:
                    raise
                logger.exception(u"[%s] Can't index %s: %s",
                                 index_name, item['meta'], e)
                if self.config['ignore_errors']:
                    return None

            sleep(int(self.config['error_wait']))

            if self.test_exists(index_name, item['meta']):
                return None

    def index_source(self, index_name=None, reset=False):
        if reset:
            self.source.reset()

        if not index_name:
            index_name = self.current_index

        if not index_name and self.engine.slave_mode:
            if not self.engine.heartbeat(self.source):
                return
            index_name = self.current_index

        if not index_name:
            if not self.reindex_process:
                logger.warning("No current index for %s", repr(self))
            return

        index_count = 0
        total_count = 0
        # heartbeat return False in slave mode if master is ok
        # heartbeat always True in master mode
        while self.engine.heartbeat(self.source):
            info = {}
            iter_count = 0
            for info in self.source.items():
                if self.engine.should_exit:
                    break
                if not self.test_exists(index_name, info):
                    item = self.source.get(info)
                    if self.index_item(index_name, item):
                        index_count += 1
                iter_count += 1
                total_count += 1
                # update heartbeat for long indexing
                if iter_count >= 100:
                    self.indexing_stat(
                        index_name, total_count, index_count,
                        iter_count, info.get('dateModified'))
                    self.engine.heartbeat(self.source)
                    iter_count = 0
            # break if should exit
            if self.engine.should_exit:
                logger.warning("Should exit")
                break
            # break if nothing iterated
            if iter_count > 0:
                self.indexing_stat(
                    index_name, total_count, index_count,
                    iter_count, info.get('dateModified'))
            elif getattr(self.source, 'last_skipped', None):
                logger.info(
                    "[%s] Fetched %d, last_skipped %s",
                    index_name, total_count, self.source.last_skipped)
            elif not info:
                break

        return index_count

    def stop_childs(self):
        if not self.reindex_process:
            return
        if self.reindex_process.pid == os.getpid():
            return
        logger.info("Terminate subprocess %s pid %d", 
            self.reindex_process.name, self.reindex_process.pid)
        try:
            self.reindex_process.terminate()
            self.reindex_process = None
        except (AttributeError, OSError):
            pass

    def check_subprocess(self):
        if self.reindex_process:
            self.reindex_process.join(1)
        if self.reindex_process.is_alive():
            return
        if self.reindex_process.exitcode == self.magic_exit_code:
            logger.info("Reindex subprocess success, reset source")
            self.source.reset()
        else:
            logger.error("Reindex subprocess fail, exitcode = %d",
                self.reindex_process.exitcode)
        # close process
        self.reindex_process = None

    def do_reindex(self, is_async=False):
        if is_async:
            logger.info("*** Start Reindex-%s in subprocess",
                self.__index_name__)
            # reconnect elatic and prevent future stop_childs
            self.engine.start_subprocess()
        index_name = self.new_index(is_async)
        logger.info("Starting full reindex, new index %s", index_name)
        self.index_source(index_name, reset=True)
        self.finish_index(index_name)
        self.set_current(index_name)
        logger.info("Finish full reindex, new index %s", index_name)
        # exit with specific code to signal master process reset source
        if is_async:
            logger.info("*** Exit subprocess")
            sys.exit(self.magic_exit_code)

    def reindex(self):
        # check for reindex in sync mode
        if not self.allow_async_reindex or not self.config['async_reindex']:
            return self.do_reindex()

        # check reindex process is alive
        if self.reindex_process:
            if self.reindex_process.is_alive():
                return True

        # start new reindex process
        proc_name = "Reindex-%s" % self.__index_name__
        self.reindex_process = Process(target=self.do_reindex, 
            name=proc_name, args=(True,))
        self.reindex_process.start()
        # wait for child
        for i in range(30):
            if self.reindex_process.is_alive():
                break
            sleep(1)
        if self.reindex_process.is_alive():
            logger.info("Subprocess started %s pid %d", 
                self.reindex_process.name, self.reindex_process.pid)
            sleep(2)
        else:
            logger.error("Can't start subprocess")

    def process(self, allow_reindex=True):
        if self.reindex_process:
            self.check_subprocess()

        if self.need_reindex() and allow_reindex:
            self.reindex()

        return self.index_source()
