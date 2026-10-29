"""Portable local launcher; no private-machine paths and no shell env execution."""
from __future__ import annotations
import argparse, os, re, shutil, subprocess, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def load_env(path):
    if not path.exists():
        return
    for number, raw in enumerate(path.read_text(encoding='utf-8-sig').splitlines(),1):
        line=raw.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            raise SystemExit(f'{path.name}:{number}: expected NAME=value')
        name,value=line.split('=',1);name=name.strip();value=value.strip()
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',name):
            raise SystemExit(f'{path.name}:{number}: invalid setting name')
        if len(value)>=2 and value[0]==value[-1] and value[0] in ('"',"'"):
            value=value[1:-1]
        # Existing terminal environment takes precedence. Never evaluate shell text.
        if name not in os.environ:
            os.environ[name]=value

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('service',choices=('backend','frontend','check'))
    parser.add_argument('--port',type=int)
    args=parser.parse_args()
    os.chdir(ROOT)
    load_env(ROOT/'.env')
    directory=os.environ.get('CHECKUP_REPORT_DIR','').strip()
    report_dir=Path(directory).expanduser() if directory else ROOT/'data/reports'
    if not report_dir.is_absolute():report_dir=ROOT/report_dir
    report_dir.mkdir(parents=True,exist_ok=True)
    os.environ['CHECKUP_REPORT_DIR']=str(report_dir.resolve())
    if args.service=='backend':
        if sys.version_info<(3,12):raise SystemExit('Use the tested Python 3.12 environment.')
        sys.path.insert(0,str(ROOT))
        import uvicorn
        print(f'Backend: http://127.0.0.1:{args.port or 8768}; reports: {report_dir}')
        print('Single worker only. Restarting discards a paused execution stack; saved data remains.')
        uvicorn.run('app.api.main:app',host='127.0.0.1',port=args.port or 8768,access_log=False)
    elif args.service=='frontend':
        node=shutil.which('node');npm=shutil.which('npm.cmd' if os.name=='nt' else 'npm')
        if not node or not npm:raise SystemExit('Install Node 24, then reopen the terminal.')
        version=subprocess.check_output([node,'--version'],text=True).strip()
        if int(version.lstrip('v').split('.')[0])<24:raise SystemExit('Use tested Node 24 for native TypeScript tests.')
        if not (ROOT/'frontend/node_modules').is_dir():raise SystemExit('First run npm ci inside frontend/.')
        # Do not load VITE_ values from the backend .env; Vite reads frontend/.env.local.
        raise SystemExit(subprocess.call([npm,'run','dev','--','--host','127.0.0.1','--port',str(args.port or 5173),'--strictPort'],cwd=ROOT/'frontend'))
    else:
        print(f'Python: {sys.version.split()[0]}; project: {ROOT}')
        for name in ('BAIDU_MAP_AK','DEEPSEEK_API_KEY'):
            print(f'{name}: '+('configured' if os.environ.get(name,'').strip() else 'not configured'))
        print(f'Persisted reports: {len(list(report_dir.glob("*_report.json")))}')
        print('This check does not contact Baidu or DeepSeek.')
if __name__=='__main__':main()
