#!/usr/bin/env python2.7

#
# Copyright (c) YugaByte, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except
# in compliance with the License. You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under the License
# is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
# or implied. See the License for the specific language governing permissions and limitations
# under the License.
#


import argparse
import hashlib
import multiprocessing
import os
import platform
import re
import subprocess
import sys


from build_definitions import *
import build_definitions
import_submodules(build_definitions)


CLOUDFRONT_URL = 'http://d3dr9sfxru4sde.cloudfront.net/{}'


def hashsum_file(hash, filename, block_size=65536):
    with open(filename, "rb") as f:
        for block in iter(lambda: f.read(block_size), b""):
            hash.update(block)
    return hash.hexdigest()


class Builder:
    def __init__(self):
        self.tp_dir = os.path.dirname(os.path.realpath(sys.argv[0]))
        self.tp_build_dir = os.path.join(self.tp_dir, 'build')
        self.tp_src_dir = os.path.join(self.tp_dir, 'src')
        self.tp_download_dir = os.path.join(self.tp_dir, 'download')
        self.tp_installed_dir = os.path.join(self.tp_dir, 'installed')
        self.tp_installed_common_dir = os.path.join(self.tp_installed_dir, BUILD_TYPE_COMMON)
        self.src_dir = os.path.dirname(self.tp_dir)
        if not os.path.isdir(self.src_dir):
            fatal('YB src directory "{}" does not exist'.format(self.src_dir))
        self.build_support_dir = os.path.join(self.src_dir, 'build-support')
        self.enterprise_root = os.path.join(self.src_dir, 'ent')
        self.cc_wrapper = os.path.join(self.build_support_dir, 'compiler-wrappers', 'cc')
        self.cxx_wrapper = os.path.join(self.build_support_dir, 'compiler-wrappers', 'c++')

        self.dependencies = [
            build_definitions.zlib.ZLibDependency(),
            build_definitions.lz4.LZ4Dependency(),
            build_definitions.bitshuffle.BitShuffleDependency(),
            build_definitions.libev.LibEvDependency(),
            build_definitions.rapidjson.RapidJsonDependency(),
            build_definitions.squeasel.SqueaselDependency(),
            build_definitions.curl.CurlDependency()]

        if is_linux():
            self.dependencies += [
                build_definitions.nvml.NVMLDependency()
            ]

        self.dependencies += [
            build_definitions.cqlsh.CQLShDependency(),
            build_definitions.redis_cli.RedisCliDependency(),
        ]

        if is_linux():
            self.dependencies += [
                build_definitions.llvm.LLVMDependency(),
                build_definitions.libcxx.LibCXXDependency(),

                build_definitions.libunwind.LibUnwindDependency(),
                build_definitions.libbacktrace.LibBacktraceDependency()
            ]

        self.dependencies += [
            build_definitions.aws_sdk_cpp.AwsSdkCppDependency(),
            build_definitions.protobuf.ProtobufDependency(),
            build_definitions.crypt_blowfish.CryptBlowfishDependency(),
            build_definitions.tacopie.TacopieDependency(),
            build_definitions.cpp_redis.CppRedisDependency(),
            build_definitions.boost.BoostDependency(),

            build_definitions.gflags.GFlagsDependency(),
            build_definitions.glog.GLogDependency(),
            build_definitions.gperftools.GPerfToolsDependency(),
            build_definitions.gmock.GMockDependency(),
            build_definitions.snappy.SnappyDependency(),
            build_definitions.crcutil.CRCUtilDependency(),
        ]

        self.selected_dependencies = []

        self.using_linuxbrew = False
        self.linuxbrew_dir = None
        self.cc = None
        self.cxx = None
        self.args = None

        self.detect_linuxbrew()
        self.load_expected_checksums()

    def set_compiler(self, compiler_type):
        if is_mac():
            self.compiler_type = 'clang'
            return

        self.compiler_type = compiler_type
        os.environ['YB_COMPILER_TYPE'] = compiler_type
        self.find_compiler_by_type(compiler_type)
        os.environ['CC'] = self.cc_wrapper
        os.environ['CXX'] = self.cxx_wrapper

    def init(self):
        os.environ['YB_IS_THIRDPARTY_BUILD'] = '1'

        parser = argparse.ArgumentParser(prog=sys.argv[0])
        parser.add_argument('--build-type',
                            default=None,
                            type=str,
                            help='Build only specific part of thirdparty dependencies.')
        parser.add_argument('--clean',
                            action='store_const',
                            const=True,
                            default=False,
                            help='Clean.')
        parser.add_argument('dependencies', nargs=argparse.REMAINDER, help='Dependencies to build.')
        self.args = parser.parse_args()

        if self.args.dependencies:
            names = set([dep.name for dep in self.dependencies])
            for dep in self.args.dependencies:
                if dep not in names:
                    fatal("Unknown dependency name: {}".format(dep))
            for dep in self.dependencies:
                if dep.name in self.args.dependencies:
                    self.selected_dependencies.append(dep)
        else:
            self.selected_dependencies = self.dependencies


    def run(self):
        self.set_compiler('gcc')
        if self.args.clean:
            self.clean()
        self.prepare_out_dirs()
        self.curl_path = which('curl')
        os.environ['PATH'] = os.path.join(self.tp_installed_common_dir, 'bin') + ':' + \
                             os.environ['PATH']
        self.build(BUILD_TYPE_COMMON)
        self.build(BUILD_TYPE_UNINSTRUMENTED)
        if is_linux():
            self.build(BUILD_TYPE_ASAN)
            self.build(BUILD_TYPE_TSAN)


    def find_compiler_by_type(self, compiler_type):
        compilers = None
        if compiler_type == 'gcc':
            compilers = self.find_gcc()
        elif compiler_type == 'clang':
            compilers = self.find_clang()
        else:
            fatal("Unknown compiler type {}".format(compiler_type))

        for compiler in compilers:
            if not os.path.exists(compiler):
                fatal("Compiler executable does not exist: {}".format(compiler))

        self.cc = compilers[0]
        self.cxx = compilers[1]


    def find_gcc(self):
        if 'YB_GCC_PREFIX' is os.environ:
            gcc_dir = os.environ['YB_GCC_PREFIX']
        elif self.using_linuxbrew:
            gcc_dir = self.linuxbrew_dir
        else:
            return which('gcc'), which('g++')

        gcc_bin_dir = os.path.join(gcc_dir, 'bin')

        if not os.path.isdir(gcc_bin_dir):
            fatal("Directory {} does not exist".format(gcc_bin_dir))

        return os.path.join(gcc_bin_dir, 'gcc'), os.path.join(gcc_bin_dir, 'g++')

    def find_clang(self):
        clang_dir = None
        if 'YB_CLANG_PREFIX' is os.environ:
            clang_dir = os.environ['YB_CLANG_PREFIX']
        else:
            candidate_dirs = [
                os.path.join(self.tp_dir, 'clang-toolchain'),
                self.tp_installed_common_dir,
            ]
            for dir in candidate_dirs:
                bin_dir = os.path.join(dir, 'bin')
                if os.path.isdir(bin_dir) and os.path.exists(os.path.join(bin_dir, 'clang')):
                    clang_dir = dir
                    break
            if clang_dir is None:
                fatal("Failed to find clang at the following locations: {}".format(candidate_dirs))

        clang_bin_dir = os.path.join(clang_dir, 'bin')

        return os.path.join(clang_bin_dir, 'clang'), os.path.join(clang_bin_dir)

    def detect_linuxbrew(self):
        if not is_linux():
            return

        candidates = [
            os.path.join(os.environ['HOME'], '.linuxbrew-yb-build')
        ]

        version_for_jenkins_file = os.path.join(self.tp_dir, 'linuxbrew_version_for_jenkins.txt')
        with open(version_for_jenkins_file, 'rt') as inp:
            version_for_jenkins = inp.read().strip()

        preferred_linuxbrew_dir = os.path.join(
                '/n', 'jenkins', 'linuxbrew', 'linuxbrew_{}'.format(version_for_jenkins))
        if os.path.isdir(preferred_linuxbrew_dir):
            if is_jenkins_user():
                # If we're running on Jenkins (or building something for consumption by Jenkins
                # under the "jenkins" user), then the "Linuxbrew for Jenkins" directory takes
                # precedence.
                candidates.insert(0, preferred_linuxbrew_dir)
            else:
                # Otherwise, the user's local Linuxbrew build takes precedence.
                candidates.append(preferred_linuxbrew_dir)
        elif is_jenkins():
            log("Warning: Linuxbrew directory referenced by '{}' does not exist: '{}', will "
                    "attempt to use other location."
                .format(version_for_jenkins_file, preferred_linuxbrew_dir))

        for dir in candidates:
            if os.path.isdir(dir) and \
               os.path.isdir(os.path.join(dir, 'bin')) and \
               os.path.isdir(os.path.join(dir, 'lib')) and \
               os.path.isdir(os.path.join(dir, 'include')):
                self.linuxbrew_dir = dir
                self.using_linuxbrew = True
                log("Using linuxbrew at: {}".format(self.linuxbrew_dir))
                break

        if self.using_linuxbrew:
            os.environ['PATH']=os.path.join(self.linuxbrew_dir, 'bin') + ':' + os.environ['PATH']

    def clean(self):
        heading('Clean')
        for dependency in self.selected_dependencies:
            for dir in BUILD_TYPES:
                for leaf in [dependency.name, '.build-stamp-{}'.format(dependency)]:
                    path = os.path.join(self.tp_build_dir, dir, leaf)
                    if os.path.exists(path):
                        log("Removing {} build output: {}".format(dependency.name, path))
                        remove_path(path)
            if dependency.dir is not None:
                src_dir = self.source_path(dependency)
                if os.path.exists(src_dir):
                    log("Removing {} source: {}".format(dependency.name, src_dir))
                    remove_path(src_dir)

            archive_path = self.archive_path(dependency)
            if archive_path is not None:
                log("Removing {} archive: {}".format(dependency.name, archive_path))
                remove_path(archive_path)

    def download_dependency(self, dep):
        src_path = self.source_path(dep)
        patch_level_path = os.path.join(src_path, 'patchlevel-{}'.format(dep.patch_version))
        if os.path.exists(patch_level_path):
            return

        download_url = dep.download_url
        if download_url is None:
            download_url = CLOUDFRONT_URL.format(dep.archive_name)
        archive_path = self.archive_path(dep)

        remove_path(src_path)
        # If download_url is "mkdir" then we just create empty directory with specified name.
        if download_url != 'mkdir':
            if archive_path is None:
                return
            self.ensure_file_downloaded(download_url, archive_path)
            self.extract_archive(archive_path)
        else:
            log("Creating {}".format(src_path))
            mkdir_if_missing(src_path)

        if hasattr(dep, 'extra_downloads'):
            for extra in dep.extra_downloads:
                archive_path = os.path.join(self.tp_download_dir, extra.archive_name)
                log("Fetching extra url: {}".format(extra.archive_name))
                self.ensure_file_downloaded(extra.download_url, archive_path)
                output_path = os.path.join(src_path, extra.dir)
                self.extract_archive(archive_path, output_path)
                if hasattr(extra, 'post_exec'):
                    with PushDir(output_path):
                        if isinstance(extra.post_exec[0], basestring):
                            subprocess.check_call(extra.post_exec)
                        else:
                            for command in extra.post_exec:
                                subprocess.check_call(command)

        if hasattr(dep, 'patches'):
            with PushDir(src_path):
                for patch in dep.patches:
                    process = subprocess.Popen(['patch', '-p{}'.format(dep.patch_strip)],
                                               stdin=subprocess.PIPE)
                    with open(os.path.join(self.tp_dir, 'patches', patch), 'rt') as inp:
                        patch = inp.read()
                    process.stdin.write(patch)
                    process.stdin.close()
                    exit_code = process.wait()
                    if exit_code:
                        fatal("Patch {} failed with code: {}".format(dep.name, exit_code))
                if hasattr(dep, 'post_patch'):
                    subprocess.check_call(dep.post_patch)

        with open(patch_level_path, 'wb') as out:
            pass


    def archive_path(self, dep):
        if dep.archive_name is None:
            return None
        return os.path.join(self.tp_download_dir, dep.archive_name)


    def source_path(self, dep):
        return os.path.join(self.tp_src_dir, dep.dir)

    def load_expected_checksums(self):
        checksum_file = os.path.join(self.tp_dir, 'thirdparty_src_checksums.txt')
        if not os.path.exists(checksum_file):
            fatal("Expected checksum file not found at {}".format(checksum_file))

        self.filename2checksum = {}
        with open(checksum_file, 'rt') as inp:
            for line in inp:
                sum, fname = line.strip().split(None, 1)
                if not re.match('^[0-9a-f]{64}$', sum):
                    fatal("Invalid checksum: '{}' for archive name: '{}' in {}. Expected to be a "
                                  "SHA-256 sum (64 hex characters)."
                                  .format(sum, fname, checksum_file))
                self.filename2checksum[fname] = sum

    def ensure_file_downloaded(self, url, path):
        filename = os.path.basename(path)
        if filename not in self.filename2checksum:
            fatal("No expected checksum provided for {}".format(filename))

        mkdir_if_missing(self.tp_download_dir)

        expected_checksum = self.filename2checksum[filename]
        if os.path.exists(path):
            if self.verify_checksum(path, expected_checksum):
                log("No need to re-download {}: checksum already correct".format(filename))
                return
            log("File {} already exists but has wrong checksum, removing".format(path))
            remove_path(path)
        log("Fetching {}".format(filename))
        if re.match("s3:.*", url):
            subprocess.check_call(['s3cmd', 'get', url, path])
            # Alternatively we can use AWS CLI:
            # aws s3 cp "$download_url" "$FILENAME"
        else:
            subprocess.check_call([self.curl_path, '-o', path, '--location', url])
        if not os.path.exists(path):
            fatal("Downloaded '{}' but but unable to find '{}'".format(url, path))
        if not self.verify_checksum(path, expected_checksum):
            fatal("File '{}' has wrong checksum after downloading from '{}'. "
                          "Has {}, but expected: {}"
                          .format(path, url, hashsum_file(hashlib.sha256(), path),
                                  expected_checksum))

    def verify_checksum(self, filename, expected_checksum):
        real_checksum = hashsum_file(hashlib.sha256(), filename)
        return real_checksum == expected_checksum

    def extract_archive(self, filename, out_dir=None):
        if out_dir is None:
            out_dir = self.tp_src_dir
        mkdir_if_missing(out_dir)
        for ext in ARCHIVE_TYPES:
            if filename.endswith(ext):
                with PushDir(out_dir):
                    cmd = ARCHIVE_TYPES[ext].format(filename)
                    log("Extracting: {}".format(cmd))
                    subprocess.check_call(cmd, shell=True)
                    return
        fatal("Unknown archive type for: {}".format(filename))

    def prepare_out_dirs(self):
        dirs = [os.path.join(self.tp_installed_dir, type) for type in BUILD_TYPES]
        libcxx_dirs = [os.path.join(dir, 'libcxx') for dir in dirs]
        for dir in dirs + libcxx_dirs:
            lib_dir = os.path.join(dir, 'lib')
            mkdir_if_missing(lib_dir)
            mkdir_if_missing(os.path.join(dir, 'include'))
            # On some systems, autotools installs libraries to lib64 rather than lib.    Fix
            # this by setting up lib64 as a symlink to lib.    We have to do this step first
            # to handle cases where one third-party library depends on another.    Make sure
            # we create a relative symlink so that the entire PREFIX_DIR could be moved,
            # e.g. after it is packaged and then downloaded on a different build node.
            lib64_dir = os.path.join(dir, 'lib64')
            if os.path.exists(lib64_dir):
                if os.path.islink(lib64_dir):
                    continue
                remove_path(lib64_dir)
            os.symlink('lib', lib64_dir)

    def init_flags(self):
        self.ld_flags = []
        self.compiler_flags = []
        self.c_flags = []
        self.cxx_flags = []
        self.libs = []

        self.add_linuxbrew_flags()
        # -fPIC is there to always generate position-independent code, even for static libraries.
        self.compiler_flags += \
            ['-fno-omit-frame-pointer', '-fPIC', '-O2',
             '-I{}'.format(os.path.join(self.tp_installed_common_dir, 'include'))]
        self.ld_flags.append('-L{}'.format(os.path.join(self.tp_installed_common_dir, 'lib')))
        if is_linux():
            # On Linux, ensure we set a long enough rpath so we can change it later with chrpath or
            # a similar tool.
            self.add_rpath(
                    "/tmp/making_sure_we_have_enough_room_to_set_rpath_later_{}_end_of_rpath"
                    .format('_' * 256))

            self.cxx_flags.append('-D_GLIBCXX_USE_CXX11_ABI=0')
            self.dylib_suffix = "so"
        elif is_mac():
            self.dylib_suffix = "dylib"

            # YugaByte builds with C++11, which on OS X requires using libc++ as the standard
            # library implementation. Some of the dependencies do not compile against libc++ by
            # default, so we specify it explicitly.
            self.cxx_flags.append("-stdlib=libc++")
            self.libs += ["-lc++", "-lc++abi"]
        else:
            fatal("Unsupported platform: {}".format(platform.system()))

    def add_linuxbrew_flags(self):
        if self.using_linuxbrew:
            lib_dir = os.path.join(self.linuxbrew_dir, 'lib')
            self.ld_flags.append(" -Wl,-dynamic-linker={}".format(os.path.join(lib_dir, 'ld.so')))
            self.add_lib_dir_and_rpath(lib_dir)

    def add_lib_dir_and_rpath(self, lib_dir):
        self.ld_flags.append("-L{}".format(lib_dir))
        self.add_rpath(lib_dir)

    def prepend_lib_dir_and_rpath(self, lib_dir):
        self.ld_flags.insert(0, "-L{}".format(lib_dir))
        self.prepend_rpath(lib_dir)

    def add_rpath(self, path):
        self.ld_flags.append("-Wl,-rpath,{}".format(path))

    def prepend_rpath(self, path):
        self.ld_flags.insert(0, "-Wl,-rpath,{}".format(path))

    def log_prefix(self, dep):
        return '{} ({})'.format(dep.name, self.build_type)

    def build_with_configure(self, log_prefix, extra_args=None, **kwargs):
        os.environ["YB_REMOTE_BUILD"] = "0"
        args = ['./configure', '--prefix={}'.format(self.prefix)]
        if extra_args is not None:
            args += extra_args
        log_output(log_prefix, args)
        jobs = kwargs['jobs'] if 'jobs' in kwargs else multiprocessing.cpu_count()
        log_output(log_prefix, ['make', '-j{}'.format(jobs)])
        if 'install' not in kwargs or kwargs['install']:
            log_output(log_prefix, ['make', 'install'])

    def build_with_cmake(self, dep, extra_args=None, **kwargs):
        log_prefix = self.log_prefix(dep)
        os.environ["YB_REMOTE_BUILD"] = "0"

        remove_path('CMakeCache.txt')
        remove_path('CMakeFiles')

        src_dir = self.source_path(dep)
        if 'src_dir' in kwargs:
            src_dir = os.path.join(src_dir, kwargs['src_dir'])
        args = ['cmake', src_dir]
        if extra_args is not None:
            args += extra_args
        log_output(log_prefix, args)
        log_output(log_prefix, ['make', '-j{}'.format(multiprocessing.cpu_count())])
        if 'install' not in kwargs or kwargs['install']:
            log_output(log_prefix, ['make', 'install'])

    def build(self, type):
        if type != BUILD_TYPE_COMMON and self.args.build_type is not None:
            if type != self.args.build_type:
                return

        self.set_build_type(type)
        instrumented = type == BUILD_TYPE_ASAN or type == BUILD_TYPE_TSAN
        self.set_instrumented(instrumented)
        # This is needed at least for glog to be able to find gflags.
        self.add_rpath(os.path.join(self.tp_installed_dir, self.build_type, 'lib'))
        build_group = BUILD_GROUP_COMMON if type == BUILD_TYPE_COMMON else BUILD_GROUP_INSTRUMENTED
        for dep in self.selected_dependencies:
            if dep.build_group == build_group and dep.should_build(instrumented):
                self.build_dependency(dep)

    def set_build_type(self, type):
        self.build_type = type
        self.prefix = os.path.join(self.tp_installed_dir, type)
        self.find_prefix = self.tp_installed_common_dir
        if type != BUILD_TYPE_COMMON:
            self.find_prefix += ';' + self.prefix
        self.prefix_bin = os.path.join(self.prefix, 'bin')
        self.prefix_lib = os.path.join(self.prefix, 'lib')
        self.prefix_include = os.path.join(self.prefix, 'include')
        if type in [BUILD_TYPE_COMMON, BUILD_TYPE_UNINSTRUMENTED]:
            self.set_compiler('gcc')
        else:
            self.set_compiler('clang')
        heading("Building {} dependencies".format(type))

    def set_instrumented(self, flag):
        self.init_flags()
        if flag:
            if self.build_type == BUILD_TYPE_ASAN:
                self.compiler_flags += ['-fsanitize=address', '-fsanitize=undefined',
                                        '-DADDRESS_SANITIZER']
            elif self.build_type == BUILD_TYPE_TSAN:
                self.compiler_flags += ['-fsanitize=thread', '-DTHREAD_SANITIZER']
            else:
                fatal("Wrong instrumentation type: {}".format(self.build_type))
        if self.build_type == BUILD_TYPE_ASAN or self.build_type == BUILD_TYPE_TSAN:
            stdlib_suffix = self.build_type if flag else BUILD_TYPE_UNINSTRUMENTED
            stdlib_path = os.path.join(self.tp_installed_dir, stdlib_suffix, 'libcxx')
            stdlib_include = os.path.join(stdlib_path, 'include', 'c++', 'v1')
            stdlib_lib = os.path.join(stdlib_path, 'lib')
            self.cxx_flags.insert(0, '-nostdinc++')
            self.cxx_flags.insert(0, '-isystem')
            self.cxx_flags.insert(1, stdlib_include)
            self.cxx_flags.insert(0, '-stdlib=libc++')
            # CLang complains about argument unused during compilation: '-stdlib=libc++' when both
            # -stdlib=libc++ and -nostdinc++ are specified.
            self.cxx_flags.insert(0, '-Wno-error=unused-command-line-argument')
            self.prepend_lib_dir_and_rpath(stdlib_lib)
        if self.using_linuxbrew and self.compiler_type == 'clang':
            self.compiler_flags.append('--gcc-toolchain={}'.format(self.linuxbrew_dir))

    def build_dependency(self, dep):
        if self.args.build_type == BUILD_TYPE_UNINSTRUMENTED and dep.name in ['llvm', 'libcxx']:
            return
        if not self.should_rebuild_dependency(dep):
            return
        log("")
        colored_log(YELLOW_COLOR, SEPARATOR)
        colored_log(YELLOW_COLOR, "Building {} ({})".format(dep.name, self.build_type))
        colored_log(YELLOW_COLOR, SEPARATOR)

        self.download_dependency(dep)

        os.environ["CXXFLAGS"] = " ".join(self.compiler_flags + self.cxx_flags)
        os.environ["CFLAGS"] = " ".join(self.compiler_flags + self.c_flags)
        os.environ["LDFLAGS"] = " ".join(self.ld_flags)
        os.environ["LIBS"] = " ".join(self.libs)

        with PushDir(self.create_build_dir_and_prepare(dep)):
            dep.build(self)
        self.save_build_stamp_for_dependency(dep)
        log("")
        log("Finished building {} ({})".format(dep.name, self.build_type))
        log("")

    # Determines if we should rebuild a component with the given name based on the existing "stamp"
    # file and the current value of the "stamp" (based on Git SHA1 and local changes) for the
    # component. The result is returned in should_rebuild_component_rv variable, which should have
    # been made local by the caller.
    def should_rebuild_dependency(self, dep):
        stamp_path = self.get_build_stamp_path_for_dependency(dep)
        old_build_stamp = None
        if os.path.exists(stamp_path):
            with open(stamp_path, 'rt') as inp:
                old_build_stamp = inp.read()

        new_build_stamp = self.get_build_stamp_for_dependency(dep)

        if old_build_stamp == new_build_stamp:
            log("Not rebuilding {} ({}) -- nothing changed.".format(dep.name, self.build_type))
            return False
        else:
            log("Have to rebuild {} ({}):".format(dep.name, self.build_type))
            log("Old build stamp for {}: {} (from {})".format(
                    dep.name, old_build_stamp, stamp_path))
            log("New build stamp for {}: {}".format(dep.name, new_build_stamp))
            return True

    def get_build_stamp_path_for_dependency(self, dep):
        return os.path.join(self.tp_build_dir, self.build_type, '.build-stamp-{}'.format(dep.name))

    # Come up with a string that allows us to tell when to rebuild a particular third-party
    # dependency. The result is returned in the get_build_stamp_for_component_rv variable, which
    # should have been made local by the caller.
    def get_build_stamp_for_dependency(self, dep):
        input_files_for_stamp = ['build_thirdparty.py',
                                 os.path.join('build_definitions',
                                              '{}.py'.format(dep.name.replace('-', '_')))]

        for path in input_files_for_stamp:
            abs_path = os.path.join(self.tp_dir, path)
            if not os.path.exists(abs_path):
                fatal("File '{}' does not exist -- expecting it to exist when creating a 'stamp' " \
                            "for the build configuration of '{}'.".format(abs_path, dep.name))

        with PushDir(self.tp_dir):
            git_commit_sha1 = subprocess.check_output(
                    ['git', 'log', '--pretty=%H', '-n', '1'] + input_files_for_stamp).strip()
            git_diff = subprocess.check_output(['git', 'diff'] + input_files_for_stamp)
            git_diff_sha256 = hashlib.sha256(git_diff).hexdigest()
            return 'git_commit_sha1={}\ngit_diff_sha256={}\n'.format(git_commit_sha1,
                                                                     git_diff_sha256)

    def save_build_stamp_for_dependency(self, dep):
        stamp = self.get_build_stamp_for_dependency(dep)
        stamp_path = self.get_build_stamp_path_for_dependency(dep)

        log("Saving new build stamp to '{}': {}".format(stamp_path, stamp))
        with open(stamp_path, "wt") as out:
            out.write(stamp)

    def create_build_dir_and_prepare(self, dep):
        src_dir = self.source_path(dep)
        if not os.path.isdir(src_dir):
            fatal("Directory '{}' does not exist".format(src_dir))

        build_dir = os.path.join(self.tp_build_dir, self.build_type, dep.dir)
        mkdir_if_missing(build_dir)

        if dep.copy_sources:
            log("Bootstrapping {} from {}".format(build_dir, src_dir))
            subprocess.check_call(['rsync', '-a', src_dir + '/', build_dir])
        return build_dir

def main():
    unset_if_set('CC')
    unset_if_set('CXX')

    if 'YB_BUILD_THIRDPARTY_DUMP_ENV' in os.environ:
        heading('Environment of {}:'.format(sys.argv[0]))
        for key in os.environ:
            log('{}={}'.format(key, os.environ[key]))
        log_separator()

    builder = Builder()
    builder.init()
    builder.run()

if __name__ == "__main__":
    main()
