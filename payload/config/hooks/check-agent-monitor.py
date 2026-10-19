#!/usr/bin/env python3
"""Offline checks for bounded AI delegation and restricted CI tool role."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
SHA = 'a' * 40


def check():
    with tempfile.TemporaryDirectory(prefix='ci-agent-check-') as temporary:
        home = Path(temporary)
        binary = home / '.local/bin/agy'
        binary.parent.mkdir(parents=True)
        binary.write_text("#!" + sys.executable + "\n" + """import json,os,sys
assert os.environ['AGY_GUARD_ROLE']=='ci-monitor'
assert sys.argv[sys.argv.index('--agent')+1]=='ci-status-monitor'
assert sys.argv[sys.argv.index('--print-timeout')+1]=='600s'
prompt=sys.argv[sys.argv.index('-p')+1]
assert '--seconds 480' in prompt
report={'status':'PASS','repo':'owner/repo','sha':'a'*40,'run_ids':[101],
        'runs':[{'databaseId':101,'headSha':'a'*40,'status':'completed','conclusion':'success',
                 'jobs':[{'status':'completed','conclusion':'success'}]}], 'polls':1,'elapsed_seconds':1.5,'reason':'inspected terminal worker result'}
if os.environ.get('FAKE_MODE')=='wrongscope':report['sha']='b'*40
if os.environ.get('FAKE_MODE')=='empty':report.update(runs=[],run_ids=[])
if os.environ.get('FAKE_MODE')=='incomplete':report['runs'][0]['jobs']=[]
print(json.dumps({'status':'SUCCESS','structured_output':report}))
""")
        binary.chmod(0o700)
        env={**os.environ,'HOME':str(home)}
        argv=[sys.executable,str(HERE/'independent-agent.py'),'ci-monitor','--repo','owner/repo',
              '--sha',SHA,'--run-id','101','--cwd',str(home)]
        for mode,success in [('good',True),('wrongscope',False),('empty',False),('incomplete',False)]:
            result=subprocess.run(argv,env={**env,'FAKE_MODE':mode},text=True,capture_output=True,timeout=4)
            report=json.loads(result.stdout)
            assert (result.returncode==0)==success,(mode,result.stdout,result.stderr)
            assert (report['status']=='PASS')==success
        helper=str(HERE/'ci-monitor.py')
        command='python3 '+helper+' --repo owner/repo --sha '+SHA+' --seconds 480'
        def pre(name,args,role='ci-monitor'):
            data={'conversationId':'isolated-agent-monitor','workspacePaths':[str(home)],'toolCall':{'name':name,'args':args}}
            process=subprocess.run([sys.executable,str(HERE/'review-gate.py'),'pre'],input=json.dumps(data),
                text=True,capture_output=True,env={**env,'AGY_GUARD_ROLE':role},timeout=3,check=True)
            return json.loads(process.stdout)
        assert pre('run_command',{'CommandLine':command})['decision']=='allow'
        assert pre('manage_task',{'Action':'status','TaskId':'own-returned-task'})['decision']=='allow'
        for name,args in [('run_command',{'CommandLine':'gh run view 101'}),
                          ('run_command',{'CommandLine':command+'; touch /tmp/not-allowed'}),
                          ('run_command',{'CommandLine':command+' --rerun'}),
                          ('write_to_file',{'TargetFile':'/tmp/not-allowed'}),
                          ('manage_task',{'Action':'cancel','TaskId':'other'}),('invoke_subagent',{})]:
            assert pre(name,args)['decision']=='deny',(name,args)
        denied=pre('run_command',{'CommandLine':'gh run view 101','Cwd':str(home)},role='')
        assert denied['decision']=='deny' and 'ci-monitor' in denied['reason'],denied
        print('PASS: bounded independent AI launcher, worker scope validation, CI-only tool restriction and main-agent delegation')


if __name__=='__main__':
    check()
