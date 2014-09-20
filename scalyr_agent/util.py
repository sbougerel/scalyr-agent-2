# Copyright 2014 Scalyr Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ------------------------------------------------------------------------
#
# author: Steven Czerwinski <czerwin@scalyr.com>

__author__ = 'czerwin@scalyr.com'

import base64
import os
import random
import threading
import time

from json_lib import parse, JsonParseException

# Use sha1 from hashlib (Python 2.5 or greater) otherwise fallback to the old sha module.
try:
    from hashlib import sha1
except ImportError:
    from sha import sha as sha1

# Try to use the UUID library if it is available (Python 2.5 or greater).
try:
    import uuid
except ImportError:
    uuid = None


def read_file_as_json(file_path):
    """Reads the entire file as a JSON value and return it.

    @param file_path: the path to the file to read
    @type file_path: str

    @return: The JSON value contained in the file.  This is typically a JsonObject, but could be primitive
        values such as int or str if that is all the file contains.

    @raise JsonReadFileException:  If there is an error reading the file.
    """
    f = None
    try:
        try:
            if not os.path.isfile(file_path):
                raise JsonReadFileException(file_path, 'The file does not exist.')
            if not os.access(file_path, os.R_OK):
                raise JsonReadFileException(file_path, 'The file is not readable.')
            f = open(file_path, 'r')
            data = f.read()
            return parse(data)
        except IOError, e:
            raise JsonReadFileException(file_path, 'Read error occurred: ' + str(e))
        except JsonParseException, e:
            raise JsonReadFileException(file_path, "JSON parsing error occurred: %s (line %i, byte position %i)" % (
                e.raw_message, e.line_number, e.position))
    finally:
        if f is not None:
            f.close()


def create_unique_id():
    """
    @return: A value that will be unique for all values generated by all machines.  The value
        is also encoded so that is safe to be used in a web URL.
    @rtype: str
    """
    if uuid is not None:
        # Here the uuid should be based on the mac of the machine.
        base_value = uuid.uuid1().bytes
    else:
        # Otherwise, get as good of a 16 byte random number as we can and prefix it with
        # the current time.
        try:
            base_value = os.urandom(16)
        except NotImplementedError:
            base_value = ''
            for i in range(16):
                base_value += random.randrange(256)
        base_value = str(time.time()) + base_value
    return base64.urlsafe_b64encode(sha1(base_value).digest())


def remove_newlines_and_truncate(input_string, char_limit):
    """Returns the input string but with all newlines removed and truncated.

    The newlines are replaced with spaces.  This is done both for carriage return and newline.

    Note, this does not add ellipses for the truncated text.

    @param input_string: The string to transform
    @param char_limit: The maximum number of characters the resulting string should be

    @type input_string: str
    @type char_limit: int

    @return:  The string with all newlines replaced with spaces and truncated.
    @rtype: str
    """
    return input_string.replace('\n', ' ').replace('\r', ' ')[0:char_limit]


def format_time(time_value):
    """Returns the time converted to a string in the common format used throughout the agent and in UTC.

    This should be used to make how we report times to the user consistent.

    If the time_value is None, then the returned value is 'Never'.  A time value of None usually indicates
    whatever is being timestamped has not occurred yet.

    @param time_value: The time in seconds past epoch (fractional is ok) or None
    @type time_value: float or None

    @return:  The time converted to a string, or 'Never' if time_value was None.
    @rtype: str
    """
    if time_value is None:
        return 'Never'
    else:
        return '%s UTC' % (time.asctime(time.gmtime(time_value)))


class JsonReadFileException(Exception):
    """Raised when a failure occurs when reading a file as a JSON object."""
    def __init__(self, file_path, message):
        self.file_path = file_path
        self.raw_message = message

        Exception.__init__(self, "Failed while reading file '%s': %s" % (file_path, message))


class RunState(object):
    """Keeps track of whether or not some process, such as the agent or a monitor, should be running.

    This abstraction can be used by multiple threads to efficiently monitor whether or not the process should
    still be running.  The expectation is that multiple threads will use this to attempt to quickly finish when
    the run state changes to false.
    """
    def __init__(self):
        """Creates a new instance of RunState which always is marked as running."""
        self.__condition = threading.Condition()
        self.__is_running = True
        # A list of functions to invoke when this instance becomes stopped.
        self.__on_stop_callbacks = []

    def is_running(self):
        """Returns True if the state is still set to running."""
        self.__condition.acquire()
        result = self.__is_running
        self.__condition.release()
        return result

    def sleep_but_awaken_if_stopped(self, timeout):
        """Sleeps for the specified amount of time, unless the run state changes to False, in which case the sleep is
        terminated as soon as possible.

        @param timeout: The number of seconds to sleep.

        @return: True if the run state has been set to stopped.
        """
        self.__condition.acquire()
        if not self.__is_running:
            return True

        self._wait_on_condition(timeout)
        result = not self.__is_running

        self.__condition.release()
        return result

    def stop(self):
        """Sets the run state to stopped.

        This also ensures that any threads currently sleeping in 'sleep_but_awaken_if_stopped' will be awoken.
        """
        callbacks_to_invoke = None
        self.__condition.acquire()
        if self.__is_running:
            callbacks_to_invoke = self.__on_stop_callbacks
            self.__on_stop_callbacks = []
            self.__is_running = False
            self.__condition.notifyAll()
        self.__condition.release()

        # Invoke the stopped callbacks.
        if callbacks_to_invoke is not None:
            for callback in callbacks_to_invoke:
                callback()

    def register_on_stop_callback(self, callback):
        """Adds a callback that will be invoked when this instance becomes stopped.

        The callback will be invoked as soon as possible after the instance has been stopped, but they are
        not guaranteed to be invoked before 'is_running' return False for another thread.

        @param callback: A function that does not take any arguments.
        """
        is_already_stopped = False
        self.__condition.acquire()
        if self.__is_running:
            self.__on_stop_callbacks.append(callback)
        else:
            is_already_stopped = True
        self.__condition.release()

        # Invoke the callback if we are already stopped.
        if is_already_stopped:
            callback()

    def _wait_on_condition(self, timeout):
        """Blocks for the condition to be signaled for the specified timeout.

        This is only broken out for testing purposes.

        @param timeout: The maximum number of seconds to block on the condition.
        """
        self.__condition.wait(timeout)


class FakeRunState(RunState):
    """A RunState subclass that does not actually sleep when sleep_but_awaken_if_stopped that can be used for tests.
    """
    def __init__(self):
        # The number of times this instance would have slept.
        self.__total_times_slept = 0
        RunState.__init__(self)

    def _wait_on_condition(self, timeout):
        self.__total_times_slept += 1
        return

    @property
    def total_times_slept(self):
        return self.__total_times_slept


class StoppableThread(threading.Thread):
    """A slight extension of a thread that uses a RunState instance to track if it should still be running.

    This class must be extended to actually perform work.  It is expected that the derived run method
    invokes '_run_state.is_stopped' to determine when the thread has been stopped.
    """
    def __init__(self, name=None, target=None):
        threading.Thread.__init__(self, name=name, target=target)

        # Tracks whether or not the thread should still be running.
        self._run_state = RunState()

    def stop(self, wait_on_join=True, join_timeout=5):
        """Stops the thread from running.

        By default, this will also block until the thread has completed (by performing a join).

        @param wait_on_join: If True, will block on a join of this thread.
        @param join_timeout: The maximum number of seconds to block for the join.
        """
        self._run_state.stop()
        if wait_on_join:
            self.join(join_timeout)


class RateLimiter(object):
    """An abstraction that can be used to enforce some sort of rate limit, expressed as a maximum number
    of bytes to be consumed over a period of time.

    It uses a leaky-bucket implementation.  In this approach, the rate limit is modeled as a bucket
    with a hole in it.  The bucket has a maximum size (expressed in bytes) and a fill rate (expressed in bytes
    per second).  Whenever there is an operation that would consume bytes, this abstraction checks to see if
    there are at least X number bytes available in the bucket.  If so, X is deducted from the bucket's contents.
    Otherwise, the operation is rejected.  The bucket is gradually refilled at the fill rate, but the contents
    of the bucket will never exceeded the maximum bucket size.
    """
    def __init__(self, bucket_size, bucket_fill_rate, current_time=None):
        """Creates a new bucket.

          @param bucket_size: The bucket size, which should be the maximum number of bytes that can be consumed
              in a burst.
          @param bucket_fill_rate: The fill rate, expressed as bytes per second.  This should correspond to the
              maximum desired steady state rate limit.
          @param current_time:   If not none, the value to use as the current time, expressed in seconds past epoch.
              This is used in testing.
        """
        self.__bucket_contents = bucket_size
        self.__bucket_size = bucket_size
        self.__bucket_fill_rate = bucket_fill_rate

        if current_time is None:
            current_time = time.time()

        self.__last_bucket_fill_time = current_time

    def charge_if_available(self, num_bytes, current_time=None):
        """Returns true and updates the rate limit count if there are enough bytes available for an operation
        costing num_bytes.

        @param num_bytes: The number of bytes to consume from the rate limit.
        @param current_time: If not none, the value to use as the current time, expressed in seconds past epoch. This
            is used in testing.

        @return: True if there are enough room in the rate limit to allow the operation.
        """
        if current_time is None:
            current_time = time.time()

        fill_amount = (current_time - self.__last_bucket_fill_time) * self.__bucket_fill_rate

        self.__bucket_contents = min(self.__bucket_size, self.__bucket_contents + fill_amount)
        self.__last_bucket_fill_time = current_time

        if num_bytes <= self.__bucket_contents:
            self.__bucket_contents -= num_bytes
            return True

        return False