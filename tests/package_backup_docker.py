"""Opt-in Docker drill: python3 tests/package_backup_docker.py; uses isolated volumes.
Leaves synthetic evidence in the system temp directory; removes only its test projects.
"""
import json, os, pathlib, shutil, subprocess, tempfile, time
repo=pathlib.Path(__file__).resolve().parents[1]
root=pathlib.Path(tempfile.mkdtemp(prefix='paper-backup-smoke-'))
image=dict(line.split('=',1) for line in (repo/'.env.example').read_text().splitlines() if '=' in line and not line.startswith('#'))['PAPER_APP_IMAGE']
projects=[]
evidence=pathlib.Path(os.environ.get('PAPER_BACKUP_EVIDENCE_DIR', str(root/'evidence')))
evidence.mkdir(parents=True, exist_ok=True)
report={'app_image':image, 'commit':os.environ.get('GITHUB_SHA', ''),
        'result':'failed', 'checks':[]}

def run(args,cwd,**kw):
    return subprocess.run(args,cwd=cwd,text=True,check=True,**kw)
try:
    for kind in ['source','restore']:
        work=root/kind;work.mkdir()
        shutil.copytree(repo/'scripts',work/'scripts')
        shutil.copy(repo/'docker-compose.yml',work)
        (work/'.env').write_text((repo/'.env.example').read_text().replace('PAPER_PORT=8000','PAPER_PORT=0'))
        project='paper-backup-'+str(os.getpid())+'-'+kind;projects.append((project,work))
        compose=['docker','compose','-p',project]
        if kind=='source':
            (work/'.paper-install').write_text('project='+project+'\n')
            run(compose+['create','--no-build','app'],work)
            cid=run(compose+['ps','-aq','app'],work,capture_output=True).stdout.strip()
            seed="from paper_agents.package_runtime import initialize; initialize(); from pathlib import Path; Path('data/backup-test.txt').write_text('private feedback artifact'); Path('config/backup-test.txt').write_text('private topic state')"
            run(['docker','run','--rm','--volumes-from',cid,'--entrypoint','python',image,'-c',seed],work)
            seed_db="import sqlite3; db=sqlite3.connect('/app/data/paper_agent.db'); db.execute('CREATE TABLE backup_probe (feedback TEXT)'); db.execute(\"INSERT INTO backup_probe VALUES ('synthetic saved feedback')\"); db.commit(); db.close()"
            run(['docker','run','--rm','--volumes-from',cid,'--entrypoint','python',image,'-c',seed_db],work)
            run(['bash','scripts/package_backup.sh','backup',str(root/'state.tar.gz')],work)
            # Existing destination must remain intact.
            before=(root/'state.tar.gz').read_bytes()
            failure=subprocess.run(['bash','scripts/package_backup.sh','backup',str(root/'state.tar.gz')],cwd=work,capture_output=True)
            assert failure.returncode!=0 and (root/'state.tar.gz').read_bytes()==before
        else:
            run(['bash','scripts/package_backup.sh','restore',str(root/'state.tar.gz')],work,env={**os.environ,'PAPER_RESTORE_PROJECT':project})
            assert 'app_image='+image in (work/'.paper-install').read_text()
            cid=run(compose+['ps','-aq','app'],work,capture_output=True).stdout.strip()
            check="from paper_agents.package_runtime import initialize; initialize(); from pathlib import Path; assert Path('data/backup-test.txt').read_text()=='private feedback artifact'; assert Path('config/backup-test.txt').read_text()=='private topic state'; assert Path('data/paper_agent.db').stat().st_uid==10001; print('restored state and app initialization PASS')"
            run(['docker','run','--rm','--volumes-from',cid,'--entrypoint','python',image,'-c',check],work)
            check_db="import sqlite3; db=sqlite3.connect('/app/data/paper_agent.db'); assert db.execute('SELECT feedback FROM backup_probe').fetchone()[0]=='synthetic saved feedback'; db.close()"
            run(['docker','run','--rm','--volumes-from',cid,'--entrypoint','python',image,'-c',check_db],work)
            run(compose+['up','-d','app'],work)
            for _ in range(20):
                result=subprocess.run(['docker','exec',cid,'python','-m','paper_agents.package_runtime','check-app'],capture_output=True)
                if result.returncode==0:break
                time.sleep(1)
            assert result.returncode==0,result.stderr
            failure=subprocess.run(['bash','scripts/package_backup.sh','backup',str(root/'running.tar.gz')],cwd=work,capture_output=True)
            assert failure.returncode!=0 and not (root/'running.tar.gz').exists()
            print('running-service refusal and restored web readiness PASS')
    report['checks']=['archive round trip', 'SQLite saved value', 'state in both volumes',
                      'restored UID and app initialization', 'installer identity',
                      'existing archive refusal', 'running service refusal', 'web readiness']
    report['result']='passed'
    print('DOCKER BACKUP/RESTORE PASS; evidence directory:',evidence)
finally:
    for project,work in projects:
        logs=subprocess.run(['docker','compose','-p',project,'logs','--no-color','--tail','100','app'],cwd=work,text=True,capture_output=True)
        (evidence/(project+'.log')).write_text(logs.stdout+logs.stderr)
        cleaned=subprocess.run(['docker','compose','-p',project,'down','--volumes'],cwd=work,check=False)
        if cleaned.returncode:
            report['result']='failed'
            report['cleanup_failed']=True
    (evidence/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    if report.get('cleanup_failed'):
        raise RuntimeError('Test project cleanup failed')
