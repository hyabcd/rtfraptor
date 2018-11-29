# -*- coding: utf-8 -*-

import hashlib
import logging
from time import time
from oletools.common.clsid import KNOWN_CLSIDS
from winappdbg import Debug, EventHandler, System, win32
from winappdbg.win32 import PVOID
from .utils import bytes_to_clsid


class CustomEventHandler(EventHandler):

    # The list of modules and functions we want to hook.
    _hooks = {
        "ole32.dll": {
            "OleLoad": {'args': 4, 'hook': '_hook_load'},
            "OleConvertOLESTREAMToIStorage": {'args': 3, 'hook': '_hook_data_conversion'},
            "OleGetAutoConvert": {'args': 2, 'hook': '_hook_guid_conversion'},
        }
    }

    # The memory location of the most recent pStg object is stored here,
    # enabling tracking of objects between OleLoad and OleGetAutoConvert
    # (which will also be called from elsewhere).  We assume there can
    # be no irrelevant calls to OleGetAutoConvert once OleLoad is called.
    _last_pstg = None

    def __init__(self, logger):
        super(CustomEventHandler, self).__init__()
        self._log = logger
        self.objects = {}

    def _hook_load(self, _event, _ra, pstg, _riid, _pclientsite, _ppvobjx):
        """
        Event hook for OleLoad.  This function simply saves the pStg address
        allowing tracking between different calls.

        A nicer solution would be to identify how to extract the class ID
        directly from the pStg object, which implements IStorage.  The later
        hook could then be removed.
        """
        self._last_pstg = pstg

    def _hook_guid_conversion(self, event, _ra, clsid_old, _pclsid_new):
        process = event.get_process()
        clsid_bytes = process.read(clsid_old, 16)
        clsid = bytes_to_clsid(clsid_bytes)

        # This hook will also be called from other places.  We reduce the
        # risk of false positives by only logging details if OleLoad has
        # just been called.
        if self._last_pstg:
            info = self.objects[self._last_pstg]
            info['class_id'] = clsid

            if clsid in KNOWN_CLSIDS:
                self._log.warning("Suspicious OLE object loaded, class id %s (%s)", clsid, KNOWN_CLSIDS[clsid])
                self._log.warning("Object size is %d, SHA256 is %s", info['size'], info['sha256'])
                info['description'] = KNOWN_CLSIDS[clsid]
            else:
                self._log.warning("Object found but not on blacklist %s", clsid)
                info['description'] = "Unknown (not blacklisted)"

            self._last_pstg = None

    def _hook_data_conversion(self, event, ra, lpolestream, pstg, ptd):
        info = {}

        process = event.get_process()
        hasher = hashlib.sha256()

        # Follow the lpOleStream parameter
        # TODO: Test this on 64-bit Office where pointer sizes will be different
        #       and identify how this affects offset of length
        data_addr = process.peek_pointer(process.peek_pointer(lpolestream + 8))
        info['size'] = process.peek_dword(lpolestream + 12)
        data = process.read(data_addr, info['size'])

        # Save the SHA256 of the object
        hasher.update(data)
        info['sha256'] = hasher.hexdigest()

        # TODO: Allow the target directory to be set for saved data
        with open(info['sha256'], 'wb') as fh:
            fh.write(data)

        self.objects[pstg] = info

        self._log.debug("Dumping data from 0x%08x, destination 0x%08x, length %d, hash %s", data_addr, pstg, info['size'], info['sha256'])

    def _apply_hooks(self, event, hooks):
        """
        Add hooks to the specified module.
        """
        module = event.get_module()
        pid = event.get_pid()

        for func, options in hooks.items():
            address = module.resolve(func)
            if address:
                self._log.debug("Address of %s is 0x%08x", func, address)
                signature = (PVOID,) * options['args']
                callback = getattr(self, options['hook'])
                event.debug.hook_function(pid, address, callback, signature=signature)
            else:
                self._log.error("Could not find function %s to hook", func)
                return False

        return True

    def load_dll(self, event):
        """
        This callback occurs when the debugged process loads a new module (DLL)
        into memory.  At this point we insert hooks (breakpoints) that can
        inspect relevant functions as they are called.
        """
        module = event.get_module()

        for dll, hooks in self._hooks.items():
            if module.match_name(dll):
                self._log.debug("Process loaded %s, hooks exist for this module", module.get_name())
                self._apply_hooks(event, hooks)
                # TODO: Check if the above was successful and die if not


def office_debugger(executable, target_file, timeout=10, save_objs=True):

    # TODO: Ensure executable is executable and target_file is readable

    opts = [executable, target_file]

    logger = logging.getLogger(__name__)
    handler = CustomEventHandler(logger)

    with Debug(handler, bKillOnExit=True) as debug:

        # Ensure the target application dies if the debugger is killed
        System.set_kill_on_exit_mode(True)
        max_time = time() + timeout

        try:
            debug.execv(opts)
        except WindowsError:
            logger.error("Could not run Office application, check it is 32-bit")

        try:
            while debug.get_debugee_count() > 0 and time() < max_time:
                try:
                    # Get the next debug event.
                    debug.wait(1000)

                except WindowsError, e:
                    if e.winerror in (win32.ERROR_SEM_TIMEOUT,
                                      win32.WAIT_TIMEOUT):
                        continue
                    raise

                # Dispatch the event and continue execution.
                try:
                    debug.dispatch()
                finally:
                    debug.cont()
        finally:
            debug.stop()
