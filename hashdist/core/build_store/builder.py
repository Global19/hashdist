"""
Code for setting up environment for performing a build.

Can be considered a private part of .build_store, documentation-wise and test-wise.
"""

from os.path import join as pjoin
import os
import subprocess
import shutil
import json
import errno
import sys
from textwrap import dedent

from ..source_cache import scatter_files
from ..sandbox import get_dependencies_env
from .build_spec import shorten_artifact_id
from ..common import InvalidBuildSpecError


BUILD_ID_LEN = 4
ARTIFACT_ID_LEN = 4

class BuildFailedError(Exception):
    def __init__(self, msg, build_dir):
        Exception.__init__(self, msg)
        self.build_dir = build_dir

class ArtifactBuilder(object):
    def __init__(self, build_store, build_spec, virtuals):
        self.build_store = build_store
        self.logger = build_store.logger
        self.build_spec = build_spec
        self.artifact_id = build_spec.artifact_id
        self.virtuals = virtuals

    def make_hdist_launcher(self, build_dir):
        """Creates a 'bin'-dir containing only a launcher for the 'hdist' command

        It is created with the Python interpreter currently running,
        loading the Hashdist package currently running (i.e.,
        independent of any
        "python" dependency in the build spec).
        """
        python = os.path.realpath(sys.executable)
        hashdist_package = (
            os.path.realpath(pjoin(os.path.dirname(__file__), '..', '..')))
        bin_dir = pjoin(build_dir, 'hdist-bin')
        lib_dir = pjoin(build_dir, 'hdist-lib')
        os.mkdir(bin_dir)
        os.mkdir(lib_dir)
        os.symlink(hashdist_package, pjoin(lib_dir, 'hashdist'))
        with file(pjoin(bin_dir, 'hdist'), 'w') as f:
            f.write(dedent("""\
               #!%(python)s
               import sys
               sys.path.insert(0, "%(lib_dir)s")
               from hashdist.cli.main import main
               sys.exit(main(sys.argv))
               """) % dict(python=python, lib_dir=lib_dir))
        os.chmod(pjoin(bin_dir, 'hdist'), 0700)
        return bin_dir

    def build(self, source_cache, keep_build):
        artifact_dir, artifact_link = self.make_artifact_dir()
        try:
            self.build_to(artifact_dir, source_cache, keep_build)
        except:
            shutil.rmtree(artifact_dir)
            os.unlink(artifact_link)
            raise
        return artifact_dir

    def build_to(self, artifact_dir, source_cache, keep_build):
        env = get_dependencies_env(self.build_store, self.virtuals,
                                   self.build_spec.doc.get('dependencies', ()))

        # Always clean up when these fail regardless of keep_build_policy
        build_dir = self.make_build_dir()
        try:
            self.serialize_build_spec(artifact_dir, build_dir)
            self.unpack_sources(build_dir, source_cache)
            self.unpack_files(build_dir)
        except:
            self.remove_build_dir(build_dir)
            raise

        # Conditionally clean up when this fails
        try:
            self.run_build_commands(build_dir, artifact_dir, env)
        except BuildFailedError, e:
            if keep_build == 'never':
                self.remove_build_dir(build_dir)
            raise e
        # Success
        if keep_build != 'always':
            self.remove_build_dir(build_dir)
        return artifact_dir

    def make_build_dir(self):
        short_id = shorten_artifact_id(self.artifact_id, BUILD_ID_LEN)
        build_dir = orig_build_dir = pjoin(self.build_store.temp_build_dir, short_id)
        i = 0
        # Try to make build_dir, if not then increment a -%d suffix until we
        # fine a free slot
        while True:
            try:
                os.makedirs(build_dir)
            except OSError, e:
                if e.errno != errno.EEXIST:
                    raise
            else:
                break
            i += 1
            build_dir = '%s-%d' % (orig_build_dir, i)
        return build_dir

    def remove_build_dir(self, build_dir):
        rmtree_up_to(build_dir, self.build_store.temp_build_dir)

    def make_artifact_dir(self):
        # try to make shortened dir and symlink to it; incrementally
        # lengthen the name in the case of hash collision
        store = self.build_store.artifact_store_dir
        extra = 0
        while True:
            short_id = shorten_artifact_id(self.artifact_id, ARTIFACT_ID_LEN + extra)
            artifact_dir = pjoin(store, short_id)
            try:
                os.makedirs(artifact_dir)
            except OSError, e:
                if e.errno != errno.EEXIST:
                    raise
                if os.path.exists(pjoin(store, self.artifact_id)):
                    raise NotImplementedError('race condition or unclean store')
            else:
                break
            extra += 1

        # Make a symlink from the full id to the shortened one
        artifact_link = pjoin(store, self.artifact_id)
        os.symlink(os.path.split(short_id)[-1], artifact_link)
        return artifact_dir, artifact_link
 
    def serialize_build_spec(self, build_dir, artifact_dir):
        for d in [build_dir, artifact_dir]:
            with file(pjoin(d, 'build.json'), 'w') as f:
                json.dump(self.build_spec.doc, f, separators=(', ', ' : '), indent=4, sort_keys=True)

    def unpack_sources(self, build_dir, source_cache):
        # sources
        for source_item in self.build_spec.doc.get('sources', []):
            key = source_item['key']
            target = source_item.get('target', '.')
            full_target = os.path.abspath(pjoin(build_dir, target))
            if not full_target.startswith(build_dir):
                raise InvalidBuildSpecError('source target attempted to escape '
                                            'from build directory')
            # if an exception is raised the directory is removed, so unsafe_mode
            # should be ok
            source_cache.unpack(key, full_target, unsafe_mode=True, strip=source_item['strip'])

    def unpack_files(self, build_dir):
        def parse_file_entry(entry):
            contents = os.linesep.join(entry['contents']).encode('UTF-8')
            return entry['target'], contents
            
        files = [parse_file_entry(x) for x in self.build_spec.doc.get('files', ())]
        scatter_files(files, build_dir)

    def run_build_commands(self, build_dir, artifact_dir, env):
        # Handles log-file, environment, build execution
        hdist_bin = self.make_hdist_launcher(build_dir)
        env['PATH'] = hdist_bin + os.pathsep + os.environ['PATH'] # for now
        env['TARGET'] = artifact_dir
        env['BUILD'] = build_dir

        log_filename = pjoin(build_dir, 'build.log')
        self.logger.info('Building artifact %s..., follow log with' %
                         shorten_artifact_id(self.artifact_id, ARTIFACT_ID_LEN + 2))
        self.logger.info('')
        self.logger.info('    tail -f %s\n\n' % log_filename)
        with file(log_filename, 'w') as log_file:
            logfileno = log_file.fileno()
            for command_lst in self.build_spec.doc['commands']:
                log_file.write("hdist: running command %r" % command_lst)
                try:
                    subprocess.check_call(command_lst, cwd=build_dir, env=env,
                                          stdin=None, stdout=logfileno, stderr=logfileno)
                except subprocess.CalledProcessError, e:
                    log_file.write("hdist: command FAILED with code %d" % e.returncode)
                    raise BuildFailedError('Build command failed with code %d (cwd: "%s")' %
                                           (e.returncode, build_dir), build_dir)
            log_file.write("hdist: SUCCESS")
        # On success, copy log file to artifact_dir
        shutil.copy(log_filename, pjoin(artifact_dir, 'build.log'))

def rmtree_up_to(path, parent):
    """Executes shutil.rmtree(path), and then removes any empty parent directories
    up until (and excluding) parent.
    """
    path = os.path.realpath(path)
    parent = os.path.realpath(parent)
    if path == parent:
        return
    if not path.startswith(parent):
        raise ValueError('must have path.startswith(parent)')
    shutil.rmtree(path)
    while path != parent:
        path, child = os.path.split(path)
        if path == parent:
            break
        try:
            os.rmdir(path)
        except OSError, e:
            if e.errno != errno.ENOTEMPTY:
                raise
            break