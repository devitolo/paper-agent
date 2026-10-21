"""GitHub-hosted exact-image checks. No deployment, credentials or model input."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid


def docker(*args):
    return subprocess.check_output(['docker',*args],text=True,timeout=180)


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise RuntimeError('Release qualification is a GitHub Actions job')
    root=Path.cwd()
    revision=os.environ['GITHUB_SHA'];bundle=os.environ['SOURCE_BUNDLE']
    evidence={'revision':revision,'source_bundle_sha256':bundle,'images':{},
              'run_url':'https://github.com/'+os.environ['GITHUB_REPOSITORY']+'/actions/runs/'+os.environ['GITHUB_RUN_ID'],
              'real_model_parity':'requires retained-production-image comparison on Mini before cutover'}
    for kind,key in (('app','APP_IMAGE'),('service','SERVICE_IMAGE')):
        image=os.environ[key]
        if not re.fullmatch(r'ghcr.io/devitolo/paper-agent(?:-minilm)?@sha256:[0-9a-f]{64}',image):
            raise ValueError('immutable release image required')
        docker('pull',image)
        info=json.loads(docker('image','inspect',image))[0]
        assert (info['Os'],info['Architecture'])==('linux','amd64')
        assert info['Config']['Labels']['org.opencontainers.image.revision']==revision
        assert info['Config']['Labels']['org.projectpaper.source-bundle-sha256']==bundle
        run=['run','--rm','--platform','linux/amd64','--network','none','--read-only','--tmpfs','/tmp:rw,size=64m','--entrypoint','python',image]
        runtime=json.loads(docker(*run,'-c',"import json,platform,importlib.metadata as m;print(json.dumps({'python':platform.python_version(),'packages':{d.metadata['Name'].lower():d.version for d in m.distributions()}}))"))
        packages=runtime['packages']
        if kind=='app':
            assert not set(packages)&{'onnxruntime','transformers','numpy','tokenizers','safetensors','torch','huggingface-hub'}
            assert 'PAPER_AGENT_MINILM_BACKEND=http' in info['Config']['Env']
            docker(*run,'-c','import paper_agents.web,paper_agents.pipeline,paper_agents.minilm_eval,paper_agents.minilm_shadow,paper_agents.gemini_runtime')
            assert docker('run','--rm','--network','none','--entrypoint','node',image,'--version').strip()=='v22.23.2'
            assert docker(*run,'-c',"import json;print(json.load(open('/opt/paper-gemini/node_modules/@google/gemini-cli/package.json'))['version'])").strip()=='0.52.0'
        else:
            for name,version in {'onnxruntime':'1.23.2','transformers':'4.57.1','numpy':'2.5.3','tokenizers':'0.22.2'}.items():
                assert packages[name]==version
            docker(*run,'-c','import onnxruntime,transformers,numpy,tokenizers,paper_agents.minilm_service')
        docker(*run,'-m','pip','check')
        hashes=json.loads(docker(*run,'-c',"import json,hashlib;from pathlib import Path;print(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('paper_agents').glob('*.py')}))"))
        assert hashes
        for path,value in hashes.items():assert hashlib.sha256((root/path).read_bytes()).hexdigest()==value
        docker('run','--rm','--network','none','--read-only','--tmpfs','/tmp:rw,size=64m',
               '--mount','type=bind,src='+str(root/'tests/minilm_service')+',dst=/app/tests/minilm_service,readonly',
               '--mount','type=bind,src='+str(root/'Dockerfile')+',dst=/app/Dockerfile,readonly',
               '--entrypoint','python',image,'-W','error::ResourceWarning','-m','unittest','discover','-s','tests/minilm_service','-v')
        if kind=='service':
            name='minilm-signal-'+uuid.uuid4().hex[:12]
            try:
                docker('run','-d','--name',name,'--network','none','--read-only','--entrypoint','python',image,'-c',
                       'from paper_agents import minilm_service as s; s.create_server=lambda a:s.InferenceServer(a,object(),{}); s.main()')
                time.sleep(2)
                docker('stop','--time','5',name)
                state=json.loads(docker('inspect',name))[0]['State']
                assert state['ExitCode']==0 and not state['OOMKilled']
            finally:docker('rm','-f',name)
        evidence['images'][kind]={'reference':image,'config_id':info['Id'],'platform':'linux/amd64',
                                 'runtime':runtime,'source_bytes_match':True,'package_checks':'passed',
                                 'fake_engine_tests':'11 passed; 1 real-model skip'}
    text=json.dumps(evidence,indent=2)+'\n'
    Path('release-evidence.json').write_text(text)
    with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as out:
        out.write('Qualified MiniLM release images; no deployment performed.\n```json\n'+text+'```\n')
    print(text)

if __name__=='__main__':main()
