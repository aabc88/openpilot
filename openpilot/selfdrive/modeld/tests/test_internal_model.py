from contextlib import nullcontext
import hashlib
from io import BytesIO
import subprocess

import pytest

from openpilot.selfdrive.modeld import internal_model


ONNX_BYTES = b'verified test model'
COMPILED_BYTES = b'compiled model'
COMPILER_ID = 'test compiler'


@pytest.fixture
def local_cache(tmp_path, monkeypatch):
  monkeypatch.setattr(internal_model, 'cache_dir', lambda: tmp_path)
  monkeypatch.setattr(internal_model, 'can_prepare', lambda: True)
  monkeypatch.setattr(internal_model, 'compiler_id', lambda: COMPILER_ID)
  # This process lock uses flock on the device. These tests also run on Windows.
  monkeypatch.setattr(internal_model, 'preparation_lock', lambda _folder: nullcontext())
  monkeypatch.setattr(internal_model, 'VEGA_SIZE', len(ONNX_BYTES))
  monkeypatch.setattr(internal_model, 'VEGA_SHA', hashlib.sha256(ONNX_BYTES).hexdigest())
  monkeypatch.setattr(internal_model, 'urlopen', lambda *_args, **_kwargs: BytesIO(ONNX_BYTES))
  return tmp_path


@pytest.fixture
def compiler(monkeypatch):
  executions = []

  def compile_model(source, output, offroad):
    internal_model.require_offroad(offroad)
    assert source.read_bytes() == ONNX_BYTES
    assert source.parent == output.parent
    output.write_bytes(COMPILED_BYTES)
    executions.append(output)

  monkeypatch.setattr(internal_model, 'compile_model', compile_model)
  return executions


def test_prepare_publishes_verifiable_model_without_selecting_it(local_cache, compiler):
  assert internal_model.runtime_choice() == (internal_model.CD210, None)

  internal_model.prepare(lambda: True)
  path = internal_model.prepared_path(verify=True)
  assert path == local_cache / 'vega' / 'driving_tinygrad.pkl'
  assert path.read_bytes() == COMPILED_BYTES
  assert internal_model.read_json(path.with_name('prepared.json')) == {
    'compiler': COMPILER_ID,
    'size': len(COMPILED_BYTES),
    'sha256': hashlib.sha256(COMPILED_BYTES).hexdigest(),
  }
  assert not compiler[0].parent.exists()
  assert internal_model.runtime_choice() == (internal_model.CD210, None)

  internal_model.prepare(lambda: True)
  assert len(compiler) == 1
  internal_model.select(internal_model.VEGA, lambda: True)
  assert internal_model.runtime_choice() == (internal_model.VEGA, path)
  internal_model.select(internal_model.CD210, lambda: True)
  assert internal_model.runtime_choice() == (internal_model.CD210, None)


def test_selection_rejects_onroad_unprepared_unknown_and_ignition_races(local_cache, compiler):
  with pytest.raises(ValueError, match='ignition off'):
    internal_model.select(internal_model.CD210, lambda: False)
  with pytest.raises(ValueError, match='Prepare and verify'):
    internal_model.select(internal_model.VEGA, lambda: True)
  with pytest.raises(ValueError, match='Unknown'):
    internal_model.select('other', lambda: True)

  internal_model.prepare(lambda: True)
  road_state = iter((True, False))
  with pytest.raises(ValueError, match='ignition off'):
    internal_model.select(internal_model.VEGA, lambda: next(road_state))
  assert not (local_cache / 'selection.json').exists()
  assert internal_model.runtime_choice() == (internal_model.CD210, None)


def test_corrupt_prepared_model_is_rejected_even_if_size_matches(local_cache, compiler):
  internal_model.prepare(lambda: True)
  internal_model.select(internal_model.VEGA, lambda: True)
  path = internal_model.prepared_path(verify=True)
  path.write_bytes(b'x' * len(COMPILED_BYTES))

  with pytest.raises(ValueError, match='Prepare and verify'):
    internal_model.select(internal_model.VEGA, lambda: True)
  assert internal_model.runtime_choice() == (internal_model.CD210, None)


def test_compiler_revision_invalidates_prepared_model(local_cache, compiler, monkeypatch):
  internal_model.prepare(lambda: True)
  internal_model.select(internal_model.VEGA, lambda: True)

  monkeypatch.setattr(internal_model, 'compiler_id', lambda: 'different compiler')
  assert internal_model.prepared_path(verify=True) is None
  assert internal_model.runtime_choice() == (internal_model.CD210, None)
  internal_model.prepare(lambda: True)
  assert len(compiler) == 2
  assert internal_model.runtime_choice()[0] == internal_model.VEGA


def test_runtime_failure_falls_back_and_reprepare_clears_failure(local_cache, compiler):
  internal_model.prepare(lambda: True)
  internal_model.select(internal_model.VEGA, lambda: True)
  path = internal_model.prepared_path(verify=True)

  internal_model.record_failure(path)
  assert internal_model.selected() == internal_model.VEGA
  assert internal_model.rejected(path)
  assert internal_model.runtime_choice() == (internal_model.CD210, None)
  assert internal_model.status(True)['models'][1]['ready'] is False
  with pytest.raises(ValueError, match='Prepare and verify'):
    internal_model.select(internal_model.VEGA, lambda: True)

  internal_model.prepare(lambda: True)
  assert len(compiler) == 2
  assert not internal_model.rejected(path)
  assert internal_model.runtime_choice() == (internal_model.VEGA, path)


def test_preparation_cancels_before_publication_when_ignition_changes(local_cache, monkeypatch):
  offroad = True

  def complete_compilation(_source, output, _offroad):
    nonlocal offroad
    output.write_bytes(COMPILED_BYTES)
    offroad = False

  monkeypatch.setattr(internal_model, 'compile_model', complete_compilation)
  with pytest.raises(ValueError, match='ignition off'):
    internal_model.prepare(lambda: offroad)
  assert internal_model.prepared_path(verify=True) is None
  assert not list((local_cache / 'vega').glob('prepare-*'))
  assert not (local_cache / 'vega' / 'driving_tinygrad.pkl').exists()
  assert not (local_cache / 'vega' / 'prepared.json').exists()


def test_source_checksum_failure_never_runs_compiler(local_cache, compiler, monkeypatch):
  monkeypatch.setattr(internal_model, 'urlopen', lambda *_args, **_kwargs: BytesIO(b'x' * len(ONNX_BYTES)))
  with pytest.raises(ValueError, match='checksum'):
    internal_model.prepare(lambda: True)
  assert compiler == []
  assert internal_model.prepared_path(verify=True) is None


def test_compiler_child_is_reaped_when_ignition_changes(tmp_path, monkeypatch):
  offroad = True
  calls = []

  class FakeCompiler:
    returncode = None

    def __enter__(self):
      return self

    def __exit__(self, *_args):
      assert self.returncode is not None

    def poll(self):
      return self.returncode

    def wait(self, timeout=None):
      nonlocal offroad
      if timeout is not None:
        offroad = False
        raise subprocess.TimeoutExpired('compile_modeld.py', timeout)
      calls.append('reap')
      return self.returncode

    def kill(self):
      calls.append('kill')
      self.returncode = -9

  monkeypatch.setattr(internal_model.subprocess, 'Popen', lambda *_args, **_kwargs: FakeCompiler())
  with pytest.raises(ValueError, match='ignition off'):
    internal_model.compile_model(tmp_path / 'source.onnx', tmp_path / 'target.pkl', lambda: offroad)
  assert calls == ['kill', 'reap']
