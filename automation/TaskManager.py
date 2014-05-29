from BrowserManager import Browser
from DataAggregator import DataAggregator
from SocketInterface import clientsocket

from multiprocessing import Process, Queue
from sqlite3 import OperationalError
import copy
import threading
import os
import sqlite3
import subprocess
import time


class TaskManager:
    """
    User-facing API for running browser automation
    The TaskManager runs two sub-processes - WebManger for browser actions/instrumentation and DataAggregator for DB I/O
    General paradigm is for the TaskManager to send commands and wait for response and/or restart workers if necessary
    Compared to light wrapper around WebDriver, provides robustness and timeout functionality

    <db_path> is the absolute path of the crawl DB (which may not yet exist)
    <browser_params> is a list of (or single) dictionaries that specify preferences for browsers to instantiate
    <num_browsers> is the number of browsers to instantiate
    <task_description> is an optional description string for a particular crawl (primarily for logging)
    """

    def __init__(self, db_path, browser_params, num_browsers, task_description=None):
        # sets up the information needed to write to the database
        self.desc = task_description
        self.db_path = db_path

        # sets up the crawl data database
        self.db = sqlite3.connect(db_path)
        with open(os.path.join(os.path.dirname(__file__), 'schema.sql'), 'r') as f:
            self.db.executescript(f.read())
        
        # prepares browser settings
        self.num_browsers = num_browsers
        # special case: for singleton dictionary, we perform deep copies so that number of dicts is <num_browsers>
        if type(browser_params) is not list:
            browser_params = [copy.deepcopy(browser_params) for i in xrange(0, num_browsers)]

        if len(browser_params) != num_browsers:
            raise Exception("Number of browser parameter dictionaries is not the same as <num_browsers>")

        # sets up the DataAggregator + associated queues
        self.aggregator_status_queue = None  # queue used for sending graceful KILL command to DataAggregator
        self.data_aggregator = self.launch_data_aggregator()
        self.aggregator_address = self.aggregator_status_queue.get()  # socket location: (address, port)

        # open client socket
        self.sock = clientsocket()
        self.sock.connect(self.aggregator_address[0], self.aggregator_address[1])

        # update task table
        cur = self.db.cursor()
        cur.execute("INSERT INTO task (description) VALUES (?)", (self.desc,))
        self.db.commit()
        self.task_id = cur.lastrowid
        
        # sets up the BrowserManager(s) + associated queues
        self.browsers = self.initialize_browsers(browser_params)  # List of the Browser(s)
        
        # open client socket
        self.sock = clientsocket()
        self.sock.connect(self.aggregator_address[0], self.aggregator_address[1])

    def initialize_browsers(self, browser_params):
        """ initialize the browsers, each with a unique set of parameters """
        browsers = list()
        for i in xrange(self.num_browsers):
            # update crawl table
            # TO DO: update DB with browser.browser_settings for each browser manager initialized

            cur = self.db.cursor()
            query_successful = False
            crawl_id = -1
            while not query_successful:
                try:
                    cur.execute("INSERT INTO crawl (task_id, profile, browser, headless, proxy, debugging, "
                                "timeout, disable_flash) VALUES (?,?,?,?,?,?,?,?)",
                                (self.task_id, browser_params[i]['profile_tar'], browser_params[i]['browser'],
                                 browser_params[i]['headless'], browser_params[i]['proxy'],
                                 browser_params[i]['debugging'], browser_params[i]['timeout'],
                                 browser_params[i]['disable_flash']))
                    self.db.commit()
                    crawl_id = cur.lastrowid
                    query_successful = True
                except OperationalError:
                    time.sleep(2)
                    pass

            browser_params[i]['crawl_id'] = crawl_id
            browser_params[i]['aggregator_address'] = self.aggregator_address
            browsers.append(Browser(browser_params[i]))
            # Update our DB with the random browser settings
            # These are found within the scope of each instance of Browser in the browsers list
            for item in browsers:
                if not item.browser_settings['extensions']:
                    extensions = 'None'
                else:
                    extensions = ','.join(item.browser_settings['extensions'])
                screen_res = str(item.browser_settings['screen_res'])
                ua_string = str(item.browser_settings['ua_string'])
                self.sock.send(("UPDATE crawl SET extensions = ?, screen_res = ?, ua_string = ? \
                                 WHERE crawl_id = ?", (extensions, screen_res, ua_string, item.crawl_id)))
        return browsers

    def launch_data_aggregator(self):
        """ sets up the DataAggregator (Must be launched prior to BrowserManager) """
        self.aggregator_status_queue = Queue()
        aggregator = Process(target=DataAggregator.DataAggregator,
                             args=(self.db_path, self.aggregator_status_queue, ))
        aggregator.start()
        return aggregator

    def kill_data_aggregator(self):
        """ terminates a DataAggregator with a graceful KILL COMMAND """
        self.aggregator_status_queue.put("DIE")
        self.data_aggregator.join()

    def close(self):
        """
        wait for all child processes to finish executing commands and closes everything
        Update crawl table for each browser (crawl_id) to show successful finish
        """

        for browser in self.browsers:
            if browser.command_thread is not None:
                browser.command_thread.join()
            browser.kill_browser_manager()
            if browser.current_profile_path is not None:
                subprocess.call(["rm", "-r", browser.current_profile_path])
            self.sock.send(("UPDATE crawl SET finished = 1 WHERE crawl_id = ?",
                            (browser.crawl_id,)))
        self.db.close()  # close db connection
        self.sock.close()  # close socket to data aggregator
        self.kill_data_aggregator() 

    # CRAWLER COMMAND CODE

    def distribute_command(self, command, index=None, timeout=None):
        """
        parses command type and issues command(s) to the proper browser
        <index> specifies the type of command this is:
        = None  -> first come, first serve
        =  #     -> index of browser to send command to
        = *     -> sends command to all browsers
        = **    -> sends command to all browsers (synchronized)
        """
        if index is None:
            #send to first browser available
            command_executed = False
            while True:
                for browser in self.browsers:
                    if browser.ready():
                        self.start_thread(browser, command, timeout)
                        command_executed = True
                        break
                if command_executed:
                    break
                time.sleep(0.01)

        elif 0 <= index < len(self.browsers):
            #send the command to this specific browser
            while True:
                if self.browsers[index].ready():
                    self.start_thread(self.browsers[index], command, timeout)
                    break
                time.sleep(0.01)
        elif index == '*':
            #send the command to all browsers
            command_executed = [False] * len(self.browsers)
            while False in command_executed:
                for i in xrange(len(self.browsers)):
                    if self.browsers[i].ready() and not command_executed[i]:
                        self.start_thread(self.browsers[i], command, timeout)
                        command_executed[i] = True
                time.sleep(0.01)
        elif index == '**':
            #send the command to all browsers and sync it
            condition = threading.Condition()  # Used to block threads until ready
            command_executed = [False] * len(self.browsers)
            while False in command_executed:
                for i in xrange(len(self.browsers)):
                    if self.browsers[i].ready() and not command_executed[i]:
                        self.start_thread(self.browsers[i], command, timeout, condition)
                        command_executed[i] = True
                time.sleep(0.01)
            with condition:
                condition.notifyAll()  # All browsers loaded, tell them to start
        else:
            #not a supported command
            print "Command index type is not supported or out of range"

    def start_thread(self, browser, command, timeout, condition=None):
        """  starts the command execution thread """
        args = (browser, command, timeout, condition)
        thread = threading.Thread(target=self.issue_command, args=args)
        browser.command_thread = thread
        thread.start()

    def issue_command(self, browser, command, timeout=None, condition=None):
        """
        sends command tuple to the BrowserManager
        <timeout> gives the option to override default timeout
        """
        browser.is_fresh = False  # since we are issuing a command, the BrowserManager is no longer a fresh instance
        timeout = browser.timeout if timeout is None else timeout  # allows user to overwrite timeout
        # if this is a synced call, block on condition
        if condition is not None:
            with condition:
                condition.wait()

        # passes off command and waits for a success (or failure signal)
        browser.command_queue.put(command)
        command_succeeded = False
        is_timeout = True

        # repeatedly waits for a reply from the BrowserManager; if fails/times-out => restart
        for i in xrange(0, int(timeout) * 1000):
            if browser.status_queue.empty():  # nothing to do -> sleep so as to not peg CPU
                time.sleep(0.001)
                continue

            # received reply from BrowserManager, either success signal or failure notice
            status = browser.status_queue.get()
            if status == "OK":
                #print str(browser.crawl_id) + " " + "got OK"
                command_succeeded = True
                self.sock.send(("INSERT INTO CrawlHistory (crawl_id, command, arguments, bool_success)"
                                " VALUES (?,?,?,?)", (browser.crawl_id, command[0], command[1], True)))
            is_timeout = False
            break
        if not command_succeeded:  # reboots since BrowserManager is down
            if is_timeout:
                print "TIMEOUT, KILLING BROWSER MANAGER"
            self.sock.send(("INSERT INTO CrawlHistory (crawl_id, command, arguments, bool_success) VALUES (?,?,?,?)",
                            (browser.crawl_id, command[0], command[1], False)))
            browser.restart_browser_manager()

    # DEFINITIONS OF HIGH LEVEL COMMANDS

    def get(self, url, index=None, overwrite_timeout=None):
        """ goes to a url """
        self.distribute_command(('GET', url), index, overwrite_timeout)

    def dump_storage_vectors(self, url, start_time, index=None, overwrite_timeout=None):
        """ dumps the local storage vectors (flash, localStorage, cookies) to db """
        self.distribute_command(('DUMP_STORAGE_VECTORS', url, start_time), index, overwrite_timeout)

    def dump_profile(self, dump_folder, close_webdriver=False, index=None, overwrite_timeout=None):
        """ dumps from the profile path to a given file (absolute path) """
        self.distribute_command(('DUMP_PROF', dump_folder, close_webdriver), index, overwrite_timeout)
