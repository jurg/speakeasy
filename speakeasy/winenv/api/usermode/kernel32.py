# Copyright (C) 2020 FireEye, Inc. All Rights Reserved.

import os
import ntpath
import fnmatch
import datetime
import ctypes as ct

import speakeasy.winenv.arch as e_arch
import speakeasy.winenv.defs.nt.ddk as ddk
import speakeasy.common as common
from speakeasy.errors import ApiEmuError
import speakeasy.winenv.defs.windows.windows as windefs
import speakeasy.winenv.defs.windows.kernel32 as k32types

from .. import api

PAGE_SIZE = 0x1000


class Kernel32(api.ApiHandler):
    """
    Implements exported functions from kernel32.dll
    """
    name = 'kernel32'
    apihook = api.ApiHandler.apihook
    impdata = api.ApiHandler.impdata

    def __init__(self, emu):

        super(Kernel32, self).__init__(emu)

        self.funcs = {}
        self.data = {}

        self.heaps = []
        self.curr_handle = 0x1800
        self.find_files = {}
        self.snapshots = {}
        self.find_resources = {}
        self.tick_counter = 86400000  # 1 day in millisecs
        self.perf_counter = 0x5fd27d571f

        self.command_lines = [None] * 3
        self.startup_info = {}

        self.k32types = k32types

        super(Kernel32, self).__get_hook_attrs__(self)

    def get_handle(self):
        self.curr_handle += 4
        return self.curr_handle

    def create_heap(self, emu):
        heap = self.mem_alloc(20, tag='emu.process_heap')

        self.heaps.append(heap)
        return heap

    def win_perms_to_emu_perms(self, win_perms):
        new = 0
        if (win_perms & windefs.PAGE_EXECUTE_READWRITE):
            new = common.PERM_MEM_RWX
        elif (win_perms & windefs.PAGE_NOACCESS):
            new = common.PERM_MEM_NONE
        else:
            if (win_perms & windefs.PAGE_EXECUTE or
                    win_perms & windefs.PAGE_EXECUTE_READ):
                new |= common.PERM_MEM_EXEC
            if (win_perms & windefs.PAGE_EXECUTE_READ or
                win_perms & windefs.PAGE_READONLY or
                win_perms & windefs.PAGE_READWRITE): # noqa
                new |= common.PERM_MEM_READ
            if (win_perms & windefs.PAGE_READWRITE):
                new |= common.PERM_MEM_WRITE
        return new

    def emu_perms_to_win_perms(self, emu_perms):
        new = 0
        if (emu_perms & common.PERM_MEM_RWX):
            new = windefs.PAGE_EXECUTE_READWRITE
        elif (emu_perms & common.PERM_MEM_NONE):
            new = windefs.PAGE_NOACCESS
        else:
            if (emu_perms & common.PERM_MEM_EXEC):
                new |= windefs.PAGE_EXECUTE
            if (emu_perms & common.PERM_MEM_READ): # noqa
                new |= windefs.PAGE_READONLY
            if (emu_perms & common.PERM_MEM_WRITE):
                new |= windefs.PAGE_READWRITE
        return new

    def normalize_dll_name(self, name):
        ret = name
        if (name.lower().startswith('api-ms-win-crt') or name.lower().startswith('vcruntime')):
            ret = 'msvcrt'
        if (name.lower().startswith('api-ms-win-core')):
            ret = 'kernel32'
        return ret

    def normalize_res_identifier(self, emu, cw, val):
        mask = (16 ** (emu.get_ptr_size() // 2) - 1) << 16
        if val & mask:  # not an INTRESOURCE
            name = emu.read_mem_string(val, cw)
            if name[0] == "#":
                try:
                    name = int(name[1:])
                except Exception:
                    return 0
        else:
            name = val

        return name

    def find_resource(self, pe, name, type_):
        # find type
        resource_type = None

        if not hasattr(pe, 'DIRECTORY_ENTRY_RESOURCE'):
            return None

        for restype in pe.DIRECTORY_ENTRY_RESOURCE.entries:
            if type(type_) is str and restype.name is not None:
                if type_ == restype.name.decode('utf8'):
                    resource_type = restype
                    break
            elif type(type_) is int and hasattr(restype.struct, 'Id'):
                if type_ == restype.struct.Id:
                    resource_type = restype
                    break

        if not resource_type:
            return None

        if not hasattr(resource_type, 'directory'):
            return None

        for resource_id in resource_type.directory.entries:
            if type(name) is str and resource_id.name is not None:
                if name == resource_id.name.decode('utf8'):
                    return resource_id.directory.entries[0]
            elif type(name) is int and hasattr(resource_id.struct, 'Id'):
                if name == resource_id.struct.Id:
                    return resource_id.directory.entries[0]

        return None

    @apihook('OutputDebugString', argc=1)
    def OutputDebugString(self, emu, argv, ctx={}):
        '''
        void OutputDebugStringA(
            LPCSTR lpOutputString
        );
        '''
        _str, = argv
        cw = self.get_char_width(ctx)
        argv[0] = self.read_mem_string(_str, cw)

    @apihook('GetProcessHeap', argc=0)
    def GetProcessHeap(self, emu, argv, ctx={}):
        '''
        HANDLE GetProcessHeap();
        '''

        if not self.heaps:
            heap = self.create_heap(emu)
        else:
            heap = self.heaps[0]
        return heap

    @apihook('DisableThreadLibraryCalls', argc=1)
    def DisableThreadLibraryCalls(self, emu, argv, ctx={}):
        '''
        BOOL DisableThreadLibraryCalls(
            HMODULE hLibModule
        );
        '''

        hLibModule, = argv

        return True

    @apihook('CreateMutex', argc=3)
    def CreateMutex(self, emu, argv, ctx={}):
        '''
        HANDLE CreateMutex(
            LPSECURITY_ATTRIBUTES lpMutexAttributes,
            BOOL                  bInitialOwner,
            LPCSTR                lpName
        );
        '''

        attrs, owner, name = argv

        cw = self.get_char_width(ctx)

        if name:
            name = self.read_mem_string(name, cw)

        obj = self.get_object_from_name(name)

        hnd = 0
        if obj:
            hnd = emu.get_object_handle(obj)
            emu.set_last_error(windefs.ERROR_ALREADY_EXISTS)
        else:
            emu.set_last_error(windefs.ERROR_SUCCESS)
            hnd, evt = emu.create_mutant(name)

        argv[2] = name
        return hnd

    @apihook('LoadLibrary', argc=1)
    def LoadLibrary(self, emu, argv, ctx={}):
        '''HMODULE LoadLibrary(
          LPTSTR lpLibFileName
        );'''

        lib_name, = argv
        hmod = windefs.NULL

        cw = self.get_char_width(ctx)
        req_lib = self.read_mem_string(lib_name, cw)
        lib = self.normalize_dll_name(req_lib)

        hmod = emu.load_library(lib)
        argv[0] = req_lib

        return hmod

    @apihook('CreateToolhelp32Snapshot', argc=2)
    def CreateToolhelp32Snapshot(self, emu, argv, ctx={}):
        '''
        HANDLE CreateToolhelp32Snapshot(
            DWORD dwFlags,
            DWORD th32ProcessID
        );
        '''

        dwFlags, th32ProcessID, = argv

        if k32types.TH32CS_SNAPPROCESS == dwFlags:
            hnd = self.get_handle()
            index = 0
            self.snapshots.update({hnd: [index, emu.get_processes()]})
        else:
            raise ApiEmuError('Unsupported snapshot type: 0x%x' % (dwFlags))

        cap_def = k32types.get_flag_defines(dwFlags, 'TH32CS')
        if cap_def:
            cap_def = '|'.join(cap_def)
            argv[0] = cap_def

        return hnd

    @apihook('Process32First', argc=2)
    def Process32First(self, emu, argv, ctx={}):
        '''
        BOOL Process32First(
            HANDLE           hSnapshot,
            LPPROCESSENTRY32 lppe
        );
        '''

        hSnapshot, pe32, = argv
        rv = False

        snap = self.snapshots.get(hSnapshot)
        if not snap or not pe32:
            return rv

        # Reset the handle index
        snap[0] = 1
        proc = snap[1][0]

        try:
            cw = self.get_char_width(ctx)
        except Exception:
            cw = 1

        pe = self.k32types.PROCESSENTRY32(emu.get_ptr_size(), cw)
        data = self.mem_cast(pe, pe32)
        pe.th32ProcessID = proc.get_pid()
        if cw == 2:
            pe.szExeFile = proc.image.encode('utf-16le') + b'\x00'
        else:
            pe.szExeFile = proc.image.encode('utf-8') + b'\x00'

        self.mem_write(pe32, self.get_bytes(data))
        rv = True
        return rv

    @apihook('Process32Next', argc=2)
    def Process32Next(self, emu, argv, ctx={}):
        '''
        BOOL Process32Next(
            HANDLE           hSnapshot,
            LPPROCESSENTRY32 lppe
        );
        '''

        hSnapshot, pe32, = argv
        rv = False

        snap = self.snapshots.get(hSnapshot)
        if not snap or not pe32:
            return rv

        index = snap[0]
        snap[0] += 1
        if index >= len(snap[1]):
            return rv
        proc = snap[1][index]

        try:
            cw = self.get_char_width(ctx)
        except Exception:
            cw = 1

        pe = self.k32types.PROCESSENTRY32(emu.get_ptr_size(), cw)
        data = self.mem_cast(pe, pe32)
        pe.th32ProcessID = proc.get_pid()
        if cw == 2:
            pe.szExeFile = proc.image.encode('utf-16le') + b'\x00'
        else:
            pe.szExeFile = proc.image.encode('utf-8') + b'\x00'

        self.mem_write(pe32, self.get_bytes(data))
        rv = True
        return rv

    @apihook('OpenProcess', argc=3)
    def OpenProcess(self, emu, argv, ctx={}):
        '''
        HANDLE OpenProcess(
            DWORD dwDesiredAccess,
            BOOL  bInheritHandle,
            DWORD dwProcessId
        );
        '''

        access, inherit, pid = argv

        hnd = 0
        proc = emu.get_object_from_id(pid)
        if proc:
            hnd = emu.get_object_handle(proc)
        else:
            emu.set_last_error(windefs.ERROR_INVALID_PARAMETER)
        return hnd

    @apihook('OpenMutex', argc=3)
    def OpenMutex(self, emu, argv, ctx={}):
        '''
        HANDLE OpenMutex(
            DWORD   dwDesiredAccess,
            BOOL    bInheritHandle,
            LPCWSTR lpName
        );
        '''

        access, inherit, name = argv

        cw = self.get_char_width(ctx)

        if name:
            obj_name = self.read_mem_string(name, cw)
            argv[2] = obj_name

        obj = self.get_object_from_name(obj_name)

        hnd = 0
        if obj:
            hnd = emu.get_object_handle(obj)
        else:
            emu.set_last_error(windefs.ERROR_INVALID_PARAMETER)
        return hnd

    @apihook('TerminateProcess', argc=2)
    def TerminateProcess(self, emu, argv, ctx={}):
        '''
        BOOL TerminateProcess(
            HANDLE hProcess,
            UINT   uExitCode
        );
        '''

        hProcess, uExitCode = argv
        rv = False

        proc = emu.get_object_from_handle(hProcess)
        if not proc:
            return rv

        emu.kill_process(proc)
        rv = True

    @apihook('FreeLibraryAndExitThread', argc=2)
    def FreeLibraryAndExitThread(self, emu, argv, ctx={}):
        '''
        void FreeLibraryAndExitThread(
            HMODULE hLibModule,
            DWORD   dwExitCode
        );
        '''
        emu.exit_process()
        return

    @apihook('WinExec', argc=2)
    def WinExec(self, emu, argv, ctx={}):
        '''
        UINT WinExec(
            LPCSTR lpCmdLine,
            UINT   uCmdShow
        );
        '''

        lpCmdLine, uCmdShow = argv
        rv = 1

        if lpCmdLine:
            cmd = self.read_mem_string(lpCmdLine, 1)
            argv[0] = cmd
            app = cmd.split()[0]
            proc = emu.create_process(path=app, cmdline=cmd)
            self.log_process_event(proc, 'create')
            rv = 32

        return rv

    @apihook('LoadLibraryEx', argc=3)
    def LoadLibraryEx(self, emu, argv, ctx={}):
        '''HMODULE LoadLibraryExA(
          LPCSTR lpLibFileName,
          HANDLE hFile,
          DWORD  dwFlags
        );'''

        lib_name, _, dwFlags = argv

        hmod = 0

        cw = self.get_char_width(ctx)
        req_lib = self.read_mem_string(lib_name, cw)
        lib = self.normalize_dll_name(req_lib)

        hmod = emu.load_library(lib)

        flags = {
            0x1: 'DONT_RESOLVE_DLL_REFERENCES',
            0x10: 'LOAD_IGNORE_CODE_AUTHZ_LEVEL',
            0x2: 'LOAD_LIBRARY_AS_DATAFILE',
            0x40: 'LOAD_LIBRARY_AS_DATAFILE_EXCLUSIVE',
            0x20: 'LOAD_LIBRARY_AS_IMAGE_RESOURCE',
            0x200: 'LOAD_LIBRARY_SEARCH_APPLICATION_DIR',
            0x1000: 'LOAD_LIBRARY_SEARCH_DEFAULT_DIRS',
            0x100: 'LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR',
            0x800: 'LOAD_LIBRARY_SEARCH_SYSTEM32',
            0x400: 'LOAD_LIBRARY_SEARCH_USER_DIRS',
            0x8: 'LOAD_WITH_ALTERED_SEARCH_PATH',
        }

        pretty_flags = ' | '.join([name for bit, name in flags.items()
                                   if dwFlags & bit])

        argv[0] = req_lib
        argv[1] = argv[1]
        argv[2] = pretty_flags

        if not hmod:
            emu.set_last_error(windefs.ERROR_MOD_NOT_FOUND)

        return hmod

    @apihook('CreateProcess', argc=10)
    def CreateProcess(self, emu, argv, ctx={}):
        '''BOOL CreateProcess(
          LPTSTR                lpApplicationName,
          LPTSTR                lpCommandLine,
          LPSECURITY_ATTRIBUTES lpProcessAttributes,
          LPSECURITY_ATTRIBUTES lpThreadAttributes,
          BOOL                  bInheritHandles,
          DWORD                 dwCreationFlags,
          LPVOID                lpEnvironment,
          LPTSTR                lpCurrentDirectory,
          LPSTARTUPINFO         lpStartupInfo,
          LPPROCESS_INFORMATION lpProcessInformation
        );'''
        app, cmd, pa, ta, inherit, flags, env, cd, si, ppi = argv

        cw = self.get_char_width(ctx)
        cmdstr = ''
        appstr = ''
        if app:
            appstr = self.read_mem_string(app, cw)
            argv[0] = appstr
        if cmd:
            cmdstr = self.read_mem_string(cmd, cw)
            argv[1] = cmdstr

        def_flags = windefs.get_creation_flags(flags)
        if def_flags:
            def_flags = ' | '.join(def_flags)
            argv[5] = def_flags

        proc = emu.create_process(path=appstr, cmdline=cmdstr)
        proc_hnd = self.get_object_handle(proc)

        mod = proc.get_module()
        # Map the PE image for the new process
        emu.map_decoy(mod)

        thread = proc.threads[0]
        thread_hnd = self.get_object_handle(thread)
        if windefs.CREATE_SUSPENDED & flags:
            thread.suspend_count = 1

        ctx = windefs.CONTEXT(emu.get_ptr_size())
        if emu.get_arch() == e_arch.ARCH_X86:
            ctx.Eax = proc.get_ep()
            ctx.Eip = ctx.Eax
            thread.set_context(ctx)

        _pi = self.k32types.PROCESS_INFORMATION(emu.get_ptr_size())
        data = self.mem_cast(_pi, ppi)
        _pi.hProcess = proc_hnd
        _pi.hThread = thread_hnd
        _pi.dwProcessId = proc.get_id()
        _pi.dwThreadId = thread.tid

        self.mem_write(ppi, self.get_bytes(data))

        rv = 1

        self.log_process_event(proc, 'create')
        return rv

    @apihook('VirtualAlloc', argc=4)
    def VirtualAlloc(self, emu, argv, ctx={}):
        '''LPVOID WINAPI VirtualAlloc(
          _In_opt_ LPVOID lpAddress,
          _In_     SIZE_T dwSize,
          _In_     DWORD  flAllocationType,
          _In_     DWORD  flProtect
        );'''
        lpAddress, dwSize, flAllocationType, flProtect = argv
        buf = 0
        tag_prefix = 'api.VirtualAlloc'

        prot_def = windefs.get_page_rights(flProtect)
        if prot_def:
            prot_def = '|'.join(prot_def)
            argv[3] = prot_def

        # Was this address already commited?
        mm = emu.get_address_map(lpAddress)
        if mm and mm.get_tag() and mm.get_tag().startswith(tag_prefix):
            buf = lpAddress
        else:
            if dwSize:
                if lpAddress:
                    test = lpAddress & 0xFFFFFFFFFFFFF000
                else:
                    # This is an arbitrary base address and will be
                    # auto-adjusted by the memory manager
                    test = emu.virtual_mem_base
                size = dwSize
                base, size = emu.get_valid_ranges(size, addr=test)
                while base and base & 0xFFF:
                    base, size = emu.get_valid_ranges(size, addr=test)
                    test += PAGE_SIZE

                emu_perms = self.win_perms_to_emu_perms(flProtect)
                buf = self.mem_alloc(base=base, size=size, tag=tag_prefix, flags=flProtect,
                                     perms=emu_perms)

                emu._set_dyn_code_hook(buf, size)

                # In the wild, I noticed some x64 malware samples that
                # will rely on the new buffer pointer being placed into the first
                # location in the x64 register backup on the stack (performed by
                # the Windows API). Let's do that here.
                arch = emu.get_arch()
                if arch == e_arch.ARCH_AMD64:
                    sp = emu.get_stack_ptr()
                    p = buf.to_bytes(8, 'little')
                    self.mem_write(sp + 8, p)

        return buf

    @apihook('VirtualAllocEx', argc=5)
    def VirtualAllocEx(self, emu, argv, ctx={}):
        '''
        LPVOID VirtualAllocEx(
          HANDLE hProcess,
          LPVOID lpAddress,
          SIZE_T dwSize,
          DWORD  flAllocationType,
          DWORD  flProtect
        );
        '''
        hProcess, lpAddress, dwSize, flAllocationType, flProtect = argv
        buf = 0

        if hProcess == self.get_max_int():
            obj = emu.get_current_process()
        else:
            obj = self.get_object_from_handle(hProcess)

        if not obj:
            return windefs.NULL

        proc_path = obj.get_process_path()
        argv[0] = proc_path
        proc_path = ntpath.basename(proc_path)
        proc_path = proc_path.replace('.', '_')

        tag_prefix = 'api.VirtualAllocEx.%s.%d' % (proc_path, obj.get_pid())

        prot_def = windefs.get_page_rights(flProtect)
        if prot_def:
            prot_def = '|'.join(prot_def)
            argv[4] = prot_def

        # Was this address already commited?
        mm = emu.get_address_map(lpAddress)
        if mm and mm.get_tag().startswith(tag_prefix):
            buf = lpAddress
        else:

            if dwSize:
                if lpAddress:
                    test = lpAddress & 0xFFFFFFFFFFFFF000
                else:
                    test = emu.virtual_mem_base
                size = dwSize
                base, size = emu.get_valid_ranges(size, addr=test)
                while base and base & 0xFFF:
                    base, size = emu.get_valid_ranges(size, addr=test)
                    test += PAGE_SIZE

                emu_perms = self.win_perms_to_emu_perms(flProtect)
                buf = self.mem_alloc(base=base, size=size, tag=tag_prefix,
                                     flags=flProtect, perms=emu_perms, process=obj)
                mm = emu.get_address_map(buf)

                self.log_process_event(obj, 'mem_alloc', base=buf,
                                       size=dwSize, type=flAllocationType,
                                       protect=argv[4])

                emu._set_dyn_code_hook(buf, size)

        return buf

    @apihook('WriteProcessMemory', argc=5)
    def WriteProcessMemory(self, emu, argv, ctx={}):
        '''
        BOOL WriteProcessMemory(
          HANDLE  hProcess,
          LPVOID  lpBaseAddress,
          LPCVOID lpBuffer,
          SIZE_T  nSize,
          SIZE_T  *lpNumberOfBytesWritten
        );
        '''
        hProcess, lpBaseAddress, lpBuffer, nSize, lpNumberOfBytesWritten = argv
        rv = False

        if hProcess == self.get_max_int():
            obj = emu.get_current_process()
        else:
            obj = self.get_object_from_handle(hProcess)

        proc_path = obj.get_process_path()
        argv[0] = proc_path

        data = b''
        if lpBuffer and lpBaseAddress:
            data = self.mem_read(lpBuffer, nSize)
            self.mem_write(lpBaseAddress, data)
            if lpNumberOfBytesWritten:
                bw = (len(data)).to_bytes(self.get_ptr_size(), 'little')
                self.mem_write(lpNumberOfBytesWritten, bw)
            rv = True
            emu.set_last_error(windefs.ERROR_SUCCESS)
        else:
            emu.set_last_error(windefs.ERROR_INVALID_PARAMETER)

        self.log_process_event(obj, 'mem_write', base=lpBaseAddress,
                               size=nSize, data=data)

        return rv

    @apihook('ReadProcessMemory', argc=5)
    def ReadProcessMemory(self, emu, argv, ctx={}):
        '''
        BOOL ReadProcessMemory(
            HANDLE  hProcess,
            LPCVOID lpBaseAddress,
            LPVOID  lpBuffer,
            SIZE_T  nSize,
            SIZE_T  *lpNumberOfBytesRead
        );
        '''
        hProcess, lpBaseAddress, lpBuffer, nSize, lpNumberOfBytesRead = argv
        rv = False

        if hProcess == self.get_max_int():
            obj = emu.get_current_process()
        else:
            obj = self.get_object_from_handle(hProcess)

        proc_path = obj.get_process_path()
        argv[0] = proc_path

        data = b''
        if lpBuffer and lpBaseAddress:
            try:
                data = self.mem_read(lpBaseAddress, nSize)
            except Exception:
                emu.set_last_error(windefs.ERROR_ACCESS_DENIED)
                return rv
            self.mem_write(lpBuffer, data)
            if lpNumberOfBytesRead:
                bw = (len(data)).to_bytes(self.get_ptr_size(), 'little')
                self.mem_write(lpNumberOfBytesRead, bw)
            rv = True
            emu.set_last_error(windefs.ERROR_SUCCESS)
        else:
            emu.set_last_error(windefs.ERROR_INVALID_PARAMETER)

        self.log_process_event(obj, 'mem_read', base=lpBaseAddress,
                               size=nSize, data=data)

        return rv

    @apihook('CreateRemoteThread', argc=7)
    def CreateRemoteThread(self, emu, argv, ctx={}):
        '''
        HANDLE CreateRemoteThread(
          HANDLE                 hProcess,
          LPSECURITY_ATTRIBUTES  lpThreadAttributes,
          SIZE_T                 dwStackSize,
          LPTHREAD_START_ROUTINE lpStartAddress,
          LPVOID                 lpParameter,
          DWORD                  dwCreationFlags,
          LPDWORD                lpThreadId
        );
        '''
        (hProcess, lpThreadAttributes, dwStackSize, lpStartAddress,
         lpParameter, dwCreationFlags, lpThreadId) = argv

        is_remote = False
        if hProcess == self.get_max_int():
            proc_obj = emu.get_current_process()
        else:
            is_remote = True
            proc_obj = self.get_object_from_handle(hProcess)

        proc_path = proc_obj.get_process_path()
        argv[0] = proc_path
        proc_path = ntpath.basename(proc_path)
        proc_path = proc_path.replace('.', '_')

        if is_remote:
            run_type = 'injected_thread_%s_%x' % (proc_path, proc_obj.get_id())
            evt_type = 'thread_inject'
        else:
            run_type = 'thread'
            evt_type = 'thread'

        handle, obj = self.create_thread(lpStartAddress, lpParameter,
                                         proc_obj, thread_type=run_type)

        if not obj:
            return handle

        if lpThreadId:
            self.mem_write(lpThreadId, obj.get_id().to_bytes(4, 'little'))

        self.log_process_event(proc_obj, evt_type,
                               start_addr=lpStartAddress,
                               param=lpParameter)

        emu.set_last_error(windefs.ERROR_SUCCESS)

        return handle

    @apihook('CreateThread', argc=6)
    def CreateThread(self, emu, argv, ctx={}):
        '''
        HANDLE CreateThread(
            LPSECURITY_ATTRIBUTES   lpThreadAttributes,
            SIZE_T                  dwStackSize,
            LPTHREAD_START_ROUTINE  lpStartAddress,
            __drv_aliasesMem LPVOID lpParameter,
            DWORD                   dwCreationFlags,
            LPDWORD                 lpThreadId
        );
        '''
        (lpThreadAttributes, dwStackSize, lpStartAddress,
         lpParameter, dwCreationFlags, lpThreadId) = argv

        proc_obj = emu.get_current_process()

        handle, obj = self.create_thread(lpStartAddress, lpParameter,
                                         proc_obj, thread_type='thread')

        if not obj:
            return handle

        if lpThreadId:
            self.mem_write(lpThreadId, obj.get_id().to_bytes(4, 'little'))

        emu.set_last_error(windefs.ERROR_SUCCESS)

        return handle

    @apihook('ResumeThread', argc=1)
    def ResumeThread(self, emu, argv, ctx={}):
        '''
        DWORD ResumeThread(
            HANDLE hThread
        );
        '''
        hThread, = argv
        rv = -1
        obj = self.get_object_from_handle(hThread)
        if not obj:
            return rv

        rv = obj.suspend_count
        if rv > 0:
            obj.suspend_count -= 1

        if emu.get_arch() == e_arch.ARCH_X86:
            context = obj.get_context()
            proc = obj.process
            handle, obj = self.create_thread(context.Eip, 0,
                                             proc,
                                             thread_type='thread.%s.%d' % (proc.name,
                                                                           proc.get_id()))

        return rv

    @apihook('SuspendThread', argc=1)
    def SuspendThread(self, emu, argv, ctx={}):
        '''
        DWORD SuspendThread(
            HANDLE hThread
        );
        '''
        hThread, = argv
        rv = -1
        obj = self.get_object_from_handle(hThread)
        if not obj:
            return rv

        rv = obj.suspend_count
        obj.suspend_count += 1
        return rv

    @apihook('VirtualQuery', argc=3)
    def VirtualQuery(self, emu, argv, ctx={}):
        '''
        SIZE_T VirtualQuery(
            LPCVOID                   lpAddress,
            PMEMORY_BASIC_INFORMATION lpBuffer,
            SIZE_T                    dwLength
        );
        '''
        rv = 0

        lpAddress, lpBuffer, dwLength = argv
        mbi = self.k32types.MEMORY_BASIC_INFORMATION(emu.get_ptr_size())

        if mbi.sizeof() > dwLength or not lpBuffer:
            emu.set_last_error(windefs.ERROR_INVALID_PARAMETER)
            return rv

        mm = emu.get_address_map(lpAddress)
        if not mm:
            emu.set_last_error(windefs.ERROR_INVALID_PARAMETER)
            return rv

        mbi.BaseAddress = mm.get_base()
        mbi.AllocationBase = mm.get_base()
        mbi.AllocationProtect = self.emu_perms_to_win_perms(mm.get_prot())
        mbi.RegionSize = mm.get_size()
        mbi.State = windefs.MEM_COMMIT
        mbi.Protect = self.emu_perms_to_win_perms(mm.get_prot())
        mbi.Type = 0

        self.mem_write(lpBuffer, mbi.get_bytes())
        rv = mbi.sizeof()

        return mbi.sizeof()

    @apihook('VirtualProtect', argc=4)
    def VirtualProtect(self, emu, argv, ctx={}):
        '''BOOL WINAPI VirtualProtect(
          _In_  LPVOID lpAddress,
          _In_  SIZE_T dwSize,
          _In_  DWORD  flNewProtect,
          _Out_ PDWORD lpflOldProtect
        );'''
        rv = 0
        mm = None
        new = 0

        lpAddress, dwSize, flNewProtect, lpflOldProtect = argv

        maps = emu.get_mem_maps()
        for m in maps:
            if m.get_base() <= lpAddress < m.get_base() + m.get_size():
                mm = m
                break

        # convert the mem flags
        if mm and lpflOldProtect:
            # See if we saved off the flags if this chunk is from a previous
            # VirtualAlloc()
            old_prot = mm.get_flags()
            if not old_prot:
                # Otherwise, get the perms from the emulator and convert it to
                # Win32 protection flags
                old_prot = self.emu_perms_to_win_perms(mm.get_prot())
            # Check for page alignment
            addr = lpAddress & 0xFFFFFFFFFFFFF000
            size = dwSize & 0xFFFFFFFFFFFFF000
            new = self.win_perms_to_emu_perms(flNewProtect)

            remainder = dwSize & 0xFFF
            if remainder:
                size += 0x1000

            try:
                emu.mem_protect(addr, size, new)
            finally:
                mm.prot = new
                self.mem_write(lpflOldProtect, old_prot.to_bytes(4, 'little'))
                return 1

        return rv

    @apihook('VirtualProtectEx', argc=5)
    def VirtualProtectEx(self, emu, argv, ctx={}):
        '''
        BOOL VirtualProtectEx(
            HANDLE hProcess,
            LPVOID lpAddress,
            SIZE_T dwSize,
            DWORD  flNewProtect,
            PDWORD lpflOldProtect
        );
        '''
        hProcess, lpAddress, dwSize, flNewProtect, lpflOldProtect = argv

        proc_obj = self.get_object_from_handle(hProcess)
        if 0xFFFFFFFF == (0xFFFFFFFF & hProcess):
            proc_obj = emu.get_current_process()

        rv = self.VirtualProtect(emu, argv[1:], ctx)

        prot_def = windefs.get_page_rights(flNewProtect)
        if prot_def:
            prot_def = ' | '.join(prot_def)
            argv[3] = prot_def

        self.log_process_event(proc_obj, 'mem_protect', base=lpAddress,
                               size=dwSize, protect=prot_def)

        return rv

    @apihook('VirtualFree', argc=3)
    def VirtualFree(self, emu, argv, ctx={}):
        '''
        BOOL VirtualFree(
          LPVOID lpAddress,
          SIZE_T dwSize,
          DWORD  dwFreeType
        );
        '''
        rv = 0

        lpAddress, dwSize, dwFreeType = argv
        maps = emu.get_mem_maps()
        for m in maps:
            if m.base == lpAddress:
                m.set_free()
                rv = True
                break

        rv = 1

        return rv

    @apihook('GetCurrentProcess', argc=0)
    def GetCurrentProcess(self, emu, argv, ctx={}):
        '''
        HANDLE GetCurrentProcess();
        '''

        rv = self.get_max_int()

        return rv

    @apihook('GetVersion', argc=0)
    def GetVersion(self, emu, argv, ctx={}):
        '''NOT_BUILD_WINDOWS_DEPRECATE DWORD GetVersion();'''

        ver = self.get_os_version()
        build = ver['build']
        major = ver['major']
        minor = ver['minor']

        rv = 0xFFFFFFFF & ((build << 16) | minor << 8 | major)

        return rv

    @apihook('GetLastError', argc=0)
    def GetLastError(self, emu, argv, ctx={}):
        '''DWORD WINAPI GetLastError(void);'''

        rv = emu.get_last_error()

        # TODO: reset last error code here for now
        emu.set_last_error(windefs.ERROR_SUCCESS)

        return rv

    @apihook('SetLastError', argc=1)
    def SetLastError(self, emu, argv, ctx={}):
        '''
        void SetLastError(
          DWORD dwErrCode
        );
        '''
        dwErrCode, = argv

        emu.set_last_error(dwErrCode)

        return None

    @apihook('ExitProcess', argc=1)
    def ExitProcess(self, emu, argv, ctx={}):
        '''void ExitProcess(
                UINT uExitCode
        );'''

        self.exit_process()
        return 0

    @apihook('SystemTimeToTzSpecificLocalTime', argc=3)
    def SystemTimeToTzSpecificLocalTime(self, emu, argv, ctx={}):
        '''
        BOOL SystemTimeToTzSpecificLocalTime(
            const TIME_ZONE_INFORMATION *lpTimeZoneInformation,
            const SYSTEMTIME            *lpUniversalTime,
            LPSYSTEMTIME                lpLocalTime
        );
        '''
        return True

    @apihook('FileTimeToSystemTime', argc=2)
    def FileTimeToSystemTime(self, emu, argv, ctx={}):
        '''
        BOOL FileTimeToSystemTime(
            const FILETIME *lpFileTime,
            LPSYSTEMTIME   lpSystemTime
        );
        '''

        lpFileTime, lpSystemTime = argv

        st = self.k32types.SYSTEMTIME(emu.get_ptr_size())
        ft = self.k32types.FILETIME(emu.get_ptr_size())
        ft = self.mem_cast(ft, lpFileTime)

        quad = (ft.dwHighDateTime << 32) | ft.dwLowDateTime
        try:
            dt = datetime.utcfromtimestamp((quad - 116444736000000000) / 10000000)
        except ValueError:
            dt = None

        if dt:
            st.wYear = dt.year
            st.wMonth = dt.month
            st.wDayOfWeek = dt.weekday()
            st.wDay = dt.day
            st.wHour = dt.hour
            st.wMinute = dt.minute
            st.wSecond = dt.second
            rv = True
            if lpSystemTime:
                self.mem_write(lpSystemTime, st.get_bytes())
        else:
            rv = True

        return rv

    @apihook('GetSystemTimeAsFileTime', argc=1)
    def GetSystemTimeAsFileTime(self, emu, argv, ctx={}):
        '''void GetSystemTimeAsFileTime(
          LPFILETIME lpSystemTimeAsFileTime
        );'''

        lpSystemTimeAsFileTime, = argv
        ft = self.k32types.FILETIME(emu.get_ptr_size())

        timestamp = 116444736000000000 + int(datetime.datetime.utcnow().timestamp()) * 10000000
        ft.dwLowDateTime = 0xFFFFFFFF & timestamp
        ft.dwHighDateTime = timestamp >> 32

        self.mem_write(lpSystemTimeAsFileTime, self.get_bytes(ft))

        return

    @apihook('GetLocalTime', argc=1)
    def GetLocalTime(self, emu, argv, ctx={}):
        '''
        void GetLocalTime(
            LPSYSTEMTIME lpSystemTime
        );
        '''
        return self.GetSystemTime(emu, argv)

    @apihook('GetSystemTime', argc=1)
    def GetSystemTime(self, emu, argv, ctx={}):
        '''
        void GetSystemTime(
            LPSYSTEMTIME lpSystemTime
        );'''
        lpSystemTime, = argv
        st = self.k32types.SYSTEMTIME(emu.get_ptr_size())

        now = datetime.datetime.now()
        st.wYear = now.year
        st.wMonth = now.month
        st.wDayOfWeek = 2
        st.wDay = now.day
        st.wHour = now.hour
        st.wMinute = now.minute
        st.wSecond = now.second
        st.wMilliseconds = 0

        self.mem_write(lpSystemTime, self.get_bytes(st))

        return

    @apihook('GetCurrentThreadId', argc=0)
    def GetCurrentThreadId(self, emu, argv, ctx={}):
        '''DWORD GetCurrentThreadId();'''

        thread = emu.get_current_thread()
        rv = thread.get_id()

        return rv

    @apihook('GetCurrentProcessId', argc=0)
    def GetCurrentProcessId(self, emu, argv, ctx={}):
        '''DWORD GetCurrentProcessId();'''

        proc = emu.get_current_process()
        rv = proc.get_id()

        return rv

    @apihook('IsProcessorFeaturePresent', argc=1,
             conv=e_arch.CALL_CONV_STDCALL)
    def IsProcessorFeaturePresent(self, emu, argv, ctx={}):
        '''BOOL IsProcessorFeaturePresent(
              DWORD ProcessorFeature
        );'''

        rv = 1

        lookup = {
            25: 'PF_ARM_64BIT_LOADSTORE_ATOMIC',
            24: 'PF_ARM_DIVIDE_INSTRUCTION_AVAILABLE',
            26: 'PF_ARM_EXTERNAL_CACHE_AVAILABLE',
            27: 'PF_ARM_FMAC_INSTRUCTIONS_AVAILABLE',
            18: 'PF_ARM_VFP_32_REGISTERS_AVAILABLE',
            7: 'PF_3DNOW_INSTRUCTIONS_AVAILABLE',
            16: 'PF_CHANNELS_ENABLED',
            2: 'PF_COMPARE_EXCHANGE_DOUBLE',
            14: 'PF_COMPARE_EXCHANGE128',
            15: 'PF_COMPARE64_EXCHANGE128',
            23: 'PF_FASTFAIL_AVAILABLE',
            1: 'PF_FLOATING_POINT_EMULATED',
            0: 'PF_FLOATING_POINT_PRECISION_ERRATA',
            3: 'PF_MMX_INSTRUCTIONS_AVAILABLE',
            12: 'PF_NX_ENABLED',
            9: 'PF_PAE_ENABLED',
            8: 'PF_RDTSC_INSTRUCTION_AVAILABLE',
            22: 'PF_RDWRFSGSBASE_AVAILABLE',
            20: 'PF_SECOND_LEVEL_ADDRESS_TRANSLATION',
            13: 'PF_SSE3_INSTRUCTIONS_AVAILABLE',
            21: 'PF_VIRT_FIRMWARE_ENABLED',
            6: 'PF_XMMI_INSTRUCTIONS_AVAILABLE',
            10: 'PF_XMMI64_INSTRUCTIONS_AVAILABLE',
            17: 'PF_XSAVE_ENABLED',
        }

        argv[0] = lookup[argv[0]]
        return rv

    @apihook('lstrcmpi', argc=2)
    def lstrcmpi(self, emu, argv, ctx={}):
        '''int lstrcmpiA(
          LPCSTR lpString1,
          LPCSTR lpString2
        );'''
        cw = self.get_char_width(ctx)

        string1, string2 = argv
        rv = 1

        cs1 = self.read_mem_string(string1, cw)
        cs2 = self.read_mem_string(string2, cw)

        argv[0] = cs1
        argv[1] = cs2

        if cs1.lower() == cs2.lower():
            rv = 0

        return rv

    @apihook('QueryPerformanceCounter', argc=1)
    def QueryPerformanceCounter(self, emu, argv, ctx={}):
        '''BOOL WINAPI QueryPerformanceCounter(
          _Out_ LARGE_INTEGER *lpPerformanceCount
        );'''
        lpPerformanceCount, = argv

        rv = 1

        self.mem_write(lpPerformanceCount,
                       self.perf_counter.to_bytes(8, 'little'))
        return rv

    @apihook('lstrlen', argc=1)
    def lstrlen(self, emu, argv, ctx={}):
        '''
        int lstrlen(
            LPCSTR lpString
        );
        '''
        src, = argv
        try:
            cw = self.get_char_width(ctx)
        except Exception:
            cw = 1
        s = self.read_mem_string(src, cw)

        argv[0] = s

        return len(s)

    @apihook('GetModuleHandleEx', argc=3)
    def GetModuleHandleEx(self, emu, argv, ctx={}):
        '''
        BOOL GetModuleHandleExA(
            DWORD   dwFlags,
            LPCSTR  lpModuleName,
            HMODULE *phModule
        );
        '''
        dwFlags, lpModuleName, phModule = argv

        hmod = self.GetModuleHandle(emu, [lpModuleName], ctx)
        if phModule:
            _mod = (hmod).to_bytes(emu.get_ptr_size(), 'little')
            self.mem_write(phModule, _mod)
        return hmod

    @apihook('GetModuleHandle', argc=1)
    def GetModuleHandle(self, emu, argv, ctx={}):
        '''HMODULE GetModuleHandle(
          LPCSTR lpModuleName
        );'''

        mod_name, = argv

        cw = self.get_char_width(ctx)
        rv = 0

        if not mod_name:

            proc = emu.get_current_process()
            rv = proc.base
        else:
            lib = self.read_mem_string(mod_name, cw)
            argv[0] = lib
            sname, _ = os.path.splitext(lib)
            mods = emu.get_user_modules()
            for mod in mods:
                img = ntpath.basename(mod.get_emu_path())
                fname, _ = os.path.splitext(img)
                if fname.lower() == sname.lower():
                    rv = mod.get_base()

        return rv

    @apihook('GetProcAddress', argc=2)
    def GetProcAddress(self, emu, argv, ctx={}):
        '''FARPROC GetProcAddress(
          HMODULE hModule,
          LPCSTR  lpProcName
        );'''

        hmod, proc_name = argv
        rv = 0

        if proc_name:
            try:
                proc = self.read_mem_string(proc_name, 1)
                argv[1] = proc
            except Exception:
                if isinstance(proc_name, int) and proc_name < 0xFFFF:
                    # Import is most likely an ordinal
                    proc = 'ordinal_%d' % (proc_name)

        mods = emu.get_user_modules()
        for mod in mods:
            if mod.get_base() == hmod:
                bn = mod.get_base_name()
                mname, _ = os.path.splitext(bn)
                rv = emu.get_proc(mname, proc)

        return rv

    @apihook('GetConsoleWindow', argc=0)
    def GetConsoleWindow(self, emu, argv, ctx={}):
        '''HWND WINAPI GetConsoleWindow(void);'''
        hwnd = 0

        proc = emu.get_current_process()
        console = proc.get_console()
        if console:
            win = console.get_window()
            hwnd = win.handle

        return hwnd

    @apihook('Sleep', argc=1)
    def Sleep(self, emu, argv, ctx={}):
        '''void Sleep(DWORD dwMilliseconds);'''
        millisec, = argv

        return

    @apihook('GlobalAlloc', argc=2)
    def GlobalAlloc(self, emu, argv, ctx={}):
        '''
        DECLSPEC_ALLOCATOR HGLOBAL GlobalAlloc(
          UINT   uFlags,
          SIZE_T dwBytes
        );
        '''

        uFlags, dwBytes = argv

        chunk = self.heap_alloc(dwBytes, heap='GlobalAlloc')

        return chunk

    @apihook('LocalAlloc', argc=2)
    def LocalAlloc(self, emu, argv, ctx={}):
        '''
        DECLSPEC_ALLOCATOR HLOCAL LocalAlloc(
          UINT   uFlags,
          SIZE_T uBytes
        );
        '''

        uFlags, dwBytes = argv

        chunk = self.heap_alloc(dwBytes, heap='LocalAlloc')

        return chunk

    @apihook('HeapAlloc', argc=3)
    def HeapAlloc(self, emu, argv, ctx={}):
        '''
        DECLSPEC_ALLOCATOR LPVOID HeapAlloc(
          HANDLE hHeap,
          DWORD  dwFlags,
          SIZE_T dwBytes
        );
        '''

        hHeap, dwFlags, dwBytes = argv

        chunk = self.heap_alloc(dwBytes, heap='HeapAlloc')
        if chunk:
            emu.set_last_error(windefs.ERROR_SUCCESS)

        return chunk

    @apihook('HeapSize', argc=3)
    def HeapSize(self, emu, argv, ctx={}):
        '''
        SIZE_T HeapSize(
          HANDLE  hHeap,
          DWORD   dwFlags,
          LPCVOID lpMem
        );
        '''

        hHeap, dwFlags, lpMem = argv

        size = 0
        for mmap in emu.get_mem_maps():
            if lpMem == mmap.get_base():
                size = mmap.get_size()
                emu.set_last_error(windefs.ERROR_SUCCESS)

        if not size:
            emu.set_last_error(windefs.ERROR_INVALID_PARAMETER)

        return size

    @apihook('GetTickCount', argc=0)
    def GetTickCount(self, emu, argv, ctx={}):
        '''
        DWORD GetTickCount();
        '''

        self.tick_counter += 200

        return self.tick_counter

    @apihook('GetTickCount64', argc=0)
    def GetTickCount64(self, emu, argv, ctx={}):
        '''
        ULONGLONG GetTickCount64();
        '''

        self.tick_counter += 200

        return self.tick_counter

    @apihook('lstrcat', argc=2)
    def lstrcat(self, emu, argv, ctx={}):
        '''
        LPSTR lstrcat(
          LPSTR  lpString1,
          LPCSTR lpString2
        );
        '''
        lpString1, lpString2 = argv

        cw = self.get_char_width(ctx)
        s1 = self.read_mem_string(lpString1, cw)
        s2 = self.read_mem_string(lpString2, cw)

        argv[0] = s1
        argv[1] = s2

        if cw == 2:
            new = (s1 + s2).encode('utf-16le')
        else:
            new = (s1 + s2).encode('utf-8')

        self.mem_write(lpString1, new + b'\x00')

        return lpString1

    @apihook('lstrcpyn', argc=3)
    def lstrcpyn(self, emu, argv, ctx={}):
        '''
        LPSTR lstrcpynA(
          LPSTR  lpString1,
          LPCSTR lpString2,
          int    iMaxLength
        );
        '''
        dest, src, iMaxLength = argv

        cw = self.get_char_width(ctx)

        s = self.read_mem_string(src, cw)
        argv[1] = s
        s = s[:iMaxLength - 1]
        s += '\x00'

        self.write_mem_string(s, dest, cw)
        return dest

    @apihook('lstrcpy', argc=2)
    def lstrcpy(self, emu, argv, ctx={}):
        '''
        LPSTR lstrcpynA(
          LPSTR  lpString1,
          LPCSTR lpString2,
        );
        '''
        dest, src = argv

        cw = self.get_char_width(ctx)

        s = self.read_mem_string(src, cw)
        self.write_mem_string(s, dest, cw)
        argv[1] = s
        return dest

    @apihook('IsBadReadPtr', argc=2)
    def IsBadReadPtr(self, emu, argv, ctx={}):
        '''
        BOOL IsBadReadPtr(
          const VOID *lp,
          UINT_PTR   ucb
        );
        '''

        lp, ucb = argv

        rv = True

        if lp and ucb:
            v1 = emu.is_address_valid(lp)
            v2 = emu.is_address_valid(lp + (ucb - 1))

            if v1 and v2:
                rv = False

        return rv

    @apihook('HeapReAlloc', argc=4)
    def HeapReAlloc(self, emu, argv, ctx={}):
        '''
        DECLSPEC_ALLOCATOR LPVOID HeapReAlloc(
          HANDLE                 hHeap,
          DWORD                  dwFlags,
          _Frees_ptr_opt_ LPVOID lpMem,
          SIZE_T                 dwBytes
        );
        '''

        hHeap, dwFlags, lpMem, dwBytes = argv

        tag_prefix = 'api.heap'
        new_buf = 0

        if hHeap and lpMem and dwBytes:
            mm = emu.get_address_map(lpMem)
            if mm and mm.get_tag().startswith(tag_prefix):
                # Copy the existing data
                data = self.mem_read(lpMem, mm.get_size())
                new_buf = self.heap_alloc(dwBytes, heap='HeapReAlloc')
                self.mem_write(new_buf, data)

        return new_buf

    @apihook('LocalReAlloc', argc=3)
    def LocalReAlloc(self, emu, argv, ctx={}):
        '''
        DECLSPEC_ALLOCATOR HLOCAL LocalReAlloc(
          _Frees_ptr_opt_ HLOCAL hMem,
          SIZE_T                 uBytes,
          UINT                   uFlags
        );
        '''

        hMem, uBytes, uFlags = argv

        tag_prefix = 'api.heap'
        new_buf = 0

        if hMem and uBytes:
            mm = emu.get_address_map(hMem)
            if mm and mm.get_tag().startswith(tag_prefix):
                # Copy the existing data
                data = self.mem_read(hMem, mm.get_size())
                new_buf = self.heap_alloc(uBytes, heap='LocalReAlloc')
                self.mem_write(new_buf, data)

        return new_buf

    @apihook('HeapCreate', argc=3)
    def HeapCreate(self, emu, argv, ctx={}):
        '''
        HANDLE HeapCreate(
          DWORD  flOptions,
          SIZE_T dwInitialSize,
          SIZE_T dwMaximumSize
        );
        '''

        flOptions, dwInitialSize, dwMaximumSize = argv

        heap = self.create_heap(emu)

        return heap

    @apihook('GetCurrentThread', argc=0)
    def GetCurrentThread(self, emu, argv, ctx={}):
        '''
        HANDLE GetCurrentThread();
        '''

        rv = self.get_max_int()

        return rv

    @apihook('TlsAlloc', argc=0)
    def TlsAlloc(self, emu, argv, ctx={}):
        '''
        DWORD TlsAlloc();
        '''

        thread = emu.get_current_thread()
        tls = thread.get_tls()

        tls.append(0)
        thread.set_tls(tls)
        idx = len(tls) - 1

        return idx

    @apihook('TlsSetValue', argc=2)
    def TlsSetValue(self, emu, argv, ctx={}):
        '''
        BOOL TlsSetValue(
          DWORD  dwTlsIndex,
          LPVOID lpTlsValue
        );
        '''

        dwTlsIndex, lpTlsValue = argv
        rv = 0

        thread = emu.get_current_thread()
        tls = thread.get_tls()

        if dwTlsIndex < len(tls):
            tls[dwTlsIndex] = lpTlsValue
            thread.set_tls(tls)
            rv = 1
            emu.set_last_error(windefs.ERROR_SUCCESS)
        else:
            emu.set_last_error(windefs.ERROR_INVALID_PARAMETER)

        return rv

    @apihook('TlsGetValue', argc=1)
    def TlsGetValue(self, emu, argv, ctx={}):
        '''
        LPVOID TlsGetValue(
          DWORD dwTlsIndex
        );
        '''
        dwTlsIndex, = argv
        rv = 0

        thread = emu.get_current_thread()
        tls = thread.get_tls()

        if dwTlsIndex < len(tls):
            rv = tls[dwTlsIndex]
            emu.set_last_error(windefs.ERROR_SUCCESS)
        else:
            emu.set_last_error(windefs.ERROR_INVALID_PARAMETER)

        return rv

    @apihook('FlsAlloc', argc=1)
    def FlsAlloc(self, emu, argv, ctx={}):
        '''
        DWORD FlsAlloc(
          PFLS_CALLBACK_FUNCTION lpCallback
        );
        '''

        thread = emu.get_current_thread()
        fls = thread.get_fls()

        fls.append(0)
        thread.set_fls(fls)
        idx = len(fls) - 1

        return idx

    @apihook('FlsSetValue', argc=2)
    def FlsSetValue(self, emu, argv, ctx={}):
        '''
        BOOL FlsSetValue(
          DWORD dwFlsIndex,
          PVOID lpFlsData
        );
        '''

        dwFlsIndex, lpFlsData = argv
        rv = 0

        thread = emu.get_current_thread()
        fls = thread.get_fls()

        if dwFlsIndex < len(fls):
            fls[dwFlsIndex] = lpFlsData
            thread.set_fls(fls)
            rv = 1
            emu.set_last_error(windefs.ERROR_SUCCESS)
        else:
            emu.set_last_error(windefs.ERROR_INVALID_PARAMETER)

        return rv

    @apihook('FlsGetValue', argc=1)
    def FlsGetValue(self, emu, argv, ctx={}):
        '''
        PVOID FlsGetValue(
          DWORD dwFlsIndex
        );
        '''
        dwFlsIndex, = argv
        rv = 0

        thread = emu.get_current_thread()
        fls = thread.get_fls()

        if dwFlsIndex < len(fls):
            rv = fls[dwFlsIndex]
            emu.set_last_error(windefs.ERROR_SUCCESS)
        else:
            emu.set_last_error(windefs.ERROR_INVALID_PARAMETER)

        return rv

    @apihook('EncodePointer', argc=1)
    def EncodePointer(self, emu, argv, ctx={}):
        '''
        PVOID EncodePointer(
          _In_ PVOID Ptr
        );
        '''

        Ptr,  = argv
        # Just increment the pointer for now
        rv = Ptr + 1

        return rv

    @apihook('DecodePointer', argc=1)
    def DecodePointer(self, emu, argv, ctx={}):
        '''
        PVOID DecodePointer(
           PVOID Ptr
        );
        '''

        Ptr,  = argv
        # Just decrement the pointer for now
        rv = Ptr - 1

        return rv

    @apihook('InitializeCriticalSectionAndSpinCount', argc=2)
    def InitializeCriticalSectionAndSpinCount(self, emu, argv, ctx={}):
        '''
        BOOL InitializeCriticalSectionAndSpinCount(
          LPCRITICAL_SECTION lpCriticalSection,
          DWORD              dwSpinCount
        );
        '''

        lpCriticalSection, dwSpinCount = argv
        rv = 1

        return rv

    @apihook('EnterCriticalSection', argc=1)
    def EnterCriticalSection(self, emu, argv, ctx={}):
        '''
        void EnterCriticalSection(
          LPCRITICAL_SECTION lpCriticalSection
        );
        '''

        return

    @apihook('LeaveCriticalSection', argc=1)
    def LeaveCriticalSection(self, emu, argv, ctx={}):
        '''
        void LeaveCriticalSection(
          LPCRITICAL_SECTION lpCriticalSection
        );
        '''

        return

    @apihook('InterlockedIncrement', argc=1)
    def InterlockedIncrement(self, emu, argv, ctx={}):
        '''
        LONG InterlockedIncrement(
          LONG volatile *Addend
        );
        '''

        Addend, = argv

        val = self.mem_read(Addend, 4)
        ival = int.from_bytes(val, 'little')
        ival = ct.c_long(ival + 1).value & 0xFFFFFFFF
        val = (ival).to_bytes(4, 'little')
        self.mem_write(Addend, val)

        return ival

    @apihook('InterlockedDecrement', argc=1)
    def InterlockedDecrement(self, emu, argv, ctx={}):
        '''
        LONG InterlockedDecrement(
          LONG volatile *Addend
        );
        '''

        Addend, = argv

        val = self.mem_read(Addend, 4)
        ival = int.from_bytes(val, 'little')
        ival = ct.c_long(ival - 1).value & 0xFFFFFFFF
        val = (ival).to_bytes(4, 'little')
        self.mem_write(Addend, val)

        return ival

    @apihook('GetCommandLine', argc=0)
    def GetCommandLine(self, emu, argv, ctx={}):
        '''
        LPTSTR GetCommandLine();
        '''

        fn = ctx['func_name']
        cw = self.get_char_width(ctx)
        curr_proc = emu.get_current_process()

        cmdline = curr_proc.get_command_line()

        cmd_ptr = self.command_lines[cw]
        if not cmd_ptr:
            cmd_ptr = self.mem_alloc((len(cmdline) + 1) * cw, tag='api.command_line.%s' % (fn))
            self.command_lines[cw] = cmd_ptr

        if cw == 2:
            cl = cmdline.encode('utf-16le')
        elif cw == 1:
            cl = cmdline.encode('utf-8')

        self.mem_write(cmd_ptr, cl)

        return cmd_ptr

    @apihook('ExpandEnvironmentStrings', argc=3)
    def ExpandEnvironmentStrings(self, emu, argv, ctx={}):
        '''
        DWORD ExpandEnvironmentStringsA(
            LPCSTR lpSrc,
            LPSTR  lpDst,
            DWORD  nSize
        );
        '''
        lpSrc, lpDst, nSize = argv
        rv = 0

        cw = self.get_char_width(ctx)
        if lpSrc:
            src = self.read_mem_string(lpSrc, cw)
            dst = src
            argv[0] = src
            for k, v in emu.get_env().items():
                ev = '%%%s%%' % (k.lower())
                if ev in dst.lower():
                    o = dst.lower().find(ev)
                    dst = dst[: o] + v + dst[o + len(ev):]
                    dst += '\x00\x00'

            if lpDst:
                self.write_mem_string(dst, lpDst, cw)
                rv = len(dst)
                argv[1] = dst

        return rv

    @apihook('GetEnvironmentStrings', argc=0)
    def GetEnvironmentStrings(self, emu, argv, ctx={}):
        '''
        LPCH GetEnvironmentStrings();
        '''

        out = ''
        fn = ctx['func_name']
        cw = self.get_char_width(ctx)
        for k, v in emu.get_env().items():
            out += '%s %s ' % (k, v)

        out = out.strip()

        env_ptr = self.mem_alloc((len(out) + 1) * cw, tag='api.environment.%s' % (fn))

        if cw == 2:
            ev = out.encode('utf-16le')
        elif cw == 1:
            ev = out.encode('utf-8')

        self.mem_write(env_ptr, ev)

        return env_ptr

    @apihook('FreeEnvironmentStrings', argc=1)
    def FreeEnvironmentStrings(self, emu, argv, ctx={}):
        '''
        BOOL FreeEnvironmentStrings(
          LPCH penv
        );
        '''

        penv, = argv

        self.mem_free(penv)

        return True

    @apihook('GetFullPathName', argc=4)
    def GetFullPathName(self, emu, argv, ctx={}):
        '''
        DWORD GetFullPathNameA(
            LPCSTR lpFileName,
            DWORD  nBufferLength,
            LPSTR  lpBuffer,
            LPSTR  *lpFilePart
        );
        '''

        lpFileName, nBufferLength, lpBuffer, lpFilePart = argv
        cw = self.get_char_width(ctx)
        rv = 0

        if lpFileName:
            fn = self.read_mem_string(lpFileName, cw)
            bn = ntpath.basename(fn)

            offset = fn.find(bn)
            if lpBuffer:

                self.write_mem_string(fn, lpBuffer, cw)

                if '.' in bn and lpFilePart:
                    ptr = (lpBuffer + offset).to_bytes(emu.get_ptr_size(), 'little')
                    self.mem_write(lpFilePart, ptr)

                rv = len(fn)

        return rv

    @apihook('GetStartupInfo', argc=1)
    def GetStartupInfo(self, emu, argv, ctx={}):
        '''
        void GetStartupInfo(
          LPSTARTUPINFO lpStartupInfo
        );
        '''

        lpStartupInfo, = argv

        cw = self.get_char_width(ctx)
        si = self.k32types.STARTUPINFO(emu.get_ptr_size())

        # Did we already alloc memory for the process's desktop name?
        proc = emu.get_current_process()
        ps = self.startup_info.get(proc)
        if ps:
            desk = ps.get('desktop', {})
        else:
            desk = {'desktop': {}}
            self.startup_info.update({proc: desk})

        desk_name = proc.get_desktop_name()
        dn = desk.get(cw)
        if dn:
            si.lpDesktop = dn
        else:
            if cw == 2:
                out = desk_name.encode('utf-16le')
                sn = 'W'
            elif cw == 1:
                out = desk_name.encode('utf-8')
                sn = 'A'
            desk_ptr = self.mem_alloc((len(out) + cw), tag='api.struct.STARTUPINFO%s.lpDesktop' % (sn)) # noqa
            si.lpDesktop = desk_ptr
            desk.update({cw: desk_ptr})

        # Did we already alloc memory for the process's title?
        ps = self.startup_info.get(proc)
        if ps:
            title = ps.get('title', {})
        else:
            title = {'title': {}}
            self.startup_info.update({proc: title})

        title_name = proc.get_title_name()
        if not title_name:
            title_name = proc.get_process_path()

        if title:
            si.lpTitle = title
        else:
            if cw == 2:
                out = title_name.encode('utf-16le')
                sn = 'W'
            elif cw == 1:
                out = title_name.encode('utf-8')
                sn = 'A'
            title_ptr = self.mem_alloc((len(out) + cw),
                                       tag='api.struct.STARTUPINFO%s.lpTitle' % (sn))
            si.lpTitle = title_ptr
            title.update({cw: title_ptr})

        si.cb = self.sizeof(si)
        si.hStdInput = 0
        si.hStdOutput = 1
        si.hStdError = 2

        self.mem_write(lpStartupInfo, self.get_bytes(si))

        return None

    @apihook('GetStdHandle', argc=1)
    def GetStdHandle(self, emu, argv, ctx={}):
        '''
        HANDLE WINAPI GetStdHandle(
          _In_ DWORD nStdHandle
        );
        '''

        nStdHandle, = argv

        proc = emu.get_current_process()
        hnd = proc.get_std_handle(nStdHandle)

        return hnd

    @apihook('GetFileType', argc=1)
    def GetFileType(self, emu, argv, ctx={}):
        '''
        DWORD GetFileType(
          HANDLE hFile
        );
        '''
        FILE_TYPE_DISK = 1

        hFile, = argv

        return FILE_TYPE_DISK

    @apihook('SetHandleCount', argc=1)
    def SetHandleCount(self, emu, argv, ctx={}):
        '''
        UINT SetHandleCount(
          UINT uNumber
        );
        '''
        uNumber, = argv

        emu.set_last_error(windefs.ERROR_INVALID_HANDLE)

        return uNumber

    @apihook('GetACP', argc=0)
    def GetACP(self, emu, argv, ctx={}):
        '''
        UINT GetACP();
        '''

        windows_1252 = 1252

        return windows_1252

    @apihook('IsValidCodePage', argc=1)
    def IsValidCodePage(self, emu, argv, ctx={}):
        '''
        BOOL IsValidCodePage(
          UINT CodePage
        );
        '''

        CodePage, = argv

        return True

    @apihook('GetCPInfo', argc=2)
    def GetCPInfo(self, emu, argv, ctx={}):
        '''
        BOOL GetCPInfo(
          UINT     CodePage,
          LPCPINFO lpCPInfo
        );
        '''

        CodePage, lpCPInfo = argv

        cp_info = (2).to_bytes(4, 'little') + (b'\x00' * 8)

        self.mem_write(lpCPInfo, cp_info)

        return True

    @apihook('WideCharToMultiByte', argc=8)
    def WideCharToMultiByte(self, emu, argv, ctx={}):
        '''
        int WideCharToMultiByte(
          UINT                               CodePage,
          DWORD                              dwFlags,
          _In_NLS_string_(cchWideChar)LPCWCH lpWideCharStr,
          int                                cchWideChar,
          LPSTR                              lpMultiByteStr,
          int                                cbMultiByte,
          LPCCH                              lpDefaultChar,
          LPBOOL                             lpUsedDefaultChar
        );
        '''

        rv = 0

        (CodePage, dwFlags, lpWideCharStr, cchWideChar,
         lpMultiByteStr, cbMultiByte, lpDefaultChar, lpUsedDefaultChar) = argv

        if not lpWideCharStr or not cchWideChar:
            emu.set_last_error(windefs.ERROR_INVALID_PARAMETER)
        elif not lpMultiByteStr or cbMultiByte == 0:
            emu.set_last_error(windefs.ERROR_SUCCESS)
            if cchWideChar == 0xFFFFFFFF:
                wcs = b''
                o = 0
                while emu.is_address_valid(lpWideCharStr + o + 2):
                    wcs += self.mem_read(lpWideCharStr + o, 2)
                    if wcs.endswith(b'\0\0'):
                        break
                    o += 2
            else:
                wcs = self.mem_read(lpWideCharStr, cchWideChar * 2)
            cs = wcs.decode('utf-16le', 'ignore')
            rv = len(cs)
        else:
            emu.set_last_error(windefs.ERROR_SUCCESS)
            if cchWideChar == 0xFFFFFFFF:
                wcs = b''
                o = 0
                while emu.is_address_valid(lpWideCharStr + o + 2):
                    wcs += self.mem_read(lpWideCharStr + o, 2)
                    if wcs.endswith(b'\0\0'):
                        break
                    o += 2
            else:
                wcs = self.mem_read(lpWideCharStr, cchWideChar * 2)
            cs = wcs.decode('utf-16le', 'ignore')
            cs = cs.encode('utf-8', 'ignore')
            rv = cbMultiByte
            self.mem_write(lpMultiByteStr, cs)

        return rv

    @apihook('MultiByteToWideChar', argc=6)
    def MultiByteToWideChar(self, emu, argv, ctx={}):
        '''
        int MultiByteToWideChar(
          UINT                              CodePage,
          DWORD                             dwFlags,
          _In_NLS_string_(cbMultiByte)LPCCH lpMultiByteStr,
          int                               cbMultiByte,
          LPWSTR                            lpWideCharStr,
          int                               cchWideChar
        );
        '''

        (CodePage, dwFlags, lpMultiByteStr, cbMultiByte,
         lpWideCharStr, cchWideChar) = argv

        rv = 0
        if cchWideChar == 0:
            if cbMultiByte == 0xFFFFFFFF:
                mbs = self.read_mem_string(lpMultiByteStr, 1)
                argv[2] = mbs
                rv = len(mbs) + 1
        elif lpMultiByteStr == 0 or cbMultiByte == 0:
            emu.set_last_error(windefs.ERROR_INSUFFICIENT_BUFFER)
            rv = 0
        elif lpWideCharStr != 0:
            if cbMultiByte == 0xFFFFFFFF:
                mbs = self.read_mem_string(lpMultiByteStr, 1)
                argv[2] = mbs
                mbs += '\x00'
                ws = mbs.encode('utf-16le')

                if len(ws) / 2 > cchWideChar:
                    emu.set_last_error(windefs.ERROR_INSUFFICIENT_BUFFER)
                    rv = 0
                else:
                    self.mem_write(lpWideCharStr, ws)
                    emu.set_last_error(windefs.ERROR_SUCCESS)
                    rv = len(ws)
            else:
                emu.set_last_error(windefs.ERROR_SUCCESS)
                cs = self.mem_read(lpMultiByteStr, cbMultiByte)
                cs = cs.decode('utf-8', 'ignore')
                cs = cs.encode('utf-16le')
                self.mem_write(lpWideCharStr, cs)
                rv = cbMultiByte

        return rv

    @apihook('GetStringTypeA', argc=5)
    def GetStringTypeA(self, emu, argv, ctx={}):
        '''
        BOOL GetStringTypeA(
            LCID   Locale,
            DWORD  dwInfoType,
            LPCSTR lpSrcStr,
            int    cchSrc,
            LPWORD lpCharType
        );
        '''
        args = argv[1:]
        return self.GetStringTypeW(emu, args, ctx)

    @apihook('GetStringTypeW', argc=4)
    def GetStringTypeW(self, emu, argv, ctx={}):
        '''
        BOOL GetStringTypeW(
          DWORD                         dwInfoType,
          _In_NLS_string_(cchSrc)LPCWCH lpSrcStr,
          int                           cchSrc,
          LPWORD                        lpCharType
        );
        '''
        dwInfoType, lpSrcStr, cchSrc, lpCharType = argv
        rv = 0

        cw = self.get_char_width(ctx)
        if not cw:
            cw = 2

        CT_CTYPE1 = 1

        C1_UPPER, C1_LOWER, C1_DIGIT, C1_SPACE = 0x001, 0x0002, 0x0004, 0x0008
        C1_PUNCT, C1_CNTRL, C1_BLANK, C1_XDIGIT = 0x010, 0x0020, 0x0040, 0x0080
        C1_ALPHA, C1_DEFINED = 0x0100, 0x0200

        if dwInfoType == CT_CTYPE1:
            # Walk each 16bit character
            wcs = self.mem_read(lpSrcStr, cchSrc * cw)
            output = b''
            for char in [wcs[i: i + cw] for i in range(0, len(wcs), cw)]:
                ctype = 0
                c = int.from_bytes(char, 'little')
                # Is punc?
                if ((c > 0x20 and c < 0x30) or (c >= 0x3A and c <= 0x40) or
                   (c >= 0x5B and c <= 0x60) or (c >= 0x7B and c <= 0x7E)):
                    ctype |= C1_PUNCT
                if c < 0x20 or c == 0x7f:
                    ctype |= C1_CNTRL
                if c >= 0x9 and c <= 0xd:
                    ctype |= C1_SPACE
                if c == 0x20:
                    ctype |= (C1_BLANK | C1_SPACE)
                if c >= 0x41 and c <= 0x5a:
                    ctype |= C1_UPPER
                if c >= 0x61 and c <= 0x7a:
                    ctype |= C1_LOWER
                if c >= 0x30 and c <= 0x39:
                    ctype |= C1_DIGIT
                if (c >= 0x30 and c <= 0x39) or (c >= 0x41 and c <= 0x46):
                    ctype |= C1_XDIGIT
                if (c >= 0x61 and c <= 0x66):
                    ctype |= C1_XDIGIT
                if (ctype & C1_UPPER) or (ctype & C1_LOWER):
                    ctype |= C1_ALPHA
                if c != 0:
                    ctype |= C1_DEFINED

                b = (ctype).to_bytes(2, 'little')
                output += b

            self.mem_write(lpCharType, output)

        return rv

    @apihook('LCMapString', argc=6)
    def LCMapString(self, emu, argv, ctx={}):
        '''
        int LCMapString(
          LCID    Locale,
          DWORD   dwMapFlags,
          LPTSTR lpSrcStr,
          int     cchSrc,
          LPTSTR  lpDestStr,
          int     cchDest
        );
        '''

        (Locale, dwMapFlags, lpSrcStr, cchSrc,
         lpDestStr, cchDest) = argv

        rv = 0
        cw = self.get_char_width(ctx)

        if lpSrcStr == 0 or cchSrc == 0:
            emu.set_last_error(windefs.ERROR_INVALID_PARAMETER)
        if lpDestStr == 0 or cchDest == 0:
            emu.set_last_error(windefs.ERROR_SUCCESS)
            rv = cchSrc
        else:
            data = self.mem_read(lpSrcStr, cchSrc * cw)
            self.mem_write(lpDestStr, data)
            rv = int(len(data) / cw)

        return rv

    @apihook('LCMapStringEx', argc=9)
    def LCMapStringEx(self, emu, argv, ctx={}):
        '''
        int LCMapStringEx(
          LPCWSTR          lpLocaleName,
          DWORD            dwMapFlags,
          LPCWSTR          lpSrcStr,
          int              cchSrc,
          LPWSTR           lpDestStr,
          int              cchDest,
          LPNLSVERSIONINFO lpVersionInformation,
          LPVOID           lpReserved,
          LPARAM           sortHandle
        );
        '''

        (lpLocaleName, dwMapFlags, lpSrcStr, cchSrc,
         lpDestStr, cchDest, ver_info, res, sort_handle) = argv

        rv = 0

        if lpSrcStr == 0 or cchSrc == 0:
            emu.set_last_error(windefs.ERROR_INVALID_PARAMETER)
        if lpDestStr == 0 or cchDest == 0:
            emu.set_last_error(windefs.ERROR_SUCCESS)
            rv = cchSrc
        else:
            data = self.mem_read(lpSrcStr, cchSrc * 2)
            self.mem_write(lpDestStr, data)
            rv = int(len(data) / 2)

        return rv

    @apihook('GetModuleFileName', argc=3)
    def GetModuleFileName(self, emu, argv, ctx={}):
        '''
        DWORD GetModuleFileName(
          HMODULE hModule,
          LPSTR   lpFilename,
          DWORD   nSize
        );
        '''
        hModule, lpFilename, nSize = argv
        size = 0
        cw = self.get_char_width(ctx)

        filename = ''
        if hModule == 0:
            proc = emu.get_current_process()
            filename = proc.get_process_path()
        else:
            mods = emu.get_user_modules()
            cm = emu.get_current_module()
            if cm.get_base() == hModule:
                filename = cm.get_emu_path()
            else:
                for mod in mods:
                    if mod.get_base() == hModule:
                        filename = mod.get_emu_path()

        if filename:
            argv[1] = filename
            if cw == 2:
                out = filename.encode('utf-16le')
            elif cw == 1:

                out = filename.encode('utf-8')

            size = int(len(out) / cw)
            if nSize < size + 1 * cw:  # null terminator
                emu.set_last_error(windefs.ERROR_INSUFFICIENT_BUFFER)
                out = out[:nSize - 1 * cw] + b'\0' * cw
            else:
                out += b'\0' * cw

            self.mem_write(lpFilename, out)
        return size

    @apihook('HeapFree', argc=3)
    def HeapFree(self, emu, argv, ctx={}):
        '''
        BOOL HeapFree(
          HANDLE                 hHeap,
          DWORD                  dwFlags,
          _Frees_ptr_opt_ LPVOID lpMem
        );
        '''
        rv = 1
        hHeap, dwFlags, lpMem = argv

        self.mem_free(lpMem)
        emu.set_last_error(windefs.ERROR_SUCCESS)
        return rv

    @apihook('LocalFree', argc=1)
    def LocalFree(self, emu, argv, ctx={}):
        '''
        HLOCAL LocalFree(
            _Frees_ptr_opt_ HLOCAL hMem
        );
        '''
        rv = 0
        hMem, = argv

        if hMem == 0:
            return rv

        self.mem_free(hMem)
        emu.set_last_error(windefs.ERROR_SUCCESS)
        return rv

    @apihook('GetSystemDirectory', argc=2)
    def GetSystemDirectory(self, emu, argv, ctx={}):
        '''
        UINT GetSystemDirectory(
          LPSTR lpBuffer,
          UINT  uSize
        );
        '''
        rv = 0
        lpBuffer, uSize = argv

        cw = self.get_char_width(ctx)
        fn = ctx['func_name']
        if 'GetWindowsDirectory' in fn:
            sysroot = emu.get_windows_dir()
        else:
            sysroot = emu.get_system_root()
        if sysroot:
            sysroot += '\x00'
            if cw == 2:
                out = sysroot.encode('utf-16le')
            elif cw == 1:
                out = sysroot.encode('utf-8')

            if len(sysroot) > uSize:
                emu.set_last_error(windefs.ERROR_INSUFFICIENT_BUFFER)
            else:
                self.mem_write(lpBuffer, out)
                emu.set_last_error(windefs.ERROR_SUCCESS)
                rv = len(sysroot)

        return rv

    @apihook('GetWindowsDirectory', argc=2)
    def GetWindowsDirectory(self, emu, argv, ctx={}):
        '''
        UINT GetWindowsDirectory(
            LPSTR lpBuffer,
            UINT  uSize
        );
        '''
        return self.GetSystemDirectory(emu, argv, ctx)

    @apihook('CreateFileMapping', argc=6)
    def CreateFileMapping(self, emu, argv, ctx={}):
        '''
        HANDLE CreateFileMapping(
          HANDLE                hFile,
          LPSECURITY_ATTRIBUTES lpFileMappingAttributes,
          DWORD                 flProtect,
          DWORD                 dwMaximumSizeHigh,
          DWORD                 dwMaximumSizeLow,
          LPTSTR                lpName
        );
        '''
        hfile, map_attrs, prot, max_size_high, max_size_low, map_name = argv

        cw = self.get_char_width(ctx)

        # Get to full map size
        size = (max_size_high << 32) | max_size_low

        name = ''
        if map_name:
            name = self.read_mem_string(map_name, cw)
            argv[5] = name

        hmap = self.file_create_mapping(hfile, name, size, prot)

        return hmap

    @apihook('MapViewOfFile', argc=5)
    def MapViewOfFile(self, emu, argv, ctx={}):
        '''
        LPVOID MapViewOfFile(
          HANDLE hFileMappingObject,
          DWORD  dwDesiredAccess,
          DWORD  dwFileOffsetHigh,
          DWORD  dwFileOffsetLow,
          SIZE_T dwNumberOfBytesToMap
        );
        '''
        hmap, access, offset_high, offset_low, bytes_to_map = argv

        fman = emu.get_file_manager()
        mapping = fman.get_mapping_from_handle(hmap)
        tag_prefix = 'api.MapViewOfFile'

        if mapping:
            f = mapping.get_backed_file()
            full_offset = (offset_high << 32) | offset_low
            buf = 0
            size = 0
            if f:
                data = f.get_data()
                if bytes_to_map != 0:
                    data = data[full_offset: full_offset + bytes_to_map]

                base, size = emu.get_valid_ranges(len(data))
                while base and base & 0xFFF:
                    base, size = emu.get_valid_ranges(size)

                buf = self.mem_alloc(base=base, size=size, shared=True)
                mm = emu.get_address_map(buf)
                fname = ntpath.basename(f.get_path())
                fname = fname.replace('.', '_')
                mm.update_tag('%s.%s.0x%x' % (tag_prefix, fname, buf))
                mapping.add_view(buf, full_offset, size, access)
                self.mem_write(buf, data)
                emu.set_last_error(windefs.ERROR_SUCCESS)
            else:
                base, size = emu.get_valid_ranges(bytes_to_map)
                buf = self.mem_alloc(base=base, size=size, tag=tag_prefix,
                                     shared=True)
                emu.set_last_error(windefs.ERROR_SUCCESS)
                mapping.add_view(buf, full_offset, size, access)

            for base, view in mapping.views.items():
                if base != buf and view.size == size and full_offset == view.offset:
                    data = self.mem_read(base, size)
                    self.mem_write(buf, data)

        else:
            emu.set_last_error(windefs.ERROR_INVALID_PARAMETER)

        return buf

    @apihook('UnmapViewOfFile', argc=1)
    def UnmapViewOfFile(self, emu, argv, ctx={}):
        '''
        BOOL UnmapViewOfFile(
          LPCVOID lpBaseAddress
        );
        '''
        lpBaseAddress, = argv
        rv = False

        mm = emu.get_address_map(lpBaseAddress)
        if mm:
            self.mem_free(lpBaseAddress)
            rv = True
            emu.set_last_error(windefs.ERROR_SUCCESS)
        else:
            emu.set_last_error(windefs.ERROR_INVALID_PARAMETER)

        return rv

    @apihook('GetSystemInfo', argc=1)
    def GetSystemInfo(self, emu, argv, ctx={}):
        '''
        void GetSystemInfo(
            LPSYSTEM_INFO lpSystemInfo
        );
        '''
        lpSystemInfo, = argv
        ptr_size = emu.get_ptr_size()
        si = self.k32types.SYSTEM_INFO(ptr_size)
        si.dwPageSize = 0x1000

        if ptr_size == 4:
            si.wProcessorArchitecture = k32types.PROCESSOR_ARCHITECTURE_INTEL
        else:
            si.wProcessorArchitecture = k32types.PROCESSOR_ARCHITECTURE_AMD64

        self.mem_write(lpSystemInfo, si.get_bytes())
        return

    @apihook('GetFileAttributes', argc=1)
    def GetFileAttributes(self, emu, argv, ctx={}):
        '''
        DWORD GetFileAttributes(
            LPCSTR lpFileName
        );
        '''
        fn, = argv
        cw = self.get_char_width(ctx)
        rv = windefs.INVALID_FILE_ATTRIBUTES
        target = self.read_mem_string(fn, cw)
        argv[0] = target
        if self.does_file_exist(target):
            rv = windefs.FILE_ATTRIBUTE_NORMAL
        return rv

    @apihook('CreateFile', argc=7)
    def CreateFile(self, emu, argv, ctx={}):
        '''
        HANDLE CreateFile(
          LPTSTR                lpFileName,
          DWORD                 dwDesiredAccess,
          DWORD                 dwShareMode,
          LPSECURITY_ATTRIBUTES lpSecurityAttributes,
          DWORD                 dwCreationDisposition,
          DWORD                 dwFlagsAndAttributes,
          HANDLE                hTemplateFile
        );
        '''
        fname, access, share, sec_attr, disp, flags, template = argv
        hnd = windefs.INVALID_HANDLE_VALUE

        cw = self.get_char_width(ctx)

        if not fname:
            return hnd

        target = self.read_mem_string(fname, cw)
        argv[0] = target

        ad = ddk.get_access_defines(access)
        if ad:
            argv[1] = ' | '.join(ad)

        cd = windefs.get_create_disposition(disp)
        if cd:
            argv[4] = cd

        obj = self.get_object_from_name(target)
        if obj:
            hnd = self.get_object_handle(obj)
        else:
            # Does the file being opened exist in our emulation space?
            op = 'open'
            if self.does_file_exist(target):
                if disp == windefs.CREATE_ALWAYS:
                    emu.set_last_error(windefs.ERROR_ALREADY_EXISTS)
                    hnd = self.file_open(target, create=True)
                    op = 'create'
                elif disp == windefs.CREATE_NEW:
                    # Function fails
                    emu.set_last_error(windefs.ERROR_FILE_EXISTS)
                elif disp == windefs.OPEN_ALWAYS:
                    emu.set_last_error(windefs.ERROR_ALREADY_EXISTS)
                    # Open the file
                    hnd = self.file_open(target, create=False)
                elif disp == windefs.OPEN_EXISTING:
                    emu.set_last_error(windefs.ERROR_SUCCESS)
                    hnd = self.file_open(target, create=False)
                elif disp == windefs.TRUNCATE_EXISTING:
                    emu.set_last_error(windefs.ERROR_SUCCESS)
                    hnd = self.file_open(target, create=False, truncate=True)

            # We don't have a handler for this file, create it
            else:
                op = 'create'
                if disp == windefs.CREATE_ALWAYS:
                    emu.set_last_error(windefs.ERROR_SUCCESS)
                    hnd = self.file_open(target, create=True)
                elif disp == windefs.CREATE_NEW:
                    emu.set_last_error(windefs.ERROR_SUCCESS)
                    hnd = self.file_open(target, create=True)
                elif disp == windefs.OPEN_ALWAYS:
                    emu.set_last_error(windefs.ERROR_ALREADY_EXISTS)
                    # Open the file
                    hnd = self.file_open(target, create=True)
                elif disp == windefs.OPEN_EXISTING:
                    emu.set_last_error(windefs.ERROR_FILE_NOT_FOUND)
                elif disp == windefs.TRUNCATE_EXISTING:
                    emu.set_last_error(windefs.ERROR_FILE_NOT_FOUND)

            self.log_file_access(target, op, disposition=cd, access=ad)
        return hnd

    @apihook('ReadFile', argc=5)
    def ReadFile(self, emu, argv, ctx={}):
        '''
        BOOL ReadFile(
          HANDLE       hFile,
          LPVOID       lpBuffer,
          DWORD        nNumberOfBytesToRead,
          LPDWORD      lpNumberOfBytesRead,
          LPOVERLAPPED lpOverlapped
        );
        '''
        def _write_output(emu, data, pBuffer, pBytesRead):
            self.mem_write(pBuffer, data)

            if pBytesRead:
                read = (len(data)).to_bytes(4, 'little')
                self.mem_write(pBytesRead, read)

        hFile, lpBuffer, num_bytes, bytes_read, lpOverlapped = argv
        rv = False

        f = self.file_get(hFile)
        if f:
            path = f.get_path()
            data = f.get_data(num_bytes)

            if lpBuffer:
                _write_output(emu, data, lpBuffer, bytes_read)

                self.log_file_access(path, 'read', buffer=lpBuffer, size=len(data))

                rv = True
                emu.set_last_error(windefs.ERROR_SUCCESS)
            return rv

        p = emu.pipe_get(hFile)
        if p:
            data = p.get_data(num_bytes)
            if not data:
                return False
            if lpBuffer:
                _write_output(emu, data, lpBuffer, bytes_read)
            rv = True
        return rv

    @apihook('WriteFile', argc=5)
    def WriteFile(self, emu, argv, ctx={}):
        """
         BOOL WriteFile(
          HANDLE       hFile,
          LPCVOID      lpBuffer,
          DWORD        nNumberOfBytesToWrite,
          LPDWORD      lpNumberOfBytesWritten,
          LPOVERLAPPED lpOverlapped
        );
        """
        hFile, lpBuffer, num_bytes, bytes_written, lpOverlapped = argv
        rv = 0

        f = self.file_get(hFile)
        if f:
            path = f.get_path()
            data = self.mem_read(lpBuffer, num_bytes)
            if data:
                f.add_data(data)
                # Log the file event
                self.log_file_access(path, 'write', data=data, buffer=lpBuffer, size=num_bytes)

                data = data.hex()
                argv[1] = "%s (%s)" % (hex(lpBuffer), data[:0x20])

                rv = 1
                emu.set_last_error(windefs.ERROR_SUCCESS)

        return rv

    @apihook('SetFilePointer', argc=4)
    def SetFilePointer(self, emu, argv, ctx={}):
        """
        DWORD SetFilePointer(
          HANDLE hFile,
          LONG   lDistanceToMove,
          PLONG  lpDistanceToMoveHigh,
          DWORD  dwMoveMethod
        );
        """
        hFile, lDistanceToMove, lpDistanceToMoveHigh, dwMoveMethod = argv
        rv = 0

        f = self.file_get(hFile)
        if f:
            # TODO add high offset, log access?
            f.seek(lDistanceToMove, dwMoveMethod)
            rv = 1
            emu.set_last_error(windefs.ERROR_SUCCESS)

        return rv

    @apihook('GetFileSize', argc=2)
    def GetFileSize(self, emu, argv, ctx={}):
        '''
        DWORD GetFileSize(
          HANDLE  hFile,
          LPDWORD lpFileSizeHigh
        );
        '''
        hFile, lpFileSizeHigh = argv

        f = self.file_get(hFile)

        if f:

            full_size = f.get_size()
            high = (0xFFFFFFFF & (full_size >> 32))
            low = 0xFFFFFFFF & full_size

            high = (high).to_bytes(4, 'little')

            if lpFileSizeHigh:
                self.mem_write(lpFileSizeHigh, high)
            emu.set_last_error(windefs.ERROR_SUCCESS)
        else:
            low = 0xFFFFFFFF
            emu.set_last_error(windefs.ERROR_INVALID_PARAMETER)

        return low

    @apihook('CloseHandle', argc=1)
    def CloseHandle(self, emu, argv, ctx={}):
        '''
        BOOL CloseHandle(
          HANDLE hObject
        );
        '''
        hObject, = argv
        obj = self.get_object_from_handle(hObject)
        if obj:
            return True
        return False

    @apihook('IsDebuggerPresent', argc=0)
    def IsDebuggerPresent(self, emu, argv, ctx={}):
        '''
        BOOL IsDebuggerPresent();
        '''

        return False

    @apihook('CreateEvent', argc=4)
    def CreateEvent(self, emu, argv, ctx={}):
        '''
        HANDLE CreateEvent(
            LPSECURITY_ATTRIBUTES lpEventAttributes,
            BOOL                  bManualReset,
            BOOL                  bInitialState,
            LPCSTR                lpName
        );
        '''
        attrs, reset, state, name = argv

        cw = self.get_char_width(ctx)
        evt_name = None
        if name:
            evt_name = self.read_mem_string(name, cw)
            argv[3] = evt_name

        obj = self.get_object_from_name(evt_name)

        if obj:
            hnd = obj.get_handle()
            emu.set_last_error(windefs.ERROR_ALREADY_EXISTS)
        else:
            hnd, evt = emu.create_event(evt_name)

        return hnd

    @apihook('OpenEvent', argc=3)
    def OpenEvent(self, emu, argv, ctx={}):
        '''
        HANDLE OpenEvent(
            DWORD  dwDesiredAccess,
            BOOL   bInheritHandle,
            LPCSTR lpName
        );
        '''
        access, inherit, name = argv

        cw = self.get_char_width(ctx)
        evt_name = None
        hnd = 0
        if name:
            evt_name = self.read_mem_string(name, cw)
            argv[2] = evt_name

        obj = self.get_object_from_name(evt_name)

        if obj:
            hnd = obj.get_handle()
            emu.set_last_error(windefs.ERROR_ALREADY_EXISTS)
        else:
            emu.set_last_error(windefs.ERROR_PATH_NOT_FOUND)

        return hnd

    @apihook('SetEvent', argc=1)
    def SetEvent(self, emu, argv, ctx={}):
        '''
        BOOL SetEvent(
            HANDLE hEvent
        );
        '''
        hEvent, = argv

        obj = self.get_object_from_handle(hEvent)

        rv = False
        if obj:
            emu.set_last_error(windefs.ERROR_SUCCESS)
            rv = True
        return rv

    @apihook('SetUnhandledExceptionFilter', argc=1)
    def SetUnhandledExceptionFilter(self, emu, argv, ctx={}):
        '''
        LPTOP_LEVEL_EXCEPTION_FILTER SetUnhandledExceptionFilter(
          LPTOP_LEVEL_EXCEPTION_FILTER lpTopLevelExceptionFilter
        );
        '''

        return 0

    @apihook('DeleteCriticalSection', argc=1)
    def DeleteCriticalSection(self, emu, argv, ctx={}):
        '''
        void DeleteCriticalSection(
          LPCRITICAL_SECTION lpCriticalSection
        );
        '''

        return None

    @apihook('FlsFree', argc=1)
    def FlsFree(self, emu, argv, ctx={}):
        '''
        BOOL FlsFree(
          DWORD dwFlsIndex
        );
        '''

        return True

    @apihook('TlsFree', argc=1)
    def TlsFree(self, emu, argv, ctx={}):
        '''
        BOOL TlsFree(
          DWORD dwTlsIndex
        );
        '''

        return True

    @apihook('ProcessIdToSessionId', argc=2)
    def ProcessIdToSessionId(self, emu, argv, ctx={}):
        '''
        BOOL ProcessIdToSessionId(
          DWORD dwProcessId,
          DWORD *pSessionId
        );
        '''
        dwProcessId, pSessionId = argv
        rv = False

        if pSessionId:
            for p in emu.get_processes():
                if p.get_id() == dwProcessId:
                    sessid = p.get_session_id()
                    sessid = (sessid).to_bytes(4, 'little')
                    self.mem_write(pSessionId, sessid)
                    rv = True
        else:
            windefs.ERROR_INVALID_PARAMETER

        return rv

    @apihook('InitializeCriticalSectionEx', argc=3)
    def InitializeCriticalSectionEx(self, emu, argv, ctx={}):
        '''
        BOOL InitializeCriticalSectionEx(
          LPCRITICAL_SECTION lpCriticalSection,
          DWORD              dwSpinCount,
          DWORD              Flags
        );
        '''

        emu.set_last_error(windefs.ERROR_SUCCESS)
        return True

    @apihook('InitializeCriticalSection', argc=1)
    def InitializeCriticalSection(self, emu, argv, ctx={}):
        '''
        void InitializeCriticalSection(
          LPCRITICAL_SECTION lpCriticalSection
        );
        '''

        emu.set_last_error(windefs.ERROR_SUCCESS)
        return None

    @apihook('GetOEMCP', argc=0)
    def GetOEMCP(self, emu, argv, ctx={}):
        '''
        UINT GetOEMCP();
        '''
        return 1200

    @apihook('GlobalLock', argc=1)
    def GlobalLock(self, emu, argv, ctx={}):
        '''
        LPVOID GlobalLock(
          HGLOBAL hMem
        );
        '''
        hMem, = argv

        emu.set_last_error(windefs.ERROR_SUCCESS)
        return hMem

    @apihook('HeapDestroy', argc=1)
    def HeapDestroy(self, emu, argv, ctx={}):
        '''
        BOOL HeapDestroy(
          HANDLE hHeap
        );
        '''

        return True

    @apihook('InitializeSListHead', argc=1)
    def InitializeSListHead(self, emu, argv, ctx={}):
        '''
        void InitializeSListHead(
          PSLIST_HEADER ListHead
        );
        '''
        ListHead, = argv

        self.mem_write(ListHead, b'\x00' * 8)

        return None

    @apihook('FreeLibrary', argc=1)
    def FreeLibrary(self, emu, argv, ctx={}):
        '''
        BOOL FreeLibrary(
          HMODULE hLibModule
        );
        '''

        return True

    @apihook('WaitForSingleObject', argc=2)
    def WaitForSingleObject(self, emu, argv, ctx={}):
        '''
        DWORD WaitForSingleObject(
        HANDLE hHandle,
        DWORD  dwMilliseconds
        );
        '''
        hHandle, dwMilliseconds = argv

        return 0

    @apihook('GetConsoleMode', argc=2)
    def GetConsoleMode(self, emu, argv, ctx={}):
        '''
        BOOL WINAPI GetConsoleMode(
            _In_  HANDLE  hConsoleHandle,
            _Out_ LPDWORD lpMode
        );
        '''

        return True

    @apihook('HeapSetInformation', argc=4)
    def HeapSetInformation(self, emu, argv, ctx={}):
        '''
        BOOL HeapSetInformation(
            HANDLE                 HeapHandle,
            HEAP_INFORMATION_CLASS HeapInformationClass,
            PVOID                  HeapInformation,
            SIZE_T                 HeapInformationLength
        );
        '''

        return True

    @apihook('SetErrorMode', argc=1)
    def SetErrorMode(self, emu, argv, ctx={}):
        '''
        UINT SetErrorMode(
            UINT uMode
        );
        '''
        return 0

    @apihook('InterlockedCompareExchange', argc=3)
    def InterlockedCompareExchange(self, emu, argv, ctx={}):
        '''
        LONG InterlockedCompareExchange(
        LONG volatile *Destination,
        LONG          ExChange,
        LONG          Comperand
        );
        '''
        pDest, ExChange, Comperand = argv

        dest_bytes = self.mem_read(pDest, 4)
        dest = int.from_bytes(dest_bytes, 'little')

        if dest == Comperand:
            self.mem_write(pDest, dest_bytes)

        return dest

    @apihook('InterlockedExchange', argc=2)
    def InterlockedExchange(self, emu, argv, ctx={}):
        '''
        LONG InterlockedExchange(
        LONG volatile *Target,
        LONG          Value
        );
        '''
        Target, Value = argv
        tgt = self.mem_read(Target, 4)
        tgt = int.from_bytes(tgt, 'little')

        val = (Value).to_bytes(4, byteorder='little')

        self.mem_write(Target, val)

        return tgt

    @apihook('CreateNamedPipe', argc=8)
    def CreateNamedPipe(self, emu, argv, ctx={}):
        '''
        HANDLE CreateNamedPipe(
            LPCSTR                lpName,
            DWORD                 dwOpenMode,
            DWORD                 dwPipeMode,
            DWORD                 nMaxInstances,
            DWORD                 nOutBufferSize,
            DWORD                 nInBufferSize,
            DWORD                 nDefaultTimeOut,
            LPSECURITY_ATTRIBUTES lpSecurityAttributes
        );
        '''
        (lpName, dwOpenMode, dwPipeMode, nMaxInstances, nOutBufferSize, nInBufferSize,
         nDefaultTimeOut, lpSecurityAttributes) = argv

        cw = self.get_char_width(ctx)

        pipe_name = ''
        if lpName:
            pipe_name = self.read_mem_string(lpName, cw)
            argv[0] = pipe_name

        hnd = emu.pipe_open(pipe_name, dwOpenMode, nMaxInstances, nOutBufferSize, nInBufferSize)

        return hnd

    @apihook('ConnectNamedPipe', argc=2)
    def ConnectNamedPipe(self, emu, argv, ctx={}):
        '''
        BOOL ConnectNamedPipe(
            HANDLE       hNamedPipe,
            LPOVERLAPPED lpOverlapped
        );
        '''
        hNamedPipe, lpOverlapped = argv
        rv = False
        pipe = emu.pipe_get(hNamedPipe)
        if pipe:
            rv = True
        return rv

    @apihook('DisconnectNamedPipe', argc=1)
    def DisconnectNamedPipe(self, emu, argv, ctx={}):
        '''
        BOOL DisconnectNamedPipe(
            HANDLE hNamedPipe
        );
        '''
        hNamedPipe, = argv
        rv = False
        pipe = emu.pipe_get(hNamedPipe)
        if pipe:
            rv = True
        return rv

    @apihook('GetLocaleInfo', argc=4)
    def GetLocaleInfo(self, emu, argv, ctx={}):
        '''
        int GetLocaleInfoA(
          LCID   Locale,
          LCTYPE LCType,
          LPSTR  lpLCData,
          int    cchData
        );
        '''

        rv = 0

        return rv

    @apihook('IsWow64Process', argc=2)
    def IsWow64Process(self, emu, argv, ctx={}):
        '''
        BOOL IsWow64Process(
            HANDLE hProcess,
            PBOOL  Wow64Process
        );
        '''
        hProcess, Wow64Process = argv
        rv = False

        if Wow64Process:
            self.mem_write(Wow64Process, (0).to_bytes(4, 'little'))
            rv = True

        return rv

    @apihook('CheckRemoteDebuggerPresent', argc=2)
    def CheckRemoteDebuggerPresent(self, emu, argv, ctx={}):
        '''
        BOOL CheckRemoteDebuggerPresent(
            HANDLE hProcess,
            PBOOL  pbDebuggerPresent
        );
        '''
        hProcess, pbDebuggerPresent = argv
        rv = False

        if pbDebuggerPresent:
            self.mem_write(pbDebuggerPresent, (0).to_bytes(4, 'little'))
            rv = True

        return rv

    @apihook('GetComputerName', argc=2)
    def GetComputerName(self, emu, argv, ctx={}):
        '''
    BOOL GetComputerName(
        LPSTR   lpBuffer,
        LPDWORD nSize
    );
        '''

        lpBuffer, nSize = argv
        rv = False
        cw = self.get_char_width(ctx)

        host = emu.get_hostname()

        if lpBuffer and host:
            if cw == 2:
                out = host.encode('utf-16le')
            elif cw == 1:
                out = host.encode('utf-8')
            self.mem_write(lpBuffer, out)
            rv = True
        if nSize:
            self.mem_write(nSize, (len(host)).to_bytes(4, 'little'))

        return rv

    @apihook('GetVersionEx', argc=1)
    def GetVersionEx(self, emu, argv, ctx={}):
        '''
        NOT_BUILD_WINDOWS_DEPRECATE BOOL GetVersionEx(
          LPOSVERSIONINFO lpVersionInformation
        );
        '''
        lpVersionInformation, = argv

        osver = self.k32types.OSVERSIONINFO(emu.get_ptr_size())
        osver = self.mem_cast(osver, lpVersionInformation)

        # Its an OSVERSIONINFO object
        if osver.dwOSVersionInfoSize == osver.sizeof():
            pass

        ver = emu.get_os_version()
        osver.dwMajorVersion = ver['major']
        osver.dwMinorVersion = ver['minor']
        osver.dwBuildNumber = ver['build']
        osver.dwPlatformId = 2

        rv = 1
        self.mem_write(lpVersionInformation, osver.get_bytes())

        return rv

    @apihook('GetEnvironmentVariable', argc=3)
    def GetEnvironmentVariable(self, emu, argv, ctx={}):
        '''
        DWORD GetEnvironmentVariable(
        LPCTSTR lpName,
        LPTSTR  lpBuffer,
        DWORD   nSize
        );
        '''

        lpName, lpBuffer, nSize = argv
        rv = 0

        cw = self.get_char_width(ctx)

        name = self.read_mem_string(lpName, cw)
        argv[0] = name
        env = emu.get_env()

        var = env.get(name.lower())
        if var:
            var += '\x00'
            if cw == 2:
                new = (var).encode('utf-16le')
            else:
                new = (var).encode('utf-8')
            self.mem_write(lpBuffer, new)
            rv = len(var)

        return rv

    @apihook('GetCurrentPackageId', argc=2)
    def GetCurrentPackageId(self, emu, argv, ctx={}):
        '''
        LONG GetCurrentPackageId(
            UINT32 *bufferLength,
            BYTE   *buffer
        );
        '''
        return windefs.ERROR_SUCCESS

    @apihook('AreFileApisANSI', argc=0)
    def AreFileApisANSI(self, emu, argv, ctx={}):
        '''
        BOOL AreFileApisANSI();
        '''
        return True

    @apihook('FindFirstFileEx', argc=6)
    def FindFirstFileEx(self, emu, argv, ctx={}):
        '''
        HANDLE FindFirstFileExA(
            LPCSTR             lpFileName,
            FINDEX_INFO_LEVELS fInfoLevelId,
            LPVOID             lpFindFileData,
            FINDEX_SEARCH_OPS  fSearchOp,
            LPVOID             lpSearchFilter,
            DWORD              dwAdditionalFlags
        );
        '''
        lpFileName, fInfoLevelId, lpFindFileData, fSearchOp, lpSearchFilter, flags, = argv

        _argv = [lpFileName, lpFindFileData]
        rv = self.FindFirstFile(emu, _argv, ctx)
        argv[0] = _argv[0]

        return rv

    @apihook('FindFirstFile', argc=2)
    def FindFirstFile(self, emu, argv, ctx={}):
        '''
        HANDLE FindFirstFileA(
            LPCSTR             lpFileName,
            LPWIN32_FIND_DATAA lpFindFileData
        );
        '''

        lpFileName, lpFindFileData = argv

        cw = self.get_char_width(ctx)

        if not lpFileName or not lpFindFileData:
            return windefs.INVALID_HANDLE_VALUE

        srch = self.read_mem_string(lpFileName, cw)
        argv[0] = srch

        fm = emu.get_file_manager()
        fw = fm.walk_files()
        hnd = self.get_handle()
        self.find_files.update({hnd: {"search": srch, "walker": fw}})

        curr_file = next(fw)
        curr_file = ntpath.basename(curr_file)

        if cw == 2:
            cfn = curr_file.encode('utf-16le')
        else:
            cfn = curr_file.encode('utf-8')

        if fnmatch.fnmatch(curr_file, srch):
            find_data = k32types.WIN32_FIND_DATA(emu.get_ptr_size(), cw)
            find_data.dwFileAttributes = k32types.FILE_ATTRIBUTE_NORMAL
            find_data.cFileName = cfn

            self.mem_write(lpFindFileData, find_data.get_bytes())

        return hnd

    @apihook('FindNextFile', argc=2)
    def FindNextFile(self, emu, argv, ctx={}):
        '''
        BOOL FindNextFile(
            HANDLE             hFindFile,
            LPWIN32_FIND_DATAA lpFindFileData
        );
        '''

        hFindFile, lpFindFileData = argv
        rv = 1

        cw = self.get_char_width(ctx)

        fsearch = self.find_files.get(hFindFile)

        if not hFindFile or not lpFindFileData or not fsearch:
            return windefs.INVALID_HANDLE_VALUE

        search = fsearch.get('search', '').lower()
        walker = fsearch.get('walker')
        try:
            next_file = next(walker).lower()
        except StopIteration:
            return 0

        if fnmatch.fnmatch(next_file, search):
            next_file = ntpath.basename(next_file)
            argv[1] = next_file

            if cw == 2:
                cfn = next_file.encode('utf-16le')
            else:
                cfn = next_file.encode('utf-8')

            find_data = k32types.WIN32_FIND_DATA(emu.get_ptr_size(), cw)
            find_data.dwFileAttributes = k32types.FILE_ATTRIBUTE_NORMAL
            find_data.cFileName = cfn
            self.mem_write(lpFindFileData, find_data.get_bytes())

        return rv

    @apihook('FindClose', argc=1)
    def FindClose(self, emu, argv, ctx={}):
        '''
        BOOL FindClose(
            HANDLE hFindFile
        );
        '''

        hFindFile, = argv

        try:
            self.find_files.pop(hFindFile)
        except KeyError:
            return False

        return True

    @apihook('GetThreadContext', argc=2)
    def GetThreadContext(self, emu, argv, ctx={}):
        '''
        BOOL GetThreadContext(
            HANDLE    hThread,
            LPCONTEXT lpContext
        );
        '''

        hThread, lpContext = argv

        obj = self.get_object_from_handle(hThread)
        if not obj:
            return False

        context = obj.get_context()

        self.mem_write(lpContext, context.get_bytes())

        return True

    @apihook('SetThreadContext', argc=2)
    def SetThreadContext(self, emu, argv, ctx={}):
        '''
        BOOL SetThreadContext(
            HANDLE        hThread,
            const CONTEXT *lpContext
        );
        '''

        hThread, lpContext = argv

        obj = self.get_object_from_handle(hThread)
        if not obj:
            return False

        context = windefs.CONTEXT(emu.get_ptr_size())
        if lpContext:
            _context = self.mem_cast(context, lpContext)
            obj.set_context(_context)

        return True

    @apihook('CompareFileTime', argc=2)
    def CompareFileTime(self, emu, argv, ctx={}):
        '''
        LONG CompareFileTime(
            const FILETIME *lpFileTime1,
            const FILETIME *lpFileTime2
        );
        '''

        lpFileTime1, lpFileTime2 = argv
        rv = 0

        ft = self.k32types.FILETIME(emu.get_ptr_size())

        ft1 = self.mem_cast(ft, lpFileTime1)
        ft2 = self.mem_cast(ft, lpFileTime2)

        time1 = (ft1.dwHighDateTime << 32) | ft1.dwLowDateTime
        time2 = (ft2.dwHighDateTime << 32) | ft2.dwLowDateTime

        if time1 == time2:
            rv = 0
        elif time1 < time2:
            rv = -1
        else:
            rv = 1

        return rv

    @apihook('FindResource', argc=3)
    def FindResource(self, emu, argv, ctx={}):
        '''
        HRSRC FindResourceA(
            HMODULE hModule,
            LPCSTR  lpName,
            LPCSTR  lpType
        );
        '''

        cw = self.get_char_width(ctx)
        hModule, lpName, lpType = argv
        if hModule == 0:
            pe = emu.modules[0][0]
        else:
            pe = emu.get_mod_from_addr(hModule)
            if pe and hModule != pe.get_base():
                return 0

        name = self.normalize_res_identifier(emu, cw, lpName)
        type_ = self.normalize_res_identifier(emu, cw, lpType)
        res = self.find_resource(pe, name, type_)
        if res is None:
            return 0

        hnd = self.get_handle()
        self.find_resources.update({hnd: {"ptr": pe.get_base() + res.data.struct.OffsetToData,
                                          "size": res.data.struct.Size}})
        return hnd

    @apihook('LoadResource', argc=2)
    def LoadResource(self, emu, argv, ctx={}):
        '''
        HGLOBAL LoadResource(
          HMODULE hModule,
          HRSRC   hResInfo
        );
        '''

        hModule, hResInfo = argv
        return hResInfo  # we're just using the same handle to index the resource data

    @apihook('LockResource', argc=1)
    def LockResource(self, emu, argv, ctx={}):
        '''
        LPVOID LockResource(
          HGLOBAL hResData
        );
        '''

        res = self.find_resources.get(argv[0], None)
        if res is None:
            return 0

        return res['ptr']

    @apihook('GetCurrentDirectory', argc=2)
    def GetCurrentDirectory(self, emu, argv, ctx={}):
        '''
        DWORD GetCurrentDirectory(
            DWORD  nBufferLength,
            LPTSTR lpBuffer
        );
        '''
        nBufferLength, lpBuffer = argv

        cw = self.get_char_width(ctx)
        cd = emu.get_cd()
        if cw == 1:
            data = cd.encode('utf-8')
        else:
            data = cd.encode('utf-16le')

        if len(cd) > nBufferLength:
            return 0

        self.mem_write(lpBuffer, data)

        return len(cd)

    @apihook('SizeofResource', argc=2)
    def SizeofResource(self, emu, argv, ctx={}):
        '''
        DWORD SizeofResource(
          HMODULE hModule,
          HRSRC   hResInfo
        );
        '''

        hModule, hResInfo = argv

        if hModule == 0:
            pe = emu.modules[0][0]
        else:
            pe = emu.get_mod_from_addr(hModule)
            if pe and hModule != pe.get_base():
                return 0

        res = self.find_resources.get(hResInfo, None)
        if res is None:
            return 0

        return res['size']

    @apihook('VirtualAllocExNuma', argc=6)
    def VirtualAllocExNuma(self, emu, argv, ctx={}):
        '''
        LPVOID VirtualAllocExNuma(
          HANDLE hProcess,
          LPVOID lpAddress,
          SIZE_T dwSize,
          DWORD  flAllocationType,
          DWORD  flProtect,
          DWORD  nndPreferred
        );
        '''

        argv = argv[:-1]
        return self.VirtualAllocEx(emu, argv, ctx)

    @apihook('GetNativeSystemInfo', argc=1)
    def GetNativeSystemInfo(self, emu, argv, ctx={}):
        '''
        void GetNativeSystemInfo(
          LPSYSTEM_INFO lpSystemInfo
        );
        '''
        lpSystemInfo, = argv
        return 0

    @apihook('GetUserDefaultUILanguage', argc=0)
    def GetUserDefaultUILanguage(self, emu, argv, ctx={}):
        '''
        LANGID GetUserDefaultUILanguage();
        '''
        return 0xffff

    @apihook('SetCurrentDirectory', argc=1)
    def SetCurrentDirectory(self, emu, argv, ctx={}):
        '''
        BOOL SetCurrentDirectory(
            LPCTSTR lpPathName
        );
        '''
        path, = argv

        if path:
            cw = self.get_char_width(ctx)
            path_str = self.read_mem_string(path, cw)
            argv[0] = path_str
            emu.set_cd(path_str)

        return True

    @apihook('OpenThread', argc=3)
    def OpenThread(self, emu, argv, ctx={}):
        '''
        HANDLE OpenThread(
            DWORD dwDesiredAccess,
            BOOL  bInheritHandle,
            DWORD dwThreadId
        );
        '''
        access, bInheritHandle, dwThreadId = argv
        thread = emu.get_object_from_id(dwThreadId)
        hnd = emu.get_object_handle(thread)
        if not hnd:
            hnd = 0
        return hnd

    @apihook('RaiseException', argc=4)
    def RaiseException(self, emu, argv, ctx={}):
        '''
        VOID RaiseException(
            DWORD           dwExceptionCode,
            DWORD           dwExceptionFlags,
            DWORD           nNumberOfArguments,
            const ULONG_PTR *lpArguments
        );
        '''
        # Stub
        dwExceptionCode, dwExceptionFlags, nNumberOfArguments, lpArguments = argv

        return

    @apihook('VerSetConditionMask', argc=3)
    def VerSetConditionMask(self, emu, argv, ctx={}):
        '''
        NTSYSAPI ULONGLONG VerSetConditionMask(
            ULONGLONG ConditionMask,
            DWORD     TypeMask,
            BYTE      Condition
        );
        '''
        # Stub
        con_mask, type_mask, cond = argv

        return 0

    @apihook('VerifyVersionInfo', argc=3)
    def VerifyVersionInfo(self, emu, argv, ctx={}):
        '''
        BOOL VerifyVersionInfo(
            LPOSVERSIONINFOEX lpVersionInformation,
            DWORD              dwTypeMask,
            DWORDLONG          dwlConditionMask
        );
        '''
        # Stub
        vinfo, type_mask, con_mask = argv

        return True

    @apihook('IsBadWritePtr', argc=2)
    def IsBadWritePtr(self, emu, argv, ctx={}):
        '''
        BOOL IsBadWritePtr(
            LPVOID   lp,
            UINT_PTR ucb
        );
        '''
        lp, ucb = argv

        rv = True

        if lp and ucb:
            v1 = emu.is_address_valid(lp)
            v2 = emu.is_address_valid(lp + (ucb - 1))

            if v1 and v2:
                rv = False

        return rv

    @apihook('GetSystemFirmwareTable', argc=4)
    def GetSystemFirmwareTable(self, emu, argv, ctx={}):
        '''
        UINT GetSystemFirmwareTable(
            DWORD FirmwareTableProviderSignature,
            DWORD FirmwareTableID,
            PVOID pFirmwareTableBuffer,
            DWORD BufferSize
        );
        '''
        # Stub
        sig, tid, firm_buf, buf_size = argv

        if not firm_buf:
            rv = 0x100
        else:
            self.mem_write(firm_buf, b'\x01'*buf_size)
            rv = buf_size
        return rv

    @apihook('GetTempPath', argc=2)
    def GetTempPath(self, emu, argv, ctx={}):
        '''
        DWORD GetTempPathA(
        DWORD nBufferLength,
        LPSTR lpBuffer
        );
        '''

        nBufferLength, lpBuffer = argv
        rv = 0
        cw = self.get_char_width(ctx)
        tempdir = emu.get_env().get('temp', 'C:\\Windows\\temp\\')
        if cw == 2:
            new = (tempdir).encode('utf-16le') + b'\x00\x00'
        else:
            new = (tempdir).encode('utf-8') + b'\x00'
        rv = len(tempdir)
        if lpBuffer:
            argv[1] = tempdir
            self.mem_write(lpBuffer, new)
        return rv

    @apihook('SetPriorityClass', argc=2)
    def SetPriorityClass(self, emu, argv, ctx={}):
        '''
        BOOL SetPriorityClass(
        HANDLE hProcess,
        DWORD  dwPriorityClass
        );
        '''
        return 1

    @apihook('GetDriveType', argc=1)
    def GetDriveType(self, emu, argv, ctx={}):
        '''
        UINT GetDriveType(
        LPCSTR lpRootPathName
        );
        '''
        lpRootPathName, = argv
        DRIVE_FIXED = 3
        cw = self.get_char_width(ctx)
        name = self.read_mem_string(lpRootPathName, cw)
        argv[0] = name

        return DRIVE_FIXED

    @apihook('GetExitCodeProcess', argc=2)
    def GetExitCodeProcess(self, emu, argv, ctx={}):
        '''
        BOOL GetExitCodeProcess(
        HANDLE  hProcess,
        LPDWORD lpExitCode
        );
        '''
        hProcess, lpExitCode = argv
        if lpExitCode:
            self.mem_write(lpExitCode, b'\x00'*4)
        return 1

    @apihook('SetThreadPriority', argc=2)
    def SetThreadPriority(self, emu, argv, ctx={}):
        '''
        BOOL SetThreadPriority(
        HANDLE hThread,
        int    nPriority
        );
        '''
        return 1

    @apihook('ReleaseMutex', argc=1)
    def ReleaseMutex(self, emu, argv, ctx={}):
        '''
        BOOL ReleaseMutex(
            HANDLE hMutex
        );
        '''
        return 1

    @apihook('GetShortPathName', argc=3)
    def GetShortPathName(self, emu, argv, ctx={}):
        '''
        DWORD GetShortPathNameW(
          LPCWSTR lpszLongPath,
          LPWSTR  lpszShortPath,
          DWORD   cchBuffer
        );
        https://en.wikipedia.org/wiki/8.3_filename#VFAT_and_Computer-generated_8.3_filenames
        '''
        lpszLongPath, lpszShortPath, cchBuffer = argv
        cw = self.get_char_width(ctx)
        s = self.read_mem_string(lpszLongPath, cw)
        argv[0] = s
        files = s.split('\\')
        out = files[0] + '\\'
        for i, file in enumerate(files):
            if i == 0:
                continue

            file = file.upper()
            file = file.lstrip('.')
            file = file.rstrip('.')
            file = file.replace('+', '_')
            file = file.replace(' ', '')
            parts = file.rsplit('.', 1)
            if len(parts) == 2:
                file, ext = parts
            else:
                ext = None

            if len(file) > 8:
                file = file[:6]
                file += '~1'

            if ext:
                ext = ext[:3]
                file += '.' + ext

            out += file
            if i != len(files) - 1:
                out += '\\'

        if lpszShortPath and len(out) + 1 <= cchBuffer:
            argv[1] = out
            self.write_mem_string(out, lpszShortPath, cw)

        return len(out) + 1

    @apihook('QueueUserAPC', argc=3)
    def QueueUserAPC(self, emu, argv, ctx={}):
        """
        DWORD QueueUserAPC(
        PAPCFUNC  pfnAPC,
        HANDLE    hThread,
        ULONG_PTR dwData
        );
        """
        pfnAPC, hThread, dwData = argv
        run_type = 'apc_thread_%x' % hThread
        self.create_thread(pfnAPC, dwData, 0, thread_type=run_type)