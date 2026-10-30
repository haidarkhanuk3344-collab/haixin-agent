"""Double-click entry for the local personal Agent UI."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen
import webbrowser
from personal_agent.app import load_config,exclusive,AppError,ROOT
from personal_agent.web import UI,Server
from personal_agent import worker


def main():
    p=argparse.ArgumentParser();p.add_argument('--serve',action='store_true');p.add_argument('--config');p.add_argument('--port',type=int,default=18789);p.add_argument('--no-open',action='store_true');a=p.parse_args();c=load_config(a.config)
    if not (ROOT/'frontend/dist/index.html').is_file():raise AppError('BUILD','界面尚未构建，请先完成前端安装和构建。')
    url='http://127.0.0.1:'+str(a.port)
    if a.serve:
        with exclusive(c['state'],name='personal-agent-web.lock'):
            server=Server(('127.0.0.1',a.port),UI(c));worker.start(c)
            try:server.serve_forever()
            finally:server.server_close()
        return
    def ready():
        try:
            with urlopen(url+'/health',timeout=1) as r:return json.load(r).get('service')=='personal-agent-ui'
        except Exception:return False
    if not ready():
        log=c['state']/'personal-agent/web.log';log.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        fd=os.open(str(log),os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
        with os.fdopen(fd,'ab') as output:
            subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--serve','--config',c['_config_path'],'--port',str(a.port)],cwd=ROOT,stdin=subprocess.DEVNULL,stdout=output,stderr=output,start_new_session=True)
        for _ in range(60):
            if ready():break
            time.sleep(.15)
        else:raise AppError('WEB_START','页面未能启动，已有数据保留，请检查端口是否占用。')
    if not a.no_open:webbrowser.open(url)
    print('海辛已打开：'+url+'。关闭窗口后后台继续。')

if __name__=='__main__':
    try:main()
    except AppError as exc:print(exc.message);raise SystemExit(1)
