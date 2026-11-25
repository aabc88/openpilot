"""Prepared, ignition-scoped selection of models for the comma's internal GPU."""

from collections.abc import Callable
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.request import urlopen

from openpilot.common.big_model_catalog import atomic_json, boot_id, read_json, storage


CD210 = 'cd210'
VEGA = 'polaris-vega'
VEGA_SHA = 'cfd9dbb518d3fdbae5f3eb1f64eeae9a8a9ea88526529038a71359de2a9cb021'
VEGA_SIZE = 96_528_841
VEGA_URL = ('https://huggingface.co/buckets/StarPilot-Driving/StarPilot-Resources/resolve/'
            'onnx/spz_polaris_vega/spz_polaris_vega_driving_supercombo.onnx')
MODEL_DIR = Path(__file__).resolve().parent
REPO = MODEL_DIR.parents[2]


def cache_dir():
  return storage() / 'internal'


def can_prepare():
  if not Path('/AGNOS').is_file():
    return False
  device_config = read_json(MODEL_DIR / 'models' / 'tg_input_devices.json')
  modeld_config = device_config.get('openpilot.selfdrive.modeld.modeld')
  return isinstance(modeld_config, dict) and modeld_config.get('default') == {
    'WARP_DEV': 'QCOM', 'QUEUE_DEV': 'QCOM',
  }


def require_offroad(offroad: Callable[[], bool]):
  if not offroad():
    raise ValueError('Turn the ignition off to prepare or select an internal model')


def compiler_id():
  """Invalidate JITs when their compiler, tinygrad or camera inputs change."""
  digest = hashlib.sha256(VEGA_SHA.encode())
  for name in ('compile_modeld.py', 'get_model_metadata.py', 'helpers.py', 'constants.py'):
    digest.update((MODEL_DIR / name).read_bytes())
  for path in ('openpilot/system/camerad/cameras/nv12_info.py', 'openpilot/system/hardware/hw.py',
               'openpilot/common/transformations/camera.py', 'openpilot/common/transformations/model.py'):
    digest.update((REPO / path).read_bytes())
  digest.update(subprocess.check_output(['git', 'rev-parse', 'HEAD:tinygrad_repo'],
                                        cwd=REPO, timeout=4, stderr=subprocess.DEVNULL))
  return digest.hexdigest()


def selected():
  wanted = read_json(cache_dir() / 'selection.json').get('id')
  return VEGA if wanted == VEGA else CD210


def prepared_path(*, verify=False):
  path = cache_dir() / 'vega' / 'driving_tinygrad.pkl'
  try:
    receipt = read_json(path.with_name('prepared.json'))
    if not receipt or not path.is_file() or not can_prepare():
      return None
    if path.stat().st_size != receipt.get('size') or receipt.get('compiler') != compiler_id():
      return None
    if verify:
      with path.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != receipt.get('sha256'):
          return None
  except (OSError, subprocess.SubprocessError):
    return None
  return path


def rejected(path):
  failure = read_json(path.with_name('rejected.json'))
  receipt = read_json(path.with_name('prepared.json'))
  return bool(failure and failure.get('sha256') == receipt.get('sha256'))


def select(key, offroad: Callable[[], bool]):
  require_offroad(offroad)
  if key not in (CD210, VEGA):
    raise ValueError('Unknown internal model')
  if key == VEGA:
    path = prepared_path(verify=True)
    if path is None or rejected(path):
      raise ValueError('Prepare and verify Polaris Vega before selecting it')
  require_offroad(offroad)
  atomic_json(cache_dir() / 'selection.json', {'id': key})


def runtime_choice():
  # modeld is pre-imported by the manager. Resolve inside its child process at
  # the start of each ignition cycle; never read this setting on a live tick.
  if selected() == VEGA:
    path = prepared_path(verify=True)
    if path is not None and not rejected(path):
      return VEGA, path
  return CD210, None


def record_effective(key):
  if not Path('/AGNOS').is_file():
    return
  try:
    atomic_json(cache_dir() / 'running.json', {'id': key, 'pid': os.getpid(), 'boot_id': boot_id()})
  except OSError:
    pass  # Settings telemetry must not take down a running model.


def record_failure(path):
  try:
    receipt = read_json(path.with_name('prepared.json'))
    if receipt.get('sha256'):
      atomic_json(path.with_name('rejected.json'), {'sha256': receipt['sha256']})
  except OSError:
    pass


def status(can_change: bool):
  record = read_json(cache_dir() / 'running.json')
  pid = record.get('pid')
  effective = None
  if (not can_change and record.get('boot_id') == boot_id()
      and isinstance(pid, int) and pid > 0 and Path(f'/proc/{pid}').is_dir()):
    effective = record.get('id')
  path = prepared_path()
  blocked = path is not None and rejected(path)
  return {'selected': selected(), 'effective': effective, 'can_change': can_change,
          'can_prepare': can_prepare(), 'preparing': False,
          'error': 'runtime_rejected' if blocked else None,
          'models': [{'id': CD210, 'name': 'CD210', 'ready': True},
                     {'id': VEGA, 'name': 'Polaris Vega', 'ready': path is not None and not blocked}]}


@contextmanager
def preparation_lock(folder):
  import fcntl
  with (folder / '.prepare.lock').open('a') as stream:
    try:
      fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
      raise RuntimeError('Model preparation is already running') from exc
    yield


def download_model(output, offroad):
  digest = hashlib.sha256()
  size = 0
  with urlopen(VEGA_URL, timeout=12) as response, output.open('wb') as stream:
    while block := response.read(1024 * 1024):
      require_offroad(offroad)
      size += len(block)
      if size > VEGA_SIZE:
        raise ValueError('Unexpected Polaris Vega download size')
      digest.update(block)
      stream.write(block)
  if size != VEGA_SIZE or digest.hexdigest() != VEGA_SHA:
    raise ValueError('Polaris Vega checksum verification failed')


def compile_model(source, output, offroad):
  from openpilot.common.transformations.camera import _ar_ox_fisheye, _os_fisheye
  from openpilot.common.transformations.model import MEDMODEL_INPUT_SIZE
  from openpilot.selfdrive.modeld.constants import ModelConstants
  env = dict(os.environ, DEV='QCOM', IMAGE='1', FLOAT16='1', NOLOCALS='1', JIT_BATCH_SIZE='0',
             OPENPILOT_HACKS='1', WARP_DEV='QCOM')
  args = ['taskset', '-c', '7', sys.executable, str(MODEL_DIR / 'compile_modeld.py'), '--onnx', str(source),
          '--output', str(output), '--model-size', f'{MEDMODEL_INPUT_SIZE[0]}x{MEDMODEL_INPUT_SIZE[1]}',
          '--camera-resolutions', *(f'{camera.width}x{camera.height}' for camera in (_ar_ox_fisheye, _os_fisheye)),
          '--frame-skip', str(ModelConstants.MODEL_RUN_FREQ // ModelConstants.MODEL_CONTEXT_FREQ), '--match-input-dtypes']
  with output.with_name('compile.log').open('wb') as log:
    require_offroad(offroad)
    with subprocess.Popen(args, env=env, cwd=REPO, stdout=log, stderr=subprocess.STDOUT) as process:
      try:
        while process.poll() is None:
          require_offroad(offroad)
          try:
            process.wait(timeout=.25)
          except subprocess.TimeoutExpired:
            continue
      finally:
        if process.poll() is None:
          process.kill()
          process.wait()
    if process.returncode:
      raise RuntimeError('Polaris Vega could not be compiled by the internal GPU runtime')


def prepare(offroad: Callable[[], bool]):
  require_offroad(offroad)
  if not can_prepare():
    raise ValueError('This device has no supported internal QCOM model compiler')
  folder = cache_dir() / 'vega'
  folder.mkdir(parents=True, exist_ok=True)
  with preparation_lock(folder):
    existing = prepared_path(verify=True)
    if existing is not None and not rejected(existing):
      return
    signature = compiler_id()
    with tempfile.TemporaryDirectory(prefix='prepare-', dir=folder) as tmp:
      source, output = Path(tmp) / 'vega.onnx', Path(tmp) / 'driving_tinygrad.pkl'
      download_model(source, offroad)
      compile_model(source, output, offroad)
      if compiler_id() != signature:
        raise RuntimeError('Model compiler changed during preparation')
      with output.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
      receipt = {'compiler': signature, 'sha256': digest, 'size': output.stat().st_size}
      require_offroad(offroad)
      os.replace(output, folder / output.name)
      (folder / 'rejected.json').unlink(missing_ok=True)
      atomic_json(folder / 'prepared.json', receipt)
