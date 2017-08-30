# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from __future__ import absolute_import, unicode_literals

import os
import itertools
from collections import OrderedDict

import mozpack.path as mozpath
from mozbuild.base import MozbuildObject
from mozbuild.backend.base import PartialBackend, HybridBackend
from mozbuild.backend.recursivemake import RecursiveMakeBackend
from mozbuild.shellutil import quote as shell_quote
from mozbuild.util import OrderedDefaultDict

from mozpack.files import (
    FileFinder,
)

from .common import CommonBackend
from ..frontend.data import (
    ChromeManifestEntry,
    ContextDerived,
    Defines,
    FinalTargetFiles,
    FinalTargetPreprocessedFiles,
    GeneratedFile,
    BaseSources,
        Sources,
        GeneratedSources,
        HostSources,
        UnifiedSources,
    BaseProgram,
        Program,
        SimpleProgram,
        HostProgram,
        HostSimpleProgram,
    Linkable,
        Library,
        StaticLibrary,
        SharedLibrary,
        RustLibrary,
    HostDefines,
    JARManifest,
    ObjdirFiles,
)
from ..util import (
    FileAvoidWrite,
)
from ..frontend.context import (
    AbsolutePath,
    ObjDirPath,
)

def uniq(seq):
   return list(OrderedDict.fromkeys(seq))

class BackendTupfile(object):
    """Represents a generated Tupfile.
    """

    def __init__(self, srcdir, objdir, environment, topsrcdir, topobjdir):
        self.topsrcdir = topsrcdir
        self.srcdir = srcdir
        self.objdir = objdir
        self.relobjdir = mozpath.relpath(objdir, topobjdir)
        self.environment = environment
        self.name = mozpath.join(objdir, 'Tupfile')
        self.rules_included = False
        self.shell_exported = False
        self.defines = []
        self.host_defines = []
        self.delayed_generated_files = []
        self._sources = []

        self.fh = FileAvoidWrite(self.name, capture_diff=True)
        self.fh.write('# THIS FILE WAS AUTOMATICALLY GENERATED. DO NOT EDIT.\n')
        self.fh.write('\n')

    def write(self, buf):
        self.fh.write(buf)

    def include_rules(self):
        if not self.rules_included:
            self.write('include_rules\n')
            self.rules_included = True

    def rule(self, cmd, inputs=None, extra_inputs=None, outputs=None, display=None, extra_outputs=None, output_group=None, check_unchanged=True):
        inputs = inputs or [] 
        outputs = outputs or []
        display = display or ""
        self.include_rules()
        flags = ""
        if check_unchanged:
            # This flag causes tup to compare the outputs with the previous run
            # of the command, and skip the rest of the DAG for any that are the
            # same.
            flags += "o"

        if display:
            caret_text = flags + ' ' + display
        else:
            caret_text = flags

        self._includes_group = '$(MOZ_OBJ_ROOT)/<includes>'
        if any(o[-2:] == ".h" or o[-4:] == ".cfg" for o in outputs):
            output_group = self._includes_group
        if output_group:
            outputs.append(output_group)

        self.write(': %(inputs)s%(extra_inputs)s |> %(display)s%(cmd)s |> %(outputs)s%(extra_outputs)s\n' % {
            'inputs': ' '.join(inputs),
            'extra_inputs': ' | ' + ' '.join(extra_inputs) if extra_inputs else '',
            'display': '^%s^ ' % caret_text if caret_text else '',
            'cmd': ' '.join(cmd),
            'outputs': ' '.join(outputs),
            'extra_outputs': ' | ' + ' '.join(extra_outputs) if extra_outputs else '',
        })

    def symlink_rule(self, source, output=None, output_group=None):
        outputs = [output] if output else [mozpath.basename(source)]

        # The !tup_ln macro does a symlink or file copy (depending on the
        # platform) without shelling out to a subprocess.
        self.rule(
            cmd=['!tup_ln'],
            inputs=[source],
            outputs=outputs,
            output_group=output_group,
            check_unchanged=False,
        )

    def export_shell(self):
        if not self.shell_exported:
            # These are used by mach/mixin/process.py to determine the current
            # shell.
            for var in ('SHELL', 'MOZILLABUILD', 'COMSPEC'):
                self.write('export %s\n' % var)
            self.shell_exported = True

    def close(self):
        return self.fh.close()

    @property
    def diff(self):
        return self.fh.diff


class TupOnly(CommonBackend, PartialBackend):
    """Backend that generates Tupfiles for the tup build system.
    """

    def _init(self):
        CommonBackend._init(self)

        self._backend_files = {}
        self._cmd = MozbuildObject.from_environment()
        self._manifest_entries = OrderedDefaultDict(set)
        self._missing_commands = OrderedDefaultDict(int)
        self._compile_graph = OrderedDefaultDict(set)
        self._compile_env_gen_files = (
            '*.c',
            '*.cpp',
            '*.h',
            '*.inc',
            '*.py',
            '*.rs',
        )

        # This is a 'group' dependency - All rules that list this as an output
        # will be built before any rules that list this as an input.
        self._installed_files = '$(MOZ_OBJ_ROOT)/<installed-files>'
        self._includes_group = '$(MOZ_OBJ_ROOT)/<includes>'
        self._objects_group = '$(MOZ_OBJ_ROOT)/<objects>'

        import json
        # Read the database (a JSON file) to objdir/compile_commands.json
        self._commands_db = {}
        try:
            dbfile = mozpath.join(self.environment.topobjdir, 'compile_commands.json')
            with open(dbfile, "r") as jsonin:
                for entry in json.load(jsonin):
                    source, command, directory = entry['file'], entry['command'], entry['directory']
                    self._commands_db[(source, directory)] = [p if p != "/dev/null" else "%o" for p in command.split()]
        except:
            pass

    def _get_backend_file(self, relativedir, objdir=None, srcdir=None):
        objdir = objdir if objdir else mozpath.join(self.environment.topobjdir, relativedir)
        objdir = mozpath.normpath(objdir)
        srcdir = srcdir if srcdir else mozpath.join(self.environment.topsrcdir, relativedir)
        if objdir not in self._backend_files:
            self._backend_files[objdir] = \
                    BackendTupfile(srcdir, objdir, self.environment,
                                   self.environment.topsrcdir, self.environment.topobjdir)
        return self._backend_files[objdir]

    def _get_backend_file_for(self, obj):
        return self._get_backend_file(mozpath.relpath(obj.objdir, self.environment.topobjdir), obj.objdir, obj.srcdir)

    def _source_closure(self, backend_file):
        sources = []
        reldir = backend_file.relobjdir
        #while reldir:
        b = self._get_backend_file(reldir)
        for f in b._sources:
            sources.append(f)
        #reldir = mozpath.dirname(reldir)

        #sources.sort()
        return sources
            


    def _py_action(self, action):
        cmd = [
            '$(PYTHON)',
            '-m',
            'mozbuild.action.%s' % action,
        ]
        return cmd

    def consume_object(self, obj):
        """Write out build files necessary to build with tup."""

        # These are too difficult for now. (taken from CompileDB)
        if obj.relativedir in (
                'build/unix/elfhack',
                'build/unix/elfhack/inject',
                'build/clang-plugin',
                'build/clang-plugin/tests',
                ):
            return True

        if not isinstance(obj, ContextDerived):
            return False

        consumed = CommonBackend.consume_object(self, obj)
        if consumed:
            return True

        backend_file = self._get_backend_file_for(obj)

        if isinstance(obj, GeneratedFile):
            # These files are already generated by make before tup runs.
            skip_files = (
                'buildid.h',
                'source-repo.h',
            )

            if self.environment.is_artifact_build:
                skip_files = skip_files + self._compile_env_gen_files

            for f in obj.outputs:
                if any(mozpath.match(f, p) for p in skip_files):
                    return False

            if 'application.ini.h' in obj.outputs:
                # application.ini.h is a special case since we need to process
                # the FINAL_TARGET_PP_FILES for application.ini before running
                # the GENERATED_FILES script, and tup doesn't handle the rules
                # out of order.
                backend_file.delayed_generated_files.append(obj)
            else:
                self._process_generated_file(backend_file, obj)
        elif (isinstance(obj, ChromeManifestEntry) and
              obj.install_target.startswith('dist/bin')):
            top_level = mozpath.join(obj.install_target, 'chrome.manifest')
            if obj.path != top_level:
                entry = 'manifest %s' % mozpath.relpath(obj.path,
                                                        obj.install_target)
                self._manifest_entries[top_level].add(entry)
            self._manifest_entries[obj.path].add(str(obj.entry))
        elif isinstance(obj, Defines):
            self._process_defines(backend_file, obj)
        elif isinstance(obj, HostDefines):
            self._process_defines(backend_file, obj, host=True)
        elif isinstance(obj, FinalTargetFiles):
            self._process_final_target_files(obj)
        elif isinstance(obj, FinalTargetPreprocessedFiles):
            self._process_final_target_pp_files(obj, backend_file)
        elif isinstance(obj, JARManifest):
            self._consume_jar_manifest(obj)
        elif isinstance(obj, BaseSources): #(Sources, GeneratedSources, HostSources, UnifiedSources)
            self._process_sources(obj, backend_file)

        # Do that later. We need to collect all the sources first.
        elif isinstance(obj, Program):
            backend_file.delayed_generated_files.append(obj)
        elif isinstance(obj, SharedLibrary):
            backend_file.delayed_generated_files.append(obj)
        elif isinstance(obj, StaticLibrary):
            backend_file.delayed_generated_files.append(obj)

        return True

    def consume_finished(self):
        CommonBackend.consume_finished(self)

        print(self._missing_commands)

        # The approach here is similar to fastermake.py, but we
        # simply write out the resulting files here.
        for target, entries in self._manifest_entries.iteritems():
            with self._write_file(mozpath.join(self.environment.topobjdir,
                                               target)) as fh:
                fh.write(''.join('%s\n' % e for e in sorted(entries)))

        for objdir, backend_file in sorted(self._backend_files.items()):
            for obj in backend_file.delayed_generated_files:
                if isinstance(obj, GeneratedFile):
                    self._process_generated_file(backend_file, obj)
                elif isinstance(obj, BaseProgram):
                    self._process_program(backend_file, obj)
                elif isinstance(obj, SharedLibrary):
                    self._process_program(backend_file, obj)
                    #self._no_skip['syms'].add(backend_file.relobjdir)
                elif isinstance(obj, StaticLibrary):
                    self._process_program(backend_file, obj)
                else:
                    print("Unsupported postponed object: %s" % obj.__class__.__name__)
            with self._write_file(fh=backend_file):
                pass

        with self._write_file(mozpath.join(self.environment.topobjdir, 'Tuprules.tup')) as fh:
            acdefines_flags = ' '.join(['-D%s=%s' % (name, shell_quote(value))
                for (name, value) in sorted(self.environment.acdefines.iteritems())])
            # TODO: AB_CD only exists in Makefiles at the moment.
            acdefines_flags += ' -DAB_CD=en-US'

            # TODO: BOOKMARKS_INCLUDE_DIR is used by bookmarks.html.in, and is
            # only defined in browser/locales/Makefile.in
            acdefines_flags += ' -DBOOKMARKS_INCLUDE_DIR=%s/browser/locales/en-US/profile' % self.environment.topsrcdir

            # Use BUILD_FASTER to avoid CXXFLAGS/CPPFLAGS in
            # toolkit/content/buildconfig.html
            acdefines_flags += ' -DBUILD_FASTER=1'

            fh.write('MOZ_OBJ_ROOT = $(TUP_CWD)\n')
            fh.write('DIST = $(MOZ_OBJ_ROOT)/dist\n')
            fh.write('ACDEFINES = %s\n' % acdefines_flags)
            fh.write('topsrcdir = $(MOZ_OBJ_ROOT)/%s\n' % (
                os.path.relpath(self.environment.topsrcdir, self.environment.topobjdir)
            ))
            fh.write('PYTHON = PYTHONDONTWRITEBYTECODE=True $(MOZ_OBJ_ROOT)/_virtualenv/bin/python\n')
            fh.write('PYTHON_PATH = $(PYTHON) $(topsrcdir)/config/pythonpath.py\n')
            fh.write('PLY_INCLUDE = -I$(topsrcdir)/other-licenses/ply\n')
            fh.write('IDL_PARSER_DIR = $(topsrcdir)/xpcom/idl-parser\n')
            fh.write('IDL_PARSER_CACHE_DIR = $(MOZ_OBJ_ROOT)/xpcom/idl-parser/xpidl\n')

        # Run 'tup init' if necessary.
        if not os.path.exists(mozpath.join(self.environment.topsrcdir, ".tup")):
            tup = self.environment.substs.get('TUP', 'tup')
            self._cmd.run_process(cwd=self.environment.topsrcdir, log_name='tup', args=[tup, 'init'])

    def _process_generated_file(self, backend_file, obj):
        # TODO: These are directories that don't work in the tup backend
        # yet, because things they depend on aren't built yet.
        skip_directories = (
            'layout/style/test', # HostSimplePrograms
            'toolkit/library', # libxul.so
        )
        if obj.script and obj.method and obj.relobjdir not in skip_directories:
            backend_file.export_shell()
            cmd = self._py_action('file_generate')
            cmd.extend([
                obj.script,
                obj.method,
                obj.outputs[0],
                '%s.pp' % obj.outputs[0], # deps file required
            ])
            full_inputs = [f.full_path for f in obj.inputs]
            cmd.extend(full_inputs)
            cmd.extend(shell_quote(f) for f in obj.flags)

            outputs = []
            outputs.extend(obj.outputs)
            outputs.append('%s.pp' % obj.outputs[0])

            backend_file.rule(
                display='python {script}:{method} -> [%o]'.format(script=obj.script, method=obj.method),
                cmd=cmd,
                inputs=full_inputs,
                outputs=outputs,
                output_group=self._installed_files,
            )

    def _process_defines(self, backend_file, obj, host=False):
        defines = list(obj.get_defines())
        if defines:
            if host:
                backend_file.host_defines = defines
            else:
                backend_file.defines = defines

    def _process_final_target_files(self, obj):
        target = obj.install_target
        if not isinstance(obj, ObjdirFiles):
            path = mozpath.basedir(target, (
                'dist/bin',
                'dist/xpi-stage',
                '_tests',
                'dist/include',
                'dist/branding',
                'dist/sdk',
            ))
            if not path:
                raise Exception("Cannot install to " + target)

        if target.startswith('_tests'):
            # TODO: TEST_HARNESS_FILES present a few challenges for the tup
            # backend (bug 1372381).
            return

        for path, files in obj.files.walk():
            backend_file = self._get_backend_file(mozpath.join(target, path))
            for f in files:
                if not isinstance(f, ObjDirPath):
                    if '*' in f:
                        if f.startswith('/') or isinstance(f, AbsolutePath):
                            basepath, wild = os.path.split(f.full_path)
                            if '*' in basepath:
                                raise Exception("Wildcards are only supported in the filename part of "
                                                "srcdir-relative or absolute paths.")

                            # TODO: This is only needed for Windows, so we can
                            # skip this for now.
                            pass
                        else:
                            def _prefix(s):
                                for p in mozpath.split(s):
                                    if '*' not in p:
                                        yield p + '/'
                            prefix = ''.join(_prefix(f.full_path))
                            self.backend_input_files.add(prefix)
                            finder = FileFinder(prefix)
                            for p, _ in finder.find(f.full_path[len(prefix):]):
                                backend_file.symlink_rule(mozpath.join(prefix, p),
                                                          output=mozpath.join(f.target_basename, p),
                                                          output_group=self._installed_files)
                    else:
                        backend_file.symlink_rule(f.full_path, output=f.target_basename,
                                output_group=self._installed_files )
                else:
                    if (self.environment.is_artifact_build and
                        any(mozpath.match(f.target_basename, p) for p in self._compile_env_gen_files)):
                        # If we have an artifact build we never would have generated this file,
                        # so do not attempt to install it.
                        continue

                    # We're not generating files in these directories yet, so
                    # don't attempt to install files generated from them.
                    if f.context.relobjdir not in ('layout/style/test',
                                                   'toolkit/library'):
                        output = mozpath.join('$(MOZ_OBJ_ROOT)', target, path,
                                              f.target_basename)
                        gen_backend_file = self._get_backend_file(f.context.relobjdir)
                        gen_backend_file.symlink_rule(f.full_path, output=output, output_group=None)

    def _process_final_target_pp_files(self, obj, backend_file):
        for i, (path, files) in enumerate(obj.files.walk()):
            for f in files:
                self._preprocess(backend_file, f.full_path,
                                 destdir=mozpath.join(self.environment.topobjdir, obj.install_target, path))

    # CommonBackend calls this one.
    def _process_unified_sources(self, obj):
        backend_file = self._get_backend_file_for(obj)
        self._process_sources(obj, backend_file)

    def _process_sources(self, obj, backend_file):
        #command = str("!compile_{}".format(obj.canonical_suffix[1:]))
        #if isinstance(obj, GeneratedSources):
        #    base = backend_file.objdir
        #else:
        #    base = backend_file.srcdir


        #for path, files in obj.files.walk():
            #backend_file = self._get_backend_file(mozpath.join(target, path))
            #for f in files:
        unified = isinstance(obj, UnifiedSources)

        mapping = {}
        if unified and obj.have_unified_mapping:
            mapping = dict(obj.unified_source_mapping)

        files = obj.files
        if unified:
            files = mapping.keys()

        for f in sorted(files):
            self._write_sources(backend_file, obj, f, obj.canonical_suffix, mapping.get(f, []))


    def _write_sources(self, backend_file, obj, f, canonical_suffix, unified_inputs=[]):
        suffix_map = {
            '.s': 'AS',
            '.c': 'C',
            '.m': 'CM',
            '.mm': 'CMM',
            '.cpp': 'CPP',
            '.rs': 'RS',
            '.S': 'S',
        }
        label = '%s %%b' % suffix_map.get(canonical_suffix, "Compiling")
        backend_file._sources.append("%s.o" % mozpath.basename(mozpath.splitext(f)[0]))

        #f = mozpath.relpath(f, base)
        #if "js/src" not in backend_file.objdir:
        #    continue

        cmd = self._commands_db.get((f, backend_file.objdir), None)
        if unified_inputs and cmd == None:
            cmd = self._commands_db.get((unified_inputs[0], backend_file.objdir), None)
        if not cmd:
            self._missing_commands[canonical_suffix] += 1
            self._missing_commands[obj.__class__.__name__] += 1
            print("Missing command for %s" % f)
            cmd = ['touch', '%o']

        #includes = [ arg[2:] for arg in cmd if arg[:2] == '-I' and "libffi" not in arg ]
        backend_file.rule(
            inputs=[f],
            display=label,
            extra_inputs=[self._includes_group, self._installed_files] + unified_inputs,
            cmd=cmd, # XXX handle host command for HostSources (works now because we use the commands_database.json
            outputs=["%B.o"],
            output_group=self._objects_group
        )


    def _build_target_for_obj(self, obj):
        return '%s/%s' % (mozpath.relpath(obj.objdir,
            self.environment.topobjdir), obj.KIND)

    def _process_program(self, backend_file, obj):
        #if "js" not in obj.program:
        #    return

        libs = []
        system_libs = []

        def write_shared_and_system_libs(lib):
            for l in lib.linked_libraries:
                if isinstance(l, (StaticLibrary, RustLibrary)):
                    write_shared_and_system_libs(l)
                else:
                    libs.append('%s/%s' % (l.objdir, l.import_name))

            system_libs.extend(lib.linked_system_libs)

        #def pretty_relpath(lib):
        #    return '$(DEPTH)/%s' % mozpath.relpath(lib.objdir, topobjdir)

        #topobjdir = mozpath.normsep(obj.topobjdir)
        ## This will create the node even if there aren't any linked libraries.
        #build_target = self._build_target_for_obj(obj)
        #self._compile_graph[build_target]

        for lib in obj.linked_libraries:
            #if not isinstance(lib, ExternalLibrary):
            #    self._compile_graph[build_target].add(
            #        self._build_target_for_obj(lib))
            #relpath = pretty_relpath(lib)
            if isinstance(obj, Library):
                if isinstance(lib, RustLibrary):
                    # We don't need to do anything here; we will handle
                    # linkage for any RustLibrary elsewhere.
                    continue
                elif isinstance(lib, StaticLibrary):
                    libs.append('%s/%s' % (lib.objdir, lib.import_name))
                    if isinstance(obj, SharedLibrary):
                        write_shared_and_system_libs(lib)
                elif isinstance(lib, SharedLibrary):
                    libs.append('%s/%s' % (lib.objdir, lib.import_name))
            elif isinstance(obj, (Program, SimpleProgram)):
                if isinstance(lib, StaticLibrary):
                    libs.append('%s/%s' % (lib.objdir, lib.import_name))
                    write_shared_and_system_libs(lib)
                else:
                    libs.append('%s/%s' % (lib.objdir, lib.import_name))
            #elif isinstance(obj, (HostLibrary, HostProgram, HostSimpleProgram)):
            #    assert isinstance(lib, (HostLibrary, HostRustLibrary))
            #    backend_file.write_once('HOST_LIBS += %s/%s\n'
            #                       % (relpath, lib.import_name))

        ## We have to link any Rust libraries after all intermediate static
        ## libraries have been listed to ensure that the Rust libraries are
        ## searched after the C/C++ objects that might reference Rust symbols.
        #if isinstance(obj, SharedLibrary):
        #    self._process_rust_libraries(obj, backend_file, pretty_relpath)

        system_libs.extend(obj.linked_system_libs)
        #for lib in obj.linked_system_libs:
        #    if obj.KIND == 'target':
        #        backend_file.write_once('OS_LIBS += %s\n' % lib)
        #    else:
        #        backend_file.write_once('HOST_EXTRA_LIBS += %s\n' % lib)

        ## Process library-based defines
        #self._process_defines(obj.lib_defines, backend_file)

        if isinstance(obj, Program):
            cmd = self._commands_db.get((mozpath.join(backend_file.objdir, obj.program), backend_file.objdir), ['ld.gold', '-o', '%o', ''])[:-1]
            inputs = self._source_closure(backend_file) + uniq(libs)
            if "'<OBJS>'" not in cmd:
                cmd = cmd + inputs
            cmd = [e if e != "'<OBJS>'" else " ".join(inputs) for e in cmd] + uniq(system_libs)
            backend_file.rule(
                    inputs=[l + ".desc" if l.endswith('.a') else l for l in inputs],
                    outputs=[obj.program],
                    cmd=cmd,
                    display="LINK %o",
                    )
        elif isinstance(obj, SharedLibrary):
            cmd = self._commands_db.get((mozpath.join(backend_file.objdir, obj.lib_name), backend_file.objdir), ['ld.gold', '-o', '%o', ''])[:-1]
            inputs = self._source_closure(backend_file) + uniq(libs)
            if "'<OBJS>'" not in cmd:
                cmd = cmd + inputs
            cmd = [e if e != "'<OBJS>'" else " ".join(inputs) for e in cmd] + uniq(system_libs)
            backend_file.rule(
                    inputs=[l + ".desc" if l.endswith('.a') else l for l in inputs],
                    outputs=[obj.lib_name],
                    cmd=cmd,
                    display="LINK %o",
                    )
        elif isinstance(obj, StaticLibrary):
            inputs = self._source_closure(backend_file) + uniq(libs)
            cmd=["SHELL=%s" % self.environment.substs['SHELL'], "$(PYTHON_PATH)", '$(topsrcdir)/config/expandlibs_gen.py', '-o', obj.lib_name + ".desc" ]
            cmd.extend(inputs)
            backend_file.rule(
                    inputs=[l + ".desc" if l.endswith('.a') else l for l in inputs],
                    outputs=[obj.lib_name + ".desc"],
                    cmd=cmd,
                    display="AR %o",
                    )
        else:
            #print('Shared library:', obj.lib_name)
            backend_file.rule(
                    inputs=[],
                    outputs=[obj.lib_name, mozpath.join(obj.basename, obj.lib_name)],
                    cmd=['touch', '%o'],
                    display="LINK %o",
                    )


    def _handle_idl_manager(self, manager):
        if self.environment.is_artifact_build:
            return

        dist_idl_backend_file = self._get_backend_file('dist/idl')
        for idl in manager.idls.values():
            dist_idl_backend_file.symlink_rule(idl['source'], output_group=self._installed_files)

        backend_file = self._get_backend_file('xpcom/xpidl')
        backend_file.export_shell()

        for module, data in sorted(manager.modules.iteritems()):
            dest, idls = data
            cmd = [
                '$(PYTHON_PATH)',
                '$(PLY_INCLUDE)',
                '-I$(IDL_PARSER_DIR)',
                '-I$(IDL_PARSER_CACHE_DIR)',
                '$(topsrcdir)/python/mozbuild/mozbuild/action/xpidl-process.py',
                '--cache-dir', '$(IDL_PARSER_CACHE_DIR)',
                '$(DIST)/idl',
                '$(DIST)/include',
                '$(MOZ_OBJ_ROOT)/%s/components' % dest,
                module,
            ]
            cmd.extend(sorted(idls))

            outputs = ['$(MOZ_OBJ_ROOT)/%s/components/%s.xpt' % (dest, module)]
            outputs.extend(['$(MOZ_OBJ_ROOT)/dist/include/%s.h' % f for f in sorted(idls)])
            backend_file.rule(
                inputs=[
                    '$(MOZ_OBJ_ROOT)/xpcom/idl-parser/xpidl/xpidllex.py',
                    '$(MOZ_OBJ_ROOT)/xpcom/idl-parser/xpidl/xpidlyacc.py',
                    self._installed_files,
                ],
                display='XPIDL %s' % module,
                cmd=cmd,
                outputs=outputs,
            )

        for manifest, entries in manager.interface_manifests.items():
            for xpt in entries:
                self._manifest_entries[manifest].add('interfaces %s' % xpt)

        for m in manager.chrome_manifests:
            self._manifest_entries[m].add('manifest components/interfaces.manifest')

    def _preprocess(self, backend_file, input_file, destdir=None):
        # .css files use '%' as the preprocessor marker, which must be escaped as
        # '%%' in the Tupfile.
        marker = '%%' if input_file.endswith('.css') else '#'

        cmd = self._py_action('preprocessor')
        cmd.extend([shell_quote(d) for d in backend_file.defines])
        cmd.extend(['$(ACDEFINES)', '%f', '-o', '%o', '--marker=%s' % marker])

        base_input = mozpath.basename(input_file)
        if base_input.endswith('.in'):
            base_input = mozpath.splitext(base_input)[0]
        output = mozpath.join(destdir, base_input) if destdir else base_input

        backend_file.rule(
            inputs=[input_file],
            display='PREPROCESS %s%s' % ("%s -> " % input_file.split('/')[-1] if input_file.endswith(".in") else "", base_input),
            cmd=cmd,
            outputs=[output],
        )

    def _handle_ipdl_sources(self, ipdl_dir, sorted_ipdl_sources,
                             unified_ipdl_cppsrcs_mapping):

        backend_file = self._get_backend_file(ipdl_dir)

        for source, extra_sources in sorted(unified_ipdl_cppsrcs_mapping):
            self._write_sources(backend_file, None, source, '.cpp', extra_sources)

        ipdldirs = sorted(set(mozpath.dirname(p) for p in sorted_ipdl_sources))

        cmd=["$(PYTHON_PATH)", "$(PLY_INCLUDE)", self.environment.topsrcdir+"/ipc/ipdl/ipdl.py",
                "--sync-msg-list=%s/sync-messages.ini" % backend_file.srcdir,
                "--msg-metadata=%s/message-metadata.ini" % backend_file.srcdir,
                "--outheaders-dir=_ipdlheaders", "--outcpp-dir=."]
	cmd.extend('-I%s' % dir for dir in ipdldirs)
        cmd.extend(sorted_ipdl_sources)

        outputs = list(itertools.chain(*[s for _, s in unified_ipdl_cppsrcs_mapping]))


        # TODO: fix nix, or insert thin in the makefile.
        #backend_file.rule(
        #        inputs=sorted_ipdl_sources,
        #        outputs=['IPCMessageTypeName.cpp'] + outputs,
        #        cmd=cmd,
        #        output_group=self._installed_files,
        #        display="IPDL",
        #        )


    def _handle_webidl_build(self, bindings_dir, unified_source_mapping,
                             webidls, expected_build_output_files,
                             global_define_files):
        backend_file = self._get_backend_file('dom/bindings')
        backend_file.export_shell()

        for source in sorted(webidls.all_preprocessed_sources()):
            self._preprocess(backend_file, source)

        cmd = self._py_action('webidl')
        cmd.append(mozpath.join(self.environment.topsrcdir, 'dom', 'bindings'))

        # The WebIDLCodegenManager knows all of the .cpp and .h files that will
        # be created (expected_build_output_files), but there are a few
        # additional files that are also created by the webidl py_action.
        outputs = [
            '_cache/webidlyacc.py',
            'codegen.json',
            'codegen.pp',
            'parser.out',
        ]
        outputs.extend(expected_build_output_files)

        backend_file.rule(
            display='WebIDL code generation',
            cmd=cmd,
            inputs=webidls.all_non_static_basenames(),
            outputs=outputs,
            check_unchanged=True,
        )


class TupBackend(HybridBackend(TupOnly, RecursiveMakeBackend)):
    def build(self, config, output, jobs, verbose):
        status = config._run_make(directory=self.environment.topobjdir, target='tup',
                                  line_handler=output.on_line, log=False, print_directory=False,
                                  ensure_exit_code=False, num_jobs=jobs, silent=not verbose)
        return status
