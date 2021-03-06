#!/usr/bin/env python

import atexit
import contextlib
import errno
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib2
import os
import zipfile

from config import is_verbose_mode, PLATFORM
from env_util import get_vs_env

BOTO_DIR = os.path.abspath(os.path.join(__file__, '..', '..', '..', 'vendor',
                                        'boto'))

NPM = 'npm'
if sys.platform in ['win32', 'cygwin']:
  NPM += '.cmd'


def get_host_arch():
  """Returns the host architecture with a predictable string."""
  host_arch = platform.machine()

  # Convert machine type to format recognized by gyp.
  if re.match(r'i.86', host_arch) or host_arch == 'i86pc':
    host_arch = 'ia32'
  elif host_arch in ['x86_64', 'amd64']:
    host_arch = 'x64'
  elif host_arch.startswith('arm'):
    host_arch = 'arm'

  # platform.machine is based on running kernel. It's possible to use 64-bit
  # kernel with 32-bit userland, e.g. to give linker slightly more memory.
  # Distinguish between different userland bitness by querying
  # the python binary.
  if host_arch == 'x64' and platform.architecture()[0] == '32bit':
    host_arch = 'ia32'

  return host_arch


def tempdir(prefix=''):
  directory = tempfile.mkdtemp(prefix=prefix)
  atexit.register(shutil.rmtree, directory)
  return directory


@contextlib.contextmanager
def scoped_cwd(path):
  cwd = os.getcwd()
  os.chdir(path)
  try:
    yield
  finally:
    os.chdir(cwd)


@contextlib.contextmanager
def scoped_env(key, value):
  origin = ''
  if key in os.environ:
    origin = os.environ[key]
  os.environ[key] = value
  try:
    yield
  finally:
    os.environ[key] = origin


def download(text, url, path):
  safe_mkdir(os.path.dirname(path))
  with open(path, 'wb') as local_file:
    if hasattr(ssl, '_create_unverified_context'):
      ssl._create_default_https_context = ssl._create_unverified_context

    web_file = urllib2.urlopen(url)
    file_size = int(web_file.info().getheaders("Content-Length")[0])
    downloaded_size = 0
    block_size = 128

    ci = os.environ.get('CI') == '1'

    while True:
      buf = web_file.read(block_size)
      if not buf:
        break

      downloaded_size += len(buf)
      local_file.write(buf)

      if not ci:
        percent = downloaded_size * 100. / file_size
        status = "\r%s  %10d  [%3.1f%%]" % (text, downloaded_size, percent)
        print status,

    if ci:
      print "%s done." % (text)
    else:
      print
  return path


def extract_tarball(tarball_path, member, destination):
  with tarfile.open(tarball_path) as tarball:
    tarball.extract(member, destination)


def extract_zip(zip_path, destination):
  if sys.platform == 'darwin':
    # Use unzip command on Mac to keep symbol links in zip file work.
    execute(['unzip', zip_path, '-d', destination])
  else:
    with zipfile.ZipFile(zip_path) as z:
      z.extractall(destination)

def make_zip(zip_file_path, files, dirs):
  safe_unlink(zip_file_path)
  if sys.platform == 'darwin':
    files += dirs
    execute(['zip', '-r', '-y', zip_file_path] + files)
  else:
    zip_file = zipfile.ZipFile(zip_file_path, "w", zipfile.ZIP_DEFLATED)
    for filename in files:
      zip_file.write(filename, filename)
    for dirname in dirs:
      for root, _, filenames in os.walk(dirname):
        for f in filenames:
          zip_file.write(os.path.join(root, f))
    zip_file.close()


def rm_rf(path):
  try:
    shutil.rmtree(path)
  except OSError:
    pass


def safe_unlink(path):
  try:
    os.unlink(path)
  except OSError as e:
    if e.errno != errno.ENOENT:
      raise


def safe_mkdir(path):
  try:
    os.makedirs(path)
  except OSError as e:
    if e.errno != errno.EEXIST:
      raise


def execute(argv, env=os.environ, cwd=None):
  if is_verbose_mode():
    print ' '.join(argv)
  try:
    output = subprocess.check_output(argv, stderr=subprocess.STDOUT, env=env, cwd=cwd)
    if is_verbose_mode():
      print output
    return output
  except subprocess.CalledProcessError as e:
    print e.output
    raise e


def execute_stdout(argv, env=os.environ, cwd=None):
  if is_verbose_mode():
    print ' '.join(argv)
    try:
      subprocess.check_call(argv, env=env, cwd=cwd)
    except subprocess.CalledProcessError as e:
      print e.output
      raise e
  else:
    execute(argv, env, cwd)


def electron_gyp():
  SOURCE_ROOT = os.path.abspath(os.path.join(__file__, '..', '..', '..'))
  gyp = os.path.join(SOURCE_ROOT, 'electron.gyp')
  with open(gyp) as f:
    obj = eval(f.read());
    return obj['variables']


def get_electron_version():
  return 'v' + electron_gyp()['version%']


def parse_version(version):
  if version[0] == 'v':
    version = version[1:]

  vs = version.split('.')
  if len(vs) > 4:
    return vs[0:4]
  else:
    return vs + ['0'] * (4 - len(vs))


def boto_path_dirs():
  return [
    os.path.join(BOTO_DIR, 'build', 'lib'),
    os.path.join(BOTO_DIR, 'build', 'lib.linux-x86_64-2.7')
  ]


def run_boto_script(access_key, secret_key, script_name, *args):
  env = os.environ.copy()
  env['AWS_ACCESS_KEY_ID'] = access_key
  env['AWS_SECRET_ACCESS_KEY'] = secret_key
  env['PYTHONPATH'] = os.path.pathsep.join(
      [env.get('PYTHONPATH', '')] + boto_path_dirs())

  boto = os.path.join(BOTO_DIR, 'bin', script_name)
  execute([sys.executable, boto] + list(args), env)


def s3put(bucket, access_key, secret_key, prefix, key_prefix, files):
  args = [
    '--bucket', bucket,
    '--prefix', prefix,
    '--key_prefix', key_prefix,
    '--grant', 'public-read'
  ] + files

  run_boto_script(access_key, secret_key, 's3put', *args)


def import_vs_env(target_arch):
  if sys.platform != 'win32':
    return

  if target_arch == 'ia32':
    vs_arch = 'amd64_x86'
  else:
    vs_arch = 'x86_amd64'
  env = get_vs_env('[15.0,16.0)', vs_arch)
  os.environ.update(env)


def set_clang_env(env):
  SOURCE_ROOT = os.path.abspath(os.path.join(__file__, '..', '..', '..'))
  llvm_dir = os.path.join(SOURCE_ROOT, 'vendor', 'llvm-build',
                          'Release+Asserts', 'bin')
  env['CC']  = os.path.join(llvm_dir, 'clang')
  env['CXX'] = os.path.join(llvm_dir, 'clang++')


def update_electron_modules(dirname, target_arch, nodedir):
  env = os.environ.copy()
  version = get_electron_version()
  env['npm_config_arch']    = target_arch
  env['npm_config_target']  = version
  env['npm_config_nodedir'] = nodedir
  update_node_modules(dirname, env)
  execute_stdout([NPM, 'rebuild'], env, dirname)


def update_node_modules(dirname, env=None):
  if env is None:
    env = os.environ.copy()
  if PLATFORM == 'linux':
    # Use prebuilt clang for building native modules.
    set_clang_env(env)
    env['npm_config_clang'] = '1'
  with scoped_cwd(dirname):
    args = [NPM, 'install']
    if is_verbose_mode():
      args += ['--verbose']
    # Ignore npm install errors when running in CI.
    if os.environ.has_key('CI'):
      try:
        execute_stdout(args, env)
      except subprocess.CalledProcessError:
        pass
    else:
      execute_stdout(args, env)
