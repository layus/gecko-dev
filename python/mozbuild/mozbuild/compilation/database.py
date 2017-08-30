# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# This modules provides functionality for dealing with code completion.

import os
import types

from mozbuild.compilation import util
from mozbuild.backend.common import CommonBackend
from mozbuild.frontend.data import (
    Sources,
    GeneratedSources,
    DirectoryTraversal,
    Defines,
    Linkable,
    SharedLibrary,
    LocalInclude,
    PerSourceFlag,
    VariablePassthru,
    SimpleProgram,
    Program
)
from mozbuild.shellutil import (
    quote as shell_quote,
)
from mozbuild.util import expand_variables
import mozpack.path as mozpath
from collections import (
    defaultdict,
    OrderedDict,
)


class CompileDBBackend(CommonBackend):
    def _init(self):
        CommonBackend._init(self)
        if not util.check_top_objdir(self.environment.topobjdir):
            raise Exception()

        # The database we're going to dump out to.
        self._db = OrderedDict()

        # The cache for per-directory flags
        self._flags = {}

        self._envs = {}
        self._includes = defaultdict(list)
        self._defines = defaultdict(list)
        self._local_flags = defaultdict(dict)
        self._per_source_flags = defaultdict(list)
        self._extra_includes = defaultdict(list)
        self._gyp_dirs = set()
        self._dist_include_testing = '-I%s' % mozpath.join(
            self.environment.topobjdir, 'dist', 'include', 'testing')

    def consume_object(self, obj):
        # Those are difficult directories, that will be handled later.
        if obj.relativedir in (
                'build/unix/elfhack',
                'build/unix/elfhack/inject',
                'build/clang-plugin',
                'build/clang-plugin/tests',
                'toolkit/crashreporter/google-breakpad/src/common'):
            return True

        consumed = CommonBackend.consume_object(self, obj)

        if consumed:
            return True

        if isinstance(obj, DirectoryTraversal):
            self._envs[obj.objdir] = obj.config
            for var in ('STL_FLAGS', 'VISIBILITY_FLAGS', 'WARNINGS_AS_ERRORS'):
                value = obj.config.substs.get(var)
                if value:
                    self._local_flags[obj.objdir][var] = value

        elif isinstance(obj, (Sources, GeneratedSources)):
            if isinstance(obj.defines, Defines): # As opposed to HostDefines
                for d in obj.defines.get_defines():
                    if d not in self._defines[obj.objdir]:
                        self._defines[obj.objdir].append(d)
            # For other sources, include each source file.
            for f in obj.files:
                self._compiler_db_entry(obj.objdir, obj.relativedir, obj.config, f,
                                    obj.canonical_suffix)

        elif isinstance(obj, Program):
            self._linker_db_entry(obj, obj.objdir, obj.relativedir, obj.config, obj.program)

        elif isinstance(obj, SharedLibrary):
            self._linker_db_entry(obj, obj.objdir, obj.relativedir, obj.config, obj.lib_name)

        elif isinstance(obj, LocalInclude):
            self._includes[obj.objdir].append('-I%s' % mozpath.normpath(
                obj.path.full_path))

        elif isinstance(obj, Linkable):
            if isinstance(obj.defines, Defines): # As opposed to HostDefines
                for d in obj.defines.get_defines():
                    if d not in self._defines[obj.objdir]:
                        self._defines[obj.objdir].append(d)
            self._defines[obj.objdir].extend(obj.lib_defines.get_defines())
            if isinstance(obj, SimpleProgram) and obj.is_unit_test:
                if (self._dist_include_testing not in
                        self._extra_includes[obj.objdir]):
                    self._extra_includes[obj.objdir].append(
                        self._dist_include_testing)

        elif isinstance(obj, VariablePassthru):
            if obj.variables.get('IS_GYP_DIR'):
                self._gyp_dirs.add(obj.objdir)
            for var in ('MOZBUILD_CFLAGS', 'MOZBUILD_CXXFLAGS',
                        'MOZBUILD_CMFLAGS', 'MOZBUILD_CMMFLAGS',
                        'MOZBUILD_ASFLAGS', 'ASFLAGS',
                        'AS', 'AS_DASH_C_FLAG',
                        'RTL_FLAGS', 'VISIBILITY_FLAGS'):
                if var in obj.variables:
                    self._local_flags[obj.objdir][var] = obj.variables[var]
            if (obj.variables.get('DISABLE_STL_WRAPPING') and
                    'STL_FLAGS' in self._local_flags[obj.objdir]):
                del self._local_flags[obj.objdir]['STL_FLAGS']
            if (obj.variables.get('ALLOW_COMPILER_WARNINGS') and
                    'WARNINGS_AS_ERRORS' in self._local_flags[obj.objdir]):
                del self._local_flags[obj.objdir]['WARNINGS_AS_ERRORS']

        elif isinstance(obj, PerSourceFlag):
            self._per_source_flags[obj.file_name].extend(obj.flags)

        return True

    def consume_finished(self):
        CommonBackend.consume_finished(self)

        db = []

        for (directory, filename, unified), cmd in self._db.iteritems():
            env = self._envs[directory]
            cmd = list(cmd)
            if unified is None:
                cmd.append(filename)
            else:
                cmd.append(unified)
            local_extra = list(self._extra_includes[directory])
            if directory not in self._gyp_dirs:
                for var in (
                    'NSPR_CFLAGS',
                    'NSS_CFLAGS',
                    'MOZ_JPEG_CFLAGS',
                    'MOZ_PNG_CFLAGS',
                    'MOZ_ZLIB_CFLAGS',
                    'MOZ_PIXMAN_CFLAGS',
                ):
                    f = env.substs.get(var)
                    if f:
                        local_extra.extend(f)
            variables = dict(env.substs)
            variables.update({
                'LOCAL_INCLUDES': self._includes[directory],
                'DEFINES': self._defines[directory],
                'EXTRA_INCLUDES': local_extra,
                'DIST': mozpath.join(env.topobjdir, 'dist'),
                'DEPTH': env.topobjdir,
                'MOZILLA_DIR': env.topsrcdir,
                'topsrcdir': env.topsrcdir,
                'topobjdir': env.topobjdir,
                })
            variables.update(self._local_flags[directory])
            while True:
                c = []
                for a in cmd:
                    a = expand_variables(a, variables).split()
                    if not a:
                        continue
                    if isinstance(a, types.StringTypes):
                        c.append(a)
                    else:
                        c.extend(a)
                #print (cmd, c)
                if c == cmd:
                    break
                cmd = c
            per_source_flags = self._per_source_flags.get(filename)
            if per_source_flags is not None:
                c.extend(per_source_flags)
            db.append({
                'directory': directory,
                'command': ' '.join(shell_quote(a) for a in c),
                'file': mozpath.join(directory, filename),
            })

        import json
        # Output the database (a JSON file) to objdir/compile_commands.json
        outputfile = os.path.join(self.environment.topobjdir, 'compile_commands.json')
        with self._write_file(outputfile) as jsonout:
            json.dump(db, jsonout, indent=0)

    def _process_unified_sources(self, obj):
        # For unified sources, only include the unified source file.
        # Note that unified sources are never used for host sources.
        for f in obj.unified_source_mapping:
            self._compiler_db_entry(obj.objdir, obj.relativedir, obj.config, f[0],
                                obj.canonical_suffix)
            for entry in f[1]:
                self._compiler_db_entry(obj.objdir, obj.relativedir, obj.config,
                                    entry, obj.canonical_suffix, unified=f[0])

    def _handle_idl_manager(self, idl_manager):
        pass

    def _handle_ipdl_sources(self, ipdl_dir, sorted_ipdl_sources,
                             unified_ipdl_cppsrcs_mapping):
        for f in unified_ipdl_cppsrcs_mapping:
            self._compiler_db_entry(ipdl_dir, None, self.environment, f[0],
                                '.cpp')

    def _handle_webidl_build(self, bindings_dir, unified_source_mapping,
                             webidls, expected_build_output_files,
                             global_define_files):
        for f in unified_source_mapping:
            self._compiler_db_entry(bindings_dir, None, self.environment, f[0],
                                '.cpp')

    COMPILERS = {
        '.c': 'CC',
        '.cpp': 'CXX',
        '.m': 'CC',
        '.mm': 'CXX',
        # Should be AS, but AS is $(CC)...
        '.S': 'AS',
        '.s': 'AS',
    }

    CFLAGS = {
        '.c': 'CFLAGS',
        '.cpp': 'CXXFLAGS',
        '.m': 'CFLAGS',
        '.mm': 'CXXFLAGS',
        '.S': 'ASFLAGS',
        '.s': 'ASFLAGS',
    }

    def _compiler_db_entry(self, objdir, reldir, cenv, filename,
                       canonical_suffix, unified=None):
        if canonical_suffix not in self.COMPILERS:
            return
        db = self._db.setdefault((objdir, filename, unified),
            ['$(%s)' % self.COMPILERS[canonical_suffix], '-o', '/dev/null', '-c'])
        reldir = reldir or mozpath.relpath(objdir, cenv.topobjdir)

        if canonical_suffix == '.s':
            db[:] = "$(AS) -o /dev/null $(ASFLAGS) $(MOZBUILD_ASFLAGS) $(AS_DASH_C_FLAG)".split()


            return
        elif canonical_suffix == '.S':
            db[:] = "$(AS) -o /dev/null $(DEFINES) $(ASFLAGS) $(MOZBUILD_ASFLAGS) $(LOCAL_INCLUDES) -c".split()
            return



        def append_var(name):
            value = cenv.substs.get(name)
            if not value:
                return
            if isinstance(value, types.StringTypes):
                value = value.split()
            db.extend(value)

        if canonical_suffix in ('.mm', '.cpp'):
            db.append('$(STL_FLAGS)')

        db.extend((
            #'$(VISIBILITY_FLAGS)',
            '$(DEFINES)',
            '-I%s' % mozpath.join(cenv.topsrcdir, reldir),
            '-I%s' % objdir,
            '$(LOCAL_INCLUDES)',
            '-I%s/dist/include' % cenv.topobjdir,
            '$(EXTRA_INCLUDES)',
        ))
        append_var('DSO_CFLAGS')
        append_var('DSO_PIC_CFLAGS')
        if canonical_suffix in ('.c', '.cpp'):
            db.append('$(RTL_FLAGS)')
        append_var('OS_COMPILE_%s' % self.CFLAGS[canonical_suffix])
        append_var('OS_CPPFLAGS')
        append_var('OS_%s' % self.CFLAGS[canonical_suffix])
        append_var('MOZ_DEBUG_FLAGS')
        append_var('MOZ_OPTIMIZE_FLAGS')
        append_var('MOZ_FRAMEPTR_FLAGS')
        db.append('$(WARNINGS_AS_ERRORS)')
        db.append('$(MOZBUILD_%s)' % self.CFLAGS[canonical_suffix])
        if canonical_suffix == '.m':
            append_var('OS_COMPILE_CMFLAGS')
            db.append('$(MOZBUILD_CMFLAGS)')
        elif canonical_suffix == '.mm':
            append_var('OS_COMPILE_CMMFLAGS')
            db.append('$(MOZBUILD_CMMFLAGS)')
        elif canonical_suffix == '.s':
            db.append('$(ASFLAGS)')


    def _linker_db_entry(self, obj, objdir, reldir, cenv, filename):
        reldir = reldir or mozpath.relpath(objdir, cenv.topobjdir)

        db = self._db.setdefault((objdir, filename, None), [])
        def append_var(name):
            value = cenv.substs.get(name)
            if not value:
                return
            if isinstance(value, types.StringTypes):
                value = value.split()
            db.extend(value)

        if isinstance(obj, Program):
            command = "SHELL=$(SHELL) $(PYTHON) -B $(MOZILLA_DIR)/config/expandlibs_exec.py --uselist -- $(CXX)"
        elif isinstance(obj, SharedLibrary):
            command = "SHELL=$(SHELL) $(PYTHON) -B $(MOZILLA_DIR)/config/expandlibs_exec.py --uselist -- $(CC)"
        elif isinstance(obj, StaticLibrary):
            command = "SHELL=$(SHELL) $(PYTHON) $(MOZILLA_DIR)/config/expandlibs_gen.py -o /dev/null"
            db[:] = command
            return

        db.append(command)
        db.extend(('-o', '/dev/null'))
        db.append('$(DSO_PIC_CFLAGS)')
        db.append('$(DSO_LDOPTS)')
        #'$(CXXFLAGS)',
        append_var('MOZ_DEBUG_FLAGS')
        append_var('MOZ_OPTIMIZE_FLAGS')
        append_var('MOZ_FRAMEPTR_FLAGS')
        db.append('$(WARNINGS_AS_ERRORS)')

        #'$(PROGOBJS)',
        db.append('<OBJS>') # A placeholder for later...

        #'$(RESFILE)' ignored, for now
        #'$(WIN32_EXE_LDFLAGS)' ignored, for now

        # $(LDFLAGS)
        # LDFLAGS = $(OS_LDFLAGS) $(MOZBUILD_LDFLAGS) $(MOZ_FIX_LINK_PATHS)
        #   $(OS_LDFLAGS)
        db.append('$(LDFLAGS)')
        db.append('$(MOZBUILD_LDFLAGS)')
        db.append('$(MOZ_FIX_LINK_PATHS)')
        db.append('$(OS_LDFLAGS)')
        append_var('LINKER_LDFLAGS')

        # $(WRAP_LDFLAGS) seems unused
        # $(STATIC_LIBS)
        db.append('$(STATIC_LIBS)')

        # $(MOZ_PROGRAM_LDFLAGS) used for arm-Darwin
        # $(SHARED_LIBS)
        db.append('$(SHARED_LIBS)')
        # $(EXTRA_LIBS)' ignore, mainly used in recursive .mk files

        db.append('$(OS_LIBS)')
        # $(BIN_FLAGS)
        # $(EXE_DEF_FILE) windows, skipped for now.



