import importlib.util
import os
from pathlib import Path
import subprocess
import pytest

spec=importlib.util.spec_from_file_location('worker_install',Path(__file__).resolve().parents[1]/'scripts/install_hrrn_worker_readonly.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def test_install_preserves_other_keys_and_restricts_ours(tmp_path):
    ssh=tmp_path/'.ssh';ssh.mkdir()
    original='# unrelated\nssh-ed25519 AAAAother other-user\n'
    (ssh/'authorized_keys').write_text(original)
    module.install(tmp_path)
    result=(ssh/'authorized_keys').read_text()
    assert result.startswith(original)
    assert result.count(module.KEY)==1
    assert 'restrict,command=' in result
    assert list(ssh.glob('authorized_keys.before-*'))
    assert (ssh/'gpu-lab-hrrn-basic-worker-readonly.py').read_text()==module.GATE

@pytest.mark.parametrize('command',['sh','hrrn-basic-ro gpu; id','hrrn-basic-ro restart','hrrn-stage5 start'])
def test_gate_denies_non_readonly_commands(monkeypatch,command):
    monkeypatch.setenv('SSH_ORIGINAL_COMMAND',command)
    def bad(*a,**kw): raise AssertionError('command executed')
    monkeypatch.setattr(subprocess,'run',bad)
    with pytest.raises(SystemExit) as e: exec(compile(module.GATE,'gate','exec'),{})
    assert e.value.code==126

def test_gpu_command_is_fixed_and_without_shell(monkeypatch):
    monkeypatch.setenv('SSH_ORIGINAL_COMMAND','hrrn-basic-ro gpu')
    calls=[]
    def capture(argv,**kwargs):
        calls.append((argv,kwargs))
        return subprocess.CompletedProcess(argv,0)
    monkeypatch.setattr(subprocess,'run',capture)
    with pytest.raises(SystemExit) as e:exec(compile(module.GATE,'gate','exec'),{})
    assert e.value.code==0 and calls[0][0][0]=='nvidia-smi'
    assert not calls[0][1].get('shell',False)
